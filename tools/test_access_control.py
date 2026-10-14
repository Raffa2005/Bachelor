#!/usr/bin/env python3

import concurrent.futures
import os
import sqlite3
import tempfile
import unittest

from tools.access_control import (
    ACCESS_COOKIE,
    AccessStore,
    InvitationLimitError,
    InvalidInvitationError,
    InvalidTransferError,
    MODE_INVITE_ONLY,
    MODE_OPEN,
    default_access_database_path,
)


class AccessStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "access.sqlite3")
        self.now = [1_700_000_000]
        self.store = AccessStore(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_store_is_open_and_path_tracks_game_database(self):
        self.assertEqual(self.store.get_mode(), MODE_OPEN)
        self.assertEqual(
            default_access_database_path(
                {"DM_DATABASE_PATH": "/srv/double-move/games.sqlite3"}
            ),
            "/srv/double-move/access.sqlite3",
        )
        self.assertEqual(
            default_access_database_path(
                {"DM_ACCESS_DATABASE_PATH": "/private/invites.sqlite3"}
            ),
            "/private/invites.sqlite3",
        )
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_invite_only_authorizes_one_of_five_redeemed_browsers(self):
        invitation = self.store.create_invitation("Recipient")
        self.store.set_mode(MODE_INVITE_ONLY)
        self.assertFalse(self.store.authorize(None).authorized)

        redemptions = [
            self.store.redeem(invitation.token) for _index in range(5)
        ]
        self.assertTrue(
            self.store.authorize(redemptions[0].session_token).authorized
        )
        self.assertEqual(redemptions[-1].remaining_redemptions, 0)
        with self.assertRaises(InvitationLimitError):
            self.store.redeem(invitation.token)

        listed = self.store.list_invitations()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["redemption_count"], 5)
        self.assertEqual(listed[0]["remaining_redemptions"], 0)

    def test_same_browser_reopening_link_does_not_consume_again(self):
        invitation = self.store.create_invitation()
        first = self.store.redeem(invitation.token)
        second = self.store.redeem(
            invitation.token,
            first.session_token,
        )
        self.assertFalse(second.created)
        self.assertEqual(second.session_token, first.session_token)
        self.assertEqual(second.remaining_redemptions, 4)
        self.assertEqual(
            self.store.list_invitations()[0]["redemption_count"], 1
        )

    def test_invitation_and_session_do_not_expire_with_time(self):
        invitation = self.store.create_invitation()
        redemption = self.store.redeem(invitation.token)
        self.store.set_mode(MODE_INVITE_ONLY)
        self.now[0] += 25 * 365 * 24 * 60 * 60

        self.assertTrue(
            self.store.authorize(redemption.session_token).authorized
        )
        another = self.store.redeem(invitation.token)
        self.assertTrue(another.created)

    def test_session_transfer_is_one_use_and_does_not_consume_invitation(self):
        invitation = self.store.create_invitation()
        source = self.store.redeem(invitation.token)
        grant = self.store.create_transfer(source.session_token)

        transferred = self.store.redeem_transfer(grant.token)

        self.assertTrue(transferred.created)
        self.assertNotEqual(transferred.session_token, source.session_token)
        self.assertTrue(
            self.store.authorize(transferred.session_token).authorized
        )
        self.assertEqual(transferred.remaining_redemptions, 4)
        self.assertEqual(
            self.store.list_invitations()[0]["redemption_count"], 1
        )
        with self.assertRaises(InvalidTransferError):
            self.store.redeem_transfer(grant.token)

    def test_session_transfer_expires_and_obeys_invitation_revocation(self):
        invitation = self.store.create_invitation()
        source = self.store.redeem(invitation.token)
        expired = self.store.create_transfer(source.session_token)
        self.now[0] = expired.expires_at
        with self.assertRaises(InvalidTransferError):
            self.store.redeem_transfer(expired.token)

        self.now[0] += 1
        live = self.store.create_transfer(source.session_token)
        self.store.revoke_invitation(invitation.id)
        with self.assertRaises(InvalidTransferError):
            self.store.redeem_transfer(live.token)

    def test_schema_one_is_migrated_with_transfer_storage(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE access_transfers")
            connection.execute("PRAGMA user_version = 1")

        migrated = AccessStore(self.path, clock=lambda: self.now[0])

        self.assertTrue(migrated.health_check())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                2,
            )

    def test_revocation_immediately_invalidates_every_issued_session(self):
        invitation = self.store.create_invitation()
        sessions = [
            self.store.redeem(invitation.token).session_token
            for _index in range(2)
        ]
        self.store.set_mode(MODE_INVITE_ONLY)

        result = self.store.revoke_invitation(invitation.id)
        self.assertEqual(result["sessions_revoked"], 2)
        self.assertTrue(all(
            not self.store.authorize(token).authorized for token in sessions
        ))
        with self.assertRaises(InvalidInvitationError):
            self.store.redeem(invitation.token)

    def test_purge_removes_only_revoked_invitations_and_their_records(self):
        revoked = self.store.create_invitation("Revoked")
        revoked_session = self.store.redeem(revoked.token)
        self.store.create_transfer(revoked_session.session_token)
        active = self.store.create_invitation("Active")
        active_session = self.store.redeem(active.token)
        self.store.create_transfer(active_session.session_token)
        self.store.revoke_invitation(revoked.id)

        result = self.store.purge_revoked_invitations()

        self.assertEqual(result, {
            "invitations_deleted": 1,
            "sessions_deleted": 1,
            "transfers_deleted": 1,
        })
        self.assertEqual(
            [row["id"] for row in self.store.list_invitations()],
            [active.id],
        )
        self.assertTrue(
            self.store.authorize(active_session.session_token).authorized
        )
        self.assertEqual(
            self.store.purge_revoked_invitations(),
            {
                "invitations_deleted": 0,
                "sessions_deleted": 0,
                "transfers_deleted": 0,
            },
        )

    def test_open_mode_allows_everyone_even_after_revocation(self):
        invitation = self.store.create_invitation()
        session = self.store.redeem(invitation.token)
        self.store.revoke_invitation(invitation.id)

        self.assertTrue(self.store.authorize(None).authorized)
        self.assertTrue(self.store.authorize(session.session_token).authorized)

    def test_secrets_are_not_stored_in_plaintext(self):
        invitation = self.store.create_invitation()
        redemption = self.store.redeem(invitation.token)
        transfer = self.store.create_transfer(redemption.session_token)

        with open(self.path, "rb") as database:
            contents = database.read()
        self.assertNotIn(invitation.token.encode("ascii"), contents)
        self.assertNotIn(redemption.session_token.encode("ascii"), contents)
        self.assertNotIn(transfer.token.encode("ascii"), contents)

    def test_live_store_does_not_recreate_a_deleted_database(self):
        os.unlink(self.path)

        with self.assertRaises(sqlite3.Error):
            self.store.authorize(None)
        self.assertFalse(os.path.exists(self.path))

    def test_cookie_parser_rejects_duplicates_and_cookie_can_be_secure(self):
        secure = AccessStore(
            os.path.join(self.temp.name, "secure.sqlite3"),
            secure_cookie=True,
        )
        invitation = secure.create_invitation()
        token = secure.redeem(invitation.token).session_token
        header = secure.session_cookie_header(token)

        self.assertIn(f"{ACCESS_COOKIE}={token}", header)
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Lax", header)
        self.assertIn("Secure", header)
        self.assertEqual(
            secure.parse_session_cookie(f"other=x; {ACCESS_COOKIE}={token}"),
            token,
        )
        self.assertIsNone(
            secure.parse_session_cookie(
                f"{ACCESS_COOKIE}={token}; {ACCESS_COOKIE}={token}"
            )
        )

        self.store.set_public_origin("https://funnel.example.test")
        invitation = self.store.create_invitation()
        token = self.store.redeem(invitation.token).session_token
        self.assertIn("Secure", self.store.session_cookie_header(token))

    def test_concurrent_redemption_never_exceeds_five(self):
        invitation = self.store.create_invitation()

        def redeem_once(_index):
            independent = AccessStore(self.path)
            try:
                independent.redeem(invitation.token)
                return True
            except InvitationLimitError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(redeem_once, range(12)))

        self.assertEqual(sum(outcomes), 5)
        with sqlite3.connect(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM access_sessions"
            ).fetchone()[0]
            redemptions = connection.execute(
                "SELECT redemption_count FROM invitations"
            ).fetchone()[0]
        self.assertEqual(count, 5)
        self.assertEqual(redemptions, 5)


if __name__ == "__main__":
    unittest.main()
