# install.ps1 - Download, verify, and install the Haydar EXEs on Windows.
#
# Downloads haydar.exe and haydar-cli.exe (plus their .sha256 files) from a
# GitHub release, verifies each against its checksum, installs them to
# %LOCALAPPDATA%\Haydar\, and adds that directory to the user PATH.
#
# Usage:
#   .\install.ps1                 # latest release
#   .\install.ps1 -Version v0.2.0 # a specific tag
#
# No admin rights required (user-scoped install + PATH).

[CmdletBinding()]
param(
    [string]$Repo = "haydar-search/haydar",
    [string]$Version = "latest"
)

$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:LOCALAPPDATA "Haydar"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

if ($Version -eq "latest") {
    $base = "https://github.com/$Repo/releases/latest/download"
} else {
    $base = "https://github.com/$Repo/releases/download/$Version"
}

$exes = @("haydar.exe", "haydar-cli.exe")

foreach ($exe in $exes) {
    $exePath = Join-Path $installDir $exe
    $sumPath = "$exePath.sha256"

    Write-Host "Downloading $exe ..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri "$base/$exe" -OutFile $exePath
    Invoke-WebRequest -Uri "$base/$exe.sha256" -OutFile $sumPath

    $actual = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLower()
    $expected = ((Get-Content -LiteralPath $sumPath -Raw).Trim() -split '\s+')[0].ToLower()

    if ($actual -ne $expected) {
        Remove-Item -LiteralPath $exePath -Force
        Write-Host "Checksum mismatch for $exe - aborting." -ForegroundColor Red
        Write-Host "  expected: $expected"
        Write-Host "  actual:   $actual"
        exit 1
    }
    Write-Host "  verified $exe" -ForegroundColor Green
}

# Add the install dir to the user PATH if it isn't already there.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ';') -notcontains $installDir) {
    $newPath = if ([string]::IsNullOrEmpty($userPath)) { $installDir } else { "$userPath;$installDir" }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Added $installDir to your user PATH (restart your shell to pick it up)." -ForegroundColor Cyan
}

Write-Host ""
Write-Host "Installed to $installDir" -ForegroundColor Green
Write-Host "Run 'haydar-cli.exe init' to set up your index." -ForegroundColor Green
