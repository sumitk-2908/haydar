[CmdletBinding()]
param(
    [switch]$KeepData,
    [switch]$RemoveData,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$installDir = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Haydar" } else { $null }
$dataDir = if ($env:USERPROFILE) { Join-Path $env:USERPROFILE ".haydar" } else { $null }
$startupDir = if ($env:APPDATA) { Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup" } else { $null }
$autostartFile = if ($startupDir) { Join-Path $startupDir "haydar_watcher.bat" } else { $null }
$willRemoveData = $RemoveData -and -not $KeepData

function Resolve-SafePath([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return $null }
    try { return [IO.Path]::GetFullPath($path) } catch { return $null }
}
function Same-Path([string]$left, [string]$right) {
    $a = Resolve-SafePath $left; $b = Resolve-SafePath $right
    return $null -ne $a -and $null -ne $b -and $a.TrimEnd('\').Equals($b.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
}
function Remove-KnownAsset([string]$path, [string]$description) {
    if (-not $path -or -not (Test-Path -LiteralPath $path)) { Write-Host "$description not found: $path" -ForegroundColor DarkGray; return }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Write-Warning "Skipping reparse point: $path"; return }
    if ($item.PSIsContainer) { throw "Refusing to recursively remove unexpected directory '$path'." }
    try { Remove-Item -LiteralPath $path -Force -ErrorAction Stop; Write-Host "Removed ${description}: $path" -ForegroundColor Green }
    catch { throw "Could not remove $description at '$path'. Close Haydar or its watcher and retry. The file may be locked. $($_.Exception.Message)" }
}
function Remove-HaydarData([string]$path) {
    if (-not $path -or -not (Test-Path -LiteralPath $path)) { Write-Host "Haydar user data not found: $path" -ForegroundColor DarkGray; return }
    $item = Get-Item -LiteralPath $path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing to recursively remove unsafe data path '$path'."
    }
    try { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop; Write-Host "Removed Haydar user data: $path" -ForegroundColor Green }
    catch { throw "Could not remove Haydar user data at '$path'. Close Haydar or its watcher and retry. A file may be locked. $($_.Exception.Message)" }
}

if (-not $installDir) { Write-Warning "LOCALAPPDATA is unavailable; install files will be skipped." }
if (-not $Yes) {
    $interactive = $true
    try { $null = $Host.UI.RawUI.WindowSize } catch { $interactive = $false }
    if (-not $interactive) { throw "Noninteractive use requires -Yes; no files were changed." }
    Write-Host "Haydar uninstall targets:"
    Write-Host "  Install: $installDir"
    Write-Host "  Startup: $autostartFile"
    if ($willRemoveData) { Write-Host "  Data: $dataDir (WILL BE DELETED)" }
    else { Write-Host "  Data: $dataDir (preserved)" }
    $choices = [Management.Automation.Host.ChoiceDescription[]] @(
        [Management.Automation.Host.ChoiceDescription]::new("&No", "Cancel"),
        [Management.Automation.Host.ChoiceDescription]::new("&Yes", "Uninstall Haydar")
    )
    if ($Host.UI.PromptForChoice("Uninstall Confirmation", "Proceed?", $choices, 0) -ne 1) { Write-Host "Uninstallation cancelled." -ForegroundColor Yellow; exit 0 }
}

try {
    # Remove only known release assets; leave unrelated user files in the folder.
    if ($installDir) {
        foreach ($asset in @("haydar.exe", "haydar-cli.exe", "haydar.exe.sha256", "haydar-cli.exe.sha256")) {
            Remove-KnownAsset (Join-Path $installDir $asset) $asset
        }
        if (Test-Path -LiteralPath $installDir -PathType Container) {
            $remaining = Get-ChildItem -LiteralPath $installDir -Force
            if (-not $remaining) { Remove-Item -LiteralPath $installDir -Force }
            else { Write-Warning "Keeping non-Haydar files in $installDir." }
        }
    }

    if ($autostartFile -and $startupDir) {
        $resolvedStartup = Resolve-SafePath $startupDir
        $resolvedAuto = Resolve-SafePath $autostartFile
        if ($resolvedAuto -and $resolvedStartup -and $resolvedAuto.StartsWith($resolvedStartup.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            Remove-KnownAsset $autostartFile "autostart script"
        } else { Write-Warning "Unsafe startup path; skipped: $autostartFile" }
    }

    # Preserve blank and unrelated PATH entries exactly as written.
    try {
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if ($null -ne $userPath -and $installDir) {
            $entries = $userPath -split ';', -1
            $newEntries = foreach ($entry in $entries) { if (Same-Path $entry $installDir) { continue }; $entry }
            $newPath = $newEntries -join ';'
            if ($newPath -ne $userPath) { [Environment]::SetEnvironmentVariable("Path", $newPath, "User"); Write-Host "Removed Haydar from user PATH." -ForegroundColor Green }
            else { Write-Host "User PATH already clean." -ForegroundColor DarkGray }
        }
    } catch { Write-Warning "Could not update User PATH; other cleanup was completed. $_" }

    if ($willRemoveData -and $dataDir) {
        $resolvedData = Resolve-SafePath $dataDir
        $expectedData = Resolve-SafePath (Join-Path $env:USERPROFILE '.haydar')
        if (Same-Path $resolvedData $expectedData) { Remove-HaydarData $resolvedData }
        else { Write-Warning "Unsafe data path; skipped: $dataDir" }
    }
    Write-Host "Uninstallation complete." -ForegroundColor Green
} catch { Write-Error $_; exit 1 }
