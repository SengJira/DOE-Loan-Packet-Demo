<#
.SYNOPSIS
    End-to-end smoke test: build, generate twice, verify, compare, dry-run upload.

.DESCRIPTION
    Exits non-zero if any check fails. Needs network access only for the image
    build; every other step runs offline. The work directory deliberately
    contains a space, because a bind-mount path with a space is the failure
    mode this setup hits most often on Windows.

.EXAMPLE
    .\smoke-test.ps1
    .\smoke-test.ps1 -Packets 4 -Image loan-packet-demo:ci
#>
[CmdletBinding()]
param(
    [int]$Packets = 10,
    [int]$Seed = 7,
    [string]$Image = 'localhost/loan-packet-demo:local',
    [string]$Work = (Join-Path $PSScriptRoot '.smoke\smoke run'),
    [switch]$Rebuild
)

# Individual checks must be able to fail without aborting the run.
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_podman-common.ps1')

$script:Failed = 0
$script:StepNo = 0

function Step   { param([string]$m) $script:StepNo++; Write-Host ""; Write-Host "=== [$script:StepNo] $m" }
function Pass   { param([string]$m) Write-Host "    PASS  $m" }
function Fail   { param([string]$m) $script:Failed++; Write-Host "    FAIL  $m" -ForegroundColor Red }
function Assert { param([bool]$ok, [string]$m) if ($ok) { Pass $m } else { Fail $m } }

try {
    Test-PodmanAvailable
} catch {
    Write-Host "    FAIL  $_" -ForegroundColor Red
    exit 1
}

# The generator's default 60/30/10 split assigns whole packets, so fewer than
# 10 packets leaves `incoming` empty and the layout check below would fail for
# a reason that has nothing to do with packaging.
if ($Packets -lt 10) {
    Write-Host "-Packets must be at least 10: the default 60/30/10 split gives the incoming split zero packets below that." -ForegroundColor Red
    exit 2
}

Write-Host "image   : $Image"
Write-Host "workdir : $Work"
Write-Host "packets : $Packets (seed $Seed)"

Step "build the image if it is not present"
try {
    Invoke-BuildImage -Image $Image -ContextPath $PSScriptRoot -Force:$Rebuild
    Pass "image available"
} catch {
    Fail "build: $_"
    Write-Host "SMOKE TEST FAILED: cannot continue without an image" -ForegroundColor Red
    exit 1
}

$userNs = Get-UserNsArgs -Image $Image

function Invoke-Image {
    param([string]$HostPath, [string]$Mode = 'rw', [string[]]$ImageArgs, [switch]$NoNetwork)
    $a = @('run', '--rm') + $userNs
    if ($NoNetwork) { $a += '--network=none' }
    $a += (Get-MountSpec -HostPath $HostPath -ContainerPath '/data' -Mode $Mode)
    $a += @($Image) + $ImageArgs
    # Out-Host, not plain output: anything podman prints would otherwise become
    # this function's return value and swamp the exit code.
    & podman @a 2>&1 | Out-Host
    return $LASTEXITCODE
}

Step "clean the work directory"
if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
$runA = Resolve-HostDirectory (Join-Path $Work 'run_a')
$runB = Resolve-HostDirectory (Join-Path $Work 'run_b')
$runC = Resolve-HostDirectory (Join-Path $Work 'run_c')
$empty = Resolve-HostDirectory (Join-Path $Work 'empty')
$workPath = (Resolve-Path -LiteralPath $Work).ProviderPath
Assert (Test-Path -LiteralPath $runA) "created '$workPath' (path contains a space on purpose)"

Step "generate run A ($Packets packets, seed $Seed)"
Assert ((Invoke-Image -HostPath $runA -ImageArgs @('generate', '--packets', "$Packets", '--out', '/data', '--seed', "$Seed")) -eq 0) "generator exited 0"

Step "generate run B (same seed)"
Assert ((Invoke-Image -HostPath $runB -ImageArgs @('generate', '--packets', "$Packets", '--out', '/data', '--seed', "$Seed")) -eq 0) "generator exited 0"

Step "generate run C (seed $($Seed + 1), control for the comparison)"
Assert ((Invoke-Image -HostPath $runC -ImageArgs @('generate', '--packets', "$Packets", '--out', '/data', '--seed', "$($Seed + 1)")) -eq 0) "generator exited 0"

Step "output layout and metadata files"
foreach ($split in @('base', 'unlabeled', 'incoming')) {
    $n = @(Get-ChildItem -LiteralPath (Join-Path $runA $split) -Filter '*.pdf' -ErrorAction SilentlyContinue).Count
    Assert ($n -gt 0) "$split/ contains $n PDFs"
}
foreach ($f in @('ground_truth.json', 'prelabels.json', 'dataloop_metadata.json', 'manifest.csv')) {
    $p = Join-Path $runA $f
    Assert ((Test-Path -LiteralPath $p) -and ((Get-Item -LiteralPath $p).Length -gt 0)) "$f present"
}

Step "the Windows user can read and open the generated files"
$pdf = Get-ChildItem -LiteralPath $runA -Recurse -Filter '*.pdf' | Sort-Object FullName | Select-Object -First 1
if ($null -eq $pdf) {
    Fail "no PDFs were produced"
} else {
    try {
        $fs = [System.IO.File]::OpenRead($pdf.FullName)
        $head = New-Object byte[] 5
        $null = $fs.Read($head, 0, 5)
        $fs.Close()
        Assert ([System.Text.Encoding]::ASCII.GetString($head) -eq '%PDF-') "readable and starts with %PDF- : $($pdf.Name)"
    } catch {
        Fail "cannot read $($pdf.FullName): $_"
    }
}

Step "verify run A (PDFs open, text layers match the scanned flag, manifest count)"
Assert ((Invoke-Image -HostPath $runA -ImageArgs @('verify', '--data', '/data', '--emit-digest', '/data/digest.json')) -eq 0) "verify_output.py on run A"

Step "verify run B"
Assert ((Invoke-Image -HostPath $runB -ImageArgs @('verify', '--data', '/data', '--emit-digest', '/data/digest.json')) -eq 0) "verify_output.py on run B"

Step "verify run C"
Assert ((Invoke-Image -HostPath $runC -ImageArgs @('verify', '--data', '/data', '--emit-digest', '/data/digest.json')) -eq 0) "verify_output.py on run C"

Step "same seed => identical file list and identical ground_truth.json"
Assert ((Invoke-Image -HostPath $workPath -ImageArgs @('verify', '--compare', '/data/run_a/digest.json', '/data/run_b/digest.json')) -eq 0) "run A and run B are identical"

Step "same seed => PDF bytes match (digital exactly; scanned modulo the random trailer /ID)"
Assert ((Invoke-Image -HostPath $workPath -ImageArgs @('verify', '--compare-trees', '/data/run_a', '/data/run_b')) -eq 0) "PDF content identical across runs"

Step "different seed => different output (proves the comparison is meaningful)"
Assert ((Invoke-Image -HostPath $workPath -ImageArgs @('verify', '--compare', '/data/run_a/digest.json', '/data/run_c/digest.json')) -ne 0) "run A and run C differ, as expected"

Step "uploader --dry-run with networking disabled"
Assert ((Invoke-Image -HostPath $runA -Mode 'ro' -NoNetwork -ImageArgs @('upload', '--data', '/data', '--project', 'Loan-Docs-Demo', '--dry-run')) -eq 0) "dry run succeeded with --network=none"

Step "uploader --dry-run fails on an unusable data directory"
$null = Invoke-Image -HostPath $empty -Mode 'ro' -NoNetwork -ImageArgs @('upload', '--data', '/data', '--dry-run') 2>&1
Assert ($LASTEXITCODE -ne 0) "empty directory rejected with a non-zero exit"

Step "no credentials baked into the image"
$listing = & podman run --rm $Image sh -c 'ls -A /home/app; ls /app' 2>&1
Assert (-not ($listing -match '(?i)token|\.env|credential')) "no token/.env/credential files in the image"

Write-Host ""
Write-Host "========================================="
if ($script:Failed -eq 0) {
    Write-Host "SMOKE TEST PASSED ($script:StepNo steps)" -ForegroundColor Green
    exit 0
}
Write-Host "SMOKE TEST FAILED: $script:Failed check(s) failed" -ForegroundColor Red
exit 1
