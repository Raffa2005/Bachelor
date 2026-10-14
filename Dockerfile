# syntax=docker/dockerfile:1.7

FROM ubuntu:24.04 AS engine-builder

ARG DEBIAN_FRONTEND=noninteractive
ARG DM_BUILD_JOBS=4
# Portable CPU image for quick testing; prefer native builds for hosting.

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        libabsl-dev \
        libeigen3-dev \
        libopenblas-dev \
        meson \
        ninja-build \
        pkg-config \
        python3 \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/double-move

COPY runtime/manifest.json runtime/manifest.json
COPY tools/build_engine.sh tools/build_engine.sh

# Fetching the manifest-pinned tree here avoids copying a potentially dirty
# local submodule into the image. build_engine.sh validates HEAD once more.
RUN python3 - <<'PY'
import json
from pathlib import Path
import re
import subprocess

manifest = json.loads(Path("runtime/manifest.json").read_text(encoding="utf-8"))
engine = manifest["engine"]
repository = engine["repository"]
revision = engine["revision"]

if not re.fullmatch(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
    repository,
):
    raise SystemExit(f"Invalid HTTPS GitHub engine repository: {repository!r}")
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise SystemExit(f"Invalid engine revision: {revision!r}")

subprocess.run(["git", "init", "src/lc0"], check=True)
subprocess.run(
    ["git", "-C", "src/lc0", "remote", "add", "origin", repository],
    check=True,
)
subprocess.run(
    ["git", "-C", "src/lc0", "fetch", "--depth=1", "origin", revision],
    check=True,
)
subprocess.run(
    ["git", "-C", "src/lc0", "checkout", "--detach", revision],
    check=True,
)
actual = subprocess.check_output(
    ["git", "-C", "src/lc0", "rev-parse", "HEAD"],
    text=True,
).strip()
if actual != revision:
    raise SystemExit(f"Fetched engine revision {actual}, expected {revision}")
PY

RUN ./tools/build_engine.sh \
        --backend blas \
        --portable \
        --jobs "${DM_BUILD_JOBS}"

RUN set -eu; \
    engine="build/lc0/blas/release/lc0"; \
    ldd "$engine" > /tmp/lc0.ldd; \
    ! grep -q "not found" /tmp/lc0.ldd; \
    install -D -m 0755 "$engine" /out/lc0; \
    strip /out/lc0; \
    revision="$(git -C src/lc0 rev-parse HEAD)"; \
    git -C src/lc0 archive \
        --format=tar.gz \
        --prefix="lc0-${revision}/" \
        "$revision" > /out/lc0-source.tar.gz; \
    tar -czf /out/lc0-subprojects.tar.gz -C src/lc0 subprojects; \
    install -D -m 0644 src/lc0/COPYING /out/lc0-COPYING; \
    install -D -m 0755 tools/build_engine.sh /out/build_engine.sh


FROM ubuntu:24.04 AS model-fetcher

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/double-move

COPY runtime/manifest.json runtime/manifest.json
COPY tools/model_manager.py tools/model_manager.py

# Public model download, verified against the manifest checksum.
RUN     python3 tools/model_manager.py \
        --manifest runtime/manifest.json \
        --models-dir models \
        --model v43-latest \
        sync \
    && python3 tools/model_manager.py \
        --manifest runtime/manifest.json \
        --models-dir models \
        --model v43-latest \
        verify


FROM ubuntu:24.04 AS runtime

ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="Double-move chess inference" \
      org.opencontainers.image.source="https://github.com/Raffa2005/Bachelor"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        libabsl20220623t64 \
        libopenblas0-pthread \
        python3 \
        zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 doublemove \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --no-create-home \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        doublemove \
    && install -d -o 10001 -g 10001 \
        /opt/double-move/build/lc0/blas/release \
        /opt/double-move/models \
        /opt/double-move/runs \
        /opt/double-move/runtime \
        /opt/double-move/tools \
        /opt/double-move/web \
        /usr/share/doc/double-move-inference \
    && install -d -m 0700 -o 10001 -g 10001 /var/lib/double-move

WORKDIR /opt/double-move

COPY --chown=10001:10001 runtime/manifest.json runtime/manifest.json
COPY --chown=10001:10001 tools/access_admin.py tools/access_control.py tools/dm_cli.py tools/game_admin.py tools/game_store.py tools/play_service.py tools/server.py tools/uci2lichess.py tools/smoke_inference.py tools/
COPY --chown=10001:10001 --chmod=0555 access.sh access.sh
COPY --chown=10001:10001 web/ web/
COPY --chown=10001:10001 --from=engine-builder /out/lc0 build/lc0/blas/release/lc0
COPY --chown=10001:10001 --from=model-fetcher /opt/double-move/models/v43-latest.pb.gz models/v43-latest.pb.gz

COPY --chmod=0555 docker/healthcheck.py /usr/local/bin/double-move-healthcheck
COPY docker/SOURCE_AND_LICENSES.md /usr/share/doc/double-move-inference/SOURCE_AND_LICENSES.md
COPY LICENSE /usr/share/doc/double-move-inference/PROJECT-LICENSE
COPY LICENSES.md /usr/share/doc/double-move-inference/LICENSES.md
COPY MODEL_CARD.md /usr/share/doc/double-move-inference/MODEL_CARD.md
COPY --from=engine-builder /out/lc0-source.tar.gz /usr/share/doc/double-move-inference/lc0-source.tar.gz
COPY --from=engine-builder /out/lc0-subprojects.tar.gz /usr/share/doc/double-move-inference/lc0-subprojects.tar.gz
COPY --from=engine-builder /out/lc0-COPYING /usr/share/doc/double-move-inference/lc0-COPYING
COPY --from=engine-builder /out/build_engine.sh /usr/share/doc/double-move-inference/build_engine.sh

ENV DM_PROFILE=hosted \
    DM_HOST=0.0.0.0 \
    DM_PORT=8082 \
    DM_RUNS_ROOT=/opt/double-move/runs \
    DM_DATABASE_PATH=/var/lib/double-move/games.sqlite3 \
    DM_ACCESS_DATABASE_PATH=/var/lib/double-move/access.sqlite3 \
    DM_LC0_PATH=/opt/double-move/build/lc0/blas/release/lc0 \
    DM_WEIGHTS_PATH=/opt/double-move/models/v43-latest.pb.gz \
    DM_BACKEND=blas \
    DM_USE_MODULES=false \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp

USER 10001:10001

# This checks the packaged engine/model interface without assessing move quality.
RUN python3 tools/smoke_inference.py \
        --lc0 "${DM_LC0_PATH}" \
        --weights "${DM_WEIGHTS_PATH}" \
        --backend "${DM_BACKEND}" \
        --minibatch-size 1 \
        --nodes 1

EXPOSE 8082
VOLUME ["/var/lib/double-move"]
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["/usr/local/bin/double-move-healthcheck"]

CMD ["python3", "tools/server.py"]
