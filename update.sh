#!/usr/bin/env bash
set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pull="false"
setup_args=()

usage() {
  echo "Usage: ./update.sh [--pull] [--backend blas|cuda|sycl] [--jobs N] [--skip-smoke]"
}

while (($#)); do
  case "$1" in
    --pull)
      pull="true"
      shift
      ;;
    --jobs|--backend|--meson-option|--skip-smoke)
      setup_args+=("$1")
      if [[ "$1" != "--skip-smoke" ]]; then
        [[ $# -ge 2 ]] || { usage >&2; exit 2; }
        setup_args+=("$2")
        shift
      fi
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$pull" == "true" ]]; then
  if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=no)" ]]; then
    echo "ERROR: refusing to pull over tracked local changes." >&2
    echo "Commit or stash them, then retry." >&2
    exit 2
  fi
  git -C "$repo_root" pull --ff-only --recurse-submodules
  git -C "$repo_root" submodule sync --recursive
  git -C "$repo_root" submodule update --init --recursive
fi

exec "$repo_root/setup.sh" "${setup_args[@]}"
