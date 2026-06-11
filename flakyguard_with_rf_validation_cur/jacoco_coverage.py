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

import logging
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from models import TestInput

logger = logging.getLogger(__name__)

_COVERAGE_CACHE: dict[tuple[str, str, str, str], list[str]] = {}


def _run_cmd(cmd: str, cwd: str, timeout: int) -> tuple[bool, str]:
    """Run a shell command and return (success, combined output)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "") + "\nTIMEOUT"
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

    module = getattr(test_input, "module", "") or ""
    module = module.strip()

    module_part = ""
    if module and module != ".":
        module_part = f"-pl {module} -am "

    return (
            "mvn -q "
        f"{module_part}"
        "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent "
        "test "
        "org.jacoco:jacoco-maven-plugin:0.8.12:report "
        f"-Dtest={test_class}#{test_input.test_func} "
        "-Drat.skip=true "
        "-Dcheckstyle.skip=true "
        "-Denforcer.skip=true"
    )
    # return (
    #     "mvn -q "
    #     "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent "
    #     "test "
    #     "org.jacoco:jacoco-maven-plugin:0.8.12:report "
    #     f"-Dtest={test_class}#{test_input.test_func} "
    #     "-Drat.skip=true"
    # )


def _resolve_report_path(test_input: TestInput) -> str:
    """Return absolute JaCoCo XML report path."""
    report = test_input.coverage_report or "target/site/jacoco/jacoco.xml"
    if os.path.isabs(report):
        return report
    return os.path.join(test_input.repo_root, report)


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

    cmd = _build_coverage_cmd(test_input)
    logger.info("Collecting JaCoCo coverage: %s", cmd)
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
