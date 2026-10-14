#!/usr/bin/env python3
"""Install and verify inference networks declared in runtime/manifest.json."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
DEFAULT_MANIFEST = REPO_ROOT / "runtime" / "manifest.json"
DEFAULT_MODELS_DIR = REPO_ROOT / "models"
GITHUB_API = "https://api.github.com"
USER_AGENT = "DoubleStrikeChess/1"
COPY_CHUNK_SIZE = 1024 * 1024


class ModelManagerError(RuntimeError):
    pass


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward a GitHub token to a signed cross-host asset URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise ModelManagerError(
                f"Refusing non-HTTPS redirect while downloading a model: {newurl}"
            )
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = urllib.parse.urlparse(req.full_url).hostname
        new_host = urllib.parse.urlparse(newurl).hostname
        if old_host != new_host:
            redirected.remove_header("Authorization")
        return redirected


URL_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def _require_text(mapping, key, context):
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ModelManagerError(f"{context}.{key} must be a non-empty string")
    return value


def _load_manifest(path):
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelManagerError(f"Cannot read manifest {path}: {exc}") from exc

    if manifest.get("schema_version") != 1:
        raise ModelManagerError("Unsupported manifest schema_version")
    models = manifest.get("models")
    if not isinstance(models, dict) or not models:
        raise ModelManagerError("Manifest must declare at least one model")
    default_model = _require_text(manifest, "default_model", "manifest")
    if default_model not in models:
        raise ModelManagerError("default_model is not present in models")

    installed = {}
    for model_id, model in models.items():
        if not isinstance(model_id, str) or not model_id:
            raise ModelManagerError("Model IDs must be non-empty strings")
        if not isinstance(model, dict):
            raise ModelManagerError(f"models.{model_id} must be an object")
        _validate_artifact(model, f"models.{model_id}")
        fallback = model.get("fallback_weights")
        if fallback is not None:
            if not isinstance(fallback, dict):
                raise ModelManagerError(f"models.{model_id}.fallback_weights must be an object")
            _validate_artifact(fallback, f"models.{model_id}.fallback_weights")
            if fallback["installed_filename"] == model["installed_filename"]:
                raise ModelManagerError("Expert and fallback must use different filenames")
        for artifact in model_artifacts(model):
            filename = artifact["installed_filename"]
            identity = (artifact["size_bytes"], artifact["sha256"].lower())
            if filename in installed and installed[filename] != identity:
                raise ModelManagerError(f"Conflicting declarations for {filename}")
            installed[filename] = identity
    return manifest


def _validate_artifact(model, context):
    filename = _require_text(model, "installed_filename", context)
    if filename != Path(filename).name or filename in (".", ".."):
        raise ModelManagerError(
            f"{context}.installed_filename must be a plain filename"
        )
    expected_hash = _require_text(model, "sha256", context).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ModelManagerError(f"{context}.sha256 must contain 64 hex characters")
    expected_size = model.get("size_bytes")
    if type(expected_size) is not int or expected_size < 1:
        raise ModelManagerError(f"{context}.size_bytes must be a positive integer")
    release = model.get("release")
    if not isinstance(release, dict):
        raise ModelManagerError(f"{context}.release must be an object")
    repository = _require_text(release, "repository", f"{context}.release")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ModelManagerError(f"{context}.release.repository is invalid")
    _require_text(release, "tag", f"{context}.release")
    asset = _require_text(release, "asset", f"{context}.release")
    if asset != Path(asset).name or asset in (".", ".."):
        raise ModelManagerError(f"{context}.release.asset must be a plain filename")


def model_artifacts(model):
    """Install the fallback first so a fresh expert is never installed alone."""
    fallback = model.get("fallback_weights")
    return (fallback, model) if fallback is not None else (model,)


def verify_bundle(model, models_dir):
    messages = []
    for artifact in model_artifacts(model):
        valid, message = verify_model(_model_path(artifact, models_dir), artifact)
        if not valid:
            return False, message
        messages.append(message)
    return True, "; ".join(messages)


def _select_model(manifest, model_id=None):
    selected = model_id or manifest["default_model"]
    try:
        return selected, manifest["models"][selected]
    except KeyError as exc:
        known = ", ".join(sorted(manifest["models"]))
        raise ModelManagerError(
            f"Unknown model {selected!r}; available models: {known}"
        ) from exc


def _model_path(model, models_dir):
    return Path(models_dir) / model["installed_filename"]


def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def verify_model(path, model):
    path = Path(path)
    if not path.is_file():
        return False, f"missing: {path}"
    size, digest = _hash_file(path)
    if size != model["size_bytes"]:
        return False, f"wrong size: expected {model['size_bytes']}, found {size}"
    if digest != model["sha256"].lower():
        return False, f"wrong SHA-256: expected {model['sha256']}, found {digest}"
    return True, f"verified: {path}"


def _discover_token():
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value.strip()

    secret_path = Path("/run/secrets/github_token")
    if secret_path.is_file():
        value = secret_path.read_text(encoding="utf-8").strip()
        if value:
            return value

    gh = shutil.which("gh")
    if gh:
        result = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def _request(url, token=None, accept="application/vnd.github+json"):
    if urllib.parse.urlparse(url).scheme != "https":
        raise ModelManagerError(f"Refusing non-HTTPS URL: {url}")
    headers = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _open(request, timeout=60):
    return URL_OPENER.open(request, timeout=timeout)


def _asset_api_url(model, token):
    release = model["release"]
    repository = release["repository"]
    tag = urllib.parse.quote(release["tag"], safe="")
    url = f"{GITHUB_API}/repos/{repository}/releases/tags/{tag}"
    try:
        with contextlib.closing(_open(_request(url, token))) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise ModelManagerError(
                "GitHub release is unavailable. Check connectivity, the release tag, "
                "and the GitHub API rate limit."
            ) from exc
        raise ModelManagerError(
            f"GitHub release lookup failed with HTTP {exc.code}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelManagerError(f"GitHub release lookup failed: {exc}") from exc

    expected_name = release["asset"]
    for asset in payload.get("assets", []):
        if asset.get("name") == expected_name:
            api_url = asset.get("url")
            if isinstance(api_url, str) and api_url.startswith(
                f"{GITHUB_API}/repos/"
            ):
                return api_url
    raise ModelManagerError(
        f"Release {release['tag']} has no asset named {expected_name}"
    )


@contextlib.contextmanager
def _installation_lock(models_dir):
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    lock_path = models_dir / ".model-manager.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _download_verified(model, destination, token):
    destination = Path(destination)
    asset_url = _asset_api_url(model, token)
    request = _request(asset_url, token, accept="application/octet-stream")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
            delete=False,
        ) as temp_handle:
            temp_path = Path(temp_handle.name)
            digest = hashlib.sha256()
            size = 0
            try:
                response_context = contextlib.closing(_open(request, timeout=300))
                with response_context as response:
                    while True:
                        chunk = response.read(COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > model["size_bytes"]:
                            raise ModelManagerError(
                                "Downloaded model exceeds the declared size"
                            )
                        digest.update(chunk)
                        temp_handle.write(chunk)
            except urllib.error.HTTPError as exc:
                raise ModelManagerError(
                    f"Model download failed with HTTP {exc.code}"
                ) from exc
            except OSError as exc:
                raise ModelManagerError(f"Model download failed: {exc}") from exc
            temp_handle.flush()
            os.fsync(temp_handle.fileno())

        if size != model["size_bytes"]:
            raise ModelManagerError(
                f"Downloaded size mismatch: expected {model['size_bytes']}, "
                f"found {size}"
            )
        actual_hash = digest.hexdigest()
        if actual_hash != model["sha256"].lower():
            raise ModelManagerError(
                f"Downloaded SHA-256 mismatch: expected {model['sha256']}, "
                f"found {actual_hash}"
            )
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def sync_model(manifest_path=DEFAULT_MANIFEST, models_dir=DEFAULT_MODELS_DIR,
               model_id=None, token=None):
    manifest = _load_manifest(manifest_path)
    selected, model = _select_model(manifest, model_id)
    destination = _model_path(model, models_dir)
    with _installation_lock(models_dir):
        downloaded = False
        download_token = token
        for artifact in model_artifacts(model):
            artifact_path = _model_path(artifact, models_dir)
            valid, _message = verify_model(artifact_path, artifact)
            if valid:
                continue
            if download_token is None:
                download_token = _discover_token()
            _download_verified(artifact, artifact_path, download_token)
            downloaded = True
        valid, message = verify_bundle(model, models_dir)
        if not valid:
            raise ModelManagerError(
                f"Internal error after verified download: {message}"
            )
        return selected, destination, message, downloaded


def _build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    parser.add_argument("--model", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync", help="Download if missing or invalid")
    subparsers.add_parser("verify", help="Verify the model and its required fallback")
    subparsers.add_parser("path", help="Print the expected installed path")
    subparsers.add_parser("fallback-path", help="Print the required fallback path, if any")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        manifest = _load_manifest(args.manifest)
        selected, model = _select_model(manifest, args.model)
        destination = _model_path(model, args.models_dir)
        if args.command == "path":
            print(destination)
            return 0
        if args.command == "fallback-path":
            fallback = model.get("fallback_weights")
            if fallback is not None:
                print(_model_path(fallback, args.models_dir))
            return 0
        if args.command == "verify":
            valid, message = verify_bundle(model, args.models_dir)
            print(f"{selected}: {message}")
            return 0 if valid else 1
        selected, destination, message, downloaded = sync_model(
            manifest_path=args.manifest,
            models_dir=args.models_dir,
            model_id=args.model,
        )
        action = "downloaded and verified" if downloaded else "already verified"
        print(f"{selected}: {action}: {destination}")
        return 0
    except ModelManagerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
