$ErrorActionPreference = "Stop"

$Source = "git+https://github.com/MLTCorp/sincron-brain-model.git"
$UvInstallUrl = "https://astral.sh/uv/install.ps1"
$LocalBin = Join-Path $env:USERPROFILE ".local\bin"

function Add-PathForCurrentSession {
    param([string]$PathToAdd)

    if ((Test-Path -LiteralPath $PathToAdd) -and (($env:Path -split ";") -notcontains $PathToAdd)) {
        $env:Path = "$PathToAdd;$env:Path"
    }
}

function Find-CommandPath {
    param([string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    return $null
}

Write-Host ""
Write-Host "Sincron Brain installer" -ForegroundColor Cyan
Write-Host "Installing from: $Source"
Write-Host ""

Add-PathForCurrentSession $LocalBin

$uv = Find-CommandPath "uv"
if (-not $uv) {
    Write-Host "uv was not found. Installing uv for the current user..."
    Invoke-RestMethod $UvInstallUrl | Invoke-Expression
    Add-PathForCurrentSession $LocalBin
    $uv = Find-CommandPath "uv"
}

if (-not $uv) {
    throw "uv was installed, but the uv command is still not available. Open a new PowerShell window and run this installer again."
}

Write-Host "Using uv: $uv"
Write-Host "Installing sincron-brain..."
& $uv tool install --force $Source

Add-PathForCurrentSession $LocalBin
$sincronBrain = Find-CommandPath "sincron-brain"

Write-Host ""
if ($sincronBrain) {
    Write-Host "sincron-brain installed successfully." -ForegroundColor Green
    Write-Host "Command: $sincronBrain"
} else {
    Write-Host "sincron-brain was installed, but it is not visible in this PowerShell session yet." -ForegroundColor Yellow
    Write-Host "Open a new PowerShell window and run: sincron-brain --help"
}

Write-Host ""
Write-Host "Next steps:"
Write-Host "  sincron-brain init"
Write-Host "  sincron-brain stats"
Write-Host ""
Write-Host "MCP command:"
Write-Host '  "command": "sincron-brain",'
Write-Host '  "args": ["serve"]'
Write-Host ""
