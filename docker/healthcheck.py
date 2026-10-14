#!/usr/bin/env python3
"""Container-local readiness probe for the hosted inference service."""

import json
import os
import urllib.error
import urllib.request


def main():
    try:
        port = int(os.environ.get("DM_PORT", "8082"))
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/health",
            headers={"User-Agent": "double-move-container-healthcheck/1"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.load(response)
        ready = (
            response.status == 200
            and payload.get("status") == "ok"
            and payload.get("engine") == "ready"
            and payload.get("model") == "ready"
        )
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        ready = False
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
