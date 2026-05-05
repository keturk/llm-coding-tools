#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Run Radon metrics (complexity, raw, Halstead, MI) for one or more Python projects.

.DESCRIPTION
    Runs the complexity.py script for each specified project directory.
    Mode: check (enforce max complexity), cc, raw, halstead, mi.

    In mode=check, two metrics are enforced:
    - Cyclomatic complexity (Radon/McCabe): decision points and paths.
    - Cognitive complexity: understandability, including nesting.
    Both use the same threshold (default 15).

.PARAMETER Projects
    One or more project paths or directory names.

.PARAMETER Mode
    Metric to run: check (enforce max cyclomatic and cognitive), cc (cyclomatic), raw, halstead, mi.

.PARAMETER Max
    Maximum cyclomatic and cognitive complexity allowed per block (mode=check). Default: 15.

.PARAMETER NoSrc
    Scan project root directly instead of project_root/src.

.PARAMETER Exclusions
    Path to a JSON file with exclusion lists for cyclomatic and cognitive blocks to skip.

.PARAMETER Fix
    Fix the worst complexity violation using local Ollama (mode=check only).

.PARAMETER FixAll
    Fix ALL complexity violations (not just the first). Implies -Fix.

.PARAMETER Test
    Run pytest after fix to verify correctness; revert if tests fail. Use with -Fix/-FixAll.

.PARAMETER MaxRetries
    Maximum number of Ollama retry attempts per violation (default: 3). Use with -Fix.

.PARAMETER OllamaUrl
    Ollama server URL (default: http://localhost:11434).

.PARAMETER Model
    Ollama model name (default: qwen3-coder:30b).

.PARAMETER StopOnError
    Stop on first project failure instead of continuing.

.PARAMETER VerboseOutput
    Enable verbose output from the script.

.PARAMETER Dbg
    Enable debug logging.

.EXAMPLE
    .\complexity.ps1 .\my-project
    Check my-project with default max complexity 15 (mode=check).

.EXAMPLE
    .\complexity.ps1 .\my-project -Mode raw
    Report raw metrics (SLOC, comment/blank, LOC, LLOC) for my-project.

.EXAMPLE
    .\complexity.ps1 .\project1 .\project2 -Mode halstead
    Report Halstead metrics for multiple projects.

.EXAMPLE
    .\complexity.ps1 .\my-project -Fix
    Fix the worst complexity violation using Ollama.

.EXAMPLE
    .\complexity.ps1 .\my-project -Fix -Test
    Fix with auto-revert if tests fail.

.EXAMPLE
    .\complexity.ps1 .\my-project -FixAll -Test
    Fix all violations, testing after each fix.

.EXAMPLE
    .\complexity.ps1 .\my-project --no-src
    Scan project root directly instead of src/ subdirectory.
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Projects,

    [ValidateSet("check", "cc", "raw", "halstead", "mi")]
    [string]$Mode = "check",
    [Parameter()]
    [int]$Max = 15,
    [switch]$NoSrc,
    [string]$Exclusions = "",
    [switch]$Fix,
    [switch]$FixAll,
    [switch]$Test,
    [int]$MaxRetries = 3,
    [string]$OllamaUrl = "http://localhost:11434",
    [string]$Model = "qwen3-coder:30b",
    [switch]$StopOnError,
    [switch]$VerboseOutput,
    [switch]$Dbg
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$complexityScript = Join-Path $scriptDir "complexity.py"

if (-not (Test-Path $complexityScript)) {
    Write-Error "Error: complexity.py not found at: $complexityScript"
    exit 1
}

if ($FixAll) { $Fix = $true }
if ($Fix -and $Mode -ne "check") {
    Write-Host "ERROR: -Fix/-FixAll can only be used with -Mode check (the default)." -ForegroundColor Red
    exit 1
}
if ($Test -and -not $Fix) {
    Write-Host "ERROR: -Test can only be used with -Fix/-FixAll." -ForegroundColor Red
    exit 1
}

if ($Projects.Count -eq 0) {
    Write-Host ""
    Write-Host "ERROR: No projects specified." -ForegroundColor Red
    Write-Host ""
    Write-Host "Usage:" -ForegroundColor Yellow
    Write-Host "    .\complexity.ps1 <project-path> [options]" -ForegroundColor Cyan
    Write-Host "    .\complexity.ps1 <path1> <path2> ... [options]" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Examples:" -ForegroundColor Yellow
    Write-Host "    .\complexity.ps1 .\my-project" -ForegroundColor Cyan
    Write-Host "    .\complexity.ps1 .\my-project -Mode raw" -ForegroundColor Cyan
    Write-Host "    .\complexity.ps1 .\project1 .\project2 -Mode halstead" -ForegroundColor Cyan
    Write-Host "    .\complexity.ps1 .\my-project -Mode check -Max 10" -ForegroundColor Cyan
    Write-Host "    .\complexity.ps1 .\my-project -NoSrc" -ForegroundColor Cyan
    Write-Host "    .\complexity.ps1 .\my-project -Fix" -ForegroundColor Cyan
    Write-Host "    .\complexity.ps1 .\my-project -FixAll -Test" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Parameters:" -ForegroundColor Yellow
    Write-Host "    -Mode           check | cc | raw | halstead | mi (default: check)" -ForegroundColor Gray
    Write-Host "    -Max            Max complexity for mode=check (default: 15)" -ForegroundColor Gray
    Write-Host "    -NoSrc          Scan project root directly instead of src/" -ForegroundColor Gray
    Write-Host "    -Exclusions     Path to JSON exclusions file" -ForegroundColor Gray
    Write-Host "    -Fix            Fix worst violation via Ollama (mode=check only)" -ForegroundColor Gray
    Write-Host "    -FixAll         Fix ALL violations (implies -Fix)" -ForegroundColor Gray
    Write-Host "    -Test           Run pytest after fix; revert if tests fail" -ForegroundColor Gray
    Write-Host "    -MaxRetries     Max Ollama retry attempts per violation (default: 3)" -ForegroundColor Gray
    Write-Host "    -OllamaUrl      Ollama server URL (default: http://localhost:11434)" -ForegroundColor Gray
    Write-Host "    -Model          Ollama model name (default: qwen3-coder:30b)" -ForegroundColor Gray
    Write-Host "    -StopOnError    Stop on first project failure" -ForegroundColor Gray
    Write-Host "    -VerboseOutput  Verbose output" -ForegroundColor Gray
    Write-Host "    -Dbg            Debug output" -ForegroundColor Gray
    Write-Host ""
    exit 1
}

# Ensure radon is installed
$oldErr = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
$null = & pip show radon 2>$null
$radonInstalled = $LASTEXITCODE -eq 0
$ErrorActionPreference = $oldErr
if (-not $radonInstalled) {
    Write-Host "Installing radon..." -ForegroundColor Cyan
    & pip install "radon>=6.0" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install radon. Run: pip install radon>=6.0"
        exit 1
    }
}

# For mode=check, ensure cognitive_complexity is installed
if ($Mode -eq "check") {
    $oldErr2 = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $null = & pip show cognitive_complexity 2>$null
    $cogInstalled = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $oldErr2
    if (-not $cogInstalled) {
        Write-Host "Installing cognitive_complexity..." -ForegroundColor Cyan
        & pip install cognitive_complexity 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to install cognitive_complexity. Run: pip install cognitive_complexity"
            exit 1
        }
    }
}

$results = @{}
$totalProjects = $Projects.Count
$currentProject = 0

foreach ($project in $Projects) {
    $currentProject++

    # Resolve to absolute path
    $projectRoot = Resolve-Path -Path $project -ErrorAction SilentlyContinue
    if (-not $projectRoot) {
        Write-Host "[FAIL] Project path not found: $project" -ForegroundColor Red
        $results[$project] = $false
        if ($StopOnError) { break }
        continue
    }
    $projectRoot = $projectRoot.Path
    $projectName = Split-Path -Leaf $projectRoot

    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "[$currentProject/$totalProjects] $projectName (mode=$Mode)" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""

    $projectArgs = @(
        $complexityScript,
        "--project-root", $projectRoot,
        "--mode", $Mode,
        "--max", $Max
    )
    if ($NoSrc) { $projectArgs += "--no-src" }
    if ($Exclusions) { $projectArgs += "--exclusions", $Exclusions }
    if ($VerboseOutput) { $projectArgs += "--verbose" }
    if ($Dbg) { $projectArgs += "--debug" }
    if ($Fix) {
        $projectArgs += "--fix"
        if ($FixAll) { $projectArgs += "--fix-all" }
        if ($MaxRetries -ne 3) { $projectArgs += "--max-retries", $MaxRetries }
        $projectArgs += "--ollama-url", $OllamaUrl
        $projectArgs += "--model", $Model
    }
    if ($Test) { $projectArgs += "--test" }

    & python @projectArgs
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        $results[$projectName] = $true
        Write-Host ""
        Write-Host "[PASS] Complexity ($Mode) passed for $projectName" -ForegroundColor Green
    } else {
        $results[$projectName] = $false
        Write-Host ""
        Write-Host "[FAIL] Complexity ($Mode) failed for $projectName (exit code: $exitCode)" -ForegroundColor Red
        if ($StopOnError) {
            Write-Host "Stopping due to -StopOnError flag" -ForegroundColor Red
            break
        }
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Complexity Summary (mode=$Mode)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$passed = ($results.Values | Where-Object { $_ -eq $true }).Count
$failed = ($results.Values | Where-Object { $_ -eq $false }).Count

foreach ($p in ($results.Keys | Sort-Object)) {
    $status = if ($results[$p]) { "[PASS]" } else { "[FAIL]" }
    $color = if ($results[$p]) { "Green" } else { "Red" }
    Write-Host "    $status : $p" -ForegroundColor $color
}

Write-Host ""
Write-Host "Total: $totalProjects | Passed: $passed | Failed: $failed" -ForegroundColor Cyan

if ($failed -gt 0) { exit 1 }
exit 0
