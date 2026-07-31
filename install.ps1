# install.ps1 - transactionally download, verify, and install Haydar on Windows.
[CmdletBinding()]
param(
    [string]$Repo = "haydar-search/haydar",
    [string]$Version = "latest",
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

function Normalize-Version ([string]$ver) {
    if ([string]::IsNullOrWhiteSpace($ver)) { return "" }
    $ver = $ver.Trim().ToLowerInvariant()
    if ($ver -match "^(?:.*? )?v?([\d\.]+(?:-[a-z0-9]+)?)$") { return $matches[1] }
    return $ver
}

function Normalize-PathEntry ([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return "" }
    try { return [IO.Path]::GetFullPath($path.Trim()).TrimEnd('\').ToLowerInvariant() }
    catch { return $path.Trim().TrimEnd('\').ToLowerInvariant() }
}

function Get-TempRoot {
    foreach ($candidate in @($env:TEMP, $env:TMP, [IO.Path]::GetTempPath())) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Container)) {
            return $candidate
        }
    }
    throw "No usable temporary directory is available."
}

function Get-ExeVersion ([string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return "" }
    $stdout = Join-Path (Get-TempRoot) "haydar_ver_$([Guid]::NewGuid().ToString('N')).txt"
    $stderr = "$stdout.err"
    try {
        $proc = Start-Process -FilePath $path -ArgumentList @("--version") `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
            -WindowStyle Hidden -PassThru
        $proc | Wait-Process -Timeout 5 -ErrorAction SilentlyContinue
        if (-not $proc.HasExited) { $proc | Stop-Process -Force -ErrorAction SilentlyContinue; return "" }
        if (Test-Path -LiteralPath $stdout) {
            return Normalize-Version (Get-Content -LiteralPath $stdout -Raw -ErrorAction SilentlyContinue)
        }
        return ""
    } finally {
        Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    }
}

$installDir = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Haydar" } else { throw "LOCALAPPDATA is not available." }
$cliPath = Join-Path $installDir "haydar-cli.exe"
$assets = @("haydar.exe", "haydar.exe.sha256", "haydar-cli.exe", "haydar-cli.exe.sha256")

if (Test-Path -LiteralPath $cliPath) {
    $installedVersion = Get-ExeVersion $cliPath
    if ($installedVersion) { Write-Host "Found existing installation: v$installedVersion" -ForegroundColor Cyan }
    else { Write-Host "Found existing installation at $cliPath (version unknown)." -ForegroundColor Yellow }
    if ($Version -ne "latest" -and $installedVersion -eq (Normalize-Version $Version)) {
        Write-Host "The requested version is already installed." -ForegroundColor Yellow
    }

    if (-not $Yes) {
        $interactive = $true
        try { $null = $Host.UI.RawUI.WindowSize } catch { $interactive = $false }
        if (-not $interactive) { throw "Noninteractive use requires -Yes; the existing installation was not changed." }
        $choices = [Management.Automation.Host.ChoiceDescription[]] @(
            [Management.Automation.Host.ChoiceDescription]::new("&No", "Keep the existing installation"),
            [Management.Automation.Host.ChoiceDescription]::new("&Yes", "Replace the existing installation")
        )
        if ($Host.UI.PromptForChoice("Existing Installation", "Replace the existing installation?", $choices, 0) -ne 1) {
            Write-Host "Installation cancelled; no files were changed." -ForegroundColor Yellow
            exit 0
        }
    }
}

$base = if ($Version -eq "latest") { "https://github.com/$Repo/releases/latest/download" } else { "https://github.com/$Repo/releases/download/$Version" }
$transaction = Join-Path (Get-TempRoot) "haydar_install_$([Guid]::NewGuid().ToString('N'))"
$stage = Join-Path $transaction "stage"
$backup = Join-Path $transaction "backup"
$committed = [Collections.Generic.List[string]]::new()

try {
    New-Item -ItemType Directory -Force -Path $stage, $backup | Out-Null

    # Download and validate every asset before touching the installation.
    foreach ($exe in @("haydar.exe", "haydar-cli.exe")) {
        Write-Host "Downloading $exe ..." -ForegroundColor Cyan
        $stagedExe = Join-Path $stage $exe
        $stagedSum = Join-Path $stage "$exe.sha256"
        Invoke-WebRequest -Uri "$base/$exe" -OutFile $stagedExe
        Invoke-WebRequest -Uri "$base/$exe.sha256" -OutFile $stagedSum
        $expected = ((Get-Content -LiteralPath $stagedSum -Raw).Trim() -split '\s+')[0]
        if ($expected -notmatch '^[0-9a-fA-F]{64}$') { throw "The checksum for $exe is not a valid SHA-256 value." }
        $actual = (Get-FileHash -LiteralPath $stagedExe -Algorithm SHA256).Hash
        if ($actual -ne $expected) { throw "Checksum mismatch for $exe." }
        Write-Host "Verified $exe." -ForegroundColor Green
    }

    New-Item -ItemType Directory -Force -Path $installDir | Out-Null
    foreach ($asset in $assets) {
        $destination = Join-Path $installDir $asset
        if (Test-Path -LiteralPath $destination) { Copy-Item -LiteralPath $destination -Destination (Join-Path $backup $asset) -Force }
    }

    try {
        foreach ($asset in $assets) {
            $committed.Add($asset)
            Move-Item -LiteralPath (Join-Path $stage $asset) -Destination (Join-Path $installDir $asset) -Force
        }
    } catch {
        $commitError = $_.Exception.Message
        $rollbackErrors = [Collections.Generic.List[string]]::new()
        # Include the currently failing asset: a provider may modify a destination
        # before reporting failure. Restore every attempted destination in reverse.
        for ($index = $committed.Count - 1; $index -ge 0; $index--) {
            $asset = $committed[$index]
            $destination = Join-Path $installDir $asset
            $prior = Join-Path $backup $asset
            try {
                if (Test-Path -LiteralPath $prior) { Copy-Item -LiteralPath $prior -Destination $destination -Force }
                else { Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue }
            } catch { $rollbackErrors.Add("$asset`: $($_.Exception.Message)") }
        }
        if ($rollbackErrors.Count -gt 0) {
            throw "Installation commit failed. Rollback was incomplete; close Haydar and restore the affected files from '$backup' before deleting it. Commit error: $commitError. Rollback errors: $($rollbackErrors -join '; ')"
        }
        throw "Installation commit failed and the previous installation was restored. $commitError"
    }

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $normalizedInstall = Normalize-PathEntry $installDir
    $present = @($userPath -split ';' | Where-Object { (Normalize-PathEntry $_) -eq $normalizedInstall }).Count -gt 0
    if (-not $present) {
        $newPath = if ([string]::IsNullOrEmpty($userPath)) { $installDir } else { "$userPath;$installDir" }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "Added $installDir to user PATH; restart the shell to pick it up." -ForegroundColor Cyan
    }

    $finalVersion = Get-ExeVersion $cliPath
    if ($finalVersion) { Write-Host "Installed Haydar v$finalVersion to $installDir." -ForegroundColor Green }
    else { Write-Host "Installed Haydar to $installDir." -ForegroundColor Green }
    Write-Host "Run 'haydar-cli.exe init' to set up your index." -ForegroundColor Green
} catch {
    Write-Error $_
    exit 1
} finally {
    # Keep an incomplete rollback backup for manual recovery; otherwise cleanup.
    if (-not ($rollbackErrors -and $rollbackErrors.Count -gt 0)) {
        Remove-Item -LiteralPath $transaction -Recurse -Force -ErrorAction SilentlyContinue
    }
}
