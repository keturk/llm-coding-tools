#!/usr/bin/env python
"""Generate a commit message for the current git repo and commit + push it.

Run this from anywhere inside a git working tree. The tool finds the repository
root, and if there are uncommitted changes it generates a commit message and
then stages, commits, and pushes the repo in one pass.

The commit message comes from one of two backends:

* **Ollama** (preferred) -- a local model reached over HTTP. Used when the Ollama
  endpoint is reachable.
* **Claude Code CLI** -- the ``claude`` command. Used as the fallback when Ollama
  is not reachable (or when ``--message-source claude`` is forced).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


TEXT_SNIPPET_EXTENSIONS = {
    ".c",
    ".cfg",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".md",
    ".php",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

PATH_DUMP_EXTENSIONS = (
    "py|ps1|md|ts|tsx|js|jsx|json|yml|yaml|toml|sql|sh|go|rs|java|rb|c|cpp|h|hpp|cs|kt|php|swift"
)

# A message-generating backend: (user_prompt, system_prompt) -> raw model text.
Generator = Callable[[str, str], str]


@dataclass(frozen=True)
class RepoChanges:
    name: str
    path: Path
    branch: str
    porcelain: str
    name_status: str
    diff_stat: str
    diff_sample: str
    untracked_note: str


class ScriptError(RuntimeError):
    """Fatal script error."""


def run_git(repo_path: Path, args: list[str], *, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo_path.as_posix()}", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = "\n".join(part for part in [result.stdout, result.stderr] if part)
    output = "\n".join(
        line for line in output.splitlines() if not line.lower().startswith("warning:")
    )
    if check and result.returncode != 0:
        raise ScriptError(f"git {' '.join(args)} failed in {repo_path.name}: {output}")
    return output.rstrip()


def git_toplevel(start: Path) -> Path:
    """Return the repository root that contains ``start``."""
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ScriptError(f"Not inside a git repository: {start} ({detail})")
    return Path(result.stdout.strip())


def truncate_text(text: str, max_chars: int) -> str:
    if not text or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[... truncated for prompt size ...]"


def parse_name_status_paths(name_status: str) -> list[str]:
    paths: list[str] = []
    for raw_line in name_status.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = re.split(r"\t+", line)
        if len(parts) < 2:
            continue
        status = parts[0]
        path = parts[-1] if status.startswith("R") or status.startswith("C") else parts[1]
        if path not in paths:
            paths.append(path)
    return paths


def build_diff_sample(repo_path: Path, name_status: str, max_chars: int) -> str:
    """Build a balanced diff payload so late files are not lost to prefix truncation."""
    paths = parse_name_status_paths(name_status)
    if not paths:
        return truncate_text(
            run_git(repo_path, ["-c", "core.autocrlf=false", "diff", "HEAD"]),
            max_chars,
        )

    per_file_limit = max(1800, min(9000, max_chars // max(1, len(paths))))
    sections: list[str] = []
    remaining = max_chars
    for path in paths:
        if remaining <= 0:
            sections.append("[... additional files omitted for prompt size ...]")
            break
        diff = run_git(
            repo_path,
            ["-c", "core.autocrlf=false", "diff", "HEAD", "--", path],
            check=False,
        )
        if not diff.strip():
            continue
        section_limit = min(per_file_limit, remaining)
        section = f"--- diff for {path} ---\n{truncate_text(diff, section_limit)}"
        sections.append(section)
        remaining -= len(section) + 2
    return "\n\n".join(sections)


def is_text_path(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SNIPPET_EXTENSIONS


def build_untracked_note(repo_path: Path, untracked_paths: list[str]) -> str:
    if not untracked_paths:
        return ""
    lines = ["", "-- untracked paths and snippets --"]
    for rel in untracked_paths[:30]:
        lines.append(rel)
        file_path = repo_path / rel
        if not is_text_path(file_path) or not file_path.is_file():
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippet = truncate_text(text.strip(), 2400)
        if snippet:
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
    if len(untracked_paths) > 30:
        lines.append(f"[... {len(untracked_paths) - 30} additional untracked paths omitted ...]")
    return "\n".join(lines)


def collect_repo(repo_path: Path, max_diff_chars: int) -> RepoChanges | None:
    """Collect change context for the repo, or None if the tree is clean."""
    porcelain = run_git(repo_path, ["status", "--porcelain"])
    if not porcelain.strip():
        return None
    branch = run_git(repo_path, ["branch", "--show-current"], check=False) or "(unknown branch)"
    diff_stat = run_git(repo_path, ["-c", "core.autocrlf=false", "diff", "HEAD", "--stat"])
    name_status = run_git(repo_path, ["-c", "core.autocrlf=false", "diff", "HEAD", "--name-status"])
    diff_sample = build_diff_sample(repo_path, name_status, max_diff_chars)
    untracked_raw = run_git(repo_path, ["ls-files", "--others", "--exclude-standard"])
    untracked_paths = [line.strip() for line in untracked_raw.splitlines() if line.strip()]
    return RepoChanges(
        name=repo_path.name,
        path=repo_path,
        branch=branch.strip(),
        porcelain=porcelain.rstrip(),
        name_status=name_status.rstrip(),
        diff_stat=diff_stat.rstrip(),
        diff_sample=diff_sample.rstrip(),
        untracked_note=build_untracked_note(repo_path, untracked_paths),
    )


def repo_bundle(repo: RepoChanges) -> str:
    parts = [
        f"=== REPO: {repo.name} (branch: {repo.branch}) ===",
        "-- git status --porcelain --",
        repo.porcelain,
        "-- git diff HEAD --name-status --",
        repo.name_status,
        "-- git diff HEAD --stat --",
        repo.diff_stat,
        "-- balanced git diff excerpts --",
        repo.diff_sample,
    ]
    if repo.untracked_note.strip():
        parts.append(repo.untracked_note.rstrip())
    return "\n".join(parts)


def repo_bundle_lite(repo: RepoChanges, excerpt_max_chars: int = 3600) -> str:
    parts = [
        f"=== REPO: {repo.name} (branch: {repo.branch}) ===",
        "-- git status --porcelain --",
        repo.porcelain,
        "-- git diff HEAD --name-status --",
        repo.name_status,
        "-- git diff HEAD --stat --",
        repo.diff_stat,
        "-- short diff excerpt --",
        truncate_text(repo.diff_sample, excerpt_max_chars),
    ]
    if repo.untracked_note.strip():
        parts.append(truncate_text(repo.untracked_note.rstrip(), 5000))
    return "\n".join(parts)


def repo_bundle_paths_only(repo: RepoChanges) -> str:
    parts = [
        f"=== REPO: {repo.name} (branch: {repo.branch}) ===",
        "-- git status --porcelain --",
        repo.porcelain,
        "-- git diff HEAD --name-status --",
        repo.name_status,
        "-- git diff HEAD --stat --",
        repo.diff_stat,
    ]
    if repo.untracked_note.strip():
        paths_only = [
            line
            for line in repo.untracked_note.splitlines()
            if line and line != "```" and not line.startswith("--")
        ]
        parts.append("-- untracked paths --")
        parts.extend(paths_only[:30])
    return "\n".join(parts)


def ollama_reachable(base_url: str, timeout_ms: int) -> bool:
    """Return True if the Ollama endpoint answers its tag-list query in time."""
    uri = f"{base_url.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(uri, timeout=max(1, timeout_ms / 1000.0)) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def invoke_ollama_generate(
    base_url: str,
    model: str,
    prompt: str,
    timeout_ms: int,
    num_predict: int,
    system: str,
) -> str:
    uri = f"{base_url.rstrip('/')}/api/generate"
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {
                "num_predict": num_predict,
                "temperature": 0.2,
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        uri,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=max(1, timeout_ms / 1000.0)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ScriptError(f"Ollama request failed: {exc}") from exc
    response = data.get("response")
    if not response:
        raise ScriptError("Ollama returned no .response field")
    return str(response)


def resolve_claude_exe() -> str | None:
    """Locate the Claude Code CLI, preferring the cmd/exe shims on Windows."""
    for name in ("claude.cmd", "claude.exe", "claude"):
        path = shutil.which(name)
        if path:
            return path
    return None


def invoke_claude_generate(
    repo_path: Path,
    model: str,
    prompt: str,
    timeout_ms: int,
    system: str,
) -> str:
    """Generate a commit message with the Claude Code CLI for the repo.

    Claude runs non-interactively with read-only investigation tools scoped to
    the repo, and returns JSON so the final assistant text is read from
    ``.result`` regardless of any intermediate tool use.

    The prompt is fed via stdin rather than a ``-p`` argument: a large diff
    bundle would otherwise overflow the OS command-line length limit (~32K
    characters on Windows).
    """
    exe = resolve_claude_exe()
    if not exe:
        raise ScriptError(
            "Claude Code CLI not found in PATH. Install with "
            "'npm install -g @anthropic-ai/claude-code' or add 'claude' to PATH."
        )
    args = [
        exe,
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read",
        "Glob",
        "Grep",
        "Bash(git:*)",
        "--add-dir",
        str(repo_path),
        "--append-system-prompt",
        system,
    ]
    env = {**os.environ, "NO_COLOR": "1"}
    try:
        result = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_path),
            env=env,
            timeout=max(1, timeout_ms / 1000.0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ScriptError(f"Claude CLI timed out for {repo_path.name}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ScriptError(f"Claude CLI failed ({result.returncode}) for {repo_path.name}: {detail}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ScriptError(f"Claude CLI returned non-JSON output for {repo_path.name}: {exc}") from exc
    response = data.get("result")
    if not response:
        raise ScriptError(f"Claude CLI returned no .result field for {repo_path.name}")
    return str(response)


def normalize_message(raw: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    fence = re.fullmatch(r"```(?:\w+)?\s*\n([\s\S]*?)\n?```", text)
    if fence:
        text = fence.group(1).strip()
    return text.replace("\r\n", "\n").strip()


def first_line(text: str) -> str:
    return text.split("\n", 1)[0].strip()


def is_generic_subject(subject: str, repo_name: str) -> bool:
    normalized = re.sub(r"\s+", " ", subject.strip().lower())
    generic_exact = {
        f"update {repo_name.lower()}",
        f"update {repo_name.lower()} files",
        "update files",
        "update code",
        "update source",
        "modify files",
        "modify code",
        "change files",
        "misc updates",
        "various updates",
    }
    if normalized in generic_exact:
        return True
    repoish = re.escape(repo_name.lower())
    return bool(
        re.fullmatch(
            rf"(update|modify|improve|adjust|refactor) ({repoish}|[\w.-]+-repo|repo|repository)",
            normalized,
        )
    )


def looks_like_path_dump_line(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("- "):
        stripped = stripped[2:].strip()
    if not stripped:
        return False
    if re.search(rf"(^|[/\\])[\w.-]+\.({PATH_DUMP_EXTENSIONS})\b", stripped):
        return True
    return bool(re.match(r"^[\w.-]+[/\\][\w./\\-]+(?:\s+\([A-Z?]+\))?$", stripped))


def message_quality_problem(text: str, repo_name: str) -> str | None:
    if not text.strip():
        return "empty output"
    if len(text) > 3200:
        return "message is too long"
    subject = first_line(text)
    if not subject:
        return "missing subject"
    if subject.startswith("- "):
        return "subject is a bullet path, not a summary"
    if len(subject) > 120:
        return "subject is too long"
    if is_generic_subject(subject, repo_name):
        return "subject is too generic"

    lowered = text.strip().lower()
    chatty_prefixes = (
        "you're ",
        "you are ",
        "it looks ",
        "here's ",
        "here is ",
        "below is ",
        "the following ",
        "this diff ",
        "this code ",
        "this script ",
        "based on ",
        "###",
        "## ",
        "let me know",
        "i'm happy to",
        "would you like",
        "please provide",
        "i can help",
        "need help with",
    )
    if lowered.startswith(chatty_prefixes):
        return "chat-style explanation"
    lines = [line.rstrip() for line in text.splitlines()]
    bullet_lines = [line for line in lines if line.lstrip().startswith("- ")]
    if len(bullet_lines) > 6:
        return "too many bullet lines"
    if bullet_lines and sum(1 for line in bullet_lines if looks_like_path_dump_line(line)) >= max(2, len(bullet_lines) // 2):
        return "file path dump instead of semantic description"
    non_empty_body_lines = [line for line in lines[1:] if line.strip()]
    if non_empty_body_lines and sum(1 for line in non_empty_body_lines if looks_like_path_dump_line(line)) >= max(2, len(non_empty_body_lines) // 2):
        return "file path dump instead of semantic description"
    if any(re.match(r"^\s*\d+\.\s+", line) for line in lines):
        return "numbered analysis list instead of commit message"
    if any(re.match(r"^\s*[*-]\s+\*\*", line) for line in lines):
        return "markdown analysis bullets instead of commit message"
    head = lowered[:1200]
    if re.search(
        r"(comparing two|version control system|pasted a large|your message|"
        r"provided git|git output|git diff output|summary of the changes|"
        r"summary of the test files|here's a breakdown|key features:|"
        r"prerequisites:|sample output|in essence|"
        r"\*\*usage:\*\*|\*\*dependencies:\*\*)",
        head,
    ):
        return "analysis prose instead of commit message"
    return None


def fallback_commit_message(repo: RepoChanges) -> str:
    """Last-resort message derived from file categories when the model fails."""
    text = "\n".join([repo.name_status, repo.diff_stat, repo.untracked_note]).lower()
    if "test" in text or "spec" in text:
        subject = "Update tests"
    elif "readme" in text or ".md" in text or "docs/" in text or "/doc/" in text:
        subject = "Update documentation"
    elif any(token in text for token in (".yml", ".yaml", ".toml", ".ini", ".cfg", ".json", "config")):
        subject = "Update configuration"
    else:
        subject = f"Update {repo.name}"
    body = (
        "Apply the changes captured in this commit. Generated automatically because a "
        "model-written summary was unavailable; review and amend if more detail is needed."
    )
    return subject + "\n\n" + body


def build_user_prompt(repo_name: str, bundle: str, repair_reason: str | None = None) -> str:
    retry = ""
    if repair_reason:
        retry = (
            f"\nPrevious output was rejected because: {repair_reason}.\n"
            "Write a concrete subject that names the feature, behavior, or contract changed.\n"
        )
    return (
        f"Repository folder name: {repo_name}\n"
        f"{retry}\n"
        "Machine-readable git output only. Write the commit message from this data; "
        "do not describe the format of the data.\n\n"
        "<<<GIT_OUTPUT>>>\n"
        f"{bundle}\n"
        "<<<END_GIT_OUTPUT>>>\n"
    )


SYSTEM_PROMPT = """You output ONLY the body of one git commit message. You are not a tutor or reviewer.

GIT_OUTPUT may contain source files and automation scripts. Never write a walkthrough, feature list, README, or "what this script does" article. Never say "the script you've provided" or similar. Write a git log entry: what changed, in imperative mood.

Forbidden in your output: addressing the reader; markdown headings; fenced code blocks; questions; suggestions; tables; explaining what git or a diff is.

Required shape:
Line 1: one short, concrete summary of the semantic change. Do not use a generic subject like "Update repo" or a path-only subject.
Line 2: completely empty.
Lines 3+: one short paragraph, 1 to 4 sentences, describing what behavior, contract, validation, generation, or workflow changed and why it matters. Prefer prose over bullets.

Do not list filenames or paths. Git already records changed files. Mention a module or area only when it explains the behavior, such as "input validation" or "configuration loading".

Bullets are allowed only when there are separate semantic areas to describe, and then max 4 bullets. Never use bullets that are just file paths. Whole message max 3200 characters.
"""


def request_commit_message(repo: RepoChanges, generate: Generator) -> str:
    """Drive the chosen backend through escalating bundles until output passes QA."""
    attempts = [
        ("full", repo_bundle(repo), SYSTEM_PROMPT),
        (
            "repair-full",
            repo_bundle(repo),
            SYSTEM_PROMPT
            + "\nIf the prior message was generic or path-only, replace it with a specific semantic summary and prose body. Do not list files.\n",
        ),
        (
            "lite",
            repo_bundle_lite(repo),
            SYSTEM_PROMPT + "\nGIT_OUTPUT is shortened. Infer intent from paths, stat, snippets, and excerpts, but do not repeat file paths in the output.\n",
        ),
        (
            "paths-only",
            repo_bundle_paths_only(repo),
            SYSTEM_PROMPT + "\nGIT_OUTPUT has path lists and stats only. Still write a concrete best-effort subject and prose body. Do not output the paths.\n",
        ),
    ]
    last_problem: str | None = None
    for label, bundle, system in attempts:
        if last_problem:
            print(f"Warning: retrying with {label}: {last_problem}", file=sys.stderr)
        raw = generate(build_user_prompt(repo.name, bundle, last_problem), system)
        message = normalize_message(raw)
        problem = message_quality_problem(message, repo.name)
        if problem is None:
            return message
        last_problem = problem

    print("Warning: model output still unusable; using deterministic fallback.", file=sys.stderr)
    return fallback_commit_message(repo)


def make_generator(source: str, args: argparse.Namespace, repo: RepoChanges) -> Generator:
    """Bind the selected backend to a (prompt, system) -> text callable."""
    if source == "ollama":
        return lambda prompt, system: invoke_ollama_generate(
            base_url=args.ollama_url,
            model=args.ollama_model,
            prompt=prompt,
            timeout_ms=args.ollama_timeout_ms,
            num_predict=args.ollama_num_predict,
            system=system,
        )
    return lambda prompt, system: invoke_claude_generate(
        repo_path=repo.path,
        model=args.claude_model,
        prompt=prompt,
        timeout_ms=args.claude_timeout_ms,
        system=system,
    )


def decide_source(args: argparse.Namespace) -> str:
    """Resolve which backend to use, honoring a forced choice or auto-detecting Ollama."""
    if args.message_source == "ollama":
        if not ollama_reachable(args.ollama_url, args.ollama_reachable_timeout_ms):
            raise ScriptError(
                f"Ollama not reachable at {args.ollama_url} but --message-source=ollama was forced."
            )
        print(f"Using local Ollama model '{args.ollama_model}' at {args.ollama_url}.")
        return "ollama"
    if args.message_source == "claude":
        print(f"Using Claude Code CLI model '{args.claude_model}'.")
        return "claude"

    # auto: prefer local Ollama when reachable, otherwise fall back to Claude.
    if ollama_reachable(args.ollama_url, args.ollama_reachable_timeout_ms):
        print(f"Ollama reachable at {args.ollama_url} -- using local model '{args.ollama_model}'.")
        return "ollama"
    print(
        f"Ollama not reachable at {args.ollama_url} -- "
        f"falling back to Claude Code CLI model '{args.claude_model}'."
    )
    return "claude"


def set_git_identity(repo_path: Path, name: str, email: str) -> None:
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", name], check=False)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", email], check=False)


def clean_git_locks(repo_path: Path) -> None:
    git_dir = repo_path / ".git"
    if not git_dir.is_dir():
        return
    for lock in git_dir.rglob("*.lock"):
        try:
            lock.unlink()
        except OSError:
            pass


def commit_and_push_repo(repo_path: Path, message: str, *, push: bool) -> str:
    """Stage, commit, and (optionally) push. Returns 'pushed', 'committed', or 'clean'."""
    name = repo_path.name
    add = subprocess.run(
        ["git", "-C", str(repo_path), "add", "-A"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if add.returncode != 0:
        raise ScriptError(f"{name}: git add failed ({add.returncode}): {add.stderr or add.stdout}")

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(message)
        temp_path = handle.name
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-F", temp_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    if commit.returncode == 1:
        return "clean"
    if commit.returncode != 0:
        raise ScriptError(f"{name}: git commit failed ({commit.returncode}): {commit.stderr or commit.stdout}")

    if not push:
        return "committed"

    pushed = subprocess.run(
        ["git", "-C", str(repo_path), "push"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if pushed.returncode != 0:
        raise ScriptError(f"{name}: git push failed ({pushed.returncode}): {pushed.stderr or pushed.stdout}")
    return "pushed"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=".",
        help="Path inside the target repo (default: current directory). The repo root is auto-detected.",
    )
    parser.add_argument(
        "--message-source",
        choices=["auto", "ollama", "claude"],
        default="auto",
        help="Backend for the commit message: auto (Ollama if reachable, else Claude), or force one.",
    )
    parser.add_argument("--ollama-url", default="http://10.94.0.100:11434")
    parser.add_argument("--ollama-model", default="qwen3-coder:30b-ctx32k")
    parser.add_argument("--ollama-timeout-ms", type=int, default=180000)
    parser.add_argument(
        "--ollama-reachable-timeout-ms",
        type=int,
        default=3000,
        help="Timeout for the quick Ollama reachability probe.",
    )
    parser.add_argument("--ollama-num-predict", type=int, default=896)
    parser.add_argument("--claude-model", default="sonnet")
    parser.add_argument("--claude-timeout-ms", type=int, default=300000)
    parser.add_argument("--max-diff-chars", type=int, default=45000)
    parser.add_argument(
        "--git-user-name",
        default="",
        help="If set with --git-user-email, configure the repo-local commit identity.",
    )
    parser.add_argument("--git-user-email", default="")
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Commit but do not push.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and print the commit message but do not commit or push.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    start = Path(args.repo).resolve()
    repo_root = git_toplevel(start)
    print(f"Repository: {repo_root}")

    repo = collect_repo(repo_root, args.max_diff_chars)
    if repo is None:
        print("Working tree is clean. Nothing to commit.")
        return 0

    source = decide_source(args)

    print(f"Generating commit message via {source}...")
    message = request_commit_message(repo, make_generator(source, args, repo))
    print("")
    print("========== Commit message ==========")
    print(message)
    print("====================================")
    print("")

    if args.dry_run:
        print("Dry run complete; no commit was made.")
        return 0

    if args.git_user_name and args.git_user_email:
        set_git_identity(repo_root, args.git_user_name, args.git_user_email)

    clean_git_locks(repo_root)
    outcome = commit_and_push_repo(repo_root, message, push=not args.no_push)
    if outcome == "clean":
        print("Nothing to commit (working tree clean).")
    elif outcome == "committed":
        print(f"{repo.name}: committed (push skipped).")
    else:
        print(f"{repo.name}: committed and pushed successfully.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ScriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
