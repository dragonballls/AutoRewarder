# Start AutoRewarder from an extracted ZIP, clearing Windows' download marker.
#
# A ZIP downloaded through a browser is marked as coming from the Internet, and
# Windows passes that mark to every file extracted from it. The .NET Framework
# then refuses to load the marked assemblies, and the app dies before it can
# open a window:
#
#   RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
#   ... or ...
#   Could not load file or assembly '.../Microsoft.Web.WebView2.Core.dll'
#   ... An attempt was made to load an assembly from a network location
#
# The mark has to be cleared before the exe starts, so the app cannot repair it
# itself. Run this script instead of double-clicking the exe the first time; it
# unblocks the files in place, then launches the app.

[CmdletBinding()]
param(
    [string]$AppPath = $PSScriptRoot,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$exe = Join-Path $AppPath "AutoRewarder.exe"
if (!(Test-Path -LiteralPath $exe -PathType Leaf)) {
    $found = Get-ChildItem -LiteralPath $AppPath -Filter "AutoRewarder.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (!$found) {
        throw "AutoRewarder.exe was not found in '$AppPath'. Put this script next to the app folder, or pass -AppPath."
    }
    $exe = $found.FullName
    $AppPath = $found.Directory.FullName
}

Write-Host "Checking '$AppPath' for files Windows has marked as downloaded..."

$blocked = @(Get-ChildItem -LiteralPath $AppPath -File -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { (Get-Item -LiteralPath $_.FullName -Stream * -ErrorAction SilentlyContinue).Stream -contains "Zone.Identifier" })

if ($blocked.Count -gt 0) {
    foreach ($file in $blocked) {
        Unblock-File -LiteralPath $file.FullName -ErrorAction SilentlyContinue
    }
    $remaining = @(Get-ChildItem -LiteralPath $AppPath -File -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { (Get-Item -LiteralPath $_.FullName -Stream * -ErrorAction SilentlyContinue).Stream -contains "Zone.Identifier" })
    if ($remaining.Count -gt 0) {
        throw "Windows still marks $($remaining.Count) file(s) as downloaded, which stops .NET loading them. Move the folder somewhere writable (for example out of OneDrive or Program Files) and run this script again."
    }
    Write-Host "Cleared the download marker from $($blocked.Count) file(s)."
} else {
    Write-Host "Nothing was marked; the app is ready to start."
}

if ($NoLaunch) {
    Write-Host "Skipping launch (-NoLaunch). Start '$exe' when you are ready."
    return
}

Write-Host "Starting AutoRewarder..."
Start-Process -FilePath $exe -WorkingDirectory $AppPath
