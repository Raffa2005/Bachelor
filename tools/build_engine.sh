#!/usr/bin/env bash
set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
engine_source="$repo_root/src/lc0"
backend=blas
native=true
jobs="${DM_BUILD_JOBS:-4}"
extra=()
usage() {
  echo "Usage: $0 [--backend blas|cuda|sycl] [--portable] [--jobs N] [--meson-option OPTION]"
}
while (($#)); do
  case "$1" in
    --backend|--jobs|--meson-option)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      case "$1" in
        --backend) backend="$2" ;;
        --jobs) jobs="$2" ;;
        --meson-option) extra+=("$2") ;;
      esac
      shift 2 ;;
    --portable) native=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid job count" >&2; exit 2; }
case "$backend" in blas|cuda|sycl) ;; *) usage >&2; exit 2 ;; esac
for command_name in python3 git meson ninja; do
  command -v "$command_name" >/dev/null || {
    echo "Missing build dependency: $command_name" >&2; exit 2;
  }
done
[[ -f "$engine_source/meson.build" ]] || {
  echo "Run git submodule update --init --recursive -- src/lc0" >&2; exit 2;
}
expected="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["engine"]["revision"])' "$repo_root/runtime/manifest.json")"
actual="$(git -C "$engine_source" rev-parse HEAD)"
[[ "$actual" == "$expected" ]] || {
  echo "Engine revision mismatch. Run git submodule update --init --recursive -- src/lc0" >&2
  exit 2
}
options=(
  --buildtype=release "-Dnative_arch=$native"
  -Dblas=true -Dopenblas=true -Dmkl=false -Ddnnl=false
  -Dplain_cuda=false -Dcudnn=false -Dcutlass=false -Dnvcc=false
  -Dopencl=false -Donnx=false -Dxla=false -Dtensorflow=false
  -Donednn=false -Dmetal=disabled -Daccelerate=false
  -Dispc=false -Dgtest=false -Dtrace_library=off -Dsycl=off
)
case "$backend" in
  blas) options+=(-Ddefault_backend=blas) ;;
  cuda)
    command -v nvcc >/dev/null || { echo "Add the CUDA toolkit bin directory to PATH." >&2; exit 2; }
    options+=(-Dplain_cuda=true -Dnvcc=true "-Dnative_cuda=$native" -Ddefault_backend=cuda-auto)
    ;;
  sycl)
    command -v icpx >/dev/null || { echo "Initialize the Intel oneAPI compiler and oneMKL environment first." >&2; exit 2; }
    export CC="${CC:-icx}" CXX="${CXX:-icpx}"
    options+=(-Dsycl=l0 -Ddefault_backend=sycl-auto -Db_lto=false)
    ;;
esac
build_dir="$repo_root/build/lc0/$backend/release"
if [[ -f "$build_dir/build.ninja" ]]; then
  meson setup --reconfigure "$build_dir" "$engine_source" "${options[@]}" "${extra[@]}"
else
  meson setup "$build_dir" "$engine_source" "${options[@]}" "${extra[@]}"
fi
meson compile -C "$build_dir" -j "$jobs"
echo "Built engine: $build_dir/lc0"
