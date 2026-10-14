import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest

from tools import access_admin
from tools.access_control import AccessStore, MODE_INVITE_ONLY, MODE_OPEN


class AccessAdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name, "access.sqlite3")

    def run_command(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            stderr
        ):
            code = access_admin.main(
                ["--database", os.fspath(self.database), *arguments]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_mode_commands_switch_live_persistent_setting(self):
        code, output, error = self.run_command("status")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("everyone can use", output)
        self.assertEqual(AccessStore(self.database).get_mode(), MODE_OPEN)

        code, output, error = self.run_command("invite-only")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("invitation-only", output)
        self.assertEqual(
            AccessStore(self.database).get_mode(), MODE_INVITE_ONLY
        )

        code, _output, error = self.run_command("open")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(AccessStore(self.database).get_mode(), MODE_OPEN)

    def test_create_prints_secret_once_and_revoke_invalidates_it(self):
        code, output, error = self.run_command(
            "create",
            "Stream guest",
            "--origin",
            "https://chess.example.test",
        )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Uses: 5 browsers", output)
        self.assertIn("Expires: never", output)
        link = next(
            line for line in output.splitlines() if "/invite#" in line
        )
        token = link.rsplit("#", 1)[1]
        invitation_id = output.splitlines()[0].split(": ", 1)[1]

        store = AccessStore(self.database)
        session = store.redeem(token)
        store.set_mode(MODE_INVITE_ONLY)
        self.assertTrue(store.authorize(session.session_token).authorized)

        code, listed, error = self.run_command("list")
        self.assertEqual((code, error), (0, ""))
        self.assertIn(invitation_id, listed)
        self.assertIn("1/5", listed)
        self.assertNotIn(token, listed)

        code, revoked, error = self.run_command("revoke", invitation_id)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("1 browser session(s) invalidated", revoked)
        self.assertFalse(store.authorize(session.session_token).authorized)

    def test_origin_command_saves_value_for_later_links(self):
        code, output, error = self.run_command(
            "origin", "https://funnel.example.test"
        )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("saved", output)

        code, output, error = self.run_command("create", "Guest")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("https://funnel.example.test/invite#", output)

    def test_purge_revoked_removes_them_from_the_operator_list(self):
        code, output, error = self.run_command("create", "Old guest")
        self.assertEqual((code, error), (0, ""))
        invitation_id = output.splitlines()[0].split(": ", 1)[1]
        self.assertEqual(self.run_command("revoke", invitation_id)[0], 0)

        code, output, error = self.run_command("purge-revoked")

        self.assertEqual((code, error), (0, ""))
        self.assertIn("Deleted 1 revoked invitation(s)", output)
        code, listed, error = self.run_command("list")
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn(invitation_id, listed)


if __name__ == "__main__":
    unittest.main()
