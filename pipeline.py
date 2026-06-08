"""
core/pipeline.py – FlakyGuard-style fixing loop.

This simplified version follows the original FlakyGuard control flow:

  Problem -> Context -> Fix -> Apply -> Validate

It does not use a separate "thought" stage. Each fix attempt makes one LLM
call that directly produces SEARCH/REPLACE edits.
"""

from __future__ import annotations

import logging
import os

from models import TestInput, FlakyInfo, Context, Fix
from prompts import get_flaky_test_fixing_prompt, SEARCH_REPLACE_FORMAT
from graph import CallGraphBuilder, files_near_test
from jacoco_coverage import collect_jacoco_coverage_files
from smart_search import smart_bfs
from search_replace import apply_fix, revert_all, parse_fix, write_patch_file
from llm import complete
from runner import validate_fix
from simplifier import simplify_test, extract_test_func

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)


def collect_context(
    test_input: TestInput,
    flaky_info: FlakyInfo,
    k: int = 3,
    depth_limit: int = -1,
    max_funcs: int = 5,
) -> Context:
    """
    Build the call graph for files near the test and run smart BFS
    to collect the most relevant function nodes.
    """
    test_file_abs = os.path.join(test_input.repo_root, test_input.test_file)

    coverage_files = collect_jacoco_coverage_files(test_input)
    if coverage_files:
        scope_files = coverage_files
        logger.info("Using JaCoCo coverage scope: %d file(s)", len(scope_files))
    else:
        scope_files = files_near_test(test_file_abs, up_levels=2, language=test_input.language)
        logger.info("Using nearby-file scope: %d file(s)", len(scope_files))

    if test_file_abs not in scope_files:
        scope_files.append(test_file_abs)

    builder = CallGraphBuilder(test_input.repo_root, scope_files, language=test_input.language)
    graph = builder.build()

    problem_statement = (
        f"Flaky test '{test_input.test_func}/{test_input.test_case}'. "
        f"Error: {flaky_info.error[:300]}"
    )

    relevant_nodes = smart_bfs(
        graph=graph,
        start_funcs=[test_input.test_func],
        problem_statement=problem_statement,
        k=k,
        depth_limit=depth_limit,
        F=max_funcs,
        callee_only=True,
    )

    imports: dict[str, str] = {}
    for fd in relevant_nodes:
        filepath = fd.filepath
        if filepath not in imports:
            imports[filepath] = _extract_imports(filepath, test_input.language)

    context = Context(func_nodes=relevant_nodes, imports=imports)
    logger.info(
        "Context: %d function nodes from %d files",
        len(context.func_nodes),
        len({func.filepath for func in context.func_nodes}),
    )
    return context


def _extract_imports(filepath: str, language: str) -> str:
    """Extract the import block from a source file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as file:
            source = file.read()
    except OSError:
        return ""

    lang = language.lower()

    if lang == "go":
        import re

        match = re.search(r'import\s*\(([^)]*)\)', source, re.DOTALL)
        if match:
            return "import (\n" + match.group(1) + ")\n"

        single = re.search(r'import\s+"[^"]+"', source)
        return single.group(0) if single else ""

    if lang == "python":
        return "\n".join(
            line for line in source.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        )

    if lang == "java":
        return "\n".join(
            line for line in source.splitlines()
            if line.strip().startswith("import ")
        )

    return ""


def _format_context(context: Context) -> str:
    """Format context nodes into a string for the LLM prompt."""
    sections: list[str] = []
    seen_files: set[str] = set()

    for fd in context.func_nodes:
        filepath = fd.filepath

        if filepath not in seen_files:
            seen_files.add(filepath)
            imports = context.imports.get(filepath, "")
            if imports:
                sections.append(f"=== {filepath} (imports) ===\n{imports}\n")

        sections.append(f"=== {filepath} ===\n{fd.source}\n")

    return "\n".join(sections)


def _valid_filenames_for_context(test_input: TestInput, context: Context) -> list[str]:
    """
    Return paths that the LLM is allowed to edit.

    This is used only to help the original/Aider-style parser choose the
    correct filename near a SEARCH/REPLACE block.
    """
    valid = [test_input.test_file]

    test_abs = os.path.join(test_input.repo_root, test_input.test_file)
    valid.append(test_abs)

    for node in context.func_nodes:
        abs_path = node.filepath
        valid.append(abs_path)

        try:
            rel_path = os.path.relpath(abs_path, test_input.repo_root)
            valid.append(rel_path)
        except ValueError:
            pass

    # Preserve order while removing duplicates.
    seen = set()
    result = []
    for item in valid:
        if item and item not in seen:
            seen.add(item)
            result.append(item)

    return result


def generate_fix(
    test_input: TestInput,
    flaky_info: FlakyInfo,
    context: Context,
) -> Fix | None:
    """
    Convert context to a concrete fix using a single LLM prompt.
    """
    test_file_abs = os.path.join(test_input.repo_root, test_input.test_file)

    try:
        with open(test_file_abs, "r", encoding="utf-8", errors="replace") as file:
            file_source = file.read()
    except OSError:
        logger.error("Cannot read test file: %s", test_file_abs)
        return None

    original_test = (
        extract_test_func(file_source, test_input.test_func, test_input.language)
        or file_source
    )
    simplified_test = simplify_test(original_test, test_input.test_case, test_input.language)

    prompt = get_flaky_test_fixing_prompt(
        simplified_test_code=simplified_test,
        original_test_code=original_test,
        assertion_failures=flaky_info.error,
        error_trace=flaky_info.error_trace,
        code_context=_format_context(context),
        language=test_input.language,
        output_format=SEARCH_REPLACE_FORMAT,
    )

    response = complete(prompt, temperature=0.1)
    if not response:
        return None

    fix = parse_fix(
        response,
        default_filepath=test_input.test_file,
        valid_fnames=_valid_filenames_for_context(test_input, context),
    )

    if not fix.edits:
        logger.warning("LLM produced no SEARCH/REPLACE edits.")
        return None

    return fix


def run_pipeline(
    test_input: TestInput,
    flaky_info: FlakyInfo,
    M: int = 3,
    N: int = 3,
    k: int = 3,
    depth_limit: int = -1,
    max_funcs: int = 5,
    output_dir: str = "patches",
    validation_runs: int = 10,
) -> tuple[bool, str]:
    """
    Run the simplified FlakyGuard fixing pipeline.
    """
    total_attempts = 0

    for m_iter in range(1, M + 1):
        logger.info("─── Context attempt %d/%d: collecting context ───", m_iter, M)

        try:
            context = collect_context(
                test_input,
                flaky_info,
                k=k,
                depth_limit=depth_limit,
                max_funcs=max_funcs,
            )
        except Exception as exc:
            logger.error("Context collection failed: %s", exc)
            context = Context()

        for n_iter in range(1, N + 1):
            total_attempts += 1
            logger.info("  Fix attempt %d/%d for this context", n_iter, N)

            fix = generate_fix(test_input, flaky_info, context)
            if fix is None:
                continue

            ok, result = apply_fix(fix, test_input.repo_root)
            if not ok:
                logger.warning("  Apply failed: %s", result)
                continue

            backups = result  # type: ignore[assignment]

            passed = validate_fix(test_input, runs=validation_runs)
            if passed:
                patch_path = write_patch_file(
                    backups,
                    output_dir,
                    prefix=test_input.test_func,
                )
                revert_all(backups)

                logger.info("✓ Fix validated! Patch saved to: %s", patch_path)
                return True, fix.explanation or "(no explanation)"

            logger.info("  Validation failed – reverting")
            revert_all(backups)

    return False, f"No fix found after {total_attempts} attempts."
