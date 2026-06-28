# llm-coding-tools

A collection of Python tools powered by LLMs to analyze, optimize, and transform code. Includes utilities for complexity analysis, automatic code simplification, and more — improving readability, maintainability, and performance.

## Tools

| Tool | Description | Docs |
|---|---|---|
| [Complexity Analyzer](metrics/complexity.py) | Analyze Python code complexity (cyclomatic, cognitive, Halstead, MI) and auto-refactor with Ollama | [Details](metrics/COMPLEXITY.md) |
| [Commit and Push](git/commit-and-push.py) | Generate a commit message for the current repo with a local Ollama model (or the Claude Code CLI), then commit and push | [Details](git/COMMIT-AND-PUSH.md) |

## Quick Start

```bash
# Check complexity of a project
python metrics/complexity.py --project-root ./my-project --mode check

# Auto-fix violations using a local Ollama LLM
python metrics/complexity.py --project-root ./my-project --fix

# Commit and push the current repo with an LLM-written message
python git/commit-and-push.py
```

## Requirements

- Python 3.10+
- Tool-specific dependencies listed in each tool's documentation

## License

[MIT](LICENSE)
