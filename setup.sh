#!/usr/bin/env bash
set -euo pipefail
repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
backend=blas
skip_smoke=false
build_args=()
usage() {
  echo "Usage: $0 [--backend blas|cuda|sycl] [--jobs N] [--meson-option OPTION] [--skip-smoke]"
}
while (($#)); do
  case "$1" in
    --backend|--jobs|--meson-option)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      [[ "$1" != --backend ]] || backend="$2"
      build_args+=("$1" "$2"); shift 2 ;;
    --skip-smoke) skip_smoke=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
case "$backend" in blas|cuda|sycl) ;; *) usage >&2; exit 2 ;; esac
[[ "$(uname -s)" == Linux ]] || {
  echo "The setup wrapper supports Linux. See docs/self-hosting.md for other systems." >&2; exit 2;
}
for command_name in git python3 meson ninja pkg-config; do
  command -v "$command_name" >/dev/null || {
    echo "Missing $command_name. Install the dependencies listed in README.md." >&2; exit 2;
  }
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11 or newer is required")'
pkg-config --exists openblas || {
  echo "Install the OpenBLAS development package listed in README.md." >&2; exit 2;
}
if [[ ! -f "$repo_root/src/lc0/meson.build" ]]; then
  git -C "$repo_root" submodule update --init --recursive -- src/lc0
fi
echo "Installing the verified Double Strike Chess network..."
python3 -B "$repo_root/tools/model_manager.py" sync
"$repo_root/tools/build_engine.sh" "${build_args[@]}"
weights="$(python3 -B "$repo_root/tools/model_manager.py" path)"
runtime_backend="$backend"
[[ "$backend" != cuda ]] || runtime_backend=cuda-auto
[[ "$backend" != sycl ]] || runtime_backend=sycl-auto
if [[ "$skip_smoke" != true ]]; then
  python3 -B "$repo_root/tools/smoke_inference.py" \
    --lc0 "$repo_root/build/lc0/$backend/release/lc0" \
    --weights "$weights" --backend "$runtime_backend" --nodes 1
fi
echo "Start the site: ./run.sh --engine $backend"
