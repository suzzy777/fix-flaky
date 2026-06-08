"""
core/prompts.py – prompt utilities for FlakyGuard Simple.

This keeps the simplified codebase close to the original FlakyGuard prompt
behavior: one LLM prompt gets the simplified test, original test, error,
stack trace, relevant context, and output format, then directly produces
SEARCH/REPLACE edits plus an explanation.
"""

from __future__ import annotations


DEFAULT_BEST_PRACTICES = """\
## Best Practices for Fixing Flaky Tests

### Code Quality Standards
- Follow existing code conventions and patterns in the codebase.
- Use existing libraries and utilities when available.
- Ensure proper error handling and edge case coverage.
- Write clear, descriptive variable and function names.
- Add meaningful comments only when necessary to explain complex logic.

### Testing Best Practices
- Prefer deterministic test patterns over time-dependent or order-dependent logic.
- Ensure test isolation; tests should not depend on external state or other tests.
- Clean up resources properly in test setup or teardown.
- Use dependency injection and mocking to control external dependencies.
"""


SEARCH_REPLACE_FORMAT = """\
## Required Output Format

# SEARCH/REPLACE block Rules:

Every SEARCH/REPLACE block must use this format:

1. The FULL file path alone on a line, verbatim. No bold asterisks, no quotes around it, no escaping of characters.
2. The opening fence and code language, e.g. ```java
3. The start of search block: <<<<<<< SEARCH
4. A contiguous chunk of lines to search for in the existing source code.
5. The dividing line: =======
6. The lines to replace into the source code.
7. The end of the replace block: >>>>>>> REPLACE
8. The closing fence: ```

Example:

src/test/java/MyTest.java
```java
<<<<<<< SEARCH
    assertEquals(expected, result);
=======
    assertEquals(expected, actualResult);
>>>>>>> REPLACE
```

Every SEARCH section must EXACTLY MATCH the existing file content, character for character, including comments, spaces, indentation, and line breaks.

SEARCH/REPLACE blocks replace only the first matching occurrence.
Use multiple SEARCH/REPLACE blocks if needed.

Keep SEARCH/REPLACE blocks concise.
Break large edits into smaller blocks.
Include enough lines in each SEARCH section to uniquely match the code.

Only create SEARCH/REPLACE blocks for files that are provided in the prompt/context.
Prefer editing tests, not production code, unless production code is clearly necessary.

Do not include ellipses (...), [...], or incomplete code in SEARCH blocks.
Do not put explanations before the SEARCH/REPLACE blocks.

## Explanation Format

After all SEARCH/REPLACE blocks, include a brief explanation wrapped in EXPLANATION tags:

```
<EXPLANATION>
Brief explanation of what was causing the flakiness and how the fix addresses it.
</EXPLANATION>
```
"""


def get_flaky_test_fixing_prompt(
    simplified_test_code: str,
    original_test_code: str,
    assertion_failures: str,
    error_trace: str,
    code_context: str,
    language: str,
    output_format: str,
    best_practices: str | None = None,
) -> str:
    """
    Generate a comprehensive prompt for fixing flaky tests.

    This follows the original FlakyGuard prompt structure:
    simplified test, original test, error messages, stack trace, additional
    context, common flaky-test patterns, fixing strategy, output format, and
    required output.
    """
    best_practices_text = best_practices or DEFAULT_BEST_PRACTICES

    return f"""You are an expert at fixing flaky tests in {language}. Your goal is to identify and fix the root cause of non-deterministic test behavior.

{best_practices_text}

## Test Analysis

**Simplified test for reference (to help you understand the structure):**
```{language}
{simplified_test_code}
```

**Original test to be fixed:**
```{language}
{original_test_code}
```

**Error messages:**
```
{assertion_failures}
```

**Stack trace:**
```
{error_trace}
```

**Additional context:**
```
{code_context}
```

## Common Flaky Test Patterns to Look For

1. **Timing Issues**: Race conditions, asynchronous work finishing in different orders, or tests relying on sleeps/timeouts
2. **Shared State**: Global/static variables, system properties, environment variables, files, databases, caches, or mocks shared between tests
3. **Non-deterministic Ordering**: Iteration over unordered collections, order-sensitive assertions, or assumptions about execution order
4. **Non-deterministic Algorithms**: Random number generation, UUID generation, generated IDs, or data structures with unstable ordering
5. **External Dependencies**: Network calls, file system behavior, time-dependent code, or resource cleanup issues

## Fixing Strategy

1. **Analyze the error**: Look for patterns indicating non-deterministic behavior
2. **Identify the root cause**: Timing, shared state, ordering, randomness, external dependency, etc.
3. **Apply appropriate fix**:
   - Make ordering deterministic before comparison
   - Use deterministic test data
   - Isolate or reset test state
   - Add proper synchronization
   - Mock or control non-deterministic dependencies

4. **Focus on the specific failing test case** rather than modifying shared setup
5. **Fix the test logic instead of the code under test, unless you strongly believe that the code under test is buggy.**

{output_format}

## Required Output

Please analyze the test failure and provide:

1. **The necessary fixes** using the SEARCH/REPLACE format above, note the search should be with respect to the original test function.
2. **A brief explanation** wrapped in EXPLANATION tags if using the combined format.
"""
