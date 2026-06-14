param(
    [string]$VaultPath = ".\memory",
    [string]$ProjectPath = ".",
    [switch]$SkipStats
)

$ErrorActionPreference = "Stop"

$InstallUrl = "https://raw.githubusercontent.com/MLTCorp/sincron-brain-model/main/install.ps1"
$LocalBin = Join-Path $env:USERPROFILE ".local\bin"

function Resolve-ExistingDirectory {
    param([string]$PathValue)

    $resolved = Resolve-Path -LiteralPath $PathValue -ErrorAction Stop
    $item = Get-Item -LiteralPath $resolved.Path -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw "Project path is not a directory: $PathValue"
    }

    return $item.FullName
}

function Test-UnsafeProjectDirectory {
    param([string]$Directory)

    $full = [System.IO.Path]::GetFullPath($Directory).TrimEnd("\")
    $root = [System.IO.Path]::GetPathRoot($full).TrimEnd("\")
    $userHome = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd("\")

    return ($full -eq $root) -or ($full -eq $userHome)
}

function Find-SincronBrainCommand {
    $command = Get-Command "sincron-brain" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $expected = Join-Path $LocalBin "sincron-brain.exe"
    if (Test-Path -LiteralPath $expected) {
        return $expected
    }

    return $null
}

function Resolve-VaultFullPath {
    param(
        [string]$ProjectDirectory,
        [string]$VaultPathValue
    )

    if ([System.IO.Path]::IsPathRooted($VaultPathValue)) {
        return [System.IO.Path]::GetFullPath($VaultPathValue)
    }

    return [System.IO.Path]::GetFullPath((Join-Path $ProjectDirectory $VaultPathValue))
}

function Install-SincronBrain {
    $scriptPath = $PSCommandPath
    if ($scriptPath) {
        $localInstall = Join-Path (Split-Path -Parent $scriptPath) "install.ps1"
        if (Test-Path -LiteralPath $localInstall) {
            & $localInstall
            return
        }
    }

    Invoke-RestMethod $InstallUrl | Invoke-Expression
}

$projectFullPath = Resolve-ExistingDirectory $ProjectPath
if (Test-UnsafeProjectDirectory $projectFullPath) {
    throw "Run this bootstrap inside a project folder, not in '$projectFullPath'. Example: cd C:\Temp\my-project"
}

Write-Host ""
Write-Host "Sincron Brain project bootstrap" -ForegroundColor Cyan
Write-Host "Project: $projectFullPath"
Write-Host "Vault:   $VaultPath"
Write-Host ""

Write-Host "Installing/updating sincron-brain..."
Install-SincronBrain

$sincronBrain = Find-SincronBrainCommand
if (-not $sincronBrain) {
    throw "sincron-brain was installed, but the command is not visible yet. Open a new PowerShell window and run this bootstrap again."
}

Write-Host ""
Write-Host "Connecting this project to Sincron Brain..."
Push-Location $projectFullPath
try {
    & $sincronBrain connect --path $VaultPath --project .
    if ($LASTEXITCODE -ne 0) {
        throw "sincron-brain connect failed."
    }

    $env:SINCRON_BRAIN_VAULT = Resolve-VaultFullPath $projectFullPath $VaultPath

    if (-not $SkipStats) {
        Write-Host ""
        Write-Host "Vault status:"
        & $sincronBrain stats
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Sincron Brain is installed and connected to this project." -ForegroundColor Green
Write-Host ""
Write-Host "Important next step:" -ForegroundColor Yellow
Write-Host "  Restart this conversation or reload your MCP client so it can detect the new sincron-brain server."
Write-Host ""
Write-Host "After restarting, ask the agent to run:"
Write-Host "  sincron-brain stats"
Write-Host ""
