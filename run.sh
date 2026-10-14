#!/usr/bin/env bash
set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
manifest_path="$repo_root/runtime/manifest.json"
models_dir="$repo_root/models"
engine_kind="auto"
profile="local"
model="v43-latest"
server_args=()

usage() {
  cat <<'EOF'
Usage: ./run.sh [--hosted] [--engine auto|blas|cuda|sycl] [--model NAME] [server options]

  --hosted       Use the restricted profile intended for a reverse proxy.
  --model NAME   Default model: v43-latest or installed moe1-step64000.
  --engine NAME  Select the native engine (default: auto). Auto selects
                 sycl-auto and exposes the other backends when the combined
                 SYCL build is available locally.
EOF
}

while (($#)); do
  case "$1" in
    --hosted)
      profile="hosted"
      shift
      ;;
    --engine)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      engine_kind="$2"
      shift 2
      ;;
    --model)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      model="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      server_args+=("$1")
      shift
      ;;
  esac
done

case "$engine_kind" in
  auto)
    sycl_binary="$repo_root/build/lc0/sycl/release/lc0"
    if [[ -x "$sycl_binary" ]]; then
      engine_binary="$sycl_binary"
      runtime_backend="sycl-auto"
      available_backends="sycl-auto,sycl,sycl-fp16,blas"
    else
      engine_binary="$repo_root/build/lc0/blas/release/lc0"
      runtime_backend="blas"
      available_backends="blas"
    fi
    ;;
  blas)
    engine_binary="$repo_root/build/lc0/blas/release/lc0"
    runtime_backend="blas"
    available_backends="blas"
    ;;
  cuda)
    engine_binary="$repo_root/build/lc0/cuda/release/lc0"
    runtime_backend="cuda-auto"
    available_backends="cuda-auto,cuda,cuda-fp16,blas"
    ;;
  sycl)
    engine_binary="$repo_root/build/lc0/sycl/release/lc0"
    runtime_backend="sycl-auto"
    available_backends="sycl-auto,sycl,sycl-fp16,blas"
    ;;
  *)
    echo "ERROR: --engine must be auto, blas, cuda, or sycl" >&2
    exit 2
    ;;
esac

if [[ ! -x "$engine_binary" ]]; then
  echo "ERROR: engine is not built: $engine_binary" >&2
  if [[ "$engine_kind" == "sycl" ]]; then
    echo "Run tools/build_engine.sh --backend sycl first." >&2
  else
    echo "Run ./setup.sh first." >&2
  fi
  exit 2
fi

if ! python3 -B "$repo_root/tools/model_manager.py" \
  --manifest "$manifest_path" --models-dir "$models_dir" --model "$model" verify; then
  echo "Run ./setup.sh (with --with-moe for MoE1) to install the verified networks." >&2
  exit 2
fi
weights_path="$(
  python3 -B "$repo_root/tools/model_manager.py" \
    --manifest "$manifest_path" --models-dir "$models_dir" --model "$model" path
)"

exec python3 -B "$repo_root/tools/server.py" \
  --profile "$profile" \
  --lc0 "$engine_binary" \
  --weights "$weights_path" \
  --backend "$runtime_backend" \
  --available-backends "$available_backends" \
  "${server_args[@]}"
