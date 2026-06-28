# Commit and Push

Generates a commit message for the git repository containing the current
directory and then stages, commits, and pushes that one repo — in a single pass.

The commit message is written by an LLM:

- **[Ollama](https://ollama.com/)** (preferred) — a local model, used when the
  Ollama endpoint is reachable.
- **[Claude Code CLI](https://www.npmjs.com/package/@anthropic-ai/claude-code)** —
  used as the fallback when Ollama is not reachable (or when forced).

## Features

- **One command** — generate the message, commit, and push without any
  intermediate file.
- **Automatic backend selection** — probes Ollama and falls back to Claude.
- **Quality gate** — generated messages are checked for path dumps, chat-style
  prose, generic subjects, and over-length; the backend retries with escalating
  context, and a deterministic fallback is used if it still cannot produce a
  usable message.
- **Operates on one repo** — the repository that owns the current directory,
  detected with `git rev-parse --show-toplevel`.

## Requirements

- Python 3.10+
- `git` on PATH
- For the local path: an [Ollama](https://ollama.com/) server (default
  `http://10.94.0.100:11434`)
- For the fallback: the Claude Code CLI on PATH
  (`npm install -g @anthropic-ai/claude-code`)

## Usage

### PowerShell

```powershell
# Auto: Ollama if reachable, else Claude; commit + push the current repo
.\commit-and-push.ps1

# Force the local model (errors if Ollama is unreachable)
.\commit-and-push.ps1 -MessageSource ollama

# Force the Claude Code CLI
.\commit-and-push.ps1 -MessageSource claude

# Commit but do not push
.\commit-and-push.ps1 -NoPush

# Preview the generated message without committing
.\commit-and-push.ps1 -DryRun
```

### Python

```bash
# Auto-detect backend and commit + push the repo owning the current directory
python commit-and-push.py

# Target a specific path inside a repo, force Claude, skip the push
python commit-and-push.py --repo ./some/dir --message-source claude --no-push

# Print the message only
python commit-and-push.py --dry-run
```

## Options

| PowerShell | Python | Default | Description |
|---|---|---|---|
| `-Repo` | `--repo` | `.` | Path inside the target repo; the root is auto-detected |
| `-MessageSource` | `--message-source` | `auto` | `auto` \| `ollama` \| `claude` |
| `-OllamaUrl` | `--ollama-url` | `http://10.94.0.100:11434` | Ollama server URL |
| `-OllamaModel` | `--ollama-model` | `qwen3-coder:30b-ctx32k` | Ollama model name |
| `-OllamaTimeoutMs` | `--ollama-timeout-ms` | `180000` | Generate-request timeout (ms) |
| `-OllamaNumPredict` | `--ollama-num-predict` | `896` | Ollama `num_predict` (max tokens) |
| `-ClaudeModel` | `--claude-model` | `sonnet` | Claude model for the fallback |
| `-ClaudeTimeoutMs` | `--claude-timeout-ms` | `300000` | Claude CLI timeout (ms) |
| `-MaxDiffChars` | `--max-diff-chars` | `45000` | Max diff characters fed to the model |
| `-GitUserName` | `--git-user-name` | _(unset)_ | With email, sets the repo-local commit identity |
| `-GitUserEmail` | `--git-user-email` | _(unset)_ | With name, sets the repo-local commit identity |
| `-NoPush` | `--no-push` | off | Commit but do not push |
| `-DryRun` | `--dry-run` | off | Print the message; do not commit |

The Python script also accepts `--ollama-reachable-timeout-ms` (default `3000`)
for the quick reachability probe.

## Notes

- Commit identity is left untouched unless both `--git-user-name` and
  `--git-user-email` are provided, in which case they are set on the repo only.
- Message style: no conventional-commit prefix; a concrete subject line, a blank
  line, then a short prose body. Bare file-path dumps are rejected.
