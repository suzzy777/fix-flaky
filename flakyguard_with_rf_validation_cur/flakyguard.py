#!/usr/bin/env python3
"""
flakyguard.py – CLI entry point for FlakyGuard Simple.

The ReproFlake/script workflow is:
  1. Reproduce the flaky failure with single_runner.sh + test_config.csv.
  2. Collect context, optionally narrowed with JaCoCo coverage.
  3. Run the paper-style M × P × N loop: context, thoughts, then concrete fixes.
  4. Validate by applying each patch to the artifact's Flaky copy and rerunning
     the same ReproFlake helper logic on CODE_VERSION=Fixed.
"""

import argparse
import logging
import os
import sys
import csv
import shutil
import zipfile
import subprocess

from pathlib import Path
# Ensure local packages are importable when run directly.
sys.path.insert(0, os.path.dirname(__file__))

from models import TestInput
from pipeline import run_pipeline
from runner import reproduce_failure

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="FlakyGuard Simple – automated flaky-test repair."
    )

    # Required repair inputs.
    #parser.add_argument("--repo", required=True, help="Absolute path to editable repository root")
    parser.add_argument("--repo", default=".", help="Path to repo; ignored in ReproFlake mode")
    parser.add_argument("--test-file", required=True, help="Test file path relative to --repo")
    parser.add_argument("--test-func", required=True, help="Test function/method name")
    #parser.add_argument("--test-case", help="Test case name; defaults to --test-func")
    parser.add_argument("--test-case", default=None)
    parser.add_argument(
        "--language",
        default="java",
        choices=["go", "python", "java"],
        help="Source language (default: java)",
    )

    # Script-based reproduction/validation.
    parser.add_argument(
        "--repro-script",
        required=True,
        help="Path to single_runner.sh. Reproduction and validation use this script's artifact workflow.",
    )
    parser.add_argument(
        "--repro-issue-id",
        required=True,
        help="Issue id passed to single_runner.sh. Must match the issue_id column in test_config.csv.",
    )
    parser.add_argument(
        "--repro-config-csv",
        required=True,
        help="Path to test_config.csv.",
    )
    parser.add_argument(
        "--repro-zip",
        help="Optional artifact zip to copy into <workdir>/data/. If omitted, single_runner.sh can download using the CSV url column.",
    )
    parser.add_argument(
        "--repro-workdir",
        help="Directory where single_runner.sh should run. Default: directory containing --repro-script.",
    )
    parser.add_argument(
        "--repro-timeout",
        type=int,
        default=1800,
        help="Timeout in seconds for script-based reproduction/validation (default: 1800)",
    )

    # Coverage-based context narrowing.
    parser.add_argument(
        "--use-jacoco-coverage",
        action="store_true",
        help="Run JaCoCo and use covered Java files to limit call-graph context.",
    )
    parser.add_argument(
        "--coverage-runner",
        default="",
        help=(
            "Optional command template for generating JaCoCo coverage. "
            "Placeholders: {test_class}, {test_func}, {test_case}, {test_file}, {test_dir}. "
            "Default uses jacoco-maven-plugin prepare-agent/test/report."
        ),
    )
    parser.add_argument(
        "--coverage-report",
        default="target/site/jacoco/jacoco.xml",
        help="Path to JaCoCo XML report relative to --repo (default: target/site/jacoco/jacoco.xml).",
    )
    parser.add_argument(
        "--coverage-timeout",
        type=int,
        default=900,
        help="Timeout in seconds for the JaCoCo coverage command (default: 900).",
    )
    parser.add_argument(
        "--coverage-max-files",
        type=int,
        default=80,
        help="Maximum covered files to use for context (default: 80).",
    )

    # Paper-style repair-loop parameters: M × P × N.
    parser.add_argument(
        "--attempts",
        type=int,
        default=None,
        help="Legacy alias for --context-attempts/M. If set, overrides --context-attempts.",
    )
    parser.add_argument(
        "--context-attempts",
        type=int,
        default=3,
        help="M: number of context collection attempts (default: 3).",
    )
    parser.add_argument(
        "--thoughts-per-context",
        type=int,
        default=2,
        help="P: high-level thoughts/root-cause plans per context (default: 2).",
    )
    parser.add_argument(
        "--fixes-per-thought",
        type=int,
        default=3,
        help="N: concrete patch attempts per thought (default: 3).",
    )
    parser.add_argument(
        "--children",
        type=int,
        default=3,
        help="Smart BFS k: children selected per node (default: 3)",
    )
    parser.add_argument(
        "--max-funcs",
        type=int,
        default=5,
        help="Global filter F: max functions in context (default: 5)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=-1,
        help="BFS depth limit, -1 = unlimited (default: -1)",
    )

    # Output.
    parser.add_argument(
        "--output-dir",
        default="patches",
        help="Directory to save patch files (default: ./patches)",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    return parser.parse_args()

def _read_reproflake_row(csv_path: Path, issue_id: str) -> dict[str, str]:
    columns = [
        "test_type", "issue_id", "zip", "module", "preceding_test",
        "flaky_test", "iterations", "config", "javav", "nondexSeed", "url",
    ]

    with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
        sample = f.readline()
        f.seek(0)

        if "issue_id" in sample:
            reader = csv.DictReader(f)
            for row in reader:
                clean = {
                    str(k).strip().lstrip("\ufeff"): (v or "").strip()
                    for k, v in row.items()
                    if k
                }
                if clean.get("issue_id") == issue_id:
                    return clean
        else:
            reader = csv.reader(f)
            for raw in reader:
                if not raw:
                    continue
                row = dict(zip(columns, [cell.strip() for cell in raw + [""] * len(columns)]))
                if row.get("issue_id") == issue_id:
                    return row

    raise RuntimeError(f"Could not find issue_id={issue_id} in {csv_path}")


def _safe_remove_dir(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        # Best effort: make files writable, then retry.
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    os.chmod(os.path.join(root, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(path)

def _prepare_reproflake_work_repo(args) -> str:
    """
    Extract ReproFlake artifact to a stable work repo and return Flaky/ path.

    This avoids generating patches from a separate local checkout that may not
    match the ReproFlake artifact.
    """
    if not getattr(args, "repro_script", None) or not getattr(args, "repro_issue_id", None):
        return os.path.abspath(args.repo)

    workdir = (
        Path(args.repro_workdir).resolve()
        if getattr(args, "repro_workdir", None)
        else Path(args.repro_script).resolve().parent
    )

    csv_path = (
        Path(args.repro_config_csv).resolve()
        if getattr(args, "repro_config_csv", None)
        else workdir / "test_config.csv"
    )

    if not csv_path.is_file():
        raise FileNotFoundError(f"test_config.csv not found: {csv_path}")

    row = _read_reproflake_row(csv_path, args.repro_issue_id)

    zip_name = row.get("zip", "").strip()
    if not zip_name:
        raise RuntimeError(f"No zip column found for issue_id={args.repro_issue_id}")

    data_dir = workdir / "data"
    zip_path = data_dir / f"{zip_name}.zip"

    if not zip_path.is_file():
        url = row.get("url", "").strip()
        if not url:
            raise FileNotFoundError(
                f"ReproFlake zip not found and CSV has no url: {zip_path}"
            )

        data_dir.mkdir(parents=True, exist_ok=True)
        part_path = zip_path.with_name(zip_path.name + ".part")

        logger.info("Downloading ReproFlake zip directly: %s", url)

        result = subprocess.run(
            [
                "curl",
                "-L",
                "--fail",
                "--retry", "3",
                "--retry-delay", "2",
                "-o", str(part_path),
                url,
            ],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to download ReproFlake zip from {url}\n"
                f"stdout:\n{result.stdout[-2000:]}\n"
                f"stderr:\n{result.stderr[-2000:]}"
            )

        part_path.replace(zip_path)
    
    work_repo = data_dir / f"{args.repro_issue_id}_work_repo"
    _safe_remove_dir(work_repo)
    work_repo.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(work_repo)

    nested = work_repo / zip_name
    if nested.is_dir():
        for child in list(nested.iterdir()):
            shutil.move(str(child), str(work_repo / child.name))
        nested.rmdir()

    flaky_repo = work_repo / "Flaky"
    if not flaky_repo.is_dir():
        raise FileNotFoundError(f"Flaky source not found after extraction: {flaky_repo}")

    logger.info("Using ReproFlake artifact source as repo_root: %s", flaky_repo)
    return str(flaky_repo.resolve())
# def _prepare_reproflake_work_repo(args) -> str:
#     """
#     Extract ReproFlake artifact to a stable work repo and return Flaky/ path.

#     This avoids generating patches from a separate local checkout that may not
#     match the ReproFlake artifact.
#     """
#     if not getattr(args, "repro_script", None) or not getattr(args, "repro_issue_id", None):
#         return os.path.abspath(args.repo)

#     workdir = Path(args.repro_workdir).resolve() if getattr(args, "repro_workdir", None) else Path(args.repro_script).resolve().parent

#     csv_path = Path(args.repro_config_csv).resolve() if getattr(args, "repro_config_csv", None) else workdir / "test_config.csv"


#     if not zip_path.is_file():
#         logger.info("Zip missing before work_repo setup; running single_runner once to download it.")
#         result = subprocess.run(
#             ["bash", str(Path(args.repro_script).resolve()), args.repro_issue_id],
#             cwd=str(workdir),
#             capture_output=True,
#             text=True,
#             timeout=300,
#         )

#     if not zip_path.is_file():
#         raise FileNotFoundError(
#             f"ReproFlake zip not found even after running single_runner: {zip_path}\n"
#             f"stdout:\n{result.stdout[-2000:]}\n"
#             f"stderr:\n{result.stderr[-2000:]}"
#         )

#     if not csv_path.is_file():
#         raise FileNotFoundError(f"test_config.csv not found: {csv_path}")

#     row = _read_reproflake_row(csv_path, args.repro_issue_id)
#     zip_name = row.get("zip", "").strip()
#     if not zip_name:
#         raise RuntimeError(f"No zip column found for issue_id={args.repro_issue_id}")

#     data_dir = workdir / "data"
#     zip_path = data_dir / f"{zip_name}.zip"

#     if not zip_path.is_file() and getattr(args, "repro_zip", None):
#         repro_zip = Path(args.repro_zip).resolve()
#         if repro_zip.is_file():
#             data_dir.mkdir(parents=True, exist_ok=True)
#             shutil.copy2(repro_zip, zip_path)

#     if not zip_path.is_file():
#         raise FileNotFoundError(f"ReproFlake zip not found: {zip_path}")

#     work_repo = data_dir / f"{args.repro_issue_id}_work_repo"
#     _safe_remove_dir(work_repo)
#     work_repo.mkdir(parents=True, exist_ok=True)

#     with zipfile.ZipFile(zip_path) as zf:
#         zf.extractall(work_repo)

#     nested = work_repo / zip_name
#     if nested.is_dir():
#         for child in list(nested.iterdir()):
#             shutil.move(str(child), str(work_repo / child.name))
#         nested.rmdir()

#     flaky_repo = work_repo / "Flaky"
#     if not flaky_repo.is_dir():
#         raise FileNotFoundError(f"Flaky source not found after extraction: {flaky_repo}")

#     logger.info("Using ReproFlake artifact source as repo_root: %s", flaky_repo)
#     return str(flaky_repo.resolve())


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    context_attempts = args.attempts if args.attempts is not None else args.context_attempts


    #repo_root = _prepare_reproflake_work_repo(args)

    repo_root = _prepare_reproflake_work_repo(args)

    test_file = args.test_file.lstrip("/")

    if args.repro_script and args.repro_issue_id:
        workdir = (
            Path(args.repro_workdir).resolve()
            if args.repro_workdir
            else Path(args.repro_script).resolve().parent
        )
        csv_path = (
            Path(args.repro_config_csv).resolve()
            if args.repro_config_csv
            else workdir / "test_config.csv"
        )

        row = _read_reproflake_row(csv_path, args.repro_issue_id)
        module = row.get("module", "").strip().strip("/")

        logger.info("CSV module for this issue: %r", module)

        if module and module != ".":
            prefix = module + "/"
            if not test_file.startswith(prefix):
                test_file = prefix + test_file

    logger.info("Final repo_root used by FlakyGuard: %s", repo_root)
    logger.info("Final test_file used by FlakyGuard: %s", test_file)
    
    test_input = TestInput(
        #repo_root=os.path.abspath(args.repo),
        repo_root=repo_root,
        test_file=test_file,
        test_func=args.test_func,
        test_case=args.test_case or args.test_func,
        language=args.language,
        repro_script=args.repro_script,
        repro_issue_id=args.repro_issue_id,
        repro_workdir=args.repro_workdir or "",
        repro_config_csv=args.repro_config_csv,
        repro_zip=args.repro_zip or "",
        repro_timeout=args.repro_timeout,
        script_validation_iterations=10,
        context_attempts=context_attempts,
        thoughts_per_context=args.thoughts_per_context,
        fixes_per_thought=args.fixes_per_thought,
        use_jacoco_coverage=args.use_jacoco_coverage,
        coverage_cmd=args.coverage_runner or "",
        coverage_report=args.coverage_report,
        coverage_timeout=args.coverage_timeout,
        coverage_max_files=args.coverage_max_files,
    )

    print(f"\n[1/3] Reproducing flaky failure (script issue_id={test_input.repro_issue_id})…")
    flaky_info = reproduce_failure(test_input)

    if flaky_info is None:
        print("✗ Could not reproduce a failure. Test may not be flaky in this environment.")
        sys.exit(1)

    print("✓ Failure reproduced.")
    print(f"  Error: {flaky_info.error[:120]}")

    total_candidates = context_attempts * args.thoughts_per_context * args.fixes_per_thought
    print(
        f"\n[2/3] Running fixing pipeline "
        f"(M={context_attempts}, P={args.thoughts_per_context}, "
        f"N={args.fixes_per_thought}, candidates={total_candidates})…"
    )

    output_dir = os.path.join(test_input.repo_root, args.output_dir)
    success, message = run_pipeline(
        test_input=test_input,
        flaky_info=flaky_info,
        context_attempts=context_attempts,
        thoughts_per_context=args.thoughts_per_context,
        fixes_per_thought=args.fixes_per_thought,
        k=args.children,
        depth_limit=args.depth,
        max_funcs=args.max_funcs,
        output_dir=output_dir,
    )

    print("\n[3/3] Result:")
    if success:
        print("✓ Fix found and validated.")
        print(f"  Root cause: {message}")
        print(f"  Patch saved to: {output_dir}/")
    else:
        print(f"✗ No fix found: {message}")
        sys.exit(2)


if __name__ == "__main__":
    main()
