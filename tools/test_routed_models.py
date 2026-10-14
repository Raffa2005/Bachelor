import copy
import json
from pathlib import Path
import unittest

from tools.routed_models import model_identity, resume_identities


class RoutedIdentityTest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((Path(__file__).resolve().parents[1]
                                    / "runtime/manifest.json").read_text())
        self.pair = self.manifest["models"]["moe1-step64000"]

    def test_public_pair_matches_reference_identity(self):
        self.assertEqual(model_identity(self.pair),
                         "254b0e36ee46242524093436a8071797bf31775257afbd9aabc4bc8fae635c2c")
        self.assertEqual(resume_identities(self.pair), (self.pair["sha256"],))
        self.assertEqual(self.manifest["default_model"], "v43-latest")
        self.assertEqual(set(self.manifest["models"]), {"v43-latest", "moe1-step64000"})
        for artifact in (self.pair, self.pair["fallback_weights"]):
            self.assertEqual(artifact["release"]["tag"], "moe1-step64000")
        single = self.manifest["models"]["v43-latest"]
        self.assertEqual(model_identity(single), single["sha256"])

    def test_identity_does_not_depend_on_installed_name_or_release_url(self):
        renamed = copy.deepcopy(self.pair)
        renamed["installed_filename"] = "expert.pb.gz"
        renamed["fallback_weights"]["installed_filename"] = "fallback.pb.gz"
        renamed["release"]["tag"] = "different-location"
        self.assertEqual(model_identity(renamed), model_identity(self.pair))

    def test_malformed_hashes_and_resume_bindings_are_rejected(self):
        for value in (False, 123, [], "x" * 64):
            with self.subTest(value=value):
                changed = copy.deepcopy(self.pair)
                changed["fallback_weights"]["sha256"] = value
                with self.assertRaises(ValueError):
                    model_identity(changed)
                changed = dict(self.pair, resume_identities={"old": value})
                with self.assertRaises(ValueError):
                    resume_identities(changed)
