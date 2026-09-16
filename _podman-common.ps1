# _podman-common.ps1 — helpers shared by run-generate.ps1, run-upload.ps1 and
# smoke-test.ps1. Dot-sourced by each of them; not meant to be run directly.

$script:ImageNameDefault = 'localhost/loan-packet-demo:local'

function Test-PodmanAvailable {
    if (-not (Get-Command podman -ErrorAction SilentlyContinue)) {
        throw "podman was not found on PATH. Install Podman for Windows and run 'podman machine init; podman machine start'."
    }
    # `podman info` fails fast when the machine is not running, which is by far
    # the most common first-run problem on Windows.
    $null = & podman info --format '{{.Host.OS}}' 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "podman is installed but not reachable. On Windows run: podman machine init (first time only), then podman machine start."
    }
}

function Test-ImageExists {
    param([Parameter(Mandatory)][string]$Image)
    & podman image exists $Image 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-BuildImage {
    param(
        [Parameter(Mandatory)][string]$Image,
        [Parameter(Mandatory)][string]$ContextPath,
        [switch]$Force
    )
    if ((Test-ImageExists -Image $Image) -and -not $Force) {
        Write-Host "image $Image already present (use -Rebuild to force a rebuild)"
        return
    }
    Write-Host "building $Image from $ContextPath ..."
    & podman build --tag $Image --file (Join-Path $ContextPath 'Containerfile') $ContextPath
    if ($LASTEXITCODE -ne 0) { throw "podman build failed with exit code $LASTEXITCODE" }
}

function Get-UserNsArgs {
    <#
      Rootless Podman maps container UIDs into the host's subuid range. Without
      a mapping, files the container writes into a bind mount land on the host
      owned by an unmapped high UID that the calling user cannot delete.

      The image runs as uid/gid 1000, so --userns=keep-id:uid=1000,gid=1000
      maps the calling user onto that account and generated files come back
      owned by the caller. Podman older than 4.3 does not accept the uid=/gid=
      form, so fall back to plain keep-id, and to no mapping at all on hosts
      where keep-id is unavailable.

      On Windows the bind mount is a Windows path surfaced inside the Podman
      machine VM, where ownership is synthesised by the filesystem driver
      rather than stored; keep-id is still correct and harmless there.
    #>
    param([Parameter(Mandatory)][string]$Image)

    foreach ($candidate in @('--userns=keep-id:uid=1000,gid=1000', '--userns=keep-id')) {
        & podman run --rm $candidate $Image python -c "pass" *> $null
        if ($LASTEXITCODE -eq 0) { return @($candidate) }
    }
    Write-Warning "this Podman does not support --userns=keep-id; running without a UID mapping. Check ownership of the generated files."
    return @()
}

function Resolve-HostDirectory {
    <#
      Podman needs an absolute host path for -v. Create the directory first:
      a non-existent source path makes Podman invent an empty anonymous mount
      on some versions instead of failing, which looks like "the generator
      produced nothing".
    #>
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        $null = New-Item -ItemType Directory -Force -Path $Path
    }
    return (Resolve-Path -LiteralPath $Path).ProviderPath
}

function Get-MountSpec {
    <#
      Builds the -v argument. The host path is passed as a single argv element,
      so spaces in it are safe as long as the caller never re-splits the string
      (this is why every invocation here uses argument arrays, not one command
      line). No :Z / :z flag: that relabels the mount for SELinux, which this
      host does not run — adding it on Windows or a non-SELinux Linux host is
      at best inert and at worst an error from the OCI runtime.
    #>
    param(
        [Parameter(Mandatory)][string]$HostPath,
        [string]$ContainerPath = '/data',
        [ValidateSet('rw', 'ro')][string]$Mode = 'rw'
    )
    return @('-v', "${HostPath}:${ContainerPath}:${Mode}")
}
