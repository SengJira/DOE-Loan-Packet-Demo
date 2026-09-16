<#
.SYNOPSIS
    Generates synthetic loan packets into .\demo_data using the container image.

.DESCRIPTION
    Builds localhost/loan-packet-demo:local if it is not already present, then
    runs generate_loan_packets.py inside it with the output directory bind-mounted
    from the host. Flags map one-to-one onto the script's own CLI.

.EXAMPLE
    .\run-generate.ps1
    .\run-generate.ps1 -Packets 10 -Seed 7
    .\run-generate.ps1 -Packets 120 -ScanRatio 0.4 -ErrorRate 0.2 -Split "70,20,10" -Out "D:\demo runs\batch 1"
#>
[CmdletBinding()]
param(
    [int]$Packets,
    [double]$ScanRatio,
    [double]$ErrorRate,
    [string]$Split,
    [int]$Seed,

    # Host directory that receives the output. Created if it does not exist.
    [string]$Out = (Join-Path $PSScriptRoot 'demo_data'),

    [string]$Image = 'localhost/loan-packet-demo:local',

    # Rebuild the image even if it already exists.
    [switch]$Rebuild,

    # Delete the contents of -Out before generating.
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_podman-common.ps1')

Test-PodmanAvailable
Invoke-BuildImage -Image $Image -ContextPath $PSScriptRoot -Force:$Rebuild

if ($Clean -and (Test-Path -LiteralPath $Out)) {
    Write-Host "clearing $Out"
    Remove-Item -LiteralPath $Out -Recurse -Force
}
$outPath = Resolve-HostDirectory -Path $Out

# Only forward flags the caller actually supplied, so the script's own defaults
# stay authoritative.
$scriptArgs = @('generate', '--out', '/data')
if ($PSBoundParameters.ContainsKey('Packets'))   { $scriptArgs += @('--packets',    "$Packets") }
if ($PSBoundParameters.ContainsKey('Seed'))      { $scriptArgs += @('--seed',       "$Seed") }
if ($PSBoundParameters.ContainsKey('ScanRatio')) { $scriptArgs += @('--scan-ratio', [string]::Format([cultureinfo]::InvariantCulture, '{0}', $ScanRatio)) }
if ($PSBoundParameters.ContainsKey('ErrorRate')) { $scriptArgs += @('--error-rate', [string]::Format([cultureinfo]::InvariantCulture, '{0}', $ErrorRate)) }
if ($PSBoundParameters.ContainsKey('Split'))     { $scriptArgs += @('--split',      $Split) }

$runArgs = @('run', '--rm') +
           (Get-UserNsArgs -Image $Image) +
           (Get-MountSpec -HostPath $outPath -ContainerPath '/data' -Mode 'rw') +
           @($Image) + $scriptArgs

Write-Host "podman $($runArgs -join ' ')"
& podman @runArgs
if ($LASTEXITCODE -ne 0) {
    throw "generation failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "output in $outPath"
Get-ChildItem -LiteralPath $outPath | Select-Object Mode, Length, Name | Format-Table | Out-String | Write-Host
foreach ($split in @('base', 'unlabeled', 'incoming')) {
    $dir = Join-Path $outPath $split
    if (Test-Path -LiteralPath $dir) {
        $n = @(Get-ChildItem -LiteralPath $dir -Filter '*.pdf').Count
        Write-Host ("  {0,-10} {1,5} PDFs" -f $split, $n)
    }
}
Write-Host ""
Write-Host "next: .\run-upload.ps1 -DryRun"
