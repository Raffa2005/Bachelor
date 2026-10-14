#!/usr/bin/env python3
"""Pair integrity and engine lifecycle checks for the public MoE model."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from tools import model_manager, server
from tools.routed_models import ROUTING_VERSION, model_identity, resume_identities
from tools.play_service import PublicPlayError, PublicPlayService


class MoeRuntimeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.models_dir = self.root / "models"
        self.models_dir.mkdir()
        self.contents = {"v43.pb.gz": b"single", "moe.pb.gz": b"expert", "bt3.pb.gz": b"fallback"}

        def artifact(name):
            content = self.contents[name]
            (self.models_dir / name).write_bytes(content)
            return {
                "installed_filename": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "release": {"repository": "owner/models", "tag": "pair", "asset": name},
            }

        self.single = dict(artifact("v43.pb.gz"), selectable=True)
        self.expert = dict(artifact("moe.pb.gz"), selectable=True,
                           routing_version=ROUTING_VERSION,
                           fallback_weights=artifact("bt3.pb.gz"))
        self.manifest = {
            "schema_version": 1,
            "default_model": "v43",
            "models": {"v43": self.single, "moe": self.expert},
        }
        self.manifest_path = self.root / "manifest.json"
        self.write_manifest()
        for name, value in (("MANIFEST_PATH", str(self.manifest_path)),
                            ("MODELS_DIR", str(self.models_dir)),
                            ("ENGINE", None), ("ENGINE_KEY", None),
                            ("ENGINE_CONTEXT_ID", None)):
            patcher = mock.patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        executable = self.root / "lc0"
        executable.touch()
        executable.chmod(0o755)
        self.config = replace(server._profile_defaults("hosted", {}),
                              weights_path=str(self.models_dir / "v43.pb.gz"),
                              lc0_path=str(executable), runs_root=str(self.root / "runs"))

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def runtimes(self):
        return server._resolve_public_models(self.config)[0]

    def handler(self, models=None, local=False):
        handler = object.__new__(server.BoardServer)
        handler.runtime_config = replace(self.config, restricted=False, profile="local") if local else self.config
        handler.runs_root = self.config.runs_root
        handler.model_runtimes = self.runtimes() if models is None else models
        return handler

    def test_installs_and_verifies_both_files_before_exposing_pair(self):
        for name in ("bt3.pb.gz", "moe.pb.gz"):
            (self.models_dir / name).unlink()
        downloads = []

        def download(spec, destination, token):
            self.assertEqual(token, "")
            downloads.append(spec["installed_filename"])
            destination.write_bytes(self.contents[spec["installed_filename"]])

        with mock.patch.object(model_manager, "_download_verified", side_effect=download):
            selected, destination, _message, downloaded = model_manager.sync_model(
                self.manifest_path, self.models_dir, "moe", token="")
        self.assertEqual(downloads, ["bt3.pb.gz", "moe.pb.gz"])
        self.assertEqual(selected, "moe")
        self.assertEqual(destination.name, "moe.pb.gz")
        self.assertTrue(downloaded)
        self.assertTrue(model_manager.verify_bundle(self.expert, self.models_dir)[0])

    def test_pair_verification_includes_existing_fallback(self):
        (self.models_dir / "bt3.pb.gz").write_bytes(b"bad data")
        self.assertTrue(model_manager.verify_model(self.models_dir / "moe.pb.gz", self.expert)[0])
        self.assertFalse(model_manager.verify_bundle(self.expert, self.models_dir)[0])

    def test_invalid_fallback_declarations_are_rejected(self):
        for fallback in ("/arbitrary/path", dict(self.expert["fallback_weights"], installed_filename="../bt3"),
                         dict(self.expert["fallback_weights"], sha256="invalid"),
                         dict(self.expert["fallback_weights"], installed_filename="moe.pb.gz")):
            with self.subTest(fallback=fallback):
                self.expert["fallback_weights"] = fallback
                self.write_manifest()
                with self.assertRaises(model_manager.ModelManagerError):
                    model_manager._load_manifest(self.manifest_path)

    def test_catalog_exposes_pair_but_never_fallback_alone(self):
        models, default = server._resolve_public_models(self.config)
        self.assertEqual(default, "v43.pb.gz")
        self.assertEqual([m.choice.id for m in models], ["v43.pb.gz", "moe.pb.gz"])
        self.assertIsNone(models[0].fallback)
        self.assertEqual(models[1].fallback.path, str(self.models_dir / "bt3.pb.gz"))
        self.assertEqual(models[0].choice.sha256, self.single["sha256"])
        self.assertEqual(models[1].choice.sha256, model_identity(self.expert))
        self.assertNotEqual(models[1].choice.sha256, self.expert["sha256"])

    def test_unsupported_routing_is_rejected_by_installer_and_server(self):
        for routing in (None, "other-routing"):
            with self.subTest(routing=routing):
                self.expert["routing_version"] = routing
                self.write_manifest()
                with self.assertRaises(model_manager.ModelManagerError):
                    model_manager._load_manifest(self.manifest_path)
                self.assertEqual([m.choice.id for m in self.runtimes()], ["v43.pb.gz"])
        self.single["routing_version"] = ROUTING_VERSION
        self.write_manifest()
        with self.assertRaises(OSError):
            self.runtimes()

    def test_saved_pair_identity_and_explicit_legacy_binding(self):
        old_identity = self.expert["sha256"]
        pair_identity = model_identity(self.expert)
        self.assertEqual(resume_identities(self.expert), ())
        self.expert["resume_identities"] = {old_identity: pair_identity}
        self.write_manifest()
        choice = self.runtimes()[1].choice
        service = object.__new__(PublicPlayService)
        service.backend = "blas"
        service._compatibility_model_ids = set()
        service._models_by_id = {choice.id: choice}
        for identity in (old_identity, pair_identity):
            game = {"model": {"id": "old-name", "sha256": identity}, "backend": "blas"}
            self.assertEqual(service._game_engine_config(game), (choice.id, "blas"))
        # Keeping the expert and old binding while replacing BT3 must not resume old games.
        content = b"different fallback"
        (self.models_dir / "bt3.pb.gz").write_bytes(content)
        self.expert["fallback_weights"].update(size_bytes=len(content),
                                              sha256=hashlib.sha256(content).hexdigest())
        self.write_manifest()
        changed = self.runtimes()[1].choice
        self.assertNotEqual(changed.sha256, pair_identity)
        self.assertEqual(changed.compatibility_identities, ())
        service._models_by_id = {changed.id: changed}
        for identity in (old_identity, pair_identity):
            game = {"model": {"id": changed.id, "sha256": identity}, "backend": "blas"}
            with self.assertRaises(PublicPlayError):
                service._game_engine_config(game)

    def test_menus_expose_verified_pair_identity(self):
        for local in (False, True):
            with self.subTest(local=local):
                handler = self.handler(local=local)
                handler._send_json = mock.Mock()
                handler._handle_models()
                menu = handler._send_json.call_args.args[0]["models"]
                self.assertEqual([m["identity"] for m in menu],
                                 [self.single["sha256"], model_identity(self.expert)])

    def test_fallback_cannot_overwrite_another_declared_model(self):
        self.expert["fallback_weights"]["installed_filename"] = "v43.pb.gz"
        self.write_manifest()
        with self.assertRaisesRegex(model_manager.ModelManagerError, "Conflicting"):
            model_manager._load_manifest(self.manifest_path)

    def test_missing_or_corrupt_fallback_hides_optional_expert(self):
        fallback = self.models_dir / "bt3.pb.gz"
        fallback.unlink()
        self.assertEqual([m.choice.id for m in self.runtimes()], ["v43.pb.gz"])
        fallback.write_bytes(b"bad data")
        self.assertEqual([m.choice.id for m in self.runtimes()], ["v43.pb.gz"])

    def test_selected_expert_without_fallback_fails_closed(self):
        self.config = replace(self.config, weights_path=str(self.models_dir / "moe.pb.gz"))
        (self.models_dir / "bt3.pb.gz").unlink()
        with self.assertRaises(OSError):
            self.runtimes()
        with self.assertRaises(server.RequestError), mock.patch.object(server, "Lc0") as launch:
            self.handler(models=())._get_engine({})
        launch.assert_not_called()

    def test_explicit_expert_default_preserves_single_network_identifier(self):
        self.config = replace(self.config, weights_path=str(self.models_dir / "moe.pb.gz"))
        models, default = server._resolve_public_models(self.config)
        self.assertEqual(default, "moe.pb.gz")
        self.assertEqual([m.choice.id for m in models], ["moe.pb.gz", "v43.pb.gz"])

    def test_renamed_expert_still_requires_manifest_fallback(self):
        renamed = self.root / "renamed.pb.gz"
        renamed.write_bytes(self.contents["moe.pb.gz"])
        handler = self.handler(local=True)
        (self.models_dir / "bt3.pb.gz").unlink()
        with self.assertRaises(server.RequestError), mock.patch.object(server, "Lc0") as launch:
            handler._get_engine({"weights": str(renamed)})
        launch.assert_not_called()

    def test_hosted_requests_cannot_override_fallback_or_select_it(self):
        handler = self.handler()
        for body in ({"fallback_weights": "/tmp/other.pb.gz"},
                     {"options": {"FallbackWeights": "/tmp/other.pb.gz"}},
                     {"model_id": "bt3.pb.gz"}):
            with self.subTest(body=body), self.assertRaises(server.RequestError):
                handler._get_engine(body)

    def test_analysis_reuses_pair_and_relaunches_on_model_switch(self):
        handler = self.handler()
        with mock.patch.object(server, "Lc0", side_effect=lambda *a, **kw: mock.Mock(proc=None)) as launch:
            first = handler._engine_for_context({"model_id": "moe.pb.gz"}, "a" * 16)
            self.assertEqual(launch.call_args.kwargs["fallback_weights"], str(self.models_dir / "bt3.pb.gz"))
            self.assertIs(handler._engine_for_context({"model_id": "moe.pb.gz"}, "a" * 16), first)
            self.assertEqual(launch.call_count, 1)
            handler._engine_for_context({"model_id": "v43.pb.gz"}, "a" * 16)
            self.assertIsNone(launch.call_args.kwargs["fallback_weights"])
            first.quit.assert_called_once()
            handler._engine_for_context({"model_id": "moe.pb.gz"}, "a" * 16)
            self.assertEqual(launch.call_count, 3)
            self.assertEqual(launch.call_args.kwargs["fallback_weights"], str(self.models_dir / "bt3.pb.gz"))

    def test_play_and_analysis_share_the_same_pair_configuration(self):
        models = self.runtimes()
        sessions = server._PublicPlayEngineSessions(self.config, models)
        handler = self.handler(models)
        with mock.patch.object(server, "Lc0", return_value=mock.Mock(proc=None)) as launch:
            with sessions("public:game", "moe.pb.gz", "blas") as first:
                self.assertEqual(launch.call_args.kwargs["fallback_weights"], models[1].fallback.path)
            with sessions("public:game", "moe.pb.gz", "blas") as second:
                self.assertIs(first, second)
            first.new_game.assert_not_called()
            self.assertIs(handler._engine_for_context({"model_id": "moe.pb.gz"}, "a" * 16), first)
            first.new_game.assert_called_once()
            self.assertEqual(launch.call_count, 1)

    def test_changed_pair_is_rejected_before_reusing_engine(self):
        for filename in ("moe.pb.gz", "bt3.pb.gz"):
            with self.subTest(filename=filename):
                handler = self.handler()
                target = self.models_dir / filename
                metadata = target.stat()
                with mock.patch.object(server, "Lc0", return_value=mock.Mock(proc=None)):
                    handler._get_engine({"model_id": "moe.pb.gz"})
                    # Some filesystems update ctime at coarser than nanosecond resolution.
                    time.sleep(0.02)
                    target.write_bytes(b"x" * metadata.st_size)
                    os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                    with self.assertRaises(server.RequestError):
                        handler._get_engine({"model_id": "moe.pb.gz"})
                    sessions = server._PublicPlayEngineSessions(self.config, handler.model_runtimes)
                    with self.assertRaises(RuntimeError):
                        with sessions("public:game", "moe.pb.gz", "blas"):
                            self.fail("changed pair was reused")
                target.write_bytes(self.contents[filename])

    def test_local_analysis_lists_and_selects_installed_pair(self):
        handler = self.handler(local=True)
        self.assertEqual([m["path"] for m in handler._discover_weights()],
                         [str(self.models_dir / "v43.pb.gz"), str(self.models_dir / "moe.pb.gz")])
        with mock.patch.object(server, "Lc0", return_value=mock.Mock(proc=None)) as launch:
            handler._get_engine({"weights": str(self.models_dir / "moe.pb.gz")})
            self.assertEqual(launch.call_args.kwargs["fallback_weights"], str(self.models_dir / "bt3.pb.gz"))

    def test_health_requires_default_experts_fallback(self):
        self.config = replace(self.config, weights_path=str(self.models_dir / "moe.pb.gz"))
        handler = self.handler()
        handler.play_service = mock.Mock()
        handler.play_service.store.health_check.return_value = True
        handler.access_store = mock.Mock()
        handler.access_store.health_check.return_value = True
        handler._send_json = mock.Mock()
        handler._handle_health()
        self.assertEqual(handler._send_json.call_args.args[1], 200)
        (self.models_dir / "bt3.pb.gz").unlink()
        handler._handle_health()
        self.assertEqual(handler._send_json.call_args.args[1], 503)


if __name__ == "__main__":
    unittest.main()
