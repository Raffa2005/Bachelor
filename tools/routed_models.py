"""Stable identities for position-routed model pairs."""

import hashlib
import json
from pathlib import Path
import re


ROUTING_VERSION = "double-strike-moe1-v1"


def _valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def fallback_specification(model):
    fallback = model.get("fallback_weights")
    routing = model.get("routing_version")
    if fallback is None:
        if routing is not None:
            raise ValueError("routing_version requires fallback_weights")
        return None
    if not isinstance(fallback, dict) or routing != ROUTING_VERSION:
        raise ValueError("unsupported routed model configuration")
    filename = fallback.get("installed_filename")
    size = fallback.get("size_bytes")
    if (not isinstance(filename, str) or not filename
            or filename != Path(filename).name or filename in (".", "..")
            or filename == model.get("installed_filename")
            or type(size) is not int or size < 1
            or not _valid_hash(fallback.get("sha256"))):
        raise ValueError("invalid fallback artifact specification")
    return fallback


def model_identity(model):
    """Single networks keep their file hash; pairs include routing semantics."""
    if not _valid_hash(model.get("sha256")):
        raise ValueError("invalid model SHA-256")
    fallback = fallback_specification(model)
    if fallback is None:
        return model["sha256"]
    payload = {
        "schema": "double-strike-routed-model-identity-v1",
        "variant_sha256": model["sha256"],
        "fallback_sha256": fallback["sha256"],
        "routing_version": model["routing_version"],
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("ascii")).hexdigest()


def resume_identities(model):
    """Accept old saved identities only when explicitly bound to this pair."""
    identity = model_identity(model)
    bindings = model.get("resume_identities", {})
    if (not isinstance(bindings, dict)
            or any(not _valid_hash(old) or not _valid_hash(target)
                   for old, target in bindings.items())):
        raise ValueError("invalid saved-model identity bindings")
    return tuple(old for old, target in bindings.items() if target == identity)
