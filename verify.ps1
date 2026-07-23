# verify.ps1 - Verify a Haydar EXE against its published SHA-256 checksum.
#
# Usage:
#   .\verify.ps1                       # verifies .\haydar-cli.exe
#   .\verify.ps1 -Path .\haydar.exe    # verifies a specific EXE
#
# The matching "<name>.sha256" file must sit next to the EXE. Exit code 0 means
# the download is intact; 1 means a mismatch or missing file.

[CmdletBinding()]
param(
    [string]$Path = "haydar-cli.exe"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Path)) {
    Write-Host "File not found: $Path" -ForegroundColor Red
    exit 1
}

$checksumFile = "$Path.sha256"
if (-not (Test-Path -LiteralPath $checksumFile)) {
    Write-Host "Checksum file not found: $checksumFile" -ForegroundColor Red
    exit 1
}

$actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()

# Accept both "<hash>" and "<hash>  filename" formats.
$expected = ((Get-Content -LiteralPath $checksumFile -Raw).Trim() -split '\s+')[0].ToLower()

if ($actual -eq $expected) {
    Write-Host "OK: $Path matches its SHA-256 checksum." -ForegroundColor Green
    exit 0
} else {
    Write-Host "MISMATCH for $Path" -ForegroundColor Red
    Write-Host "  expected: $expected"
    Write-Host "  actual:   $actual"
    exit 1
}
