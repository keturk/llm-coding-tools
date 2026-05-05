# Complexity Analyzer

Analyzes Python code complexity using multiple metrics and optionally auto-refactors high-complexity functions using a local [Ollama](https://ollama.com/) LLM.

## Features

- **Cyclomatic complexity** (McCabe) via [Radon](https://radon.readthedocs.io/) - counts linearly independent paths through code
- **Cognitive complexity** via [cognitive_complexity](https://github.com/Melevir/cognitive_complexity) - measures how hard code is to understand
- **Raw metrics** - SLOC, comments, blank lines, LOC, LLOC per file
- **Halstead metrics** - volume, difficulty, effort per file
- **Maintainability Index** - composite maintainability score per file
- **Auto-fix with Ollama** - automatically refactors functions that exceed the complexity threshold using a local LLM, with retry logic and safety checks

## Requirements

- Python 3.10+
- [radon](https://pypi.org/project/radon/) >= 6.0
- [cognitive_complexity](https://pypi.org/project/cognitive-complexity/) (for cognitive complexity checks)
- [Ollama](https://ollama.com/) (only for `--fix` / `--fix-all`)
- [ruff](https://docs.astral.sh/ruff/) (optional, used to validate fixes)
- [pytest](https://docs.pytest.org/) (optional, used with `--test` to verify fixes)

Install the Python dependencies:

```bash
pip install "radon>=6.0" cognitive_complexity
```

## Usage

### Python

```bash
# Check complexity (cyclomatic + cognitive) against a threshold
python complexity.py --project-root ./my-project --mode check --max 15

# Report cyclomatic complexity per function/class
python complexity.py --project-root ./my-project --mode cc

# Report raw metrics (SLOC, comments, blanks)
python complexity.py --project-root ./my-project --mode raw

# Report Halstead metrics
python complexity.py --project-root ./my-project --mode halstead

# Report Maintainability Index
python complexity.py --project-root ./my-project --mode mi

# Auto-fix the worst violation using Ollama
python complexity.py --project-root ./my-project --mode check --fix

# Auto-fix all violations, running tests after each fix
python complexity.py --project-root ./my-project --mode check --fix-all --test
```

### PowerShell Wrapper

A convenience wrapper for running `complexity.py` across one or more projects on Windows. Automatically installs missing Python dependencies.

```powershell
# Check a single project
.\complexity.ps1 .\my-project

# Check multiple projects
.\complexity.ps1 .\project1 .\project2

# Different modes
.\complexity.ps1 .\my-project -Mode raw
.\complexity.ps1 .\my-project -Mode halstead

# Fix with Ollama and test verification
.\complexity.ps1 .\my-project -Fix -Test
.\complexity.ps1 .\my-project -FixAll -Test

# Scan project root directly (no src/ subdirectory)
.\complexity.ps1 .\my-project -NoSrc

# Stop on first project failure
.\complexity.ps1 .\project1 .\project2 -StopOnError
```

## Options

| Option | Description |
|---|---|
| `--project-root PATH` | Path to the project root (expects a `src/` subdirectory by default) |
| `--mode MODE` | `check`, `cc`, `raw`, `halstead`, or `mi` (default: `check`) |
| `--max N` | Max cyclomatic/cognitive complexity for `check` mode (default: `15`) |
| `--no-src` | Scan project root directly instead of `project_root/src/` |
| `--ignore DIRS` | Comma-separated directory names to skip (default: `tests,test,__pycache__,.git,node_modules,.venv,venv`) |
| `--ignore-path-contains SEG` | Skip files whose path contains all listed segments |
| `--exclusions FILE` | JSON file with blocks to exclude from checks |
| `--fix` | Fix the worst violation using Ollama (mode=check only) |
| `--fix-all` | Fix all violations (implies `--fix`) |
| `--test` | Run pytest after each fix; revert on failure |
| `--max-retries N` | Max Ollama attempts per violation (default: `3`) |
| `--ollama-url URL` | Ollama server URL (default: `http://localhost:11434`) |
| `--model NAME` | Ollama model (default: `qwen3-coder:30b`) |
| `--verbose` | Verbose output |

## Performance Note

Auto-fix performance depends heavily on the Ollama model and available GPU hardware. Larger models produce better refactoring results but require more VRAM and take longer per fix attempt. Running without GPU acceleration (CPU-only) will be significantly slower.

## Auto-Fix Workflow

When `--fix` or `--fix-all` is used, the tool:

1. Collects all complexity violations (cyclomatic and cognitive), sorted worst-first
2. Extracts the function source and surrounding file context (imports, class info, constants)
3. Sends the function to Ollama with a refactoring prompt and complexity reduction strategies
4. Validates the fix in-memory (syntax check + complexity re-measurement)
5. Writes the fix to disk and runs `ruff` (undefined name check) and optionally `pytest`
6. Reverts on any failure and retries with feedback up to `--max-retries` times

## Exclusions File Format

```json
{
    "cyclomatic": [["path/substring", "function_name"], ...],
    "cognitive": [["path/substring", "function_name"], ...]
}
```
