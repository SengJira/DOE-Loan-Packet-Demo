# syntax-agnostic Containerfile — builds with podman build (and docker build).
#
# Base is python:3.12-slim pinned by manifest-list digest so the build is
# reproducible across hosts and architectures. To move to a newer base:
#   podman pull docker.io/library/python:3.12-slim
#   podman image inspect docker.io/library/python:3.12-slim --format '{{index .RepoDigests 0}}'
# and paste the result below.
FROM docker.io/library/python@sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79
# docker.io/library/python:3.12-slim  (Python 3.12.14, Debian trixie slim)

# uid/gid 1000 is deliberate: it matches the default user inside the Podman
# machine VM on Windows and the first human user on most Linux hosts, so
# `--userns=keep-id` lines the container user up with the host user and files
# written into the bind-mounted output directory come back owned by that user.
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/app \
    MPLCONFIGDIR=/tmp/matplotlib \
    SOURCE_DATE_EPOCH=0 \
    RL_invariant=1

# RL_invariant freezes the CreationDate/ModDate/document ID ReportLab would
# otherwise stamp from the clock, so two runs with the same --seed produce
# byte-identical digital PDFs rather than merely equivalent ones. Scanned
# (rasterised) PDFs still differ in the PDF trailer /ID, which MuPDF generates
# randomly on save; their page content is identical. See README.

RUN groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first so the layer caches independently of script edits.
# --require-hashes is implied by the hashes in requirements.txt: a tampered or
# substituted wheel fails the build instead of silently shipping.
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --no-deps -r /app/requirements.txt \
 && python -m pip check

COPY generate_loan_packets.py upload_to_dataloop.py verify_output.py /app/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /app/*.py

# Default output location. Anything mounted over it inherits the mount's
# ownership; when nothing is mounted the run still succeeds.
RUN mkdir -p /data && chown "${APP_UID}:${APP_GID}" /data
VOLUME ["/data"]

USER app
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--help"]
