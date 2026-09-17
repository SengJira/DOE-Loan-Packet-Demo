# Offline demo bundle

How to carry the demo to a customer site on a USB drive and run it on a laptop
with no registry, no PyPI, and no internet at all.

## What is in the bundle

```
offline_demo\
  loan-packet-demo.tar     the container image (podman save, docker-archive)
  image-digest.txt         name + image ID of what was saved
  demo_data\               a pre-generated 120-packet dataset, already verified
  SHA256SUMS.txt           checksum of every file above
  restore-bundle.ps1       run this on the target machine
  run-generate.ps1         generate more data offline
  run-upload.ps1           upload / dry-run (upload itself obviously needs network)
  _podman-common.ps1
  README-offline.md        this file
```

Roughly 500 MB, most of it the image tar.

## Building it (on a machine with network)

```powershell
cd path\to\loan-packet-demo
.\build-bundle.ps1                    # writes D:\DOE_Demo\offline_demo
.\build-bundle.ps1 -Out "E:\usb\offline_demo" -Packets 120 -Seed 7
```

It builds the image if needed, generates the dataset, **verifies it before
writing the bundle** (a bundle that fails verification is never produced),
saves the image, copies the wrappers and writes `SHA256SUMS.txt`.

Then copy the whole folder to the USB drive. Copy the folder, not its contents
— `restore-bundle.ps1` expects its siblings next to it.

## Restoring it (on the target machine, offline)

The target still needs Podman installed with its machine initialised. That
part cannot be carried on a USB drive; do it before you travel:

```powershell
podman machine init
podman machine start
```

Then, from the bundle folder:

```powershell
cd D:\DOE_Demo\offline_demo
.\restore-bundle.ps1
```

This checks every checksum, runs `podman load`, and then verifies the bundled
dataset **with the restored image and `--network=none`**, so a successful run
proves the demo works with no connectivity. Skip the checksum pass with
`-SkipChecksums` if you are in a hurry and the copy is known good.

After that:

```powershell
Invoke-Item .\demo_data\base                       # the pre-generated PDFs
.\run-generate.ps1 -Packets 10 -Out .\more_data    # generate more, offline
.\run-upload.ps1 -DryRun -Project "Loan-Docs-Demo" # the upload plan, offline
```

A real upload needs network and credentials; everything else does not.

## Troubleshooting

**`loan-packet-demo.tar not found`** — only the scripts were copied. Copy the
whole bundle folder.

**A checksum fails** — the USB copy is corrupt or truncated (a large tar copied
to a FAT32 stick is the usual cause; FAT32 caps files at 4 GB and some copiers
fail quietly). Re-copy onto exFAT or NTFS.

**`podman load` fails with `payload does not match any of the supported image
formats`** — the tar is truncated, same cause as above. Compare its size with
`image-digest.txt`'s source machine.

**The image loads under a different name** — `podman load` preserves whatever
tag was saved. Check `podman images` and pass `-Image <name>:<tag>` to
`restore-bundle.ps1` and the wrappers.

**`Error: unrecognized namespace mode keep-id:uid=1000,gid=1000 passed`** —
Podman older than 4.3. Harmless: the wrappers fall back automatically. See the
main README's troubleshooting section.

**Podman itself is not installed on the target** — nothing in this bundle can
fix that; the Podman installer must be on the USB drive too. Download
`podman-installer-windows-amd64.msi` from the Podman releases page and carry
it alongside the bundle, and note that `podman machine init` downloads a WSL2
image the first time, so it must be run **before** going offline.
