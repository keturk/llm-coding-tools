#!/usr/bin/env python3
"""
Run Radon metrics (complexity, raw, Halstead, MI) on a Python project.

Modes:
    check - Enforce max cyclomatic and cognitive complexity; exit 1 if any block exceeds --max.
    cc - Report cyclomatic complexity (McCabe) per block.
    raw - Report raw metrics (SLOC, comment/blank lines, LOC, LLOC) per file.
    halstead - Report Halstead metrics (volume, difficulty, etc.) per file.
    mi - Report Maintainability Index per file.

Only analyzes Python files under project_root/src (or project_root if --no-src is set);
tests are excluded by default.

In mode=check, both cyclomatic complexity (Radon) and cognitive complexity
(via cognitive_complexity) are enforced.

Usage:
    python complexity.py --project-root ./my-project --mode check --max 15
    python complexity.py --project-root ./my-project --mode raw
    python complexity.py --project-root . --mode halstead --verbose
    python complexity.py --project-root . --no-src --mode check --max 10
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

try:
    from radon.complexity import cc_visit
    from radon.metrics import h_visit, mi_visit
    from radon.raw import analyze as raw_analyze
    from radon.visitors import Class
except ImportError:
    print(
        "Error: radon is not installed. Install with: pip install radon>=6.0",
        file=sys.stderr,
    )
    sys.exit(2)

try:
    from cognitive_complexity.api import get_cognitive_complexity
except ImportError:
    get_cognitive_complexity = None  # type: ignore[assignment, misc]

DEFAULT_MAX_COMPLEXITY = 15
DEFAULT_IGNORE_DIRS = ("tests", "test", "__pycache__", ".git", "node_modules", ".venv", "venv")

OLLAMA_DEFAULT_URL = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "qwen3-coder:30b"
OLLAMA_TIMEOUT_SECONDS = 300
OLLAMA_DEFAULT_NUM_PREDICT = 32768
OLLAMA_MAX_FIX_RETRIES = 3

_CONTEXT_MAX_IMPORT_LINES = 80
_CONTEXT_MAX_INIT_LINES = 15
_CONTEXT_MAX_CONSTANT_LINES = 20
_CONTEXT_MAX_METHODS = 30


def get_block_complexity(block: object) -> int | None:
    """Return cyclomatic complexity for a radon Function or Class block.

    For ``Class`` blocks, Radon's ``real_complexity`` sums every nested
    method's complexity. That rejects large cohesive types even when each
    method is within the limit. Use the class node's own ``complexity``;
    each method is still reported and checked as its own block.
    """
    if isinstance(block, Class):
        return getattr(block, "complexity", None)
    real = getattr(block, "real_complexity", None)
    if real is not None:
        return real
    return getattr(block, "complexity", None)


def collect_py_files(
    project_root: Path,
    ignore_dirs: tuple[str, ...],
    ignore_path_contains_all: tuple[str, ...] | None = None,
    no_src: bool = False,
) -> list[Path]:
    """Return sorted list of Python files under project_root/src (or project_root if no_src).

    Excludes paths that contain any of ignore_dirs, or that contain all
    segment names in ignore_path_contains_all.
    """
    base = project_root if no_src else project_root / "src"
    if not base.is_dir():
        return []
    out: list[Path] = []
    for py_path in base.rglob("*.py"):
        parts = py_path.relative_to(base).parts
        if any(part in ignore_dirs for part in parts):
            continue
        if ignore_path_contains_all and all(
            seg in parts for seg in ignore_path_contains_all
        ):
            continue
        out.append(py_path)
    return sorted(out)


def _load_exclusions(exclusions_file: Path | None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Load exclusion lists from a JSON file.

    The file should have the format:
    {
        "cyclomatic": [["path_substring", "block_name"], ...],
        "cognitive": [["path_substring", "function_name"], ...]
    }

    Returns (cyclomatic_exclusions, cognitive_exclusions).
    """
    if exclusions_file is None or not exclusions_file.is_file():
        return [], []
    try:
        data = json.loads(exclusions_file.read_text(encoding="utf-8"))
        cyclomatic = [tuple(item) for item in data.get("cyclomatic", [])]
        cognitive = [tuple(item) for item in data.get("cognitive", [])]
        return cyclomatic, cognitive  # type: ignore[return-value]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"Warning: could not load exclusions from {exclusions_file}: {e}", file=sys.stderr)
        return [], []


def run_check(
    project_root: Path,
    max_complexity: int,
    ignore_dirs: tuple[str, ...],
    verbose: bool,
    ignore_path_contains_all: tuple[str, ...] | None = None,
    excluded_blocks: list[tuple[str, str]] | None = None,
    no_src: bool = False,
) -> list[tuple[str, str, int, int]]:
    """Check that no block exceeds max complexity. Return list of violations."""
    excluded = excluded_blocks or []
    violations: list[tuple[str, str, int, int]] = []
    for file_path in collect_py_files(
        project_root, ignore_dirs, ignore_path_contains_all, no_src=no_src
    ):
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            if verbose:
                print(f"Warning: could not read {file_path}: {e}", file=sys.stderr)
            continue
        try:
            blocks = cc_visit(source)
        except Exception as e:
            if verbose:
                print(f"Warning: radon failed on {file_path}: {e}", file=sys.stderr)
            continue
        rel_path = str(file_path)
        rel_path_norm = rel_path.replace("\\", "/")
        for block in blocks:
            complexity = get_block_complexity(block)
            if complexity is None or complexity <= max_complexity:
                continue
            name = getattr(block, "name", "?")
            if any(
                path_part in rel_path_norm and name == block_name
                for path_part, block_name in excluded
            ):
                continue
            lineno = getattr(block, "lineno", 0)
            violations.append((rel_path, name, lineno, complexity))
    return violations


def run_check_cognitive(
    project_root: Path,
    max_complexity: int,
    ignore_dirs: tuple[str, ...],
    verbose: bool,
    ignore_path_contains_all: tuple[str, ...] | None = None,
    excluded_blocks: list[tuple[str, str]] | None = None,
    no_src: bool = False,
) -> list[tuple[str, str, int, int]]:
    """Check that no function exceeds max cognitive complexity. Return list of violations."""
    if get_cognitive_complexity is None:
        raise RuntimeError(
            "cognitive_complexity is not installed. Install with: pip install cognitive_complexity"
        )
    excluded = excluded_blocks or []
    violations: list[tuple[str, str, int, int]] = []
    for file_path in collect_py_files(
        project_root, ignore_dirs, ignore_path_contains_all, no_src=no_src
    ):
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            if verbose:
                print(f"Warning: could not read {file_path}: {e}", file=sys.stderr)
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            if verbose:
                print(f"Warning: could not parse {file_path}: {e}", file=sys.stderr)
            continue
        rel_path = str(file_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                try:
                    complexity = get_cognitive_complexity(node)
                except Exception as e:
                    if verbose:
                        print(
                            f"Warning: cognitive_complexity failed on {file_path} {node.name}: {e}",
                            file=sys.stderr,
                        )
                    continue
                if complexity <= max_complexity:
                    continue
                rel_path_norm = rel_path.replace("\\", "/")
                if any(
                    path_part in rel_path_norm and node.name == func_name
                    for path_part, func_name in excluded
                ):
                    continue
                violations.append(
                    (rel_path, node.name, node.lineno, complexity)
                )
    return violations


def find_function_node(
    source: str, func_name: str, lineno: int,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find the AST function/method node matching name and line number."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name and node.lineno == lineno:
                return node
    return None


def get_function_line_range(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[int, int]:
    """Return (start_line, end_line) 1-indexed, inclusive of decorators."""
    start = node.lineno
    if node.decorator_list:
        start = min(d.lineno for d in node.decorator_list)
    assert node.end_lineno is not None
    return start, node.end_lineno


def normalize_indentation(source: str, target_indent: str) -> str:
    """Re-indent source so its base indentation matches target_indent."""
    lines = source.splitlines(keepends=True)
    if not lines:
        return source
    # Detect current base indentation from first non-empty line
    current_indent = ""
    for line in lines:
        stripped = line.lstrip()
        if stripped:
            current_indent = line[: len(line) - len(stripped)]
            break
    if current_indent == target_indent:
        return source
    result: list[str] = []
    for line in lines:
        if not line.strip():
            result.append(line)
        elif line.startswith(current_indent):
            result.append(target_indent + line[len(current_indent):])
        else:
            result.append(line)
    return "".join(result)


def parse_ollama_response(text: str) -> str:
    """Extract Python code from Ollama response, stripping think tags and code fences."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    code_match = re.search(r"```(?:python)?\s*\n(.*?)```", cleaned, flags=re.DOTALL)
    if code_match:
        return code_match.group(1)
    return cleaned


def _build_system_prompt(complexity_type: str) -> str:
    """Build a system prompt for the Ollama refactoring request."""
    if complexity_type == "cyclomatic":
        metric_explanation = (
            "Cyclomatic complexity counts linearly independent paths through code. "
            "Each `if`, `elif`, `for`, `while`, `except`, `and`, `or`, `assert`, "
            "and ternary expression adds 1. A function starts at 1."
        )
    else:
        metric_explanation = (
            "Cognitive complexity measures how hard code is to understand. "
            "Each `if`, `for`, `while`, `except` adds 1, plus a nesting penalty "
            "equal to the current nesting depth. Boolean sequences (`and`/`or`) "
            "add 1 per sequence. Nesting is the primary driver of high scores."
        )
    return (
        "You are a Python refactoring expert. You reduce function complexity "
        "while preserving exact behavior and all type annotations.\n\n"
        f"Metric: {complexity_type} complexity.\n"
        f"{metric_explanation}\n\n"
        "Strategies (use whichever apply):\n"
        "- Early returns / guard clauses to flatten nesting\n"
        "- Extract helper functions or methods (preserve `self` for instance methods)\n"
        "- Replace long if/elif chains with dispatch dicts or mappings\n"
        "- Replace nested conditionals with guard clauses\n"
        "- Use comprehensions instead of loop-accumulate patterns\n"
        "- Decompose compound boolean expressions into named predicates\n"
        "- Invert conditions to reduce nesting depth\n\n"
        "Output rules:\n"
        "- Return ONLY valid Python code inside a single ```python code fence\n"
        "- No explanations, no comments about the changes, no markdown outside the fence\n"
        "- Preserve the original indentation level\n"
        "- If extracting helpers, place them BEFORE the main function at the same "
        "indentation level"
    )


def _detect_indent(source: str) -> str:
    """Return the leading whitespace of the first non-empty line."""
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped:
            return line[: len(line) - len(stripped)]
    return ""


def _extract_imports(tree: ast.Module, lines: list[str]) -> str:
    """Extract import lines from AST, capped at _CONTEXT_MAX_IMPORT_LINES."""
    import_lines: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        start = node.lineno - 1
        end = node.end_lineno if node.end_lineno is not None else node.lineno
        for i in range(start, min(end, len(lines))):
            import_lines.append(lines[i].rstrip())
        if len(import_lines) >= _CONTEXT_MAX_IMPORT_LINES:
            break
    return "\n".join(import_lines[:_CONTEXT_MAX_IMPORT_LINES])


def _extract_class_context(
    tree: ast.Module,
    lines: list[str],
    func_name: str,
    func_lineno: int,
) -> str | None:
    """Extract class context if the target function is a method.

    Returns class declaration, __init__ attributes, and sibling method
    signatures.  Returns None if the function is not inside a class.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        method_match = any(
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == func_name
            and item.lineno == func_lineno
            for item in node.body
        )
        if not method_match:
            continue
        # Class header line
        header_line = lines[node.lineno - 1].rstrip()
        parts: list[str] = [f"## Class context\n{header_line}"]
        # __init__ attributes
        init_lines = _extract_init_lines(node, lines)
        if init_lines:
            parts.append(f"### __init__ attributes\n{init_lines}")
        # Sibling method signatures
        sigs = _extract_method_signatures(node, lines, func_name, func_lineno)
        if sigs:
            parts.append(f"### Other methods in the class\n{sigs}")
        return "\n\n".join(parts)
    return None


def _extract_init_lines(class_node: ast.ClassDef, lines: list[str]) -> str:
    """Extract __init__ method signature and self.x assignments."""
    for item in class_node.body:
        if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
            continue
        result: list[str] = [lines[item.lineno - 1].rstrip()]
        count = 1
        for stmt in ast.walk(item):
            if count >= _CONTEXT_MAX_INIT_LINES:
                break
            if not isinstance(stmt, ast.Assign):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Attribute) and isinstance(
                    target.value, ast.Name
                ) and target.value.id == "self":
                    line_idx = stmt.lineno - 1
                    if 0 <= line_idx < len(lines):
                        result.append(lines[line_idx].rstrip())
                        count += 1
                    break
        return "\n".join(result)
    return ""


def _extract_method_signatures(
    class_node: ast.ClassDef,
    lines: list[str],
    skip_name: str,
    skip_lineno: int,
) -> str:
    """Extract method signatures (def line only) for sibling methods."""
    sigs: list[str] = []
    for item in class_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if item.name == skip_name and item.lineno == skip_lineno:
            continue
        if len(sigs) >= _CONTEXT_MAX_METHODS:
            break
        sig_line = lines[item.lineno - 1].rstrip()
        # Show decorator if present
        if item.decorator_list:
            dec_line = lines[item.decorator_list[0].lineno - 1].rstrip()
            sigs.append(dec_line)
        sigs.append(f"{sig_line} ...")
    return "\n".join(sigs)


def _extract_module_constants(
    tree: ast.Module,
    lines: list[str],
    max_lines: int,
) -> str:
    """Extract top-level constant assignments (Assign/AnnAssign)."""
    result: list[str] = []
    for node in tree.body:
        if len(result) >= max_lines:
            break
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            start = node.lineno - 1
            end = node.end_lineno if node.end_lineno is not None else node.lineno
            for i in range(start, min(end, len(lines))):
                result.append(lines[i].rstrip())
                if len(result) >= max_lines:
                    break
    return "\n".join(result)


def _extract_file_context(
    source: str,
    func_name: str,
    func_lineno: int,
) -> str:
    """Build a context block with imports, class info, and module constants."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    lines = source.splitlines()
    sections: list[str] = []
    sections.append(
        "# File context (for reference only — do not reproduce in your output):"
    )
    imports = _extract_imports(tree, lines)
    if imports:
        sections.append(f"## Imports\n```python\n{imports}\n```")
    class_ctx = _extract_class_context(tree, lines, func_name, func_lineno)
    if class_ctx:
        sections.append(class_ctx)
    constants = _extract_module_constants(tree, lines, _CONTEXT_MAX_CONSTANT_LINES)
    if constants:
        sections.append(f"## Module constants\n```python\n{constants}\n```")
    return "\n\n".join(sections)


def _measure_all_complexities(
    source: str,
    complexity_type: str,
    max_complexity: int,
) -> tuple[bool, list[tuple[str, int]]]:
    """Measure complexity of all functions in a source block.

    The source may be an indented fragment (e.g. a method extracted from a
    class body). We dedent before parsing so ``ast.parse`` succeeds regardless
    of leading indentation.

    Returns (all_pass, [(func_name, complexity), ...]).
    """
    dedented = textwrap.dedent(source)
    results: list[tuple[str, int]] = []
    if complexity_type == "cyclomatic":
        try:
            blocks = cc_visit(dedented)
        except Exception:
            return False, []
        for block in blocks:
            name = getattr(block, "name", "?")
            compl = get_block_complexity(block)
            if compl is not None:
                results.append((name, compl))
    else:
        if get_cognitive_complexity is None:
            return False, []
        try:
            tree = ast.parse(dedented)
        except SyntaxError:
            return False, []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            try:
                compl = get_cognitive_complexity(node)
            except Exception:
                continue
            results.append((node.name, compl))
    all_pass = all(c <= max_complexity for _, c in results)
    return all_pass, results


def _validate_fix_inmemory(
    new_content: str,
    fixed_source: str,
    complexity_type: str,
    max_complexity: int,
) -> tuple[bool, str | None, list[tuple[str, int]]]:
    """Validate a fix in-memory: syntax check then complexity re-check.

    Returns (passed, error_message_or_none, [(name, complexity), ...]).
    """
    try:
        ast.parse(new_content)
    except SyntaxError as e:
        return False, f"Syntax error: {e}", []
    all_pass, complexities = _measure_all_complexities(
        fixed_source, complexity_type, max_complexity,
    )
    if not all_pass:
        return False, None, complexities
    return True, None, complexities


def _build_retry_feedback(
    complexity_type: str,
    complexities: list[tuple[str, int]],
    max_complexity: int,
    validation_error: str | None = None,
) -> str:
    """Build feedback for the LLM when a fix attempt failed."""
    parts: list[str] = []
    if validation_error:
        parts.append(f"Your previous attempt had a validation error: {validation_error}")
    for name, value in complexities:
        if value > max_complexity:
            parts.append(
                f"Function `{name}` still has {complexity_type} complexity "
                f"{value} (must be <= {max_complexity})."
            )
    parts.append(
        "Try a different approach: extract more helpers, use early returns, "
        "replace conditionals with dispatch dicts, or flatten nesting further."
    )
    return "\n".join(parts)


def _build_disk_error_feedback(disk_error: str) -> str:
    """Build feedback for the LLM when ruff or tests fail after disk write."""
    if disk_error == "test_failure":
        return (
            "Your previous attempt caused test failures. "
            "Ensure the refactored code preserves exact behavior — "
            "do not change logic, only restructure for lower complexity."
        )
    # Ruff undefined-name errors (F821)
    return (
        f"Your previous attempt introduced undefined names:\n{disk_error}\n\n"
        "IMPORTANT: Only use names that are already imported or defined in the file. "
        "Do NOT invent new type names. If you extract a helper function, its parameter "
        "types must use only types visible in the file's import section shown in the context."
    )


def call_ollama(
    function_source: str,
    func_name: str,
    complexity_type: str,
    complexity_value: int,
    max_complexity: int,
    ollama_url: str,
    model: str,
    file_context: str = "",
    retry_feedback: str | None = None,
) -> str:
    """Send function to Ollama for complexity reduction. Return fixed source."""
    indent_count = len(_detect_indent(function_source))
    system_prompt = _build_system_prompt(complexity_type)

    parts: list[str] = [
        f"Refactor the function `{func_name}` to reduce its {complexity_type} "
        f"complexity from {complexity_value} to at most {max_complexity}.\n",
    ]
    if retry_feedback:
        parts.append(f"IMPORTANT — Previous attempt feedback:\n{retry_feedback}\n")
    parts.append(
        "Requirements:\n"
        f"- Maintain the same function signature, behavior, and base indentation "
        f"({indent_count} spaces)\n"
        "- You may extract helper functions/methods at the same indent level, "
        "placed BEFORE the main function\n"
        "- If the function is a class method (has `self` parameter), extracted "
        "helpers should also be methods with a `self` parameter\n"
        "- Preserve all type annotations\n"
        "- Return ONLY Python code inside a single ```python code fence"
    )
    if file_context:
        parts.append(f"\n{file_context}")
    parts.append(f"\nFunction to refactor:\n```python\n{function_source}```")
    user_prompt = "\n".join(parts)

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {
                "temperature": 0.3,
                "num_predict": OLLAMA_DEFAULT_NUM_PREDICT,
            },
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{ollama_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to connect to Ollama at {ollama_url}: {e}"
        ) from e
    except TimeoutError as exc:
        raise RuntimeError(
            f"Ollama request timed out after {OLLAMA_TIMEOUT_SECONDS}s"
        ) from exc

    content = data["message"]["content"]
    return parse_ollama_response(content)


def _run_ruff_check(file_path: Path, verbose: bool) -> tuple[bool, str]:
    """Run ruff check for undefined names on the file.

    Returns (passed, error_details). error_details is empty on success or
    contains the ruff output on failure (for retry feedback).
    """
    try:
        result = subprocess.run(
            ["ruff", "check", "--select", "F821", str(file_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        if verbose:
            print(
                "Warning: ruff not found, skipping undefined name check",
                file=sys.stderr,
            )
        return True, ""
    except subprocess.TimeoutExpired:
        print("Warning: ruff check timed out", file=sys.stderr)
        return True, ""
    if result.returncode != 0:
        details = result.stdout.strip()
        print("Ruff found undefined names:", file=sys.stderr)
        for line in details.splitlines():
            print(f"  {line}", file=sys.stderr)
        return False, details
    return True, ""


PYTEST_TIMEOUT_SECONDS = 600


def _run_pytest(project_root: Path, verbose: bool) -> bool:
    """Run pytest with fail-fast on the project. Return True if all pass.

    Coverage is disabled (--no-cov, --override-ini=addopts=) to avoid false
    failures from coverage thresholds when refactoring adds extracted helpers.
    """
    print("Running tests (pytest -x -q --tb=short --no-cov)...", file=sys.stderr)
    try:
        result = subprocess.run(
            [
                "python", "-m", "pytest", "tests/", "-x", "-q", "--tb=short",
                "--no-cov", "--override-ini=addopts=",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        print("Warning: pytest not found, skipping tests", file=sys.stderr)
        return True
    except subprocess.TimeoutExpired:
        print(
            f"Warning: tests timed out after {PYTEST_TIMEOUT_SECONDS}s",
            file=sys.stderr,
        )
        return False
    if result.returncode != 0:
        print("Tests failed:", file=sys.stderr)
        output_lines = result.stdout.strip().splitlines()
        tail = output_lines[-20:] if len(output_lines) > 20 else output_lines
        for line in tail:
            print(f"  {line}", file=sys.stderr)
        return False
    passed_line = [
        line for line in result.stdout.strip().splitlines() if "passed" in line
    ]
    if passed_line:
        print(f"  {passed_line[-1]}", file=sys.stderr)
    return True


def _attempt_single_ollama_fix(
    func_source: str,
    func_name: str,
    comp_type: str,
    complexity: int,
    max_complexity: int,
    ollama_url: str,
    model: str,
    file_context: str,
    original_indent: str,
    lines: list[str],
    start: int,
    end: int,
    retry_feedback: str | None,
) -> tuple[str | None, str | None, list[tuple[str, int]]]:
    """Run one Ollama attempt and validate in-memory.

    Returns (new_content_or_none, error_message_or_none, complexities).
    On Ollama connection errors, raises RuntimeError.
    """
    fixed_source = call_ollama(
        func_source, func_name, comp_type, complexity,
        max_complexity, ollama_url, model,
        file_context=file_context,
        retry_feedback=retry_feedback,
    )
    fixed_source = normalize_indentation(fixed_source, original_indent)
    if not fixed_source.endswith("\n"):
        fixed_source += "\n"

    new_lines = lines[: start - 1] + [fixed_source] + lines[end:]
    new_content = "".join(new_lines)

    passed, error_msg, complexities = _validate_fix_inmemory(
        new_content, fixed_source, comp_type, max_complexity,
    )
    if not passed:
        return None, error_msg, complexities
    return new_content, None, complexities


def _attempt_fix_violation(
    file_path: Path,
    source: str,
    func_name: str,
    lineno: int,
    complexity: int,
    comp_type: str,
    max_complexity: int,
    ollama_url: str,
    model: str,
    verbose: bool,
    unit_tests: bool,
    project_root: Path,
    max_retries: int,
) -> bool:
    """Attempt to fix a single violation with retries. Return True if fixed."""
    node = find_function_node(source, func_name, lineno)
    if node is None:
        if verbose:
            print(
                f"Skipping {func_name} at {file_path}:{lineno} (not a function)",
                file=sys.stderr,
            )
        return False

    original_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    lines = source.splitlines(keepends=True)
    start, end = get_function_line_range(node)
    func_source = "".join(lines[start - 1: end])
    original_indent = _detect_indent(func_source)
    file_context = _extract_file_context(source, func_name, lineno)

    print(
        f"\nFixing: {file_path}:{lineno} {func_name} "
        f"({comp_type}={complexity}, max={max_complexity})",
        file=sys.stderr,
    )
    print(
        f"Extracted function ({end - start + 1} lines, lines {start}-{end})",
        file=sys.stderr,
    )

    retry_feedback: str | None = None
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            print(f"  Retry {attempt}/{max_retries}...", file=sys.stderr)
        print(f"  Sending to Ollama ({model})...", file=sys.stderr)

        try:
            new_content, error_msg, complexities = _attempt_single_ollama_fix(
                func_source, func_name, comp_type, complexity, max_complexity,
                ollama_url, model, file_context, original_indent,
                lines, start, end, retry_feedback,
            )
        except RuntimeError as e:
            print(f"  Ollama error: {e}", file=sys.stderr)
            return False

        if new_content is None:
            _log_validation_failure(error_msg, complexities, comp_type, max_complexity)
            if attempt < max_retries:
                retry_feedback = _build_retry_feedback(
                    comp_type, complexities, max_complexity, error_msg,
                )
                continue
            print(f"  All {max_retries} attempt(s) failed.", file=sys.stderr)
            return False

        # In-memory checks passed — verify file not modified, then disk checks
        disk_ok, disk_error = _apply_and_verify_on_disk(
            file_path, source, new_content, original_hash,
            verbose, unit_tests, project_root,
        )
        if not disk_ok:
            if disk_error == "file_modified":
                return False
            if attempt < max_retries:
                retry_feedback = _build_disk_error_feedback(disk_error)
                continue
            print(f"  All {max_retries} attempt(s) failed.", file=sys.stderr)
            return False

        print(f"Fixed: {file_path}:{start} {func_name}", file=sys.stderr)
        return True
    return False


def _log_validation_failure(
    error_msg: str | None,
    complexities: list[tuple[str, int]],
    comp_type: str,
    max_complexity: int,
) -> None:
    """Log why an in-memory validation failed."""
    if error_msg:
        print(f"  Validation failed: {error_msg}", file=sys.stderr)
    else:
        for name, val in complexities:
            if val > max_complexity:
                print(
                    f"  {name}: {comp_type}={val} (still > {max_complexity})",
                    file=sys.stderr,
                )


def _apply_and_verify_on_disk(
    file_path: Path,
    original_source: str,
    new_content: str,
    original_hash: str,
    verbose: bool,
    unit_tests: bool,
    project_root: Path,
) -> tuple[bool, str]:
    """Write fixed file, run ruff/pytest. Revert on failure.

    Returns (success, error_details). error_details is non-empty when ruff
    or tests fail, suitable for inclusion in retry feedback.
    """
    current_content = file_path.read_text(encoding="utf-8")
    current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
    if current_hash != original_hash:
        print(
            f"  File {file_path} was modified during processing, skipping.",
            file=sys.stderr,
        )
        return False, "file_modified"

    file_path.write_text(new_content, encoding="utf-8")

    ruff_ok, ruff_details = _run_ruff_check(file_path, verbose)
    if not ruff_ok:
        print("  Reverting due to ruff errors.", file=sys.stderr)
        file_path.write_text(original_source, encoding="utf-8")
        return False, ruff_details

    if unit_tests and not _run_pytest(project_root, verbose):
        print("  Reverting due to test failures.", file=sys.stderr)
        file_path.write_text(original_source, encoding="utf-8")
        return False, "test_failure"

    return True, ""


def _collect_violations(
    project_root: Path,
    max_complexity: int,
    ignore_dirs: tuple[str, ...],
    verbose: bool,
    ignore_path_contains_all: tuple[str, ...] | None,
    excluded_cyclomatic: list[tuple[str, str]] | None = None,
    excluded_cognitive: list[tuple[str, str]] | None = None,
    no_src: bool = False,
) -> list[tuple[str, str, int, int, str]]:
    """Collect, combine, deduplicate, and sort all complexity violations."""
    cc_violations = run_check(
        project_root, max_complexity, ignore_dirs, verbose, ignore_path_contains_all,
        excluded_blocks=excluded_cyclomatic, no_src=no_src,
    )
    cog_violations: list[tuple[str, str, int, int]] = []
    if get_cognitive_complexity is not None:
        try:
            cog_violations = run_check_cognitive(
                project_root, max_complexity, ignore_dirs, verbose,
                ignore_path_contains_all, excluded_blocks=excluded_cognitive,
                no_src=no_src,
            )
        except RuntimeError as e:
            print(str(e), file=sys.stderr)

    all_violations: list[tuple[str, str, int, int, str]] = []
    for path, name, lineno, compl in cc_violations:
        all_violations.append((path, name, lineno, compl, "cyclomatic"))
    for path, name, lineno, compl in cog_violations:
        all_violations.append((path, name, lineno, compl, "cognitive"))

    all_violations.sort(key=lambda v: v[3], reverse=True)

    seen: set[tuple[str, str, int]] = set()
    unique: list[tuple[str, str, int, int, str]] = []
    for v in all_violations:
        key = (v[0], v[1], v[2])
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def run_fix(
    project_root: Path,
    max_complexity: int,
    ignore_dirs: tuple[str, ...],
    verbose: bool,
    ignore_path_contains_all: tuple[str, ...] | None,
    ollama_url: str,
    model: str,
    unit_tests: bool = False,
    max_retries: int = OLLAMA_MAX_FIX_RETRIES,
    fix_all: bool = False,
    excluded_cyclomatic: list[tuple[str, str]] | None = None,
    excluded_cognitive: list[tuple[str, str]] | None = None,
    no_src: bool = False,
) -> int:
    """Fix complexity violations using Ollama. Return exit code.

    Tries violations from worst to least complex, with up to max_retries
    Ollama attempts per violation. On any failure (Ollama error, syntax error,
    complexity still too high, ruff check, or test failure), reverts the file
    and moves to the next violation.

    With fix_all=False (default), exits after the first successful fix.
    With fix_all=True, continues fixing all violations. After a file is
    modified, subsequent violations in that file are re-collected to account
    for shifted line numbers.
    """
    unique = _collect_violations(
        project_root, max_complexity, ignore_dirs, verbose, ignore_path_contains_all,
        excluded_cyclomatic=excluded_cyclomatic, excluded_cognitive=excluded_cognitive,
        no_src=no_src,
    )
    if not unique:
        print("No complexity violations found. Nothing to fix.", file=sys.stderr)
        return 0

    fixed_count = 0
    failed_count = 0
    modified_files: set[str] = set()

    for file_path_str, func_name, lineno, complexity, comp_type in unique:
        if file_path_str in modified_files:
            # Line numbers have shifted; skip — will be re-collected in next pass
            continue
        file_path = Path(file_path_str)
        try:
            source = file_path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Warning: could not read {file_path}: {e}", file=sys.stderr)
            continue

        if _attempt_fix_violation(
            file_path, source, func_name, lineno, complexity, comp_type,
            max_complexity, ollama_url, model, verbose, unit_tests,
            project_root, max_retries,
        ):
            fixed_count += 1
            modified_files.add(file_path_str)
            if not fix_all:
                print("Re-run the script to fix more violations.", file=sys.stderr)
                return 0
        else:
            failed_count += 1

    if fix_all and modified_files:
        # Re-collect to handle violations in modified files (shifted line numbers)
        remaining = _collect_violations(
            project_root, max_complexity, ignore_dirs, verbose,
            ignore_path_contains_all,
            excluded_cyclomatic=excluded_cyclomatic,
            excluded_cognitive=excluded_cognitive,
            no_src=no_src,
        )
        for file_path_str, func_name, lineno, complexity, comp_type in remaining:
            file_path = Path(file_path_str)
            try:
                source = file_path.read_text(encoding="utf-8")
            except OSError as e:
                print(f"Warning: could not read {file_path}: {e}", file=sys.stderr)
                continue
            if _attempt_fix_violation(
                file_path, source, func_name, lineno, complexity, comp_type,
                max_complexity, ollama_url, model, verbose, unit_tests,
                project_root, max_retries,
            ):
                fixed_count += 1
            else:
                failed_count += 1

    if fix_all:
        print(
            f"\nFix-all complete: {fixed_count} fixed, {failed_count} failed.",
            file=sys.stderr,
        )
        return 0 if fixed_count > 0 or failed_count == 0 else 1

    print("No violations could be fixed.", file=sys.stderr)
    return 1


def run_cc(
    project_root: Path,
    ignore_dirs: tuple[str, ...],
    verbose: bool,
    ignore_path_contains_all: tuple[str, ...] | None = None,
    no_src: bool = False,
) -> None:
    """Print cyclomatic complexity per block."""
    for file_path in collect_py_files(
        project_root, ignore_dirs, ignore_path_contains_all, no_src=no_src
    ):
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            if verbose:
                print(f"Warning: could not read {file_path}: {e}", file=sys.stderr)
            continue
        try:
            blocks = cc_visit(source)
        except Exception as e:
            if verbose:
                print(f"Warning: radon failed on {file_path}: {e}", file=sys.stderr)
            continue
        rel_path = str(file_path)
        for block in blocks:
            complexity = get_block_complexity(block)
            name = getattr(block, "name", "?")
            lineno = getattr(block, "lineno", 0)
            if complexity is not None:
                print(f"{rel_path}:{lineno} {name} (complexity={complexity})")


def run_raw(
    project_root: Path,
    ignore_dirs: tuple[str, ...],
    verbose: bool,
    ignore_path_contains_all: tuple[str, ...] | None = None,
    no_src: bool = False,
) -> None:
    """Print raw metrics (SLOC, comment, blank, LOC, LLOC) per file."""
    for file_path in collect_py_files(
        project_root, ignore_dirs, ignore_path_contains_all, no_src=no_src
    ):
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            if verbose:
                print(f"Warning: could not read {file_path}: {e}", file=sys.stderr)
            continue
        try:
            mod = raw_analyze(source)
        except Exception as e:
            if verbose:
                print(f"Warning: radon raw failed on {file_path}: {e}", file=sys.stderr)
            continue
        # loc, lloc, sloc, comments, multi, blank, single_comments
        print(
            f"{file_path} sloc={mod.sloc} lloc={mod.lloc} loc={mod.loc} "
            f"comments={mod.comments} blank={mod.blank} multi={mod.multi}"
        )


def run_halstead(
    project_root: Path,
    ignore_dirs: tuple[str, ...],
    verbose: bool,
    ignore_path_contains_all: tuple[str, ...] | None = None,
    no_src: bool = False,
) -> None:
    """Print Halstead metrics (volume, difficulty, etc.) per file."""
    for file_path in collect_py_files(
        project_root, ignore_dirs, ignore_path_contains_all, no_src=no_src
    ):
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            if verbose:
                print(f"Warning: could not read {file_path}: {e}", file=sys.stderr)
            continue
        try:
            hal = h_visit(source)
        except Exception as e:
            if verbose:
                print(
                    f"Warning: radon halstead failed on {file_path}: {e}",
                    file=sys.stderr,
                )
            continue
        t = hal.total
        print(
            f"{file_path} volume={t.volume:.1f} difficulty={t.difficulty:.1f} "
            f"effort={t.effort:.1f} h1={t.h1} h2={t.h2} N1={t.N1} N2={t.N2}"
        )


def run_mi(
    project_root: Path,
    ignore_dirs: tuple[str, ...],
    verbose: bool,
    count_multi: bool = True,
    ignore_path_contains_all: tuple[str, ...] | None = None,
    no_src: bool = False,
) -> None:
    """Print Maintainability Index per file."""
    for file_path in collect_py_files(
        project_root, ignore_dirs, ignore_path_contains_all, no_src=no_src
    ):
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            if verbose:
                print(f"Warning: could not read {file_path}: {e}", file=sys.stderr)
            continue
        try:
            mi = mi_visit(source, count_multi)
        except Exception as e:
            if verbose:
                print(
                    f"Warning: radon MI failed on {file_path}: {e}",
                    file=sys.stderr,
                )
            continue
        print(f"{file_path} mi={mi:.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Python complexity metrics (cyclomatic, cognitive, raw, Halstead, MI).",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        required=True,
        help="Path to the project root (containing src/ or use --no-src).",
    )
    parser.add_argument(
        "--mode",
        choices=("check", "cc", "raw", "halstead", "mi"),
        default="check",
        help="Metric to run: check (enforce max), cc, raw, halstead, mi (default: check).",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX_COMPLEXITY,
        metavar="N",
        help=f"Max cyclomatic and cognitive complexity for mode=check (default: {DEFAULT_MAX_COMPLEXITY}).",
    )
    parser.add_argument(
        "--ignore",
        type=str,
        default=",".join(DEFAULT_IGNORE_DIRS),
        help="Comma-separated directory names to ignore.",
    )
    parser.add_argument(
        "--ignore-path-contains",
        type=str,
        default="",
        metavar="SEG1,SEG2",
        help="Skip files whose path contains all these segment names.",
    )
    parser.add_argument(
        "--no-src",
        action="store_true",
        help="Scan project_root directly instead of project_root/src.",
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
        default=None,
        metavar="FILE",
        help="JSON file with exclusion lists (cyclomatic and cognitive blocks to skip).",
    )
    parser.add_argument(
        "--no-multi",
        action="store_true",
        help="For mode=mi: do not count multiline strings as comments.",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose output.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Debug output (same as verbose for now).",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Fix the worst complexity violation using local Ollama (mode=check only).",
    )
    parser.add_argument(
        "--fix-all",
        action="store_true",
        help="Fix ALL violations (not just the first). Implies --fix.",
    )
    parser.add_argument(
        "--ollama-url",
        type=str,
        default=OLLAMA_DEFAULT_URL,
        help=f"Ollama server URL (default: {OLLAMA_DEFAULT_URL}).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=OLLAMA_DEFAULT_MODEL,
        help=f"Ollama model name (default: {OLLAMA_DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run pytest after fix to verify; revert if tests fail (--fix only).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=OLLAMA_MAX_FIX_RETRIES,
        metavar="N",
        help=f"Max Ollama retry attempts per violation (default: {OLLAMA_MAX_FIX_RETRIES}).",
    )

    args = parser.parse_args()
    project_root = args.project_root.resolve()
    if not project_root.is_dir():
        print(
            f"Error: project root is not a directory: {project_root}",
            file=sys.stderr,
        )
        return 1

    ignore_dirs = tuple(s.strip() for s in args.ignore.split(",") if s.strip())
    ignore_path_contains_all: tuple[str, ...] | None = None
    if args.ignore_path_contains:
        ignore_path_contains_all = tuple(
            s.strip() for s in args.ignore_path_contains.split(",") if s.strip()
        )
    verbose = args.verbose or args.debug
    no_src = args.no_src

    # Load exclusions if provided
    excluded_cyclomatic: list[tuple[str, str]] = []
    excluded_cognitive: list[tuple[str, str]] = []
    if args.exclusions:
        excluded_cyclomatic, excluded_cognitive = _load_exclusions(args.exclusions)

    if args.fix or args.fix_all:
        if args.mode != "check":
            print(
                "Error: --fix/--fix-all can only be used with --mode check.",
                file=sys.stderr,
            )
            return 1
        return run_fix(
            project_root,
            max_complexity=args.max,
            ignore_dirs=ignore_dirs,
            verbose=verbose,
            ignore_path_contains_all=ignore_path_contains_all,
            ollama_url=args.ollama_url,
            model=args.model,
            unit_tests=args.test,
            max_retries=args.max_retries,
            fix_all=args.fix_all,
            excluded_cyclomatic=excluded_cyclomatic,
            excluded_cognitive=excluded_cognitive,
            no_src=no_src,
        )

    if args.mode == "check":
        cc_violations = run_check(
            project_root,
            max_complexity=args.max,
            ignore_dirs=ignore_dirs,
            verbose=verbose,
            ignore_path_contains_all=ignore_path_contains_all,
            excluded_blocks=excluded_cyclomatic,
            no_src=no_src,
        )
        try:
            cog_violations = run_check_cognitive(
                project_root,
                max_complexity=args.max,
                ignore_dirs=ignore_dirs,
                verbose=verbose,
                ignore_path_contains_all=ignore_path_contains_all,
                excluded_blocks=excluded_cognitive,
                no_src=no_src,
            )
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        if not cc_violations and not cog_violations:
            print(
                f"Cyclomatic and cognitive complexity: all blocks within limit ({args.max}).",
                file=sys.stderr,
            )
            if verbose:
                print(
                    f"OK: no blocks exceed cyclomatic or cognitive complexity {args.max}",
                    file=sys.stderr,
                )
            return 0
        has_failure = False
        if cc_violations:
            has_failure = True
        print(
            f"Radon (cyclomatic): {len(cc_violations)} block(s) exceed max {args.max}:",
            file=sys.stderr,
        )
        for file_path, name, lineno, complexity in sorted(
            cc_violations, key=lambda v: (v[0], v[2])
        ):
            print(
                f"  {file_path}:{lineno} {name} (cyclomatic={complexity})",
                file=sys.stderr,
            )
        if cog_violations:
            has_failure = True
        print(
            f"Cognitive complexity: {len(cog_violations)} function(s) exceed max {args.max}:",
            file=sys.stderr,
        )
        for file_path, name, lineno, complexity in sorted(
            cog_violations, key=lambda v: (v[0], v[2])
        ):
            print(
                f"  {file_path}:{lineno} {name} (cognitive={complexity})",
                file=sys.stderr,
            )
        return 1 if has_failure else 0

    if args.mode == "cc":
        run_cc(project_root, ignore_dirs, verbose, ignore_path_contains_all, no_src=no_src)
    elif args.mode == "raw":
        run_raw(project_root, ignore_dirs, verbose, ignore_path_contains_all, no_src=no_src)
    elif args.mode == "halstead":
        run_halstead(
            project_root, ignore_dirs, verbose, ignore_path_contains_all, no_src=no_src
        )
    elif args.mode == "mi":
        run_mi(
            project_root,
            ignore_dirs,
            verbose,
            count_multi=not args.no_multi,
            ignore_path_contains_all=ignore_path_contains_all,
            no_src=no_src,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
