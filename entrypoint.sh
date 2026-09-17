#!/bin/sh
# Dispatches to one of the toolkit scripts. Everything after the subcommand is
# handed to that script unchanged, so the scripts' own CLI flags keep working.
set -eu

usage() {
    cat <<'EOF'
loan-packet-demo — synthetic loan document generator + Dataloop uploader

usage: <image> <command> [args...]

commands:
  generate [flags]   generate_loan_packets.py  (--packets --out --seed
                     --scan-ratio --split --error-rate)
  upload   [flags]   upload_to_dataloop.py     (--data --project --dataset
                     --splits --dry-run)
  verify   [flags]   verify_output.py — offline acceptance checks over a
                     generated directory
  python   [args]    the image's Python interpreter
  sh       [args]    a shell, for poking around

The default output directory inside the image is /data; bind-mount a host
directory there. Example:

  podman run --rm -v "$PWD/demo_data:/data:rw" <image> generate --packets 10 --out /data
EOF
}

cmd="${1:---help}"
[ $# -gt 0 ] && shift

case "$cmd" in
    generate)      exec python /app/generate_loan_packets.py "$@" ;;
    upload)        exec python /app/upload_to_dataloop.py "$@" ;;
    verify)        exec python /app/verify_output.py "$@" ;;
    python)        exec python "$@" ;;
    sh|bash|shell) exec /bin/sh "$@" ;;
    -h|--help|help) usage ;;
    *)
        echo "unknown command: $cmd" >&2
        echo >&2
        usage >&2
        exit 2
        ;;
esac
