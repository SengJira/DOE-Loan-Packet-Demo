# Loan packet demo — containerised generator and Dataloop uploader

Generates synthetic loan-application packets (loan application, pay stub, bank
statement, W-2, ID verification) as PDFs plus `ground_truth.json`,
`prelabels.json`, `dataloop_metadata.json` and `manifest.csv`, then uploads them
to a Dataloop project. Everything runs inside a container, so the laptop needs
Podman and nothing else — no Python, no Docker Desktop, no admin rights.

All generated data is synthetic.

---

## Quickstart (Windows 11, PowerShell)

### 1. Install Podman and start the machine — once per laptop

Install Podman Desktop or the Podman CLI (`winget install RedHat.Podman`), open
a **new** PowerShell window, then:

```powershell
podman machine init      # first time only; downloads the WSL2 VM image
podman machine start
podman info               # should print host details, not an error
```

`podman machine init` needs WSL2 available. If it complains, run
`wsl --install --no-distribution` in an elevated window once, reboot, and retry.

### 2. Generate demo data

```powershell
cd path\to\loan-packet-demo
.\run-generate.ps1 -Packets 10 -Seed 7
```

The first run builds the image (a few minutes; needs PyPI access). Later runs
reuse it — pass `-Rebuild` to force a rebuild.

Flags map one-to-one onto the generator's CLI and anything you omit keeps the
script's own default:

| Wrapper flag | Script flag | Default |
| --- | --- | --- |
| `-Packets` | `--packets` | 120 |
| `-Seed` | `--seed` | 7 |
| `-ScanRatio` | `--scan-ratio` | 0.25 |
| `-ErrorRate` | `--error-rate` | 0.12 |
| `-Split` | `--split` | `60,30,10` |
| `-Out` | `--out` | `.\demo_data` |

### 3. Inspect the output

```powershell
Get-ChildItem .\demo_data -Recurse -Filter *.pdf | Measure-Object
Invoke-Item .\demo_data\base          # open the folder, double-click a PDF
```

```
demo_data\
  base\        unlabeled\        incoming\      <- PDFs, split 60/30/10 by packet
  ground_truth.json  prelabels.json  dataloop_metadata.json  manifest.csv
```

The bundled verifier checks the output without you having to eyeball it — every
PDF opens and renders, scanned documents carry no text layer while digital ones
do, `manifest.csv` row count matches the PDFs on disk, and every name in
`ground_truth.json` exists:

```powershell
podman run --rm --userns=keep-id -v "${PWD}\demo_data:/data:ro" `
  localhost/loan-packet-demo:local verify --data /data
```

### 4. Upload to Dataloop

Always start with the dry run. It validates the data directory and prints the
project, dataset, ontology and per-file upload plan, with networking disabled
inside the container:

```powershell
.\run-upload.ps1 -DryRun -Project "Loan-Docs-Demo"
```

For a real upload, credentials come from your own machine — never from the
image or this repo. Either mount your existing dtlpy state directory:

```powershell
.\run-upload.ps1 -Project "Loan-Docs-Demo" -StateDir "$env:USERPROFILE\.dataloop"
```

or mount a single token file (`-TokenFile`), or forward environment variables
that are already set in your session (`-ForwardEnv DTLPY_CUSTOM_ENV`). Log in
once on the host with the dtlpy CLI so `%USERPROFILE%\.dataloop` exists; the
container has no browser and cannot complete an interactive login itself.

### 5. Run the whole chain

```powershell
.\smoke-test.ps1
```

Builds, generates three runs, verifies them, compares two same-seed runs,
dry-runs the uploader offline, and exits non-zero on any failure.

---

## Linux / CI

The same image and checks run on Linux, which keeps this usable in CI:

```bash
podman build -t localhost/loan-packet-demo:local .
./smoke-test.sh
```

`podman-compose.yml` is an optional convenience (`podman-compose run --rm
generate --packets 10`). The `.ps1` scripts do not use it and work standalone.

---

## Reproducibility

Two runs with the same `--seed` produce the same file list, the same
`ground_truth.json`, `prelabels.json`, `dataloop_metadata.json` and
`manifest.csv`, and byte-identical digital PDFs. Verify it yourself:

```powershell
.\run-generate.ps1 -Packets 10 -Seed 7 -Out .\run_a
.\run-generate.ps1 -Packets 10 -Seed 7 -Out .\run_b
podman run --rm -v "${PWD}:/w:ro" localhost/loan-packet-demo:local `
  verify --compare-trees /w/run_a /w/run_b
```

Two honest caveats:

- **Digital PDFs are byte-identical only because the image sets
  `RL_invariant=1`.** ReportLab otherwise stamps `/CreationDate`, `/ModDate`
  and a clock-derived document ID into every file, so the same seed gave
  different bytes every run. The environment variable freezes those.
- **Scanned (rasterised) PDFs are *not* byte-identical.** PyMuPDF writes a
  random `/ID` into the PDF trailer on every save and offers no way to pin it.
  Their page content is identical: `--compare-trees` rasterises any scanned PDF
  whose bytes differ and requires the pixels to match exactly, so a real content
  difference still fails. If you need byte-identical scanned PDFs too, the fix
  belongs in the generator (post-process the trailer after `doc.save`), which is
  outside the scope of this packaging change.

Pinning:

- Base image is pinned by digest, not by tag (Python 3.12.14 slim).
- `requirements.txt` is a hash-locked `pip-compile` output; the build installs
  it with `--require-hashes --no-deps`, so an unpinned or tampered wheel fails
  the build rather than silently changing the image.

### Regenerating requirements.txt

Edit `requirements.in` (direct dependencies only), then:

```bash
python scripts/refresh-constraints.py   # caps everything at releases >= 7 days old
./scripts/refresh-requirements.sh       # pip-compile inside the pinned base image
podman build -t localhost/loan-packet-demo:local .
./smoke-test.sh
```

`constraints.txt` exists so the lock never picks up a release published in the
last week — that window is where most supply-chain compromises are caught and
yanked.

---

## Troubleshooting

Things that actually went wrong while building this, plus the Windows failure
modes this setup is designed around.

**`podman: command not found` / `Cannot connect to Podman`**
The machine is not running. `podman machine start`. After installing Podman you
also need a fresh PowerShell window for the PATH change to apply. The wrappers
check this up front and say so rather than failing deep inside a `run`.

**A bind-mount path containing a space silently mounts nothing**
Only when the path gets re-split by the shell. Every invocation here builds the
podman command as a PowerShell *argument array* (`& podman @args`), so
`C:\Users\Jane Doe\demo` stays a single argument. If you call podman by hand,
quote the whole `-v` value: `-v "${PWD}\demo data:/data:rw"`. Both smoke tests
deliberately use a work directory named `.smoke\smoke run` to keep this covered.

**Generated files are owned by a UID you cannot delete (rootless Podman)**
Rootless Podman maps container UIDs into your subuid range, so files written to
a bind mount can land owned by an unmapped high UID. The image runs as uid/gid
1000 and the wrappers pass `--userns=keep-id:uid=1000,gid=1000`, which maps that
back onto the calling user. Podman older than 4.3 rejects the `uid=`/`gid=` form
— `Error: unrecognized namespace mode keep-id:uid=1000,gid=1000 passed` — so the
wrappers probe it, fall back to plain `--userns=keep-id`, and then to no mapping
with a warning. On Windows the mount is a Windows path surfaced through the
Podman machine VM, where ownership is synthesised by the filesystem driver, so
this problem usually does not appear at all; the flag is harmless there.

**`:Z` on the mount**
Not used. `:Z` relabels the mount for SELinux, which neither the Podman machine
nor this host runs. On a non-SELinux host it is inert at best and an error from
the OCI runtime at worst.

**`incoming\` is empty**
The 60/30/10 split assigns *whole packets*, so fewer than 10 packets rounds the
smallest split to zero. Both smoke tests refuse `-Packets` below 10 for this
reason. Not a bug in the packaging.

**`PermissionError` when writing a digest outside `/data`**
Paths like `/data/../out.json` escape the bind mount into the container's own
read-only-ish filesystem, where the `app` user cannot write. Keep output paths
inside `/data`.

**PowerShell function returns `System.Object[]` instead of an exit code**
Anything a function writes to the success stream becomes part of its return
value, so `podman`'s stdout got mixed into the exit code. The helpers pipe
podman through `Out-Host` and return `$LASTEXITCODE` alone. Worth knowing if you
extend these scripts.

**The build cannot reach PyPI**
The build needs PyPI (and the registry for the base image) and nothing else. On
a locked-down network that is the only egress to allow. An offline USB bundle
(`podman save`/`podman load`) is planned as a follow-up.

---

## What was changed in the supplied scripts

`generate_loan_packets.py` is unmodified. `upload_to_dataloop.py` keeps its
layouts, field names, ontology and existing flags; the diff is:

1. `import dtlpy as dl` moved out of module scope into a `load_dtlpy()` helper
   called by the upload path, so `--dry-run` can run with no SDK import and no
   network namespace at all.
2. `ensure_login()` takes the module as an argument, following from (1).
3. New `--dry-run` flag and `dry_run()` function: validates the data directory,
   the four metadata files, the split folders and the prelabel structure, prints
   the planned dataset/ontology/upload/prediction actions, and exits non-zero on
   any validation failure.

`verify_output.py` is new and is only used for verification; nothing in the
generate or upload path depends on it.

---

## Not verified on Windows

This was built and tested on Linux with rootless Podman 3.4.4 and PowerShell
7.4.6. **No Windows host was available**, so nothing below is confirmed:

- [ ] `podman machine init` / `start` succeed on your laptop without admin rights.
- [ ] `.\run-generate.ps1 -Packets 10` writes PDFs into `.\demo_data` and you can
      double-click one open in Edge/Acrobat.
- [ ] Run it once from a directory whose path contains a space
      (e.g. `C:\Users\<you>\My Demos\loan-packet-demo`).
- [ ] The generated files are writable/deletable from Explorer afterwards.
- [ ] `podman --version` is 4.3 or newer — otherwise expect the `keep-id:uid=...`
      fallback warning described above.
- [ ] `.\smoke-test.ps1` finishes with `SMOKE TEST PASSED`.
- [ ] `.\run-upload.ps1 -DryRun -Project "Loan-Docs-Demo"` prints the plan.

If any of these fail, the error text plus `podman version` is usually enough to
pin it down.
