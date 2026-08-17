[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Message,

    [string]$Remote = "origin",

    [switch]$DryRun,

    [switch]$Yes
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Test-GeneratedPath {
    param([string]$Path)
    $normalized = $Path.Replace("\", "/")
    return (
        $normalized -match "(^|/)(__pycache__|\.pytest_cache)(/|$)" -or
        $normalized -match "\.py[co]$" -or
        $normalized -match "(^|/)(analysis_results|analysis_tasks|test_output)(/|$)"
    )
}

try {
    $scriptRoot = Split-Path -Parent $PSScriptRoot
    Set-Location -LiteralPath $scriptRoot

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git was not found. Install Git for Windows and add it to PATH."
    }

    $repositoryRoot = (& git rev-parse --show-toplevel 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $repositoryRoot) {
        throw "The script directory is not inside a Git repository."
    }
    Set-Location -LiteralPath $repositoryRoot

    & git remote get-url $Remote *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Remote '$Remote' is not configured. Run: git remote add $Remote <GitHub URL>"
    }

    & git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "The staging area is not empty. Commit or unstage it before running this script."
    }

    $tracked = @(
        & git -c core.quotepath=false diff --name-only --diff-filter=ACMRD
    ) | Where-Object { $_ -and -not (Test-GeneratedPath $_) }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read tracked file changes."
    }

    $untracked = @(
        & git -c core.quotepath=false ls-files --others --exclude-standard
    ) | Where-Object { $_ -and -not (Test-GeneratedPath $_) }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read untracked files."
    }

    $files = @($tracked + $untracked | Sort-Object -Unique)
    if ($files.Count -eq 0) {
        Write-Host "No source changes are available to commit." -ForegroundColor Yellow
        exit 0
    }

    if ([string]::IsNullOrWhiteSpace($Message)) {
        $Message = Read-Host "Enter the commit message"
    }
    if ([string]::IsNullOrWhiteSpace($Message)) {
        throw "The commit message cannot be empty."
    }

    $currentBranch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $currentBranch) {
        throw "Could not determine the current Git branch."
    }
    $defaultBranchRef = (& git symbolic-ref --quiet --short "refs/remotes/$Remote/HEAD" 2>$null)
    $defaultBranch = if ($LASTEXITCODE -eq 0 -and $defaultBranchRef) {
        ($defaultBranchRef.Trim() -split "/")[-1]
    } else {
        "main"
    }
    $targetBranch = $defaultBranch

    Write-Host ""
    Write-Host "Files selected for commit:" -ForegroundColor Cyan
    foreach ($file in $files) {
        $kind = if ($untracked -contains $file) { "new" } else { "changed" }
        Write-Host "  [$kind] $file"
    }
    Write-Host ""
    Write-Host "Commit: $Message"
    Write-Host "Remote: $Remote"
    Write-Host "Push:   $currentBranch -> $Remote/$targetBranch"

    if ($DryRun) {
        Write-Host "Dry run complete. No branch, commit, or remote was changed." -ForegroundColor Yellow
        exit 0
    }

    if (-not $Yes) {
        $answer = Read-Host "Commit and push these files? Enter y to continue"
        if ($answer -notin @("y", "Y", "yes", "YES")) {
            Write-Host "Cancelled. No commit was created." -ForegroundColor Yellow
            exit 0
        }
    }

    Invoke-Git fetch $Remote $targetBranch
    & git merge-base --is-ancestor "$Remote/$targetBranch" HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "Remote $targetBranch has commits missing locally. Sync or rebase before retrying."
    }

    foreach ($file in $files) {
        Invoke-Git add -- $file
    }

    Invoke-Git commit -m $Message
    Invoke-Git push $Remote "HEAD:refs/heads/$targetBranch"

    if ($currentBranch -ne $targetBranch) {
        Invoke-Git branch -f $targetBranch HEAD
        Invoke-Git switch $targetBranch
    }

    Write-Host ""
    Write-Host "Done: committed and pushed directly to $Remote/$targetBranch" -ForegroundColor Green
    Write-Host "No feature branch or Pull Request was created."
}
catch {
    Write-Host ""
    Write-Host "Failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
