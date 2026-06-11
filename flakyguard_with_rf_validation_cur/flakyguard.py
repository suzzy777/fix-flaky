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
    parser.add_argument("--repo", required=True, help="Absolute path to editable repository root")
    parser.add_argument("--test-file", required=True, help="Test file path relative to --repo")
    parser.add_argument("--test-func", required=True, help="Test function/method name")
    parser.add_argument("--test-case", help="Test case name; defaults to --test-func")

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


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    context_attempts = args.attempts if args.attempts is not None else args.context_attempts

    test_input = TestInput(
        repo_root=os.path.abspath(args.repo),
        test_file=args.test_file,
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
