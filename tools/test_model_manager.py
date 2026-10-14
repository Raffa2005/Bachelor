#!/usr/bin/env python3

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from tools import model_manager


class FakeResponse(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)


def write_manifest(root, content=b"network", filename="test.pb.gz"):
    manifest = {
        "schema_version": 1,
        "default_model": "test",
        "models": {
            "test": {
                "release": {
                    "repository": "owner/repository",
                    "tag": "test-tag",
                    "asset": "source.pb.gz",
                },
                "installed_filename": filename,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
    }
    path = Path(root) / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class ModelManifestTest(unittest.TestCase):
    def test_rejects_traversing_install_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_manifest(
                temp_dir, filename="../outside.pb.gz"
            )
            with self.assertRaisesRegex(
                model_manager.ModelManagerError, "plain filename"
            ):
                model_manager._load_manifest(manifest_path)

    def test_rejects_invalid_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = write_manifest(temp_dir)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["models"]["test"]["sha256"] = "not-a-hash"
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                model_manager.ModelManagerError, "64 hex characters"
            ):
                model_manager._load_manifest(manifest_path)


class SafeRedirectTest(unittest.TestCase):
    def test_strips_authorization_on_cross_host_redirect(self):
        handler = model_manager._SafeRedirectHandler()
        request = urllib.request.Request(
            "https://api.github.com/repos/owner/repository/releases/assets/1",
            headers={"Authorization": "Bearer secret"},
        )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://release-assets.githubusercontent.com/network.pb.gz",
        )

        self.assertIsNotNone(redirected)
        self.assertFalse(redirected.has_header("Authorization"))

    def test_rejects_non_https_redirect(self):
        handler = model_manager._SafeRedirectHandler()
        request = urllib.request.Request(
            "https://api.github.com/repos/owner/repository/releases/assets/1"
        )

        with self.assertRaisesRegex(
            model_manager.ModelManagerError, "non-HTTPS redirect"
        ):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://downloads.example/network.pb.gz",
            )


class ModelSyncTest(unittest.TestCase):
    def fake_open(self, content):
        release = json.dumps(
            {
                "assets": [
                    {
                        "name": "source.pb.gz",
                        "url": (
                            "https://api.github.com/repos/owner/repository/"
                            "releases/assets/1"
                        ),
                    }
                ]
            }
        ).encode()

        def open_request(request, timeout=60):
            del timeout
            if request.full_url.endswith("/releases/tags/test-tag"):
                return FakeResponse(release)
            if request.full_url.endswith("/releases/assets/1"):
                return FakeResponse(content)
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        return open_request

    def test_downloads_and_atomically_installs_valid_model(self):
        content = b"valid-network"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_manifest(root, content=content)
            models_dir = root / "models"
            with mock.patch.object(
                model_manager, "_open", side_effect=self.fake_open(content)
            ):
                selected, path, _message, downloaded = (
                    model_manager.sync_model(
                        manifest_path=manifest_path,
                        models_dir=models_dir,
                        token="test-token",
                    )
                )

            self.assertEqual(selected, "test")
            self.assertTrue(downloaded)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(list(models_dir.glob("*.part")), [])

    def test_existing_valid_model_skips_network(self):
        content = b"valid-network"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_manifest(root, content=content)
            models_dir = root / "models"
            models_dir.mkdir()
            (models_dir / "test.pb.gz").write_bytes(content)

            with mock.patch.object(
                model_manager,
                "_open",
                side_effect=AssertionError("network must not be used"),
            ):
                _selected, path, _message, downloaded = (
                    model_manager.sync_model(
                        manifest_path=manifest_path,
                        models_dir=models_dir,
                    )
                )

            self.assertFalse(downloaded)
            self.assertEqual(path.read_bytes(), content)

    def test_bad_download_preserves_existing_file(self):
        expected = b"expected-network"
        existing = b"previous-file"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_manifest(root, content=expected)
            models_dir = root / "models"
            models_dir.mkdir()
            destination = models_dir / "test.pb.gz"
            destination.write_bytes(existing)

            with mock.patch.object(
                model_manager,
                "_open",
                side_effect=self.fake_open(b"corrupt-network"),
            ):
                with self.assertRaises(model_manager.ModelManagerError):
                    model_manager.sync_model(
                        manifest_path=manifest_path,
                        models_dir=models_dir,
                        token="test-token",
                    )

            self.assertEqual(destination.read_bytes(), existing)
            self.assertEqual(list(models_dir.glob("*.part")), [])

    def test_short_download_is_rejected(self):
        expected = b"expected-network"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_manifest(root, content=expected)
            with mock.patch.object(
                model_manager,
                "_open",
                side_effect=self.fake_open(b"short"),
            ):
                with self.assertRaisesRegex(
                    model_manager.ModelManagerError, "size mismatch"
                ):
                    model_manager.sync_model(
                        manifest_path=manifest_path,
                        models_dir=root / "models",
                        token="test-token",
                    )

    def test_concurrent_syncs_share_one_locked_download(self):
        content = b"valid-network"
        release = json.dumps(
            {
                "assets": [
                    {
                        "name": "source.pb.gz",
                        "url": (
                            "https://api.github.com/repos/owner/repository/"
                            "releases/assets/1"
                        ),
                    }
                ]
            }
        ).encode()
        download_started = threading.Event()
        allow_download = threading.Event()
        both_lock_attempts_started = threading.Event()
        worker_barrier = threading.Barrier(2)
        opened_urls = []
        opened_urls_lock = threading.Lock()
        lock_attempt_count = 0
        lock_attempt_count_lock = threading.Lock()
        original_installation_lock = model_manager._installation_lock

        class BlockingResponse(FakeResponse):
            def __init__(self, payload):
                super().__init__(payload)
                self.first_read = True

            def read(self, size=-1):
                if self.first_read:
                    self.first_read = False
                    download_started.set()
                    if not allow_download.wait(timeout=5):
                        raise AssertionError("test did not release model download")
                return super().read(size)

        def open_request(request, timeout=60):
            del timeout
            with opened_urls_lock:
                opened_urls.append(request.full_url)
            if request.full_url.endswith("/releases/tags/test-tag"):
                return FakeResponse(release)
            if request.full_url.endswith("/releases/assets/1"):
                return BlockingResponse(content)
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        def observed_installation_lock(models_dir):
            nonlocal lock_attempt_count
            with lock_attempt_count_lock:
                lock_attempt_count += 1
                if lock_attempt_count == 2:
                    both_lock_attempts_started.set()
            return original_installation_lock(models_dir)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_manifest(root, content=content)
            models_dir = root / "models"

            def sync():
                worker_barrier.wait(timeout=5)
                return model_manager.sync_model(
                    manifest_path=manifest_path,
                    models_dir=models_dir,
                    token="test-token",
                )

            with mock.patch.object(
                model_manager, "_open", side_effect=open_request
            ), mock.patch.object(
                model_manager,
                "_installation_lock",
                side_effect=observed_installation_lock,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(sync) for _ in range(2)]
                    self.assertTrue(both_lock_attempts_started.wait(timeout=5))
                    self.assertTrue(download_started.wait(timeout=5))
                    allow_download.set()
                    results = [future.result(timeout=5) for future in futures]

            downloaded = sorted(result[3] for result in results)
            self.assertEqual(downloaded, [False, True])
            self.assertEqual(
                sum(
                    url.endswith("/releases/tags/test-tag")
                    for url in opened_urls
                ),
                1,
            )
            self.assertEqual(
                sum(
                    url.endswith("/releases/assets/1")
                    for url in opened_urls
                ),
                1,
            )
            self.assertEqual((models_dir / "test.pb.gz").read_bytes(), content)

    def test_midstream_oserror_preserves_destination_and_removes_part(self):
        expected = b"complete-network"
        existing = b"previous-file"
        release = json.dumps(
            {
                "assets": [
                    {
                        "name": "source.pb.gz",
                        "url": (
                            "https://api.github.com/repos/owner/repository/"
                            "releases/assets/1"
                        ),
                    }
                ]
            }
        ).encode()

        class FailingResponse(FakeResponse):
            def __init__(self):
                super().__init__(b"")
                self.read_count = 0

            def read(self, size=-1):
                del size
                self.read_count += 1
                if self.read_count == 1:
                    return b"partial"
                raise OSError("connection interrupted")

        def open_request(request, timeout=60):
            del timeout
            if request.full_url.endswith("/releases/tags/test-tag"):
                return FakeResponse(release)
            if request.full_url.endswith("/releases/assets/1"):
                return FailingResponse()
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path = write_manifest(root, content=expected)
            models_dir = root / "models"
            models_dir.mkdir()
            destination = models_dir / "test.pb.gz"
            destination.write_bytes(existing)

            with mock.patch.object(
                model_manager, "_open", side_effect=open_request
            ):
                with self.assertRaisesRegex(
                    model_manager.ModelManagerError,
                    "Model download failed: connection interrupted",
                ):
                    model_manager.sync_model(
                        manifest_path=manifest_path,
                        models_dir=models_dir,
                        token="test-token",
                    )

            self.assertEqual(destination.read_bytes(), existing)
            self.assertEqual(
                [
                    path
                    for path in models_dir.iterdir()
                    if path.name.endswith(".part")
                ],
                [],
            )


if __name__ == "__main__":
    unittest.main()
