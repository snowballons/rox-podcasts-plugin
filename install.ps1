# Install the Rox podcast plugin (native Windows PowerShell).
#
# Downloads the 10 plugin files from the public repo and copies them into
# Rox's plugins folder: %APPDATA%\rox\plugins\podcast
#
# Git Bash / WSL users: use install.sh instead.
#
# Usage:
#   .\install.ps1 [-Target DIR] [-Branch main]
#
# Afterwards: Rox > Settings > Plugins > switch Podcasts on and approve
# the trust card. (Any new or changed files start switched off.)

param(
    [string]$Target = "",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

$Repo = "snowballons/rox-podcasts-plugin"

# The 10 plugin files. Everything else in the repo (installer scripts,
# LICENSE, git metadata) is deliberately left out of Rox's file hash.
$PluginFiles = @(
    "podcast.py", "protocol.py", "store.py", "directory.py", "feed.py",
    "stream.py", "handlers.py", "plugin.json", "icon.svg", "README.md"
)

if ([string]::IsNullOrWhiteSpace($Target)) {
    if ([string]::IsNullOrWhiteSpace($env:APPDATA)) {
        Write-Error "APPDATA is not set; pass -Target DIR explicitly."
        exit 1
    }
    $Target = Join-Path $env:APPDATA "rox\plugins\podcast"
}

if (Test-Path $Target) {
    Write-Error "Exists: $Target — remove it first to reinstall, then re-approve the plugin in Rox."
    exit 1
}

$havePython = (Get-Command py -ErrorAction SilentlyContinue) -or
              (Get-Command python3 -ErrorAction SilentlyContinue) -or
              (Get-Command python -ErrorAction SilentlyContinue)
if (-not $havePython) {
    Write-Error "No Python 3 found (tried py, python3, python). Install it from https://www.python.org/downloads/ and re-run."
    exit 1
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("rox-podcast-" + [System.Guid]::NewGuid().ToString("N"))
$zip = Join-Path $work "plugin.zip"
try {
    New-Item -ItemType Directory -Path $work | Out-Null
    Write-Host "Downloading $Repo ($Branch)..."
    Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $work "src")

    # GitHub zips extract to a single top-level dir (<repo>-<branch>).
    $src = Get-ChildItem (Join-Path $work "src") -Directory | Select-Object -First 1
    if ($null -eq $src) { Write-Error "Could not unpack the download."; exit 1 }

    New-Item -ItemType Directory -Path $Target | Out-Null
    foreach ($f in $PluginFiles) {
        $from = Join-Path $src.FullName $f
        if (-not (Test-Path $from)) {
            Write-Error "Expected file missing from download: $f"
            Remove-Item -Recurse -Force $Target
            exit 1
        }
        Copy-Item $from $Target
    }

    $count = (Get-ChildItem $Target -File).Count
    if ($count -ne 10) {
        Write-Error "Expected exactly 10 files in $Target (found $count); aborting."
        Remove-Item -Recurse -Force $Target
        exit 1
    }
}
finally {
    if (Test-Path $work) { Remove-Item -Recurse -Force $work }
}

Write-Host "Installed 10 files to: $Target"
Write-Host "Next steps:"
Write-Host "  1. Rox > Settings > Plugins > turn on Enable Plugins"
Write-Host "  2. Switch Podcasts on and approve the trust card"
