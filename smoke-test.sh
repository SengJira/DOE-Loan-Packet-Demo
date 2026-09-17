#!/usr/bin/env bash
# smoke-test.sh — Linux/CI equivalent of smoke-test.ps1.
#
# Runs the whole chain end to end against the container image and exits
# non-zero on any failure. No network access is needed beyond the image build.
#
#   ./smoke-test.sh                 # build if missing, then test
#   IMAGE=loan-packet-demo:ci ./smoke-test.sh
#   PACKETS=10 SEED=7 ./smoke-test.sh
set -uo pipefail

IMAGE="${IMAGE:-localhost/loan-packet-demo:local}"
PACKETS="${PACKETS:-10}"
SEED="${SEED:-7}"
ENGINE="${ENGINE:-podman}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# A directory with a space in the name, because that is the mount case that
# breaks most often.
WORK="${WORK:-$ROOT/.smoke/smoke run}"

# The generator's default 60/30/10 split assigns whole packets, so fewer than
# 10 packets leaves `incoming` empty and the layout check below would fail for
# a reason that has nothing to do with packaging.
if [ "$PACKETS" -lt 10 ]; then
    echo "PACKETS must be at least 10: the default 60/30/10 split gives the incoming split zero packets below that." >&2
    exit 2
fi

FAILED=0
STEP=0

step()  { STEP=$((STEP + 1)); printf '\n=== [%d] %s\n' "$STEP" "$1"; }
pass()  { printf '    PASS  %s\n' "$1"; }
fail()  { printf '    FAIL  %s\n' "$1" >&2; FAILED=$((FAILED + 1)); }
check() { if [ "$1" -eq 0 ]; then pass "$2"; else fail "$2"; fi; }

# keep-id lines the container user up with the invoking user so generated files
# come back owned by that user instead of an unmapped subuid.
USERNS_FLAG="--userns=keep-id"
if ! $ENGINE run --rm $USERNS_FLAG "$IMAGE" python -c "pass" >/dev/null 2>&1; then
    USERNS_FLAG=""
fi

run_image() {
    local mount="$1"; shift
    # shellcheck disable=SC2086
    $ENGINE run --rm $USERNS_FLAG -v "$mount" "$IMAGE" "$@"
}

printf 'image   : %s\nengine  : %s\nworkdir : %s\npackets : %s (seed %s)\n' \
    "$IMAGE" "$ENGINE" "$WORK" "$PACKETS" "$SEED"

step "build the image if it is not present"
if $ENGINE image exists "$IMAGE"; then
    pass "image $IMAGE already present"
else
    $ENGINE build -t "$IMAGE" -f "$ROOT/Containerfile" "$ROOT"
    check $? "podman build"
fi

step "clean the work directory"
rm -rf "$WORK"
mkdir -p "$WORK/run_a" "$WORK/run_b" "$WORK/run_c" "$WORK/empty"
check $? "created $WORK"

step "generate run A ($PACKETS packets, seed $SEED)"
run_image "$WORK/run_a:/data:rw" generate --packets "$PACKETS" --out /data --seed "$SEED"
check $? "generator exited 0"

step "generate run B (same seed)"
run_image "$WORK/run_b:/data:rw" generate --packets "$PACKETS" --out /data --seed "$SEED"
check $? "generator exited 0"

step "generate run C (seed $((SEED + 1)), control for the comparison)"
run_image "$WORK/run_c:/data:rw" generate --packets "$PACKETS" --out /data --seed "$((SEED + 1))"
check $? "generator exited 0"

step "output layout and metadata files"
for split in base unlabeled incoming; do
    n=$(find "$WORK/run_a/$split" -name '*.pdf' 2>/dev/null | wc -l)
    if [ "$n" -gt 0 ]; then pass "$split/ contains $n PDFs"; else fail "$split/ has no PDFs"; fi
done
for f in ground_truth.json prelabels.json dataloop_metadata.json manifest.csv; do
    if [ -s "$WORK/run_a/$f" ]; then pass "$f present"; else fail "$f missing or empty"; fi
done

step "host user can read the generated files"
first_pdf=$(find "$WORK/run_a" -name '*.pdf' | sort | head -1)
if [ -r "$first_pdf" ] && head -c 5 "$first_pdf" | grep -q '%PDF'; then
    pass "readable as $(id -un): $(ls -l "$first_pdf" | awk '{print $3":"$4, $1}')"
else
    fail "cannot read $first_pdf as the host user"
fi

step "verify run A (PDFs open, text layers match the scanned flag, manifest count)"
run_image "$WORK/run_a:/data:rw" verify --data /data --emit-digest /data/digest.json
check $? "verify_output.py on run A"

step "verify run B"
run_image "$WORK/run_b:/data:rw" verify --data /data --emit-digest /data/digest.json
check $? "verify_output.py on run B"

step "verify run C"
run_image "$WORK/run_c:/data:rw" verify --data /data --emit-digest /data/digest.json
check $? "verify_output.py on run C"

step "same seed => identical file list and identical ground_truth.json"
run_image "$WORK:/data:rw" verify --compare /data/run_a/digest.json /data/run_b/digest.json
check $? "run A and run B are identical"

step "same seed => PDF bytes match (digital exactly; scanned modulo the random trailer /ID)"
run_image "$WORK:/data:rw" verify --compare-trees /data/run_a /data/run_b
check $? "PDF content identical across runs"

step "different seed => different output (proves the comparison is meaningful)"
run_image "$WORK:/data:rw" verify --compare /data/run_a/digest.json /data/run_c/digest.json
if [ $? -ne 0 ]; then pass "run A and run C differ, as expected"; else fail "seed $SEED and seed $((SEED + 1)) produced identical output"; fi

step "uploader --dry-run with networking disabled"
# shellcheck disable=SC2086
$ENGINE run --rm --network=none $USERNS_FLAG -v "$WORK/run_a:/data:ro" "$IMAGE" \
    upload --data /data --project "Loan-Docs-Demo" --dry-run
check $? "dry run succeeded with --network=none"

step "uploader --dry-run fails on an unusable data directory"
# shellcheck disable=SC2086
$ENGINE run --rm --network=none $USERNS_FLAG -v "$WORK/empty:/data:ro" "$IMAGE" \
    upload --data /data --dry-run >/dev/null 2>&1
if [ $? -ne 0 ]; then pass "empty directory rejected with a non-zero exit"; else fail "empty directory was accepted"; fi

step "no credentials baked into the image"
if $ENGINE run --rm "$IMAGE" sh -c 'ls -A /home/app; ls /app' | grep -Eqi 'token|\.env|credential'; then
    fail "something credential-shaped is present in the image"
else
    pass "no token/.env/credential files in the image"
fi

printf '\n=========================================\n'
if [ "$FAILED" -eq 0 ]; then
    printf 'SMOKE TEST PASSED (%d steps)\n' "$STEP"
    exit 0
fi
printf 'SMOKE TEST FAILED: %d check(s) failed\n' "$FAILED" >&2
exit 1
