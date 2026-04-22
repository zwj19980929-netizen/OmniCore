[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [switch]$IncludeDebug,
    [switch]$IncludeLogs,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Get-CleanupTargets {
    param(
        [string]$DataDir,
        [switch]$IncludeDebug,
        [switch]$IncludeLogs
    )

    if (-not (Test-Path -LiteralPath $DataDir)) {
        return @()
    }

    $targets = New-Object System.Collections.Generic.List[string]
    foreach ($item in Get-ChildItem -LiteralPath $DataDir -Force) {
        $name = $item.Name
        if ($name -eq "browser_state_smoketest" -or $name.StartsWith("test_") -or $name.StartsWith("test-")) {
            $targets.Add($item.FullName)
        }
    }

    if ($IncludeDebug) {
        $debugDir = Join-Path $DataDir "debug"
        if (Test-Path -LiteralPath $debugDir) {
            $targets.Add($debugDir)
        }
    }

    if ($IncludeLogs) {
        $logsDir = Join-Path $DataDir "logs"
        if (Test-Path -LiteralPath $logsDir) {
            $targets.Add($logsDir)
        }
    }

    return $targets.ToArray() | Sort-Object -Unique
}

function Remove-DirectoryTree {
    param([string]$TargetPath)

    cmd.exe /d /c "rd /s /q `"$TargetPath`""
    if (Test-Path -LiteralPath $TargetPath) {
        throw "Failed to remove directory: $TargetPath"
    }
}

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$dataDir = Join-Path $resolvedRoot "data"
$targets = Get-CleanupTargets -DataDir $dataDir -IncludeDebug:$IncludeDebug -IncludeLogs:$IncludeLogs

if ($targets.Count -eq 0) {
    Write-Output "No transient test artifacts found."
    exit 0
}

foreach ($target in $targets) {
    $relative = Resolve-Path -LiteralPath $target -Relative
    if ($DryRun) {
        Write-Output ("dry-run  {0}" -f $relative)
        continue
    }

    if (Test-Path -LiteralPath $target -PathType Container) {
        Remove-DirectoryTree -TargetPath $target
    } else {
        Remove-Item -LiteralPath $target -Force
    }
    Write-Output ("removed  {0}" -f $relative)
}

if ($DryRun) {
    Write-Output ("Dry run complete. {0} target(s) matched." -f $targets.Count)
} else {
    Write-Output ("Removed {0} target(s)." -f $targets.Count)
}
