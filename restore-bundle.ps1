<#
.SYNOPSIS
    Restores the offline bundle on a machine with no registry access.

.DESCRIPTION
    Verifies the checksums, loads the image from the tar archive, and checks
    the loaded image runs. Makes no network calls: every podman run here uses
    --network=none, and `podman load` never contacts a registry.

.EXAMPLE
    .\restore-bundle.ps1
    .\restore-bundle.ps1 -SkipChecksums
#>
[CmdletBinding()]
param(
    [string]$BundleDir = $PSScriptRoot,
    [string]$Image = 'localhost/loan-packet-demo:local',
    [switch]$SkipChecksums
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_podman-common.ps1')

$bundlePath = (Resolve-Path -LiteralPath $BundleDir).ProviderPath
$tarPath = Join-Path $bundlePath 'loan-packet-demo.tar'
if (-not (Test-Path -LiteralPath $tarPath)) {
    throw "loan-packet-demo.tar not found in $bundlePath. Copy the whole bundle folder, not just the scripts."
}

Test-PodmanAvailable

# --- checksums --------------------------------------------------------------
if (-not $SkipChecksums) {
    $sumsFile = Join-Path $bundlePath 'SHA256SUMS.txt'
    if (-not (Test-Path -LiteralPath $sumsFile)) { throw "SHA256SUMS.txt missing from $bundlePath" }
    Write-Host "verifying checksums ..."
    $bad = 0
    foreach ($line in Get-Content -LiteralPath $sumsFile) {
        if (-not $line.Trim()) { continue }
        $expected, $rel = $line -split '\s+', 2
        $file = Join-Path $bundlePath $rel.Trim()
        if (-not (Test-Path -LiteralPath $file)) {
            Write-Host "  MISSING  $rel" -ForegroundColor Red; $bad++; continue
        }
        $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expected.Trim().ToLower()) {
            Write-Host "  CORRUPT  $rel" -ForegroundColor Red; $bad++
        }
    }
    if ($bad -gt 0) { throw "$bad file(s) failed the checksum check. Re-copy the bundle from the USB drive." }
    Write-Host "  all files match"
}

# --- load -------------------------------------------------------------------
Write-Host "loading the image from $tarPath ..."
& podman load --input $tarPath
if ($LASTEXITCODE -ne 0) { throw "podman load failed with exit code $LASTEXITCODE" }

if (-not (Test-ImageExists -Image $Image)) {
    throw "the archive loaded but $Image is not present. Check 'podman images' and pass -Image with the name shown there."
}

# --- prove it works, offline -----------------------------------------------
$dataPath = Join-Path $bundlePath 'demo_data'
if (Test-Path -LiteralPath (Join-Path $dataPath 'ground_truth.json')) {
    Write-Host "verifying the bundled dataset with the restored image (no network) ..."
    $runArgs = @('run', '--rm', '--network=none') + (Get-UserNsArgs -Image $Image) +
               (Get-MountSpec -HostPath $dataPath -ContainerPath '/data' -Mode 'ro') +
               @($Image, 'verify', '--data', '/data')
    & podman @runArgs
    if ($LASTEXITCODE -ne 0) { throw "the restored image could not verify the bundled dataset" }
} else {
    Write-Warning "no demo_data\ in the bundle; skipping the dataset check"
}

Write-Host ""
Write-Host "restore complete. The demo is ready:"
Write-Host "    the pre-generated dataset is in .\demo_data"
Write-Host "    generate more with  .\run-generate.ps1 -Packets 10"
Write-Host "    dry-run the upload  .\run-upload.ps1 -DryRun -Project ""Loan-Docs-Demo"""
