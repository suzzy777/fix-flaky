"""
utils/jacoco_coverage.py – collect Java coverage context with JaCoCo.

This module narrows FlakyGuard's context scope in the same spirit as the
original FlakyGuard coverage-first flow:

  run coverage -> parse covered source files -> build/search graph only there

For Maven Java projects, the default command runs the JaCoCo Maven plugin and
produces target/site/jacoco/jacoco.xml. A custom command can be supplied with
--coverage-runner when a project needs special Maven flags.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from models import TestInput

logger = logging.getLogger(__name__)

_COVERAGE_CACHE: dict[tuple[str, str, str, str], list[str]] = {}


def _as_text(value) -> str:
    """Return subprocess output as text, even when TimeoutExpired stores bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run_cmd(cmd: str | list[str], cwd: str, timeout: int, shell: bool = True) -> tuple[bool, str]:
    """Run a command and return (success, combined output)."""
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired as exc:
        output = _as_text(exc.stdout) + _as_text(exc.stderr) + "\nTIMEOUT"
        return False, output


def _java_test_class_from_file(test_input: TestInput) -> str:
    """
    Infer a Java fully-qualified class name from the test file path.

    Example:
      src/test/java/com/example/MyTest.java -> com.example.MyTest
    """
    test_file = test_input.test_file.replace("\\", "/")

    for marker in ("src/test/java/", "src/main/java/"):
        if marker in test_file:
            rel = test_file.split(marker, 1)[1]
            return rel[:-5].replace("/", ".") if rel.endswith(".java") else rel.replace("/", ".")

    # Fallback: read package declaration from the file.
    test_file_abs = test_input.test_file
    if not os.path.isabs(test_file_abs):
        test_file_abs = os.path.join(test_input.repo_root, test_input.test_file)

    try:
        source = Path(test_file_abs).read_text(encoding="utf-8", errors="replace")
    except OSError:
        class_name = Path(test_file).stem
        return class_name

    package_match = re.search(r"^\s*package\s+([\w.]+)\s*;", source, re.MULTILINE)
    class_name = Path(test_file).stem
    if package_match:
        return f"{package_match.group(1)}.{class_name}"
    return class_name


def _build_coverage_cmd(test_input: TestInput) -> str:
    """
    Build the JaCoCo command.

    A user-provided coverage command can use placeholders:
      {test_class}, {test_func}, {test_case}, {test_file}, {test_dir}
    """
    test_dir = os.path.dirname(test_input.test_file) or "."
    test_class = _java_test_class_from_file(test_input)

    if test_input.coverage_cmd:
        return test_input.coverage_cmd.format(
            test_class=test_class,
            test_func=test_input.test_func,
            test_case=test_input.test_case,
            test_file=test_input.test_file,
            test_dir=test_dir,
        )

    # Default Maven/JaCoCo command. Users can override this if their project
    # needs extra flags or a different module path.
    return (
        "mvn -q "
        "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent "
        "test "
        "org.jacoco:jacoco-maven-plugin:0.8.12:report "
        f"-Dtest={test_class}#{test_input.test_func} "
        "-Drat.skip=true"
    )


def _resolve_report_path(test_input: TestInput) -> str:
    """Return absolute JaCoCo XML report path."""
    report = test_input.coverage_report or "target/site/jacoco/jacoco.xml"
    if os.path.isabs(report):
        return report
    return os.path.join(test_input.repo_root, report)


# ── ReproFlake Docker coverage helpers ───────────────────────────────────────

def _script_workdir(test_input: TestInput) -> Path:
    """Return the directory containing the ReproFlake helper scripts."""
    if test_input.repro_workdir:
        return Path(test_input.repro_workdir).resolve()
    if test_input.repro_script:
        return Path(test_input.repro_script).resolve().parent
    return Path(test_input.repo_root).resolve()


def _read_reproflake_row(test_input: TestInput) -> dict[str, str] | None:
    """Read test_config.csv row for the current issue id."""
    if not test_input.repro_issue_id:
        return None

    csv_path = Path(test_input.repro_config_csv or "")
    if not csv_path.is_file():
        csv_path = _script_workdir(test_input) / "test_config.csv"
    if not csv_path.is_file():
        return None

    columns = [
        "test_type", "issue_id", "zip", "module", "preceding_test",
        "flaky_test", "iterations", "config", "javav", "nondexSeed", "url",
    ]

    with csv_path.open(newline="", encoding="utf-8", errors="replace") as file:
        sample = file.readline()
        file.seek(0)
        if "issue_id" in sample:
            reader = csv.DictReader(file)
            for row in reader:
                clean = {str(k).strip().lstrip("\ufeff"): (v or "").strip() for k, v in row.items() if k}
                if clean.get("issue_id") == test_input.repro_issue_id:
                    return clean
        else:
            reader = csv.reader(file)
            for raw in reader:
                if not raw:
                    continue
                row = dict(zip(columns, [cell.strip() for cell in raw + [""] * (len(columns) - len(raw))]))
                if row.get("issue_id") == test_input.repro_issue_id:
                    return row
    return None


def _docker_image_and_file(row: dict[str, str]) -> tuple[str, str]:
    """Choose a ReproFlake Docker image for running Maven coverage."""
    test_type = row.get("test_type", "").strip()
    javav = row.get("javav", "").strip()

    if test_type == "od" or javav == "8":
        return "flaky_base_jdk8_od_cov", "Dockerfile.od"
    return "flaky_base_jdk_11_id_cover_new", "Dockerfile11.id"


def _safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        # Docker-created files can be root-owned. Try to make them writable, then retry.
        for child in sorted(path.rglob("*"), reverse=True):
            try:
                child.chmod(0o700 if child.is_dir() else 0o600)
            except OSError:
                pass
        try:
            path.chmod(0o700)
        except OSError:
            pass
        shutil.rmtree(path)


def _prepare_reproflake_coverage_source(test_input: TestInput, row: dict[str, str]) -> tuple[Path, Path]:
    """
    Extract the ReproFlake artifact to a stable coverage-only directory.

    Returns (source_root, m2_dir), where source_root is the Flaky source tree
    and m2_dir is the prepared Maven cache from the artifact.
    """
    workdir = _script_workdir(test_input)
    data_dir = workdir / "data"
    zip_name = row.get("zip", "").strip()
    issue_id = row.get("issue_id", test_input.repro_issue_id).strip()
    zip_path = data_dir / f"{zip_name}.zip"

    if not zip_path.is_file() and test_input.repro_zip:
        src_zip = Path(test_input.repro_zip).resolve()
        if src_zip.is_file():
            data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_zip, zip_path)

    if not zip_path.is_file():
        raise FileNotFoundError(f"ReproFlake artifact zip not found for Docker coverage: {zip_path}")

    coverage_dir = data_dir / f"{issue_id}_coverage_ctx"
    _safe_rmtree(coverage_dir)
    coverage_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zip_file:
        zip_file.extractall(coverage_dir)

    nested = coverage_dir / zip_name
    if nested.is_dir():
        for child in list(nested.iterdir()):
            shutil.move(str(child), str(coverage_dir / child.name))
        nested.rmdir()

    source_root = coverage_dir / "Flaky"
    m2_dir = coverage_dir / "Flakym2" / ".m2"
    if not source_root.is_dir():
        raise FileNotFoundError(f"Flaky source directory not found in artifact: {source_root}")
    if not m2_dir.is_dir():
        raise FileNotFoundError(f"Prepared Maven cache not found in artifact: {m2_dir}")
    return source_root, m2_dir


def _module_report_path(source_root: Path, module: str) -> Path:
    module = (module or "").strip().strip("/")
    if module and module != ".":
        return source_root / module / "target" / "site" / "jacoco" / "jacoco.xml"
    return source_root / "target" / "site" / "jacoco" / "jacoco.xml"


def _map_artifact_files_to_repo(files: list[str], artifact_root: Path, repo_root: Path, module: str) -> list[str]:
    """Map covered files from the extracted artifact back to --repo paths."""
    mapped: list[str] = []
    seen: set[str] = set()
    module = (module or "").strip().strip("/")

    for file in files:
        try:
            rel = Path(file).resolve().relative_to(artifact_root.resolve())
        except ValueError:
            continue

        candidates = [repo_root / rel]
        if module and module != ".":
            rel_s = rel.as_posix()
            prefix = module + "/"
            if rel_s.startswith(prefix):
                candidates.append(repo_root / rel_s[len(prefix):])
            else:
                candidates.append(repo_root / module / rel)

        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.is_file():
                s = str(candidate)
                if s not in seen:
                    seen.add(s)
                    mapped.append(s)
                break

    return mapped


def _collect_reproflake_docker_coverage_files(test_input: TestInput) -> list[str]:
    """Run JaCoCo inside the ReproFlake Docker/Maven environment."""
    row = _read_reproflake_row(test_input)
    if not row:
        return []

    workdir = _script_workdir(test_input)
    source_root, m2_dir = _prepare_reproflake_coverage_source(test_input, row)
    module = row.get("module", "").strip()
    test_class = _java_test_class_from_file(test_input)
    image, dockerfile = _docker_image_and_file(row)

    dockerfile_path = workdir / dockerfile
    if not dockerfile_path.is_file():
        logger.warning("Docker coverage requested, but Dockerfile not found: %s", dockerfile_path)
        return []

    build_cmd = ["docker", "build", "-t", image, "-f", str(dockerfile_path), "."]
    ok, output = _run_cmd(build_cmd, cwd=str(workdir), timeout=test_input.coverage_timeout, shell=False)
    if not ok:
        logger.warning("Docker image build for JaCoCo coverage failed; output tail:\n%s", output[-2000:])
        return []

    module_part = ""
    if module and module != ".":
        module_part = f"-pl {module} -am "

    # Use the prepared ReproFlake Maven cache mounted at /root/.m2.
    mvn_cmd = (
        "cd /app/source && "
        "mvn -q -U "
        "-Dmaven.repo.local=/root/.m2/repository "
        f"{module_part}"
        "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent "
        "test "
        "org.jacoco:jacoco-maven-plugin:0.8.12:report "
        f"-Dtest={test_class}#{test_input.test_func} "
        "-Drat.skip=true "
        "-Dcheckstyle.skip=true "
        "-Denforcer.skip=true"
    )

    cmd = [
        "docker", "run", "--rm",
        "--mount", f"type=bind,source={source_root.resolve()},target=/app/source",
        "--mount", f"type=bind,source={m2_dir.resolve()},target=/root/.m2",
        image,
        "bash", "-lc", mvn_cmd,
    ]
    logger.info("Collecting JaCoCo coverage in ReproFlake Docker: %s", " ".join(cmd))
    ok, output = _run_cmd(cmd, cwd=str(workdir), timeout=test_input.coverage_timeout, shell=False)
    if not ok:
        logger.warning("Docker JaCoCo coverage command failed; output tail:\n%s", output[-2000:])

    report_path = _module_report_path(source_root, module)
    artifact_files = parse_jacoco_xml(str(report_path), str(source_root))
    repo_files = _map_artifact_files_to_repo(
        artifact_files,
        artifact_root=source_root,
        repo_root=Path(test_input.repo_root),
        module=module,
    )

    if repo_files:
        logger.info(
            "Docker JaCoCo coverage selected %d file(s) mapped back to --repo.",
            len(repo_files),
        )
    else:
        logger.warning("Docker JaCoCo coverage produced no files that map back to --repo.")
    return repo_files


def _covered_line_count(sourcefile_elem: ET.Element) -> int:
    """Count covered lines for a JaCoCo <sourcefile> element."""
    count = 0
    for line in sourcefile_elem.findall("line"):
        try:
            ci = int(line.attrib.get("ci", "0"))
            cb = int(line.attrib.get("cb", "0"))
        except ValueError:
            continue
        if ci > 0 or cb > 0:
            count += 1
    return count


def _candidate_paths(repo_root: str, package_name: str, filename: str) -> list[str]:
    """Return plausible repo paths for a JaCoCo package/sourcefile pair."""
    package_path = package_name.replace(".", os.sep).replace("/", os.sep)
    rel = os.path.join(package_path, filename) if package_path else filename

    return [
        os.path.join(repo_root, "src", "main", "java", rel),
        os.path.join(repo_root, "src", "test", "java", rel),
        os.path.join(repo_root, rel),
    ]


def parse_jacoco_xml(report_path: str, repo_root: str) -> list[str]:
    """
    Parse target/site/jacoco/jacoco.xml and return absolute Java files that
    have at least one covered line.
    """
    if not os.path.isfile(report_path):
        logger.warning("JaCoCo report not found: %s", report_path)
        return []

    try:
        tree = ET.parse(report_path)
    except ET.ParseError as exc:
        logger.warning("Could not parse JaCoCo XML report %s: %s", report_path, exc)
        return []

    root = tree.getroot()
    covered: list[tuple[str, int]] = []

    for package in root.findall("package"):
        package_name = package.attrib.get("name", "").replace("/", ".")
        for sourcefile in package.findall("sourcefile"):
            filename = sourcefile.attrib.get("name", "")
            if not filename.endswith(".java"):
                continue

            covered_lines = _covered_line_count(sourcefile)
            if covered_lines <= 0:
                continue

            for candidate in _candidate_paths(repo_root, package_name, filename):
                if os.path.isfile(candidate):
                    covered.append((os.path.abspath(candidate), covered_lines))
                    break

    # More-covered files first, stable/deduped.
    covered.sort(key=lambda item: item[1], reverse=True)
    result: list[str] = []
    seen: set[str] = set()
    for path, _ in covered:
        if path not in seen:
            seen.add(path)
            result.append(path)

    return result


def _prioritize_coverage_files(test_input: TestInput, files: list[str]) -> list[str]:
    """
    Keep coverage scope useful and bounded.

    Original FlakyGuard uses coverage to avoid huge graph scopes. In large Maven
    projects, coverage can still include many files, so this keeps the test file
    and then prefers files in the same package area.
    """
    if not files:
        return []

    test_file_abs = test_input.test_file
    if not os.path.isabs(test_file_abs):
        test_file_abs = os.path.join(test_input.repo_root, test_input.test_file)
    test_file_abs = os.path.abspath(test_file_abs)

    max_files = max(1, test_input.coverage_max_files)

    test_parts = Path(test_file_abs).parts
    test_pkg_hint = ""
    if "java" in test_parts:
        idx = list(test_parts).index("java")
        pkg_parts = test_parts[idx + 1:-1]
        if pkg_parts:
            # Use first few package dirs as a weak locality signal.
            test_pkg_hint = os.sep.join(pkg_parts[:3])

    def score(path: str) -> tuple[int, str]:
        norm = os.path.abspath(path)
        if norm == test_file_abs:
            return (0, norm)
        if test_pkg_hint and test_pkg_hint in norm:
            return (1, norm)
        if f"{os.sep}src{os.sep}main{os.sep}java{os.sep}" in norm:
            return (2, norm)
        if f"{os.sep}src{os.sep}test{os.sep}java{os.sep}" in norm:
            return (3, norm)
        return (4, norm)

    ordered = sorted(files, key=score)

    result: list[str] = []
    seen: set[str] = set()
    if os.path.isfile(test_file_abs):
        result.append(test_file_abs)
        seen.add(test_file_abs)

    for path in ordered:
        abs_path = os.path.abspath(path)
        if abs_path in seen:
            continue
        result.append(abs_path)
        seen.add(abs_path)
        if len(result) >= max_files:
            break

    return result


def collect_jacoco_coverage_files(test_input: TestInput) -> list[str]:
    """
    Run JaCoCo coverage if enabled and return covered Java files.

    Results are cached for the process so multiple context attempts do not rerun
    Maven coverage.
    """
    if not test_input.use_jacoco_coverage:
        return []

    if test_input.language.lower() != "java":
        logger.info("JaCoCo coverage requested, but language is %s; ignoring.", test_input.language)
        return []

    cache_key = (
        os.path.abspath(test_input.repo_root),
        test_input.test_file,
        test_input.test_func,
        test_input.coverage_cmd or "",
    )
    if cache_key in _COVERAGE_CACHE:
        return _COVERAGE_CACHE[cache_key]

    report_path = _resolve_report_path(test_input)

    # Remove stale report so we do not accidentally use old coverage.
    try:
        if os.path.isfile(report_path):
            os.remove(report_path)
    except OSError:
        pass

    # In the ReproFlake workflow, prefer Docker coverage so Maven uses the
    # same prepared source/cache style as reproduction and validation. This
    # avoids local Maven failures from missing historical SNAPSHOT artifacts.
    if test_input.repro_script and test_input.repro_issue_id:
        try:
            covered = _collect_reproflake_docker_coverage_files(test_input)
        except Exception as exc:
            logger.warning("ReproFlake Docker JaCoCo coverage failed: %s", exc)
            covered = []
        if covered:
            covered = _prioritize_coverage_files(test_input, covered)
            _COVERAGE_CACHE[cache_key] = covered
            return covered

    cmd = _build_coverage_cmd(test_input)
    logger.info("Collecting JaCoCo coverage locally: %s", cmd)
    ok, output = _run_cmd(cmd, cwd=test_input.repo_root, timeout=test_input.coverage_timeout)
    if not ok:
        logger.warning("JaCoCo coverage command failed; will try to parse any report that exists. Output tail:\n%s", output[-2000:])

    covered = parse_jacoco_xml(report_path, test_input.repo_root)
    covered = _prioritize_coverage_files(test_input, covered)

    if covered:
        logger.info("JaCoCo coverage selected %d file(s) for context.", len(covered))
    else:
        logger.warning("JaCoCo coverage produced no usable files; falling back to nearby-file scope.")

    _COVERAGE_CACHE[cache_key] = covered
    return covered
