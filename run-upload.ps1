<#
.SYNOPSIS
    Uploads a generated demo_data directory into Dataloop using the container image.

.DESCRIPTION
    Runs upload_to_dataloop.py inside the container with the data directory
    bind-mounted. Credentials are never baked into the image: they come either
    from environment variables forwarded at run time, or from a Dataloop state
    directory / token file mounted into the container.

    Start with -DryRun. It validates the data directory and prints the exact
    dataset, ontology and upload actions that would run, with networking
    disabled inside the container so it cannot touch a tenant by accident.

.EXAMPLE
    .\run-upload.ps1 -DryRun
    .\run-upload.ps1 -Project "Loan-Docs-Demo" -StateDir "$env:USERPROFILE\.dataloop"
    $env:DTLPY_CUSTOM_ENV = '...'; .\run-upload.ps1 -Project "Loan-Docs-Demo"
#>
[CmdletBinding()]
param(
    [string]$Data = (Join-Path $PSScriptRoot 'demo_data'),
    [string]$Project,
    [string]$Dataset,
    [string]$Splits,

    # Validate and print the plan; makes no network calls.
    [switch]$DryRun,

    # Directory holding the dtlpy state (normally %USERPROFILE%\.dataloop).
    # Mounted read-write because dtlpy refreshes its token in place.
    [string]$StateDir,

    # A single dtlpy cookie/token file, mounted read-only over the state file.
    # Mutually exclusive with -StateDir.
    [string]$TokenFile,

    # Extra environment variable names to forward into the container,
    # e.g. -ForwardEnv DTLPY_CUSTOM_ENV,DATALOOP_PATH
    [string[]]$ForwardEnv = @(),

    [string]$Image = 'localhost/loan-packet-demo:local',
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_podman-common.ps1')

if ($StateDir -and $TokenFile) {
    throw "-StateDir and -TokenFile are mutually exclusive."
}

Test-PodmanAvailable
Invoke-BuildImage -Image $Image -ContextPath $PSScriptRoot -Force:$Rebuild

if (-not (Test-Path -LiteralPath $Data)) {
    throw "data directory '$Data' does not exist. Run .\run-generate.ps1 first."
}
$dataPath = (Resolve-Path -LiteralPath $Data).ProviderPath

$runArgs = @('run', '--rm', '-i') + (Get-UserNsArgs -Image $Image)

if ($DryRun) {
    # Belt and braces: the dry-run path does not import dtlpy at all, and with
    # no network namespace it could not reach a tenant even if it tried.
    $runArgs += '--network=none'
    $runArgs += (Get-MountSpec -HostPath $dataPath -ContainerPath '/data' -Mode 'ro')
} else {
    $runArgs += (Get-MountSpec -HostPath $dataPath -ContainerPath '/data' -Mode 'ro')

    if ($StateDir) {
        if (-not (Test-Path -LiteralPath $StateDir)) { throw "state directory '$StateDir' does not exist." }
        $statePath = (Resolve-Path -LiteralPath $StateDir).ProviderPath
        $runArgs += @('-v', "${statePath}:/home/app/.dataloop:rw")
        Write-Host "mounting dtlpy state from $statePath"
    }
    if ($TokenFile) {
        if (-not (Test-Path -LiteralPath $TokenFile)) { throw "token file '$TokenFile' does not exist." }
        $tokenPath = (Resolve-Path -LiteralPath $TokenFile).ProviderPath
        $runArgs += @('-v', "${tokenPath}:/home/app/.dataloop/cookie.json:ro")
        Write-Host "mounting token file from $tokenPath (read-only)"
    }

    # Forward credential-ish variables that are set in the caller's session.
    # Values are passed by name so they never appear in the command line, the
    # image, or this repository.
    $forward = @('DTLPY_CUSTOM_ENV', 'DATALOOP_PATH') + $ForwardEnv | Select-Object -Unique
    foreach ($name in $forward) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($null -ne $value -and $value -ne '') {
            $runArgs += @('--env', $name)
            Write-Host "forwarding $name"
        }
    }

    if (-not $StateDir -and -not $TokenFile) {
        Write-Warning "no -StateDir or -TokenFile given. dtlpy will try an interactive login inside the container, which has no browser. Run .\run-upload.ps1 -DryRun first, then supply credentials."
    }
}

$scriptArgs = @('upload', '--data', '/data')
if ($PSBoundParameters.ContainsKey('Project')) { $scriptArgs += @('--project', $Project) }
if ($PSBoundParameters.ContainsKey('Dataset')) { $scriptArgs += @('--dataset', $Dataset) }
if ($PSBoundParameters.ContainsKey('Splits'))  { $scriptArgs += @('--splits',  $Splits) }
if ($DryRun)                                   { $scriptArgs += '--dry-run' }

$runArgs += @($Image) + $scriptArgs

Write-Host "podman $($runArgs -join ' ')"
& podman @runArgs
if ($LASTEXITCODE -ne 0) {
    throw "upload failed with exit code $LASTEXITCODE"
}
