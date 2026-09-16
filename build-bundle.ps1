<#
.SYNOPSIS
    Exports the image plus a pre-generated dataset into one folder that can be
    carried to a customer site on a USB drive.

.DESCRIPTION
    Produces, in the output folder:

        loan-packet-demo.tar        the image, `podman save` archive
        image-digest.txt            the digest of the image that was saved
        demo_data\                  a pre-generated dataset (120 packets)
        SHA256SUMS.txt              checksums of everything above
        restore-bundle.ps1          restores the image on the target machine
        run-generate.ps1 ...        the wrappers, so the folder is self-contained
        README-offline.md

    Run this on a machine that has network access. Nothing in the bundle needs
    a registry or PyPI afterwards.

.EXAMPLE
    .\build-bundle.ps1
    .\build-bundle.ps1 -Out "E:\usb\offline_demo" -Packets 120 -Seed 7
#>
[CmdletBinding()]
param(
    # Default target is the demo folder on the sales laptop's D: drive.
    [string]$Out = 'D:\DOE_Demo\offline_demo',
    [int]$Packets = 120,
    [int]$Seed = 7,
    [string]$Image = 'localhost/loan-packet-demo:local',
    [switch]$Rebuild,

    # Skip regenerating the dataset if one is already in the bundle.
    [switch]$KeepData
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_podman-common.ps1')

Test-PodmanAvailable
Invoke-BuildImage -Image $Image -ContextPath $PSScriptRoot -Force:$Rebuild

$outPath = Resolve-HostDirectory -Path $Out
Write-Host "bundle target : $outPath"
Write-Host "image         : $Image"

# --- 1. the dataset ---------------------------------------------------------
$dataPath = Join-Path $outPath 'demo_data'
if ($KeepData -and (Test-Path -LiteralPath (Join-Path $dataPath 'ground_truth.json'))) {
    Write-Host "keeping the existing dataset in $dataPath"
} else {
    if (Test-Path -LiteralPath $dataPath) { Remove-Item -LiteralPath $dataPath -Recurse -Force }
    $null = New-Item -ItemType Directory -Force -Path $dataPath
    Write-Host "generating $Packets packets (seed $Seed) ..."
    $genArgs = @('run', '--rm') + (Get-UserNsArgs -Image $Image) +
               (Get-MountSpec -HostPath $dataPath -ContainerPath '/data' -Mode 'rw') +
               @($Image, 'generate', '--out', '/data', '--packets', "$Packets", '--seed', "$Seed")
    & podman @genArgs
    if ($LASTEXITCODE -ne 0) { throw "generation failed with exit code $LASTEXITCODE" }
}

# Verify before shipping: a bundle nobody can check is worse than no bundle.
Write-Host "verifying the dataset ..."
$verifyArgs = @('run', '--rm', '--network=none') + (Get-UserNsArgs -Image $Image) +
              (Get-MountSpec -HostPath $dataPath -ContainerPath '/data' -Mode 'ro') +
              @($Image, 'verify', '--data', '/data')
& podman @verifyArgs
if ($LASTEXITCODE -ne 0) { throw "the generated dataset failed verification; bundle not written" }

# --- 2. the image -----------------------------------------------------------
$tarPath = Join-Path $outPath 'loan-packet-demo.tar'
Write-Host "saving the image to $tarPath (this takes a minute) ..."
# --format oci-archive would also work, but docker-archive restores on older
# Podman and on Docker, which is one less thing to go wrong at a customer site.
& podman save --format docker-archive --output $tarPath $Image
if ($LASTEXITCODE -ne 0) { throw "podman save failed with exit code $LASTEXITCODE" }

$digest = (& podman image inspect $Image --format '{{.Id}}')
Set-Content -LiteralPath (Join-Path $outPath 'image-digest.txt') `
    -Value "$Image`n$digest`n" -NoNewline

# --- 3. the wrappers and docs ----------------------------------------------
foreach ($name in @('_podman-common.ps1', 'run-generate.ps1', 'run-upload.ps1',
                    'restore-bundle.ps1', 'README-offline.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $outPath -Force
}

# --- 4. checksums -----------------------------------------------------------
Write-Host "writing SHA256SUMS.txt ..."
$sumsFile = Join-Path $outPath 'SHA256SUMS.txt'
if (Test-Path -LiteralPath $sumsFile) { Remove-Item -LiteralPath $sumsFile -Force }
$lines = Get-ChildItem -LiteralPath $outPath -Recurse -File |
    Where-Object { $_.Name -ne 'SHA256SUMS.txt' } |
    Sort-Object FullName |
    ForEach-Object {
        $rel = $_.FullName.Substring($outPath.Length).TrimStart('\', '/').Replace('\', '/')
        "{0}  {1}" -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower(), $rel
    }
Set-Content -LiteralPath $sumsFile -Value $lines

$size = [math]::Round(((Get-ChildItem -LiteralPath $outPath -Recurse -File |
    Measure-Object -Property Length -Sum).Sum / 1MB), 1)
Write-Host ""
Write-Host "bundle written to $outPath ($size MB, $($lines.Count) files)"
Write-Host "copy the whole folder to the USB drive, then on the target machine run:"
Write-Host "    .\restore-bundle.ps1"
