param(
    [string]$Version = "",
    [switch]$SkipConnect
)

$ErrorActionPreference = "Stop"

$Repository = "https://github.com/MLTCorp/sincron-brain-model.git"
$Source = if ([string]::IsNullOrWhiteSpace($Version)) {
    "git+$Repository"
} else {
    "git+$Repository@$Version"
}
$UvInstallUrl = "https://astral.sh/uv/install.ps1"
$LocalBin = Join-Path $env:USERPROFILE ".local\bin"

function Get-PathItems {
    param([string]$PathValue)

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return @()
    }

    return @($PathValue -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Add-PathForSessionAndUser {
    param([string]$PathToAdd)

    if (-not (Test-Path -LiteralPath $PathToAdd)) {
        New-Item -ItemType Directory -Path $PathToAdd -Force | Out-Null
    }

    $sessionPathItems = Get-PathItems $env:Path
    if ($sessionPathItems -notcontains $PathToAdd) {
        $env:Path = "$PathToAdd;$env:Path"
    }

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $userPathItems = Get-PathItems $userPath
    if ($userPathItems -notcontains $PathToAdd) {
        $newUserPath = if ([string]::IsNullOrWhiteSpace($userPath)) {
            $PathToAdd
        } else {
            "$PathToAdd;$userPath"
        }
        [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
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

function Test-WritableDirectory {
    param([string]$Directory)

    if (-not (Test-Path -LiteralPath $Directory)) {
        return $false
    }

    $probe = Join-Path $Directory ".sincron-brain-write-test"
    try {
        Set-Content -LiteralPath $probe -Value "ok" -Encoding ASCII -Force
        Remove-Item -LiteralPath $probe -Force
        return $true
    } catch {
        return $false
    }
}

function Find-ExistingUserPathDirectory {
    $userRoot = [System.IO.Path]::GetFullPath($env:USERPROFILE)
    $candidates = @()

    foreach ($item in (Get-PathItems $env:Path)) {
        $expanded = [Environment]::ExpandEnvironmentVariables($item).Trim()
        if ([string]::IsNullOrWhiteSpace($expanded)) {
            continue
        }

        try {
            $full = [System.IO.Path]::GetFullPath($expanded)
        } catch {
            continue
        }

        if (-not $full.StartsWith($userRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            continue
        }

        if ($full -like "*\Microsoft\WindowsApps*") {
            continue
        }

        if ($full -like "*\.codex\tmp*") {
            continue
        }

        if ($full -like "*\AppData\Local\Temp*") {
            continue
        }

        if ($full -like "*\Temp*") {
            continue
        }

        if (-not (Test-WritableDirectory $full) -or ($full -eq $LocalBin)) {
            continue
        }

        $priority = 100
        if ($full -like "*\Python*\Scripts") {
            $priority = 10
        } elseif ($full -like "*\AppData\Roaming\npm") {
            $priority = 20
        }

        $candidates += [pscustomobject]@{
            Directory = $full
            Priority = $priority
        }
    }

    $selected = $candidates | Sort-Object Priority, Directory | Select-Object -First 1
    if ($selected) {
        return $selected.Directory
    }

    return $null
}

function Install-CommandShim {
    param(
        [string]$CommandName,
        [string]$TargetExe
    )

    $shimDir = Find-ExistingUserPathDirectory
    if (-not $shimDir) {
        return $null
    }

    $shimPath = Join-Path $shimDir "$CommandName.cmd"
    if (
        [System.IO.Path]::GetFullPath($shimPath).Equals(
            [System.IO.Path]::GetFullPath($TargetExe),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $null
    }

    $shimBody = "@echo off`r`n`"$TargetExe`" %*`r`n"
    Set-Content -LiteralPath $shimPath -Value $shimBody -Encoding ASCII -Force
    return $shimPath
}

function Stop-RunningSincronBrain {
    $currentPid = $PID
    $patterns = @(
        "\\.local\\bin\\sincron-brain\\.exe",
        "\\uv\\tools\\sincron-brain-model\\",
        "sincron-brain serve",
        "sincron_brain"
    )

    $processes = Get-CimInstance Win32_Process | Where-Object {
        $commandLine = $_.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) {
            return $false
        }
        foreach ($pattern in $patterns) {
            if ($commandLine -match $pattern) {
                return $_.ProcessId -ne $currentPid
            }
        }
        return $false
    }

    foreach ($process in $processes) {
        Write-Host "Stopping running sincron-brain process: $($process.ProcessId)"
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Sincron Brain installer" -ForegroundColor Cyan
Write-Host "Installing from: $Source"
Write-Host ""

Add-PathForSessionAndUser $LocalBin

$uv = Find-CommandPath "uv"
if (-not $uv) {
    Write-Host "uv was not found. Installing uv for the current user..."
    $uvInstaller = Join-Path $env:TEMP ("uv-install-" + [guid]::NewGuid() + ".ps1")
    try {
        Invoke-WebRequest -Uri $UvInstallUrl -OutFile $uvInstaller -UseBasicParsing
        & $uvInstaller
    } finally {
        Remove-Item -LiteralPath $uvInstaller -Force -ErrorAction SilentlyContinue
    }
    Add-PathForSessionAndUser $LocalBin
    $uv = Find-CommandPath "uv"
}

if (-not $uv) {
    throw "uv was installed, but the uv command is still not available. Open a new PowerShell window and run this installer again."
}

Write-Host "Using uv: $uv"
Write-Host "Installing sincron-brain..."
Stop-RunningSincronBrain
& $uv tool install --force $Source
if ($LASTEXITCODE -ne 0) {
    throw "uv failed to install sincron-brain. Close MCP clients/agents that may still be using it, then run this installer again."
}

Add-PathForSessionAndUser $LocalBin
$expectedSincronBrain = Join-Path $LocalBin "sincron-brain.exe"
$sincronBrain = $null
if (Test-Path -LiteralPath $expectedSincronBrain) {
    $sincronBrain = $expectedSincronBrain
}
if (-not $sincronBrain) {
    $sincronBrain = Find-CommandPath "sincron-brain"
}

$shim = $null
if ($sincronBrain) {
    $shim = Install-CommandShim "sincron-brain" $sincronBrain
}

Write-Host ""
if ($sincronBrain) {
    Write-Host "sincron-brain installed successfully." -ForegroundColor Green
    Write-Host "Command: $sincronBrain"
    if ($shim) {
        Write-Host "Compatibility shim: $shim"
    }
} else {
    Write-Host "sincron-brain was installed, but it is not visible in this PowerShell session yet." -ForegroundColor Yellow
    Write-Host "Try: $expectedSincronBrain --help"
}

Write-Host ""

if ($SkipConnect) {
    Write-Host "Skipping connect (per -SkipConnect). To wire up a project later, cd into it and run:" -ForegroundColor Yellow
    Write-Host "  sincron-brain connect"
} elseif ($sincronBrain) {
    $cwd = [System.IO.Path]::GetFullPath((Get-Location).Path).TrimEnd("\")
    $cwdRoot = [System.IO.Path]::GetPathRoot($cwd).TrimEnd("\")
    $userHome = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd("\")
    $isUnsafeDir = ($cwd -eq $cwdRoot) -or ($cwd -eq $userHome)

    if ($isUnsafeDir) {
        Write-Host "Skipping auto-connect: '$cwd' is a drive root or home directory." -ForegroundColor Yellow
        Write-Host "cd into a project folder and run: sincron-brain connect"
    } else {
        Write-Host "Connecting this project to a memory vault at .\memory..."
        & $sincronBrain connect
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Connect failed. You can re-run: sincron-brain connect" -ForegroundColor Yellow
        } else {
            Write-Host ""
            Write-Host "Restart your MCP client/agent so it detects the new .mcp.json." -ForegroundColor Cyan
        }
    }
} else {
    Write-Host "Open a new PowerShell window, cd into your project, and run:" -ForegroundColor Yellow
    Write-Host "  sincron-brain connect"
}

Write-Host ""
