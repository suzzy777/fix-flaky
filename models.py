"""
core/types.py – shared dataclasses for FlakyGuard Simple.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class TestInput:
    """Everything needed to locate, run, and repair the flaky test."""
    repo_root: str
    test_file: str
    test_func: str
    test_case: str
    language: str = "go"

    # Validation command, and command-based reproduction when no repro script is used.
    # Use {test_file}, {test_func}, {test_case}, and {test_dir} as placeholders.
    run_cmd: str = "go test -v -run {test_func} ./{test_dir}/..."
    repro_runs: int = 100

    # Optional script-based reproduction mode.
    # If repro_script is set, reproduce_failure() runs:
    #   bash ./single_runner.sh <repro_issue_id>
    # from the script workdir. The script reads test_config.csv itself.
    repro_script: str = ""
    repro_issue_id: str = ""
    repro_workdir: str = ""
    repro_config_csv: str = ""
    repro_zip: str = ""
    repro_timeout: int = 1800

    # Optional Java coverage-based context narrowing.
    # When enabled, pipeline.py runs JaCoCo and builds the call graph only
    # from covered files plus the test file, closer to original FlakyGuard.
    use_jacoco_coverage: bool = False
    coverage_cmd: str = ""
    coverage_report: str = "target/site/jacoco/jacoco.xml"
    coverage_timeout: int = 900
    coverage_max_files: int = 80


@dataclasses.dataclass
class FlakyInfo:
    """Captured details of a reproduced flaky failure."""
    error: str
    error_trace: str
    error_file: str = ""
    error_line: int = 0


@dataclasses.dataclass
class FuncNode:
    """A function definition extracted from source code."""
    name: str
    filepath: str
    start_line: int
    end_line: int
    source: str


@dataclasses.dataclass
class Context:
    """Code context collected by smart graph search."""
    func_nodes: list[FuncNode] = dataclasses.field(default_factory=list)
    imports: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class SearchReplaceEdit:
    """One SEARCH/REPLACE block produced by the LLM."""
    filepath: str
    search_text: str
    replace_text: str


@dataclasses.dataclass
class Fix:
    """A proposed fix: search/replace edits plus explanation."""
    edits: list[SearchReplaceEdit]
    explanation: str = ""
