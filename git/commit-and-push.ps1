#!/usr/bin/env pwsh
<#
.SYNOPSIS
Generate a commit message for the current git repo and commit + push it.

.DESCRIPTION
Thin wrapper around commit-and-push.py (same folder). Run it from anywhere inside
a git working tree. The Python implementation finds the repository root, and if
there are uncommitted changes it generates a commit message and then stages,
commits, and pushes that one repo.

Message source is chosen automatically:
 * If a local Ollama endpoint is reachable, the message comes from the local model.
 * Otherwise the script falls back to the Claude Code CLI.

Force a backend with -MessageSource ollama|claude.

.PARAMETER Repo
Path inside the target repo. Default: current directory. The repo root is auto-detected.

.PARAMETER MessageSource
auto (default), ollama, or claude. auto probes Ollama and falls back to Claude.

.PARAMETER OllamaUrl
Ollama server URL. Default: http://10.94.0.100:11434

.PARAMETER OllamaModel
Ollama model name. Default: qwen3-coder:30b-ctx32k

.PARAMETER OllamaTimeoutMs
HTTP timeout (ms) for the Ollama generate request.

.PARAMETER OllamaNumPredict
Ollama option num_predict (max tokens). Default 896.

.PARAMETER ClaudeModel
Claude model used by the Claude Code CLI fallback. Default: sonnet

.PARAMETER ClaudeTimeoutMs
Timeout (ms) for the Claude CLI invocation. Default 300000.

.PARAMETER MaxDiffChars
Maximum prompt characters of tracked diff context to include.

.PARAMETER MaxBatchFiles
Maximum changed files per commit before the change set is split into batches.
Default 15. Set to 0 (or use -NoBatch) to always commit everything at once.

.PARAMETER MaxBatchDiffChars
Approximate changed-diff size per commit before splitting into batches. Default 30000.

.PARAMETER NoBatch
Never split; commit the whole working tree in a single commit (legacy behavior).

.PARAMETER GitUserName
If set together with -GitUserEmail, configures the repo-local commit identity.

.PARAMETER GitUserEmail
If set together with -GitUserName, configures the repo-local commit identity.

.PARAMETER NoPush
Commit but do not push.

.PARAMETER DryRun
Generate and print the commit message but do not commit or push.

.EXAMPLE
.\commit-and-push.ps1
Auto-detect backend, generate a message, commit and push the current repo.

.EXAMPLE
.\commit-and-push.ps1 -MessageSource claude
Force the Claude Code CLI as the message source.

.EXAMPLE
.\commit-and-push.ps1 -DryRun
Print the generated message without committing.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Repo = '.',

    [ValidateSet('auto', 'ollama', 'claude')]
    [string]$MessageSource = 'auto',

    [string]$OllamaUrl = 'http://10.94.0.100:11434',
    [string]$OllamaModel = 'qwen3-coder:30b-ctx32k',
    [int]$OllamaTimeoutMs = 180000,
    [int]$OllamaNumPredict = 896,
    [string]$ClaudeModel = 'sonnet',
    [int]$ClaudeTimeoutMs = 300000,
    [int]$MaxDiffChars = 45000,
    [int]$MaxBatchFiles = 15,
    [int]$MaxBatchDiffChars = 30000,
    [switch]$NoBatch,
    [string]$GitUserName = '',
    [string]$GitUserEmail = '',
    [switch]$NoPush,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonScript = Join-Path $scriptDir 'commit-and-push.py'

if (-not (Test-Path -LiteralPath $pythonScript)) {
    Write-Error "commit-and-push.py not found at: $pythonScript"
    exit 1
}

$pyArgs = @(
    $pythonScript,
    '--repo', $Repo,
    '--message-source', $MessageSource,
    '--ollama-url', $OllamaUrl,
    '--ollama-model', $OllamaModel,
    '--ollama-timeout-ms', $OllamaTimeoutMs,
    '--ollama-num-predict', $OllamaNumPredict,
    '--claude-model', $ClaudeModel,
    '--claude-timeout-ms', $ClaudeTimeoutMs,
    '--max-diff-chars', $MaxDiffChars,
    '--max-batch-files', $MaxBatchFiles,
    '--max-batch-diff-chars', $MaxBatchDiffChars
)

if ($NoBatch) { $pyArgs += '--no-batch' }
if ($GitUserName) { $pyArgs += @('--git-user-name', $GitUserName) }
if ($GitUserEmail) { $pyArgs += @('--git-user-email', $GitUserEmail) }
if ($NoPush) { $pyArgs += '--no-push' }
if ($DryRun) { $pyArgs += '--dry-run' }

& python @pyArgs
exit $LASTEXITCODE
