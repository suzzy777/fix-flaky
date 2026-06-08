"""
utils/runner.py – run tests and capture flaky failures.

Default reproduction:
  Run TestInput.run_cmd up to TestInput.repro_runs times.

Script reproduction:
  If TestInput.repro_script is set, run single_runner.sh <issue_id>.
  The reproduction script reads test_config.csv itself, so FlakyGuard does
  not manually duplicate the CSV row fields.

Validation always uses TestInput.run_cmd on the patched repo.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from models import TestInput, FlakyInfo

logger = logging.getLogger(__name__)


def _build_cmd(test_input: TestInput) -> str:
    """Expand the run_cmd template for the given test input."""
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


# ── Go ──────────────────────────────────────────────────────────────────────

def _parse_go_failure(output: str) -> FlakyInfo | None:
    """Parse go test -v output for failure details."""
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


# ── Python ───────────────────────────────────────────────────────────────────

def _parse_python_failure(output: str) -> FlakyInfo | None:
    """Parse pytest output for failure details."""
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


# ── Java ─────────────────────────────────────────────────────────────────────

def _parse_java_failure(output: str) -> FlakyInfo | None:
    """Parse JUnit / Maven Surefire output."""
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


# ── Script-based reproduction ────────────────────────────────────────────────

def _zip_stem(zip_path: str) -> str:
    """Return zip filename without the .zip suffix."""
    name = os.path.basename(zip_path)
    return name[:-4] if name.endswith(".zip") else name


def _prepare_script_repro_workdir(test_input: TestInput) -> str:
    """
    Prepare the directory where single_runner.sh runs.

    single_runner.sh expects these files in its current directory:
      - test_config.csv
      - companion flaky_analysis_tool_*.sh scripts
      - optional data/<zip>.zip; if missing, the script can download it from
        the URL column in test_config.csv.

    By default, we run in the directory containing single_runner.sh. This is
    closest to how the script is normally used and avoids copying companion
    scripts around.
    """
    if not test_input.repro_issue_id:
        raise ValueError("--repro-issue-id is required when --repro-script is used")

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
            "test_config.csv is required for script-based reproduction. "
            "Put it next to single_runner.sh or pass --repro-config-csv."
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


def _collect_repro_logs(workdir: str, base_output: str) -> str:
    """Add relevant logs/summaries produced by the reproduction script."""
    parts = [base_output]
    workdir_path = Path(workdir)

    patterns = [
        "**/summary.txt",
        "**/rounds-test-results.csv",
        "**/testlog/**/*.log",
        "**/surefire-reports/*.txt",
        "**/surefire-reports/*.xml",
    ]

    seen: set[Path] = set()
    for pattern in patterns:
        for path in workdir_path.glob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parts.append(f"\n\n===== {path} =====\n{text[:6000]}")

    return "".join(parts)


def _run_script_reproduction(test_input: TestInput) -> FlakyInfo | None:
    """Run single_runner.sh-based reproduction once."""
    try:
        workdir = _prepare_script_repro_workdir(test_input)
    except Exception as exc:
        logger.error("Script reproduction setup failed: %s", exc)
        return None

    script_name = os.path.basename(test_input.repro_script)
    cmd = f"bash ./{script_name} {test_input.repro_issue_id}"
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


# ── Public API ───────────────────────────────────────────────────────────────

def reproduce_failure(test_input: TestInput) -> FlakyInfo | None:
    """
    Reproduce a flaky failure.

    If test_input.repro_script is set, use script-based reproduction.
    Otherwise, run test_input.run_cmd up to test_input.repro_runs times.
    """
    if test_input.repro_script:
        return _run_script_reproduction(test_input)

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


def validate_fix(test_input: TestInput, runs: int = 10) -> bool:
    """
    Return True if the validation command passes all `runs` consecutive runs.

    Validation intentionally uses run_cmd, even when reproduction used a script.
    """
    cmd = _build_cmd(test_input)
    for i in range(runs):
        passed, output = _run_once(cmd, cwd=test_input.repo_root)
        if not passed:
            logger.info("Validation failed on run %d/%d", i + 1, runs)
            return False
    logger.info("Fix validated: test passed %d/%d runs.", runs, runs)
    return True
