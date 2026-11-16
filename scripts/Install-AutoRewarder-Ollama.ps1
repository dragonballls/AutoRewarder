[CmdletBinding()]
param(
    [string]$ArchivePath,
    [string]$LegacyDataPath,
    [string]$InstallPath = (Join-Path $env:LOCALAPPDATA "Programs\AutoRewarder-Ollama"),
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Repo = "dragonballls/AutoRewarder"
$Branch = "feat/local-ollama-support-assistant"
$Workflow = "build-windows.yml"
$ArtifactName = "AutoRewarder-Ollama-Windows"
$CanonicalDataPath = Join-Path $env:LOCALAPPDATA "AutoRewarder"
$OllamaDataPath = Join-Path $env:LOCALAPPDATA "AutoRewarder-Ollama"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Get-DataSummary([string]$Path) {
    $files = @(Get-ChildItem -LiteralPath $Path -File -Recurse -Force -ErrorAction SilentlyContinue)
    $bytes = ($files | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $bytes) { $bytes = 0 }
    $accountCount = 0
    $indexPath = Join-Path $Path "accounts.json"
    if (Test-Path -LiteralPath $indexPath -PathType Leaf) {
        try {
            $index = Get-Content -LiteralPath $indexPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($index -is [System.Array]) {
                $accountCount = @($index | Where-Object { $_.id }).Count
            }
        } catch {
            throw "The accounts.json file at '$indexPath' is unreadable. Nothing has been changed."
        }
    }
    $profileCount = 0
    $accountsDir = Join-Path $Path "accounts"
    if (Test-Path -LiteralPath $accountsDir -PathType Container) {
        $profileCount = @(Get-ChildItem -LiteralPath $accountsDir -Directory -Force -ErrorAction SilentlyContinue).Count
    } elseif (Test-Path -LiteralPath (Join-Path $Path "EdgeProfile") -PathType Container) {
        # Older single-profile versions stored this at the data-root level.
        $profileCount = 1
    }
    return [pscustomobject]@{
        FileCount = $files.Count
        Bytes = [int64]$bytes
        AccountCount = $accountCount
        ProfileFolderCount = $profileCount
    }
}

function Backup-DataFolder([string]$Source, [string]$Destination) {
    if (!(Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Data folder not found: $Source"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $null = & robocopy.exe $Source $Destination /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /NFL /NDL /NP
    $code = $LASTEXITCODE
    if ($code -ge 8) {
        throw "Backup failed (Robocopy exit code $code). The old data folder was not changed."
    }
    $original = Get-DataSummary $Source
    $backup = Get-DataSummary $Destination
    if ($original.FileCount -ne $backup.FileCount -or $original.Bytes -ne $backup.Bytes) {
        throw "Backup verification failed. Source and backup file totals differ. Do not continue."
    }
    return $backup
}

function Merge-MissingData([string]$Source, [string]$Destination) {
    # Preserve the canonical data folder's settings and existing files.
    # Copy only files that do not already exist at the destination.
    $null = & robocopy.exe $Source $Destination /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /XC /XN /XO /NFL /NDL /NP
    if ($LASTEXITCODE -ge 8) {
        throw "Could not merge missing files from '$Source' into '$Destination'. Backups are retained."
    }

    $sourceIndexPath = Join-Path $Source "accounts.json"
    $targetIndexPath = Join-Path $Destination "accounts.json"
    if (Test-Path -LiteralPath $sourceIndexPath -PathType Leaf) {
        $sourceIndex = @(Get-Content -LiteralPath $sourceIndexPath -Raw -Encoding UTF8 | ConvertFrom-Json)
        $targetIndex = @()
        if (Test-Path -LiteralPath $targetIndexPath -PathType Leaf) {
            $targetIndex = @(Get-Content -LiteralPath $targetIndexPath -Raw -Encoding UTF8 | ConvertFrom-Json)
        }
        $known = @{}
        foreach ($entry in $targetIndex) {
            if ($entry.id) { $known[[string]$entry.id] = $true }
        }
        foreach ($entry in $sourceIndex) {
            if ($entry.id -and !$known.ContainsKey([string]$entry.id)) {
                $targetIndex += $entry
                $known[[string]$entry.id] = $true
            }
        }
        $tempIndex = "$targetIndexPath.migration.tmp"
        $targetIndex | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $tempIndex -Encoding UTF8
        Move-Item -LiteralPath $tempIndex -Destination $targetIndexPath -Force
    }
}

function Get-LatestBuildArchive([string]$Destination) {
    $releaseUrl = "https://api.github.com/repos/$Repo/releases/tags/autorewarder-ollama-preview"
    try {
        $release = Invoke-RestMethod -Uri $releaseUrl -Headers @{
            "Accept" = "application/vnd.github+json"
            "User-Agent" = "AutoRewarder-Ollama-Installer"
        }
    } catch {
        Start-Process "https://github.com/$Repo/actions/workflows/build-windows.yml"
        throw "The Windows preview release is not available yet. Check that the latest build workflow completed successfully, then rerun this script."
    }

    $branchInfo = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/branches/$Branch" -Headers @{
        "Accept" = "application/vnd.github+json"
        "User-Agent" = "AutoRewarder-Ollama-Installer"
    }
    $tipSha = [string]$branchInfo.commit.sha
    if ([string]$release.body -notmatch [regex]::Escape($tipSha)) {
        Start-Process "https://github.com/$Repo/actions/workflows/build-windows.yml"
        throw "The latest preview release does not match the current source commit yet. Wait for the current build to succeed, then rerun this script."
    }

    $asset = $release.assets |
        Where-Object { $_.name -eq "AutoRewarder-Ollama-Windows.zip" } |
        Select-Object -First 1
    if (!$asset -or !$asset.browser_download_url) {
        Start-Process "https://github.com/$Repo/actions/workflows/build-windows.yml"
        throw "The preview release does not contain the Windows ZIP yet. Check the build workflow and rerun after it succeeds."
    }

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $zipPath = Join-Path $Destination "AutoRewarder-Ollama-Windows.zip"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing
    if (!(Test-Path -LiteralPath $zipPath -PathType Leaf)) {
        throw "The Windows ZIP download did not create a file."
    }
    if ((Get-Item -LiteralPath $zipPath).Length -ne [int64]$asset.size) {
        throw "The downloaded ZIP size does not match GitHub's published asset size. Delete it and retry."
    }
    return $zipPath
}
function New-AppShortcut([string]$ExePath) {
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $shortcutPath = Join-Path $desktop "AutoRewarder Ollama.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $ExePath
        $shortcut.WorkingDirectory = Split-Path -Parent $ExePath
        $shortcut.Description = "AutoRewarder with local Ollama helper"
        $shortcut.Save()
        Write-Host "Created desktop shortcut: $shortcutPath"
    } catch {
        Write-Warning "The app installed, but a desktop shortcut could not be created: $($_.Exception.Message)"
    }
}

Write-Step "Pre-flight checks"
if ($env:OS -ne "Windows_NT") {
    throw "This installer must be run on Windows."
}
if ((Get-Process -Name "AutoRewarder" -ErrorAction SilentlyContinue) -or
    (Get-Process -Name "AutoRewarder-Ollama" -ErrorAction SilentlyContinue)) {
    throw "AutoRewarder is still running. Close the original and Ollama edition (including tray icons) and rerun this script. No files have been changed."
}
if (!(Test-Path -LiteralPath $env:LOCALAPPDATA -PathType Container)) {
    throw "The Windows Local AppData folder could not be found."
}

if ([string]::IsNullOrWhiteSpace($ArchivePath)) {
    Write-Step "Download the latest successful Windows build"
    $downloadDir = Join-Path $env:TEMP ("AutoRewarder-Ollama-Download-" + [guid]::NewGuid().ToString("N"))
    $ArchivePath = Get-LatestBuildArchive $downloadDir
}
if (!(Test-Path -LiteralPath $ArchivePath -PathType Leaf)) {
    throw "Build archive not found: $ArchivePath"
}
if ([IO.Path]::GetExtension($ArchivePath) -ne ".zip") {
    throw "The build artifact must be a ZIP file."
}

Write-Step "Identify the old account data"
if ([string]::IsNullOrWhiteSpace($LegacyDataPath)) {
    $LegacyDataPath = $CanonicalDataPath
}
$LegacyDataPath = [IO.Path]::GetFullPath($LegacyDataPath)
$CanonicalDataPath = [IO.Path]::GetFullPath($CanonicalDataPath)
$OllamaDataPath = [IO.Path]::GetFullPath($OllamaDataPath)

if (!(Test-Path -LiteralPath $LegacyDataPath -PathType Container)) {
    throw "The old AutoRewarder data folder was not found at '$LegacyDataPath'. If the old build was portable, rerun with -LegacyDataPath pointing to its config folder. Nothing has been changed."
}
$sourceSummary = Get-DataSummary $LegacyDataPath
if (!(Test-Path -LiteralPath (Join-Path $LegacyDataPath "settings.json")) -and
    !(Test-Path -LiteralPath (Join-Path $LegacyDataPath "accounts.json")) -and
    $sourceSummary.ProfileFolderCount -eq 0) {
    throw "The selected folder does not look like an AutoRewarder data folder. Nothing has been changed."
}

Write-Host ("Found {0} account entry/entries and {1} profile folder(s)." -f $sourceSummary.AccountCount, $sourceSummary.ProfileFolderCount)
Write-Host ("Data source: $LegacyDataPath")
Write-Host ("New app folder: $InstallPath")
Write-Host ("New app data folder: $OllamaDataPath")
Write-Host "The existing app installation and original data folder will not be removed."

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $env:LOCALAPPDATA "AutoRewarder-Backups"
$backupPath = Join-Path $backupRoot "AutoRewarder-$stamp"
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

Write-Step "Back up original data and copy it into the separate Ollama data folder"
if ($LegacyDataPath -eq $OllamaDataPath) {
    throw "The source path points to the new Ollama data folder. Select the original AutoRewarder data folder instead."
}

# If both a portable source and the normal installed data folder exist, merge
# both into the new copy. Existing destination files always take priority.
$sourcePaths = @()
if ($LegacyDataPath -ne $CanonicalDataPath -and
    (Test-Path -LiteralPath $CanonicalDataPath -PathType Container)) {
    $sourcePaths += $CanonicalDataPath
}
$sourcePaths += $LegacyDataPath

if (Test-Path -LiteralPath $OllamaDataPath -PathType Container) {
    $existingOllamaBackup = Join-Path $backupPath "existing-ollama-data"
    $null = Backup-DataFolder $OllamaDataPath $existingOllamaBackup
}
New-Item -ItemType Directory -Path $OllamaDataPath -Force | Out-Null

foreach ($sourcePath in $sourcePaths) {
    if ($sourcePath -eq $CanonicalDataPath) {
        $backupName = "original-installed-data"
    } else {
        $backupName = "original-portable-data"
    }
    $sourceBackup = Join-Path $backupPath $backupName
    $verified = Backup-DataFolder $sourcePath $sourceBackup
    Write-Host ("Verified backup of {0}: {1} files, {2:N1} MB." -f $sourcePath, $verified.FileCount, ($verified.Bytes / 1MB))
    Merge-MissingData $sourcePath $OllamaDataPath
}

Write-Step "Prepare the new app"
$tempExtract = Join-Path $env:TEMP ("AutoRewarder-Ollama-Extract-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempExtract -Force | Out-Null
Expand-Archive -LiteralPath $ArchivePath -DestinationPath $tempExtract -Force

# A manually downloaded GitHub Actions artifact is an outer ZIP containing
# the actual application ZIP. Accept either form.
$exe = Get-ChildItem -LiteralPath $tempExtract -Filter "AutoRewarder-Ollama.exe" -File -Recurse |
    Select-Object -First 1
if (!$exe) {
    $innerZip = Get-ChildItem -LiteralPath $tempExtract -Filter "*.zip" -File -Recurse |
        Select-Object -First 1
    if ($innerZip) {
        $nestedExtract = Join-Path $env:TEMP ("AutoRewarder-Ollama-Nested-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $nestedExtract -Force | Out-Null
        Expand-Archive -LiteralPath $innerZip.FullName -DestinationPath $nestedExtract -Force
        $exe = Get-ChildItem -LiteralPath $nestedExtract -Filter "AutoRewarder-Ollama.exe" -File -Recurse |
            Select-Object -First 1
    }
}
if (!$exe) {
    throw "The archive does not contain AutoRewarder-Ollama.exe. Your existing app data and backup are untouched."
}
$appSource = $exe.Directory.FullName
$windowCandidates = @(
    (Join-Path $appSource "gui\index.html"),
    (Join-Path $appSource "_internal\gui\index.html")
)
if (!($windowCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })) {
    throw "This build does not contain the application UI. Your existing app data and backup are untouched."
}

$InstallPath = [IO.Path]::GetFullPath($InstallPath)
if (Test-Path -LiteralPath $InstallPath -PathType Container) {
    $previousPath = "$InstallPath.previous-$stamp"
    Move-Item -LiteralPath $InstallPath -Destination $previousPath
    Write-Host "Preserved previous app folder at: $previousPath"
}
New-Item -ItemType Directory -Path $InstallPath -Force | Out-Null
Copy-Item -Path (Join-Path $appSource "*") -Destination $InstallPath -Recurse -Force

# The build is packaged without a config folder so the old standard data path is reused.
$unexpectedConfig = Join-Path $InstallPath "config"
if (Test-Path -LiteralPath $unexpectedConfig) {
    throw "The downloaded build unexpectedly contains a config folder. It was not removed; stop here and inspect the archive."
}

$installedExe = Join-Path $InstallPath "AutoRewarder-Ollama.exe"
if (!(Test-Path -LiteralPath $installedExe -PathType Leaf)) {
    throw "The app copy did not verify. The original data and backup remain intact."
}
if (!(Test-Path -LiteralPath $OllamaDataPath -PathType Container)) {
    throw "The separate Ollama data folder is missing. Do not launch yet; inspect the migration backup."
}

Write-Step "Verify preserved accounts and profiles"
$finalSummary = Get-DataSummary $OllamaDataPath
Write-Host ("Current data: {0} account entry/entries, {1} profile folder(s), {2} files." -f $finalSummary.AccountCount, $finalSummary.ProfileFolderCount, $finalSummary.FileCount)
Write-Host ("Backup: $backupPath")
Write-Host ("New app: $installedExe")

New-AppShortcut $installedExe

Write-Step "Complete"
Write-Host "The previous AutoRewarder installation was left in place."
Write-Host "The Ollama edition uses a separate Local AppData folder. Settings, account entries, browser profiles, history, task status, and statistics were copied into it."
Write-Host "Keep the backup until you have opened the new build and confirmed every account is present."

if ($Launch) {
    Start-Process -FilePath $installedExe -WorkingDirectory $InstallPath
}
