"""
utils/runner.py – reproduce and validate flaky tests.

Reproduction uses the ReproFlake single_runner.sh + test_config.csv workflow.
Validation applies the generated patch to the artifact's Flaky version and
reruns the same helper-script logic with CODE_VERSION=Fixed.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from models import TestInput, FlakyInfo

logger = logging.getLogger(__name__)


# ── Generic command helpers ──────────────────────────────────────────────────

def _build_cmd(test_input: TestInput) -> str:
    """Expand the fallback run_cmd template for the given test input."""
    test_dir = os.path.dirname(test_input.test_file)
    return test_input.run_cmd.format(
        test_func=test_input.test_func,
        test_case=test_input.test_case,
        test_file=test_input.test_file,
        test_dir=test_dir or ".",
    )


def _run_once(cmd: str, cwd: str, timeout: int = 120) -> tuple[bool, str]:
    """Run cmd, return (passed, combined_output)."""
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
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"


# ── Failure parsing ──────────────────────────────────────────────────────────

def _extract_flaky_info(output: str, test_input: TestInput) -> FlakyInfo | None:
    """Parse a test failure output into a FlakyInfo."""
    lang = test_input.language.lower()

    if lang == "go":
        return _parse_go_failure(output)
    if lang == "python":
        return _parse_python_failure(output)
    if lang == "java":
        return _parse_java_failure(output)

    if output.strip():
        return FlakyInfo(error=output[:500], error_trace=output)
    return None


def _parse_go_failure(output: str) -> FlakyInfo | None:
    if "FAIL" not in output and "panic" not in output:
        return None

    error_lines: list[str] = []
    trace_lines: list[str] = []
    in_trace = False

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("--- FAIL") or "Error:" in stripped or "FAIL\t" in stripped:
            error_lines.append(stripped)
        if re.match(r"\s+\S+\.go:\d+", line) or stripped.startswith("goroutine"):
            in_trace = True
        if in_trace:
            trace_lines.append(line)

    error = "\n".join(error_lines[:10]) or output[:300]
    trace = "\n".join(trace_lines[:40]) or output[:1000]

    error_file, error_line = "", 0
    match = re.search(r"(\S+\.go):(\d+)", trace)
    if match:
        error_file, error_line = match.group(1), int(match.group(2))

    return FlakyInfo(error=error, error_trace=trace, error_file=error_file, error_line=error_line)


def _parse_python_failure(output: str) -> FlakyInfo | None:
    if "FAILED" not in output and "ERROR" not in output and "assert" not in output.lower():
        return None

    failure_section = ""
    match = re.search(r"={3,} FAILURES ={3,}(.*?)(?:={3,}|\Z)", output, re.DOTALL)
    if match:
        failure_section = match.group(1)

    error_match = re.search(r"(AssertionError.*|E\s+assert.*|Exception.*)", failure_section)
    error = error_match.group(0)[:300] if error_match else failure_section[:300] or output[:300]

    error_file, error_line = "", 0
    file_match = re.search(r"(\S+\.py):(\d+)", output)
    if file_match:
        error_file, error_line = file_match.group(1), int(file_match.group(2))

    return FlakyInfo(error=error, error_trace=failure_section or output[:1000], error_file=error_file, error_line=error_line)


def _parse_java_failure(output: str) -> FlakyInfo | None:
    failure_markers = (
        "<<< FAILURE!",
        "AssertionFailedError",
        "AssertionError",
        "Tests run:",
        "There are test failures",
        "FAILURE",
        "ERROR",
    )
    if not any(marker in output for marker in failure_markers):
        return None

    lines = output.splitlines()

    error_lines = [
        line.strip()
        for line in lines
        if (
            "AssertionFailedError" in line
            or "AssertionError" in line
            or "<<< FAILURE!" in line
            or re.search(r"expected:.*but was:", line)
        )
    ]

    if not error_lines:
        error_lines = [
            line.strip()
            for line in lines
            if "Exception" in line or "Failed" in line or "FAILURE" in line
        ]

    error = "\n".join(error_lines[:8]) or output[:500]

    error_file, error_line = "", 0
    match = re.search(r"\(([^()\s]+\.java):(\d+)\)", output)
    if match:
        error_file, error_line = match.group(1), int(match.group(2))
    else:
        match = re.search(r"([^\s()]+\.java):(\d+)", output)
        if match:
            error_file, error_line = match.group(1), int(match.group(2))

    return FlakyInfo(error=error, error_trace=output[:4000], error_file=error_file, error_line=error_line)


# ── ReproFlake script helpers ────────────────────────────────────────────────

def _zip_stem(zip_path: str) -> str:
    """Return zip filename without the .zip suffix."""
    name = os.path.basename(zip_path)
    return name[:-4] if name.endswith(".zip") else name


def _prepare_script_workdir(test_input: TestInput) -> str:
    """
    Prepare the directory where single_runner.sh/helper scripts run.

    The workdir must contain:
      - single_runner.sh
      - test_config.csv
      - companion flaky_analysis_tool_*.sh scripts
      - optional data/<zip>.zip; if missing, single_runner.sh can download it
    """
    if not test_input.repro_issue_id:
        raise ValueError("--repro-issue-id is required")

    script_src = os.path.abspath(test_input.repro_script)
    if not os.path.isfile(script_src):
        raise FileNotFoundError(f"Reproduction script not found: {script_src}")

    if test_input.repro_workdir:
        workdir = os.path.abspath(test_input.repro_workdir)
        os.makedirs(workdir, exist_ok=True)
        script_dst = os.path.join(workdir, os.path.basename(script_src))
        if os.path.abspath(script_dst) != script_src:
            shutil.copy2(script_src, script_dst)
        os.chmod(script_dst, 0o755)
    else:
        workdir = os.path.dirname(script_src)

    csv_dst = os.path.join(workdir, "test_config.csv")
    if test_input.repro_config_csv:
        csv_src = os.path.abspath(test_input.repro_config_csv)
        if not os.path.isfile(csv_src):
            raise FileNotFoundError(f"test_config.csv not found: {csv_src}")
        if os.path.abspath(csv_dst) != csv_src:
            shutil.copy2(csv_src, csv_dst)

    if not os.path.isfile(csv_dst):
        raise FileNotFoundError(
            "test_config.csv is required. Put it next to single_runner.sh or pass --repro-config-csv."
        )

    if test_input.repro_zip:
        zip_src = os.path.abspath(test_input.repro_zip)
        if not os.path.isfile(zip_src):
            raise FileNotFoundError(f"Reproduction zip not found: {zip_src}")

        data_dir = os.path.join(workdir, "data")
        os.makedirs(data_dir, exist_ok=True)
        zip_dst = os.path.join(data_dir, f"{_zip_stem(zip_src)}.zip")
        if os.path.abspath(zip_dst) != zip_src:
            shutil.copy2(zip_src, zip_dst)

    return workdir


def _read_repro_row(test_input: TestInput, workdir: str) -> dict[str, str]:
    """Read the CSV row matching test_input.repro_issue_id."""
    csv_path = os.path.join(workdir, "test_config.csv")
    columns = [
        "test_type",
        "issue_id",
        "zip",
        "module",
        "preceding_test",
        "flaky_test",
        "iterations",
        "config",
        "javav",
        "nondexSeed",
        "url",
    ]

    with open(csv_path, newline="", encoding="utf-8", errors="replace") as file:
        reader = csv.reader(file)
        header = next(reader, None)
        for row in reader:
            if not row:
                continue
            padded = row + [""] * (len(columns) - len(row))
            data = dict(zip(columns, padded[: len(columns)]))
            if data.get("issue_id") == test_input.repro_issue_id:
                return data

    raise ValueError(f"Issue id not found in test_config.csv: {test_input.repro_issue_id}")


def _helper_script_for_row(row: dict[str, str]) -> str:
    """Match single_runner.sh's helper-script selection logic."""
    test_type = row.get("test_type", "")
    module = row.get("module", "")
    javav = row.get("javav", "")

    if test_type == "britle":
        return "flaky_analysis_tool_od_brittle.sh"
    if test_type == "od":
        return "flaky_analysis_tool_od_proto.sh" if module.startswith("hadoop") else "flaky_analysis_tool_od.sh"
    if test_type == "td":
        return "flaky_analysis_tool_td_proto.sh" if module.startswith("hadoop") else "flaky_analysis_tool_td.sh"
    if test_type == "id":
        if javav == "8":
            return "flaky_analysis_tool_id_8.sh"
        if javav == "17":
            return "flaky_analysis_tool_id_17.sh"
        return "flaky_analysis_tool_id_11.sh"
    if test_type == "raft":
        return "flaky_analysis_tool_raft.sh"
    if test_type == "nio":
        return "flaky_analysis_tool_nio.sh"

    return "flaky_analysis_tool_proto.sh" if module.startswith("hadoop") else "flaky_analysis_tool.sh"


def _helper_args_for_row(row: dict[str, str], iterations: int, code_version: str) -> list[str]:
    """Build helper-script args using the same convention as single_runner.sh."""
    test_type = row.get("test_type", "")
    issue_id = row.get("issue_id", "")
    zip_name = row.get("zip", "")
    module = row.get("module", "")
    preceding_test = row.get("preceding_test", "")
    flaky_test = row.get("flaky_test", "")
    nondex_seed = row.get("nondexSeed", "")
    iter_s = str(iterations)

    if test_type in ("britle", "od"):
        return [issue_id, zip_name, module, preceding_test, flaky_test, iter_s, code_version]
    if test_type == "td":
        return [issue_id, zip_name, module, flaky_test, iter_s, code_version]
    if test_type == "id":
        return [issue_id, zip_name, module, flaky_test, iter_s, code_version, nondex_seed]
    if test_type in ("raft", "nio"):
        return [issue_id, zip_name, module, flaky_test, iter_s, code_version]

    return [issue_id, zip_name, module, flaky_test, iter_s, code_version]


def _extract_artifact_for_validation(workdir: str, row: dict[str, str], patch_path: str) -> tuple[Path, Path | None]:
    """
    Extract the artifact, install the generated patch as Fixed.patch, and
    temporarily hide data/<zip>.zip so the helper does not overwrite Fixed.patch.
    """
    issue_id = row["issue_id"]
    zip_name = row["zip"]
    base_dir = Path(workdir) / "data" / issue_id
    zip_path = Path(workdir) / "data" / f"{zip_name}.zip"

    if not zip_path.is_file():
        raise FileNotFoundError(
            f"Artifact zip not found for validation: {zip_path}. "
            "Pass --repro-zip or run reproduction once so the script can download it."
        )

    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zip_file:
        zip_file.extractall(base_dir)

    nested = base_dir / zip_name
    if nested.is_dir():
        for child in list(nested.iterdir()):
            shutil.move(str(child), str(base_dir / child.name))
        nested.rmdir()

    fixed_dir = base_dir / "Fixed"
    if fixed_dir.exists():
        shutil.rmtree(fixed_dir)

    shutil.copy2(patch_path, base_dir / "Fixed.patch")

    hidden_zip = zip_path.with_suffix(zip_path.suffix + ".flakyguard_hold")
    if hidden_zip.exists():
        hidden_zip.unlink()
    shutil.move(str(zip_path), str(hidden_zip))
    return base_dir, hidden_zip


def _restore_hidden_zip(hidden_zip: Path | None) -> None:
    if hidden_zip and hidden_zip.exists():
        original = hidden_zip.with_suffix("")
        if original.exists():
            original.unlink()
        shutil.move(str(hidden_zip), str(original))


# def _collect_repro_logs(workdir: str, base_output: str) -> str:
#     """Add relevant logs/summaries produced by the reproduction/validation scripts."""
#     parts = [base_output]
#     workdir_path = Path(workdir)

#     patterns = [
#         "**/summary.txt",
#         "**/rounds-test-results.csv",
#         "**/testlog/**/*.log",
#         "**/surefire-reports/*.txt",
#         "**/surefire-reports/*.xml",
#         "**/flaky-result/**/*",
#         "**/result/**/*",
#     ]

#     seen: set[Path] = set()
#     for pattern in patterns:
#         for path in workdir_path.glob(pattern):
#             if path in seen or not path.is_file():
#                 continue
#             seen.add(path)
#             try:
#                 text = path.read_text(encoding="utf-8", errors="replace")
#             except OSError:
#                 continue
#             parts.append(f"\n\n===== {path} =====\n{text[:6000]}")

#     return "".join(parts)

def _collect_repro_logs(workdir: str, base_output: str, issue_id: str | None = None) -> str:
    """Add logs/summaries produced by the reproduction/validation scripts.

    If issue_id is provided, only collect logs under data/<issue_id>/.
    This avoids accidentally reading stale logs from a previous issue.
    """
    parts = [base_output]
    workdir_path = Path(workdir)

    if issue_id:
        search_root = workdir_path / "data" / issue_id
        if not search_root.exists():
            parts.append(f"\n\n===== No issue log directory found: {search_root} =====\n")
            return "".join(parts)
    else:
        search_root = workdir_path

    patterns = [
        "**/summary.txt",
        "**/rounds-test-results.csv",
        "**/testlog/**/*.log",
        "**/surefire-reports/*.txt",
        "**/surefire-reports/*.xml",
        "**/flaky-result/**/*",
        "**/result/**/*",
    ]

    seen: set[Path] = set()
    for pattern in patterns:
        for path in search_root.glob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parts.append(f"\n\n===== {path} =====\n{text[:6000]}")

    return "".join(parts)

def _has_failure_markers(output: str) -> bool:
    """Return True if script/test output contains clear failure markers."""
    patterns = [
        r"<<< FAILURE!",
        r"AssertionFailedError",
        r"AssertionError",
        r"There are test failures",
        r"BUILD FAILURE",
        r"Failed to apply patch",
        r"Failures:\s*[1-9]",
        r"Errors:\s*[1-9]",
        r",\s*failure\s*,",
        r"\bFAILED\b",
    ]
    return any(re.search(pattern, output, re.IGNORECASE) for pattern in patterns)


# ── Public API ───────────────────────────────────────────────────────────────

def reproduce_failure(test_input: TestInput) -> FlakyInfo | None:
    """
    Reproduce a flaky failure.

    Script mode runs single_runner.sh <issue_id>. Command mode is retained as
    a fallback for non-ReproFlake use.
    """
    if test_input.repro_script:
        try:
            workdir = _prepare_script_workdir(test_input)
        except Exception as exc:
            logger.error("Script reproduction setup failed: %s", exc)
            return None

        script_name = os.path.basename(test_input.repro_script)
        cmd = f"bash ./{script_name} {test_input.repro_issue_id}"

        issue_dir = Path(workdir) / "data" / test_input.repro_issue_id
        issue_result_dir = issue_dir / "result"

        if issue_result_dir.is_dir():
            shutil.rmtree(issue_result_dir)
        logger.info("Reproducing flaky test with script: %s (cwd=%s)", cmd, workdir)

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=test_input.repro_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") + (exc.stderr or "") + "\nTIMEOUT"
            full_output = _collect_repro_logs(workdir, output)
            return FlakyInfo(error="TIMEOUT", error_trace=full_output)

        output = result.stdout + result.stderr
        full_output = _collect_repro_logs(workdir, output)

        info = _extract_flaky_info(full_output, test_input)
        if info:
            logger.info("Failure reproduced via script.")
            return info

        if re.search(r"Failures:\s*[1-9]", full_output) or re.search(r",failure,", full_output):
            logger.info("Failure reproduced via script summary/results.")
            return FlakyInfo(error="Failure reproduced by script", error_trace=full_output[:4000])

        if result.returncode != 0:
            logger.warning("Reproduction script exited with %d but no test failure was parsed.", result.returncode)
            return FlakyInfo(
                error=f"Reproduction script failed with exit code {result.returncode}",
                error_trace=full_output[:4000],
            )

        logger.warning("Script completed but no flaky failure was found in output/logs.")
        return None

    cmd = _build_cmd(test_input)
    logger.info("Reproducing flaky test: %s (up to %d runs)", cmd, test_input.repro_runs)

    for i in range(test_input.repro_runs):
        logger.info("Reproduction run %d/%d", i + 1, test_input.repro_runs)
        passed, output = _run_once(cmd, cwd=test_input.repo_root)
        if not passed:
            info = _extract_flaky_info(output, test_input)
            if info:
                logger.info("Failure reproduced on run %d/%d", i + 1, test_input.repro_runs)
                return info

    logger.warning("No failure reproduced after %d runs.", test_input.repro_runs)
    return None


def validate_fix(test_input: TestInput, runs: int = 10, patch_path: str | None = None) -> bool:
    """
    Validate a generated fix.

    In script mode, apply the generated patch to the artifact's Flaky copy and
    run the matching helper script with CODE_VERSION=Fixed. The iteration count
    defaults to 10 through TestInput.script_validation_iterations.

    In command fallback mode, run TestInput.run_cmd `runs` times.
    """
    if test_input.repro_script and patch_path:
        try:
            workdir = _prepare_script_workdir(test_input)
            row = _read_repro_row(test_input, workdir)
            helper = _helper_script_for_row(row)
            helper_path = Path(workdir) / helper
            if not helper_path.is_file():
                logger.error("Validation helper script not found: %s", helper_path)
                return False

            base_dir, hidden_zip = _extract_artifact_for_validation(workdir, row, patch_path)
            try:
                iterations = test_input.script_validation_iterations
                args = _helper_args_for_row(row, iterations=iterations, code_version="Fixed")
                cmd = ["bash", helper] + args
                logger.info(
                    "Validating fix with ReproFlake helper: %s (cwd=%s)",
                    " ".join(cmd),
                    workdir,
                )
                result = subprocess.run(
                    cmd,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=test_input.repro_timeout,
                )
                output = result.stdout + result.stderr
                #full_output = _collect_repro_logs(str(base_dir), output)
                full_output = _collect_repro_logs(workdir, output, test_input.repro_issue_id)
                if result.returncode != 0:
                    logger.info("Script validation failed with exit code %d", result.returncode)
                    return False

                if _has_failure_markers(full_output):
                    logger.info("Script validation found failure markers in Fixed results.")
                    return False

                logger.info("Fix validated by ReproFlake helper on patched Fixed artifact (%d iterations).", iterations)
                return True
            finally:
                _restore_hidden_zip(hidden_zip)

        except Exception as exc:
            logger.error("Script-based validation failed: %s", exc)
            return False

    cmd = _build_cmd(test_input)
    for i in range(runs):
        passed, output = _run_once(cmd, cwd=test_input.repo_root)
        if not passed:
            logger.info("Validation failed on run %d/%d", i + 1, runs)
            return False
    logger.info("Fix validated: test passed %d/%d runs.", runs, runs)
    return True
