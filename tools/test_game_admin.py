import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import game_admin
from tools.game_store import GameStore


class GameAdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name, "games.sqlite3")
        GameStore(self.database)

    def run_command(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            stderr
        ):
            code = game_admin.main(
                ["--database", os.fspath(self.database), *arguments]
            )
        payload = json.loads(stdout.getvalue()) if stdout.getvalue() else None
        return code, payload, stderr.getvalue()

    def test_integrity_reports_ok(self):
        code, payload, error = self.run_command("integrity")

        self.assertEqual(code, 0)
        self.assertEqual(payload, {"integrity_check": ["ok"]})
        self.assertEqual(error, "")

    def test_database_path_defaults_to_environment(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {"DM_DATABASE_PATH": os.fspath(self.database)},
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            stderr
        ):
            code = game_admin.main(["integrity"])

        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()), {"integrity_check": ["ok"]}
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_backup_creates_verified_copy_and_refuses_overwrite(self):
        destination = Path(self.temp.name, "backup", "games.sqlite3")

        code, payload, error = self.run_command(
            "backup", os.fspath(destination)
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["integrity_check"], ["ok"])
        self.assertEqual(Path(payload["path"]), destination)
        self.assertGreater(payload["size_bytes"], 0)
        self.assertEqual(error, "")
        self.assertEqual(GameStore(destination).integrity_check(), ["ok"])

        code, payload, error = self.run_command(
            "backup", os.fspath(destination)
        )
        self.assertEqual(code, 1)
        self.assertIsNone(payload)
        self.assertIn("destination already exists", error)

    def test_cleanup_requires_explicit_confirmation(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            game_admin.main(
                [
                    "--database",
                    os.fspath(self.database),
                    "cleanup",
                ]
            )

        self.assertIn("cleanup requires --confirm", stderr.getvalue())

    def test_cleanup_uses_store_retention_policy(self):
        with mock.patch.object(
            game_admin.GameStore,
            "cleanup",
            autospec=True,
            return_value={
                "abandoned_games": 0,
                "expired_sessions": 0,
                "purged_abandoned_games": 0,
            },
        ) as cleanup:
            code, payload, error = self.run_command("cleanup", "--confirm")

        self.assertEqual(code, 0)
        self.assertEqual(payload["purged_abandoned_games"], 0)
        self.assertEqual(error, "")
        cleanup.assert_called_once()

    def test_missing_database_is_not_silently_created(self):
        missing = Path(self.temp.name, "typo.sqlite3")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = game_admin.main(
                ["--database", os.fspath(missing), "integrity"]
            )

        self.assertEqual(code, 1)
        self.assertFalse(missing.exists())
        self.assertIn("database does not exist", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
