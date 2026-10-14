#!/usr/bin/env python3

import base64
import multiprocessing
import os
import queue
import sqlite3
import tempfile
import threading
import time
import unittest

from tools.game_store import (
    GameStore,
    GameStoreError,
    IdempotencyConflict,
    InvalidTransition,
    NotFoundError,
    PendingCommand,
    RevisionConflict,
    ValidationError,
)


START_FEN = "8/8/8/8/8/8/8/8 w - - 0 1"
FEN_1 = "8/8/8/8/8/8/8/8 w - - 1 1"
FEN_2 = "8/8/8/8/8/8/8/8 b - - 2 1"
FEN_3 = "8/8/8/8/8/8/8/8 b - - 3 1"
FEN_4 = "8/8/8/8/8/8/8/8 w - - 4 2"
MODEL_SHA = "a" * 64


def _open_store_worker(database_path, start_event, result_queue):
    start_event.wait(10)
    try:
        store = GameStore(database_path)
        result_queue.put(("ok", store.integrity_check()))
    except Exception as exc:
        result_queue.put(
            ("error", f"{type(exc).__name__}: {exc}")
        )


def _clock_completion_race_worker(
    database_path,
    owner_token,
    public_id,
    operation,
    expected_revision,
    received_at_ms,
    transaction_now_ms,
    start_event,
    result_queue,
):
    store = GameStore(
        database_path,
        clock=lambda: transaction_now_ms / 1000,
    )
    start_event.wait(10)
    try:
        if operation == "takeback":
            result = store.takeback(
                owner_token,
                public_id,
                expected_revision=expected_revision,
                received_at_ms=received_at_ms,
            )
        else:
            result = store.resign_game(
                owner_token,
                public_id,
                expected_revision=expected_revision,
                resigning_color="white",
                received_at_ms=received_at_ms,
            )
        result_queue.put(
            (
                "ok",
                operation,
                result["status"],
                result["termination"],
                result["revision"],
            )
        )
    except Exception as exc:
        result_queue.put(("error", operation, type(exc).__name__))


def _begin_receipt_race_worker(
    database_path,
    owner_token,
    public_id,
    idempotency_key,
    received_at_ms,
    transaction_now_ms,
    start_event,
    result_queue,
):
    store = GameStore(
        database_path,
        clock=lambda: transaction_now_ms / 1000,
    )
    start_event.wait(10)
    try:
        result = store.begin_command(
            owner_token,
            public_id,
            idempotency_key,
            {
                "operation": "move",
                "move": "a2a3",
                "expected_revision": 0,
            },
            received_at_ms=received_at_ms,
        )
        result_queue.put(
            (
                "ok",
                bool(result["acquired"]),
                result["stage_data"]["received_at_ms"],
            )
        )
    except Exception as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))


def _pending_timeout_race_worker(
    database_path,
    owner_token,
    public_id,
    idempotency_key,
    request,
    expected_revision,
    transaction_now_ms,
    operation,
    start_event,
    result_queue,
):
    store = GameStore(
        database_path,
        clock=lambda: transaction_now_ms / 1000,
    )
    start_event.wait(10)
    try:
        if operation == "reclaim":
            result = store.begin_command(
                owner_token,
                public_id,
                idempotency_key,
                request,
                received_at_ms=transaction_now_ms,
            )
            result_queue.put(
                (
                    "reclaim",
                    result["acquired"],
                    result["reclaimed"],
                    result["stage_data"]["received_at_ms"],
                )
            )
        else:
            store.timeout_game(
                owner_token,
                public_id,
                expected_revision=expected_revision,
                timed_out_color="white",
            )
            result_queue.put(("timeout", "completed"))
    except Exception as exc:
        result_queue.put((operation, type(exc).__name__))


class FakeClock:
    def __init__(self, seconds=1_700_000_000):
        self.seconds = seconds

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


class GameStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "games.sqlite3")
        self.clock = FakeClock()
        self.store = GameStore(self.path, clock=self.clock)
        self.token = self.store.create_session(
            token="test-session-token-" + "x" * 24
        )["token"]

    def create_game(self, token=None, **overrides):
        values = {
            "start_fen": START_FEN,
            "human_color": "white",
            "model_id": "v37-test",
            "model_sha256": MODEL_SHA,
            "preset": "policy-only",
            "nodes": 1,
            "temperature": 0.8,
            "backend": "sycl",
            "clock": {"mode": "unlimited"},
        }
        values.update(overrides)
        return self.store.create_game(token or self.token, **values)

    def assert_concurrent_opens_succeed(self, database_path, workers=6):
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_open_store_worker,
                args=(database_path, start_event, result_queue),
            )
            for _ in range(workers)
        ]
        for process in processes:
            process.start()
        start_event.set()
        deadline = time.monotonic() + 20
        for process in processes:
            process.join(max(0.1, deadline - time.monotonic()))
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(2)
        results = []
        for _ in processes:
            try:
                results.append(result_queue.get(timeout=2))
            except queue.Empty:
                results.append(("error", "worker returned no result"))
        self.assertTrue(
            all(process.exitcode == 0 for process in processes),
            [process.exitcode for process in processes],
        )
        self.assertEqual(
            results,
            [("ok", ["ok"])] * workers,
        )

    def timed_game(self, **overrides):
        state = {
            "white_ms": 300_000,
            "black_ms": 300_000,
            "running_color": "white",
            "anchor_at_ms": int(self.clock() * 1000),
        }
        return self.create_game(
            clock={
                "mode": "timed",
                "initial_ms": 300_000,
                "increment_ms": 3_000,
            },
            clock_state=state,
            **overrides,
        )

    def timed_takeback_position(
        self,
        token,
        *,
        remaining_ms=1_000,
        incomplete=False,
    ):
        anchor_ms = int(self.clock() * 1000)
        game = self.create_game(
            token,
            clock={
                "mode": "timed",
                "initial_ms": 1_000,
                "increment_ms": 0,
            },
            clock_state={
                "white_ms": remaining_ms,
                "black_ms": 1_000,
                "running_color": "white",
                "anchor_at_ms": anchor_ms,
            },
        )
        if incomplete:
            latest = self.store.record_turn(
                token,
                game["id"],
                expected_revision=0,
                actor="human",
                color="white",
                legs=[{"uci": "a2a3+", "after_fen": FEN_1}],
                clock_state={
                    "white_ms": remaining_ms,
                    "black_ms": 1_000,
                    "running_color": "white",
                    "anchor_at_ms": anchor_ms,
                },
                complete=False,
            )
            return latest, anchor_ms
        human = self.store.record_turn(
            token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3", "after_fen": FEN_1}],
            clock_state={
                "white_ms": remaining_ms,
                "black_ms": 1_000,
                "running_color": "black",
                "anchor_at_ms": anchor_ms,
            },
        )
        latest = self.store.record_turn(
            token,
            game["id"],
            expected_revision=human["revision"],
            actor="engine",
            color="black",
            legs=[{"uci": "h7h6", "after_fen": FEN_2}],
            clock_state={
                "white_ms": remaining_ms,
                "black_ms": 1_000,
                "running_color": "white",
                "anchor_at_ms": anchor_ms,
            },
        )
        return latest, anchor_ms

    def test_restart_persists_game_and_never_stores_raw_token(self):
        game = self.create_game(player_name="Private Tester")

        restarted = GameStore(self.path, clock=self.clock)
        loaded = restarted.get_game_for_owner(self.token, game["id"])

        self.assertEqual(loaded["id"], game["id"])
        self.assertEqual(loaded["model"]["sha256"], MODEL_SHA)
        self.assertEqual(loaded["revision"], 0)
        self.assertEqual(restarted.integrity_check(), ["ok"])
        with open(self.path, "rb") as database:
            self.assertNotIn(self.token.encode("utf-8"), database.read())
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0],
                "delete",
            )
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 4
            )

    def test_temperature_decay_is_exact_validated_and_immutable(self):
        key = "create-decay-exact"
        game = self.create_game(
            temp_decay_moves=12,
            idempotency_key=key,
        )
        replayed = self.create_game(
            temp_decay_moves=12,
            idempotency_key=key,
        )

        self.assertEqual(game["temp_decay_moves"], 12)
        self.assertEqual(replayed["id"], game["id"])
        loaded = self.store.get_game_for_owner(self.token, game["id"])
        self.assertEqual(loaded["temp_decay_moves"], 12)
        with self.assertRaises(IdempotencyConflict):
            self.create_game(
                temp_decay_moves=13,
                idempotency_key=key,
            )
        for invalid in (True, -1, 641, 1.5, "12"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    self.create_game(temp_decay_moves=invalid)
        with sqlite3.connect(self.path) as connection:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "immutable game configuration"
            ):
                connection.execute(
                    """
                    UPDATE games SET temp_decay_moves = 13
                    WHERE public_id = ?
                    """,
                    (game["id"],),
                )

    def test_v3_database_migrates_temperature_decay_to_disabled(self):
        game = self.create_game()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "DROP TRIGGER games_immutable_configuration"
            )
            connection.execute(
                "ALTER TABLE games DROP COLUMN temp_decay_moves"
            )
            connection.execute("PRAGMA user_version=3")

        migrated = GameStore(self.path, clock=self.clock)
        loaded = migrated.get_game_for_owner(self.token, game["id"])

        self.assertEqual(loaded["temp_decay_moves"], 0)
        self.assertEqual(migrated.integrity_check(), ["ok"])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 4
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "immutable game configuration"
            ):
                connection.execute(
                    """
                    UPDATE games SET temp_decay_moves = 1
                    WHERE public_id = ?
                    """,
                    (game["id"],),
                )

    def test_concurrent_processes_serialize_fresh_database_migration(self):
        path = os.path.join(self.temp.name, "fresh-race.sqlite3")

        self.assert_concurrent_opens_succeed(path)

        with sqlite3.connect(path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 4
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )

    def test_concurrent_processes_serialize_v1_upgrade(self):
        path = os.path.join(self.temp.name, "v1-race.sqlite3")
        GameStore(path)
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version=1")

        self.assert_concurrent_opens_succeed(path)

        with sqlite3.connect(path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 4
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )

    def test_cross_process_expired_takeback_and_resign_complete_once(self):
        token = self.store.create_session(
            token="clock-race-session-" + "c" * 24
        )["token"]
        game, anchor_ms = self.timed_takeback_position(token)
        received_at_ms = anchor_ms + 1_000
        transaction_now_ms = anchor_ms + 2_000
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_clock_completion_race_worker,
                args=(
                    self.path,
                    token,
                    game["id"],
                    operation,
                    game["revision"],
                    received_at_ms,
                    transaction_now_ms,
                    start_event,
                    result_queue,
                ),
            )
            for operation in ("takeback", "resign")
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join(2)
        outcomes = [result_queue.get(timeout=2) for _ in processes]

        self.assertTrue(
            all(process.exitcode == 0 for process in processes),
            [process.exitcode for process in processes],
        )
        successes = [
            outcome for outcome in outcomes if outcome[0] == "ok"
        ]
        failures = [
            outcome for outcome in outcomes if outcome[0] == "error"
        ]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(successes[0][2:4], ("completed", "timeout"))
        self.assertEqual(successes[0][4], game["revision"] + 1)
        self.assertEqual(len(failures), 1, outcomes)
        self.assertIn(
            failures[0][2], ("InvalidTransition", "RevisionConflict")
        )

        self.clock.seconds = transaction_now_ms / 1000
        completed = self.store.get_game_for_owner(
            token, game["id"], include_history=True
        )
        self.assertEqual(completed["termination"], "timeout")
        self.assertEqual(completed["revision"], game["revision"] + 1)
        self.assertEqual(completed["takeback_count"], 0)
        self.assertEqual(
            [
                event["type"]
                for event in completed["events"]
                if event["type"] == "game_completed"
            ],
            ["game_completed"],
        )
        self.assertNotIn(
            "takeback",
            [event["type"] for event in completed["events"]],
        )

    def test_cross_process_begin_keeps_winning_first_receipt(self):
        game = self.create_game()
        transaction_now_ms = int(self.clock() * 1000) + 2_000
        receipts = (transaction_now_ms - 1_000, transaction_now_ms - 500)
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_begin_receipt_race_worker,
                args=(
                    self.path,
                    self.token,
                    game["id"],
                    "move.receipt-race-0001",
                    receipt,
                    transaction_now_ms,
                    start_event,
                    result_queue,
                ),
            )
            for receipt in receipts
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join(2)
        outcomes = [result_queue.get(timeout=2) for _ in processes]

        self.assertTrue(
            all(process.exitcode == 0 for process in processes),
            [process.exitcode for process in processes],
        )
        self.assertTrue(all(outcome[0] == "ok" for outcome in outcomes))
        self.assertEqual(
            sum(1 for outcome in outcomes if outcome[1]), 1, outcomes
        )
        persisted_receipts = {outcome[2] for outcome in outcomes}
        self.assertEqual(len(persisted_receipts), 1, outcomes)
        self.assertIn(persisted_receipts.pop(), receipts)

    def test_cross_process_timeout_cannot_overtake_pending_reclaim(self):
        token = self.store.create_session(
            token="pending-reclaim-race-session-" + "k" * 24
        )["token"]
        anchor_ms = int(self.clock() * 1000)
        game = self.create_game(
            token,
            clock={
                "mode": "timed",
                "initial_ms": 1_000,
                "increment_ms": 0,
            },
            clock_state={
                "white_ms": 1_000,
                "black_ms": 1_000,
                "running_color": "white",
                "anchor_at_ms": anchor_ms,
            },
        )
        first_receipt = anchor_ms + 999
        self.clock.seconds = first_receipt / 1000
        key = "move.pending-reclaim-race-0001"
        request = {
            "operation": "move",
            "move": "a2a3",
            "expected_revision": 0,
        }
        self.store.begin_command(
            token,
            game["id"],
            key,
            request,
            received_at_ms=first_receipt,
        )
        transaction_now_ms = first_receipt + 31_000
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_pending_timeout_race_worker,
                args=(
                    self.path,
                    token,
                    game["id"],
                    key,
                    request,
                    0,
                    transaction_now_ms,
                    operation,
                    start_event,
                    result_queue,
                ),
            )
            for operation in ("reclaim", "timeout")
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join(2)
        outcomes = [result_queue.get(timeout=2) for _ in processes]

        self.assertTrue(
            all(process.exitcode == 0 for process in processes),
            [process.exitcode for process in processes],
        )
        reclaim = next(
            outcome for outcome in outcomes if outcome[0] == "reclaim"
        )
        timeout = next(
            outcome for outcome in outcomes if outcome[0] == "timeout"
        )
        self.assertEqual(reclaim[1:3], (True, True), outcomes)
        self.assertEqual(reclaim[3], first_receipt)
        self.assertEqual(timeout[1], "PendingCommand", outcomes)
        self.clock.seconds = transaction_now_ms / 1000
        unchanged = self.store.get_game_for_owner(token, game["id"])
        self.assertEqual(unchanged["status"], "active")
        self.assertEqual(unchanged["revision"], 0)

    def test_database_and_domain_allow_only_one_active_game_per_session(self):
        first = self.create_game()
        with self.assertRaisesRegex(
            InvalidTransition, first["id"]
        ):
            self.create_game()

        self.store.complete_game(
            self.token,
            first["id"],
            expected_revision=0,
            result="1/2-1/2",
            termination="agreement",
        )
        second = self.create_game()
        self.assertNotEqual(first["id"], second["id"])

        with sqlite3.connect(self.path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE games SET status = 'active', result = NULL,
                        termination = NULL, completed_at_ms = NULL,
                        public_visible = 0
                    WHERE public_id = ?
                    """,
                    (first["id"],),
                )

    def test_player_name_requires_utf8_but_allows_unicode(self):
        invalid_name = "invalid-\ud800-name"
        with self.assertRaisesRegex(
            ValidationError, "player_name must be valid UTF-8"
        ) as raised:
            self.create_game(player_name=invalid_name)
        self.assertNotIn(invalid_name, str(raised.exception))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM games").fetchone()[0],
                0,
            )

        game = self.create_game(player_name="Éric ♞")
        loaded = self.store.get_game_for_owner(self.token, game["id"])
        self.assertEqual(loaded["player_name"], "Éric ♞")

    def test_active_game_lookup_supports_resume_without_error_parsing(self):
        self.assertIsNone(
            self.store.get_active_game_for_owner(self.token)
        )
        game = self.create_game()
        active = self.store.get_active_game_for_owner(self.token)
        self.assertEqual(active["id"], game["id"])
        self.store.complete_game(
            self.token,
            game["id"],
            expected_revision=0,
            result="1/2-1/2",
            termination="agreement",
        )
        self.assertIsNone(
            self.store.get_active_game_for_owner(self.token)
        )

    def test_optimistic_revision_allows_only_one_concurrent_writer(self):
        game = self.create_game()
        barrier = threading.Barrier(3)
        outcomes = []
        outcomes_lock = threading.Lock()

        def write(move):
            barrier.wait()
            try:
                result = self.store.record_turn(
                    self.token,
                    game["id"],
                    expected_revision=0,
                    actor="human",
                    color="white",
                    legs=[{"uci": move, "after_fen": FEN_1}],
                    clock_state={},
                )
                outcome = ("ok", result["revision"])
            except Exception as exc:  # captured for assertion in main thread
                outcome = ("error", exc)
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=write, args=("a2a3",)),
            threading.Thread(target=write, args=("b2b3",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        successes = [item for item in outcomes if item[0] == "ok"]
        failures = [item[1] for item in outcomes if item[0] == "error"]
        self.assertEqual(successes, [("ok", 1)])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RevisionConflict)
        self.assertEqual(failures[0].expected, 0)
        self.assertEqual(failures[0].actual, 1)

    def test_idempotency_replays_terminal_result_and_rejects_key_reuse(self):
        game = self.create_game()
        key = "move.command-0001"
        request = {"revision": 0, "legs": ["a2a3+", "a3a4"]}

        begun = self.store.begin_command(self.token, game["id"], key, request)
        self.assertEqual(begun["status"], "in_progress")
        self.assertTrue(begun["acquired"])
        self.assertFalse(begun["replay"])
        self.assertEqual(begun["attempt"], 1)
        lease = begun["lease_token"]
        in_progress = self.store.begin_command(
            self.token, game["id"], key, request
        )
        self.assertEqual(in_progress["status"], "in_progress")
        self.assertFalse(in_progress["acquired"])
        self.assertNotIn("lease_token", in_progress)
        with self.assertRaises(IdempotencyConflict):
            self.store.begin_command(
                self.token, game["id"], key, {"revision": 1}
            )

        first = self.store.complete_command(
            self.token,
            game["id"],
            key,
            {"revision": 1, "ok": True},
            lease_token=lease,
        )
        self.assertFalse(first["replay"])
        replay = self.store.begin_command(
            self.token, game["id"], key, request
        )
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["response"], {"ok": True, "revision": 1})
        cached = self.store.complete_command(
            self.token,
            game["id"],
            key,
            {"ignored": True},
            lease_token=lease,
        )
        self.assertTrue(cached["replay"])
        self.assertEqual(cached["response"], replay["response"])

    def test_first_command_receipt_survives_crash_and_reclaim(self):
        cases = (
            ("takeback-before", "takeback", 999, "active", None),
            ("takeback-exact", "takeback", 1_000, "completed", "timeout"),
            (
                "resign-before",
                "resign",
                999,
                "completed",
                "resignation",
            ),
            ("resign-exact", "resign", 1_000, "completed", "timeout"),
        )
        for label, operation, offset_ms, status, termination in cases:
            with self.subTest(label=label):
                token = self.store.create_session(
                    token=f"{label}-receipt-session-" + "f" * 24
                )["token"]
                game, anchor_ms = self.timed_takeback_position(token)
                first_receipt = anchor_ms + offset_ms
                self.clock.seconds = first_receipt / 1000
                key = f"{operation}.{label}-0001"
                request = {
                    "operation": operation,
                    "expected_revision": game["revision"],
                }
                begun = self.store.begin_command(
                    token,
                    game["id"],
                    key,
                    request,
                    received_at_ms=first_receipt,
                )
                self.assertEqual(
                    begun["stage_data"]["received_at_ms"],
                    first_receipt,
                )

                self.clock.advance(31)
                retry_receipt = int(self.clock() * 1000)
                restarted = GameStore(self.path, clock=self.clock)
                reclaimed = restarted.begin_command(
                    token,
                    game["id"],
                    key,
                    request,
                    received_at_ms=retry_receipt,
                )
                self.assertTrue(reclaimed["reclaimed"])
                self.assertEqual(
                    reclaimed["stage_data"]["received_at_ms"],
                    first_receipt,
                )
                self.assertNotEqual(first_receipt, retry_receipt)

                command_arguments = {
                    "idempotency_key": key,
                    "command_lease": reclaimed["lease_token"],
                    "command_stage": (
                        "takeback_recorded"
                        if operation == "takeback"
                        else "game_completed"
                    ),
                }
                if operation == "takeback":
                    result = restarted.takeback(
                        token,
                        game["id"],
                        expected_revision=game["revision"],
                        received_at_ms=first_receipt,
                        **command_arguments,
                    )
                else:
                    result = restarted.resign_game(
                        token,
                        game["id"],
                        expected_revision=game["revision"],
                        resigning_color="white",
                        received_at_ms=first_receipt,
                        **command_arguments,
                    )
                self.assertEqual(result["status"], status)
                self.assertEqual(result["termination"], termination)

    def test_begin_command_validates_first_receipt_without_writing(self):
        game = self.create_game()
        for invalid in (-1, True, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ValidationError, "non-negative integer"
                ):
                    self.store.begin_command(
                        self.token,
                        game["id"],
                        "receipt.invalid-0001",
                        {
                            "operation": "move",
                            "move": "a2a3",
                            "expected_revision": 0,
                        },
                        received_at_ms=invalid,
                    )
        with self.assertRaisesRegex(
            ValidationError, "later than transaction time"
        ):
            self.store.begin_command(
                self.token,
                game["id"],
                "receipt.future-0001",
                {
                    "operation": "move",
                    "move": "a2a3",
                    "expected_revision": 0,
                },
                received_at_ms=int(self.clock() * 1000) + 1,
            )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM idempotent_commands"
                ).fetchone()[0],
                0,
            )

    def test_recoverable_command_request_is_strict_and_secret_free(self):
        game = self.create_game()
        now_ms = int(self.clock() * 1000)
        malformed_requests = (
            {"operation": "move", "expected_revision": 0},
            {
                "operation": "move",
                "move": "not-a-move",
                "expected_revision": 0,
            },
            {"operation": "takeback", "expected_revision": True},
            {
                "operation": "resign",
                "expected_revision": 0,
                "opaque": self.token,
            },
            {"operation": "unknown", "expected_revision": 0},
        )
        for index, request in enumerate(malformed_requests):
            with self.subTest(request=request.get("operation"), index=index):
                with open(self.path, "rb") as database:
                    before = database.read()
                with self.assertRaises(ValidationError) as raised:
                    self.store.begin_command(
                        self.token,
                        game["id"],
                        f"invalid.recovery-{index:04d}",
                        request,
                        received_at_ms=now_ms,
                    )
                self.assertNotIn(self.token, str(raised.exception))
                with open(self.path, "rb") as database:
                    self.assertEqual(database.read(), before)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM idempotent_commands"
                ).fetchone()[0],
                0,
            )

    def test_pending_started_projection_is_oldest_and_immutable(self):
        game = self.create_game()
        now_ms = int(self.clock() * 1000)
        first_request = {
            "operation": "move",
            "move": "a2a3",
            "expected_revision": 0,
        }
        second_request = {
            "operation": "resign",
            "expected_revision": 0,
        }
        first = self.store.begin_command(
            self.token,
            game["id"],
            "pending.first-0001",
            first_request,
            received_at_ms=now_ms,
        )
        self.store.begin_command(
            self.token,
            game["id"],
            "pending.second-0001",
            second_request,
            received_at_ms=now_ms,
        )

        pending = self.store.get_pending_started_command(
            self.token, game["id"]
        )

        self.assertEqual(pending["idempotency_key"], "pending.first-0001")
        self.assertEqual(pending["request"], first_request)
        self.assertEqual(pending["received_at_ms"], now_ms)
        self.assertEqual(
            pending["lease_expires_at_ms"],
            first["lease_expires_at_ms"],
        )
        self.assertTrue(pending["lease_live"])
        self.assertNotIn("lease_token", pending)
        self.assertNotIn(self.token, repr(pending))
        self.clock.advance(31)
        expired = self.store.get_pending_started_command(
            self.token, game["id"]
        )
        self.assertFalse(expired["lease_live"])
        self.assertEqual(expired["received_at_ms"], now_ms)

    def test_pending_started_projection_fails_closed_on_malformed_data(self):
        game = self.create_game()
        now_ms = int(self.clock() * 1000)
        request = {
            "operation": "move",
            "move": "a2a3",
            "expected_revision": 0,
        }
        self.store.begin_command(
            self.token,
            game["id"],
            "pending.malformed-0001",
            request,
            received_at_ms=now_ms,
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE idempotent_commands
                SET stage_json = ?
                WHERE idempotency_key = 'pending.malformed-0001'
                """,
                (
                    '{"received_at_ms":'
                    + str(now_ms)
                    + ',"request":{"operation":"move",'
                    '"move":"a2a4","expected_revision":0}}',
                ),
            )

        with self.assertRaisesRegex(
            GameStoreError, "recovery data is invalid"
        ):
            self.store.get_pending_started_command(
                self.token, game["id"]
            )

    def test_command_payloads_cannot_persist_raw_bearer_tokens(self):
        game = self.create_game()
        key = "move.command-secret-0001"
        begun = self.store.begin_command(
            self.token,
            game["id"],
            key,
            {"operation": "move", "revision": 0},
        )
        lease = begun["lease_token"]
        owner_bytes = self.token.encode("utf-8")
        lease_bytes = lease.encode("utf-8")

        rejected_calls = (
            (
                "advance nested value",
                lambda: self.store.advance_command(
                    self.token,
                    game["id"],
                    key,
                    lease_token=lease,
                    stage="human_recorded",
                    stage_data={
                        "outer": [
                            {"opaque": f"before-{self.token}-after"}
                        ]
                    },
                ),
            ),
            (
                "complete mapping key",
                lambda: self.store.complete_command(
                    self.token,
                    game["id"],
                    key,
                    {f"before-{self.token}-after": True},
                    lease_token=lease,
                ),
            ),
            (
                "fail bytes-like value",
                lambda: self.store.fail_command(
                    self.token,
                    game["id"],
                    key,
                    {
                        "outer": [
                            bytearray(b"before-" + owner_bytes + b"-after")
                        ]
                    },
                    lease_token=lease,
                ),
            ),
            (
                "advance lease value",
                lambda: self.store.advance_command(
                    self.token,
                    game["id"],
                    key,
                    lease_token=lease,
                    stage="human_recorded",
                    stage_data={"opaque": f"before-{lease}-after"},
                ),
            ),
        )
        for label, rejected_call in rejected_calls:
            with self.subTest(label=label):
                with open(self.path, "rb") as database:
                    before = database.read()
                with self.assertRaisesRegex(
                    ValidationError, "must not contain bearer credentials"
                ):
                    rejected_call()
                with open(self.path, "rb") as database:
                    after = database.read()
                self.assertEqual(after, before)
                self.assertNotIn(owner_bytes, after)
                self.assertNotIn(lease_bytes, after)

        completed = self.store.complete_command(
            self.token,
            game["id"],
            key,
            {"ok": True},
            lease_token=lease,
        )
        self.assertEqual(completed["status"], "completed")
        with open(self.path, "rb") as database:
            contents = database.read()
        self.assertNotIn(owner_bytes, contents)
        self.assertNotIn(lease_bytes, contents)

    def test_expired_command_lease_is_reclaimed_with_recovery_stage(self):
        game = self.create_game()
        key = "move.command-recovery"
        request = {"revision": 0, "move": "a2a3"}
        begun = self.store.begin_command(
            self.token, game["id"], key, request
        )
        first_lease = begun["lease_token"]
        recorded = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3", "after_fen": FEN_1}],
            clock_state={},
            idempotency_key=key,
            command_lease=first_lease,
            command_stage="human_recorded",
        )
        first_turn_id = recorded["recorded_turn_id"]

        self.clock.advance(31)
        restarted = GameStore(self.path, clock=self.clock)
        reclaimed = restarted.begin_command(
            self.token, game["id"], key, request
        )
        self.assertTrue(reclaimed["acquired"])
        self.assertTrue(reclaimed["reclaimed"])
        self.assertEqual(reclaimed["attempt"], 2)
        self.assertEqual(reclaimed["stage"], "human_recorded")
        self.assertEqual(
            reclaimed["stage_data"]["recorded_turn_id"], first_turn_id
        )
        second_lease = reclaimed["lease_token"]

        with self.assertRaisesRegex(InvalidTransition, "stale"):
            restarted.record_turn(
                self.token,
                game["id"],
                expected_revision=1,
                actor="engine",
                color="black",
                legs=[{"uci": "h7h6", "after_fen": FEN_2}],
                clock_state={},
                idempotency_key=key,
                command_lease=first_lease,
                command_stage="engine_recorded",
            )
        self.assertEqual(
            restarted.get_game_for_owner(self.token, game["id"])["revision"],
            1,
        )
        engine = restarted.record_turn(
            self.token,
            game["id"],
            expected_revision=1,
            actor="engine",
            color="black",
            legs=[{"uci": "h7h6", "after_fen": FEN_2}],
            clock_state={},
            idempotency_key=key,
            command_lease=second_lease,
            command_stage="engine_recorded",
        )
        terminal = restarted.complete_command(
            self.token,
            game["id"],
            key,
            {"revision": engine["revision"], "ok": True},
            lease_token=second_lease,
        )
        self.assertFalse(terminal["replay"])
        replay = restarted.begin_command(
            self.token, game["id"], key, request
        )
        self.assertEqual(
            replay["response"], {"ok": True, "revision": 2}
        )
        history = restarted.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertEqual(len(history["turns"]), 2)

    def test_single_step_mutation_can_complete_command_atomically(self):
        game = self.create_game()
        human = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3", "after_fen": FEN_1}],
            clock_state={},
        )
        engine = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=human["revision"],
            actor="engine",
            color="black",
            legs=[{"uci": "h7h6", "after_fen": FEN_2}],
            clock_state={},
        )
        key = "takeback.atomic-0001"
        request = {
            "operation": "takeback",
            "expected_revision": engine["revision"],
        }
        begun = self.store.begin_command(
            self.token,
            game["id"],
            key,
            request,
            received_at_ms=int(self.clock() * 1000),
        )

        result = self.store.takeback(
            self.token,
            game["id"],
            expected_revision=engine["revision"],
            received_at_ms=int(self.clock() * 1000),
            idempotency_key=key,
            command_lease=begun["lease_token"],
            command_complete=True,
        )

        restarted = GameStore(self.path, clock=self.clock)
        replay = restarted.begin_command(
            self.token, game["id"], key, request
        )
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(replay["response"], result)
        self.assertEqual(replay["completed_revision"], result["revision"])

    def test_bound_takeback_must_match_durable_request_and_receipt(self):
        game = self.create_game()
        moved = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3", "after_fen": FEN_1}],
            clock_state={},
        )
        receipt_ms = int(self.clock() * 1000)
        key = "takeback.binding-0001"
        request = {
            "operation": "takeback",
            "expected_revision": moved["revision"],
        }
        begun = self.store.begin_command(
            self.token,
            game["id"],
            key,
            request,
            received_at_ms=receipt_ms,
        )
        self.clock.advance(1)

        with self.assertRaisesRegex(
            IdempotencyConflict, "durable request"
        ):
            self.store.takeback(
                self.token,
                game["id"],
                expected_revision=moved["revision"],
                received_at_ms=receipt_ms + 1,
                idempotency_key=key,
                command_lease=begun["lease_token"],
                command_stage="takeback_recorded",
            )
        unchanged = self.store.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertEqual(unchanged["revision"], moved["revision"])
        self.assertTrue(unchanged["turns"][0]["active"])

    def test_released_command_can_be_reclaimed_immediately(self):
        game = self.create_game()
        key = "move.release-0001"
        request = {"operation": "move", "revision": 0}
        begun = self.store.begin_command(
            self.token, game["id"], key, request
        )
        advanced = self.store.advance_command(
            self.token,
            game["id"],
            key,
            lease_token=begun["lease_token"],
            stage="human_recorded",
            stage_data={"revision": 1, "recorded_turn_id": "a" * 32},
        )
        self.assertEqual(advanced["stage"], "human_recorded")

        released = self.store.release_command(
            self.token,
            game["id"],
            key,
            lease_token=begun["lease_token"],
        )
        self.assertFalse(released["acquired"])
        self.assertEqual(released["retry_after_ms"], 0)
        reclaimed = self.store.begin_command(
            self.token, game["id"], key, request
        )
        self.assertTrue(reclaimed["acquired"])
        self.assertTrue(reclaimed["reclaimed"])
        self.assertEqual(reclaimed["attempt"], 2)
        self.assertEqual(reclaimed["stage"], "human_recorded")
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            self.store.heartbeat_command(
                self.token,
                game["id"],
                key,
                lease_token=begun["lease_token"],
            )

    def test_game_creation_is_idempotent_across_restart(self):
        key = "create.game-0001"
        first = self.create_game(idempotency_key=key)

        restarted = GameStore(self.path, clock=self.clock)
        replay = restarted.create_game(
            self.token,
            start_fen=START_FEN,
            human_color="white",
            model_id="v37-test",
            model_sha256=MODEL_SHA,
            preset="policy-only",
            nodes=1,
            temperature=0.8,
            backend="sycl",
            clock={"mode": "unlimited"},
            idempotency_key=key,
        )
        self.assertEqual(replay, first)
        with self.assertRaises(IdempotencyConflict):
            restarted.create_game(
                self.token,
                start_fen=START_FEN,
                human_color="black",
                model_id="v37-test",
                model_sha256=MODEL_SHA,
                preset="policy-only",
                nodes=1,
                temperature=0.8,
                backend="sycl",
                clock={"mode": "unlimited"},
                idempotency_key=key,
            )
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM games").fetchone()[0],
                1,
            )

    def test_two_leg_turn_is_grouped_and_persisted(self):
        game = self.create_game()
        updated = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[
                {"uci": "a2a3+", "after_fen": FEN_1},
                {"uci": "a3a4", "after_fen": FEN_2},
            ],
            clock_state={},
        )

        self.assertEqual(updated["active_moves"], ["a2a3+", "a3a4"])
        self.assertEqual(updated["current_fen"], FEN_2)
        loaded = self.store.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertEqual(len(loaded["turns"]), 1)
        turn = loaded["turns"][0]
        self.assertTrue(turn["completed"])
        self.assertTrue(turn["active"])
        self.assertEqual(
            [leg["uci"] for leg in turn["legs"]], ["a2a3+", "a3a4"]
        )
        self.assertEqual(turn["legs"][1]["before_fen"], FEN_1)
        self.assertEqual(turn["after_fen"], FEN_2)

    def test_turn_leg_activation_shape_is_strict(self):
        game = self.create_game()
        invalid_turns = (
            (
                [
                    {"uci": "a2a3", "after_fen": FEN_1},
                    {"uci": "a3a4", "after_fen": FEN_2},
                ],
                True,
            ),
            (
                [
                    {"uci": "a2a3+", "after_fen": FEN_1},
                    {"uci": "a3a4+", "after_fen": FEN_2},
                ],
                True,
            ),
            ([{"uci": "a2a3+", "after_fen": FEN_1}], True),
            ([{"uci": "a2a3", "after_fen": FEN_1}], False),
        )
        for legs, complete in invalid_turns:
            with self.subTest(legs=legs, complete=complete):
                with self.assertRaises(ValidationError):
                    self.store.record_turn(
                        self.token,
                        game["id"],
                        expected_revision=0,
                        actor="human",
                        color="white",
                        legs=legs,
                        clock_state={},
                        complete=complete,
                    )
        self.assertEqual(
            self.store.get_game_for_owner(self.token, game["id"])["revision"],
            0,
        )

        started = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3+", "after_fen": FEN_1}],
            clock_state={},
            complete=False,
        )
        with sqlite3.connect(self.path) as connection:
            turn_id = connection.execute(
                "SELECT id FROM turns WHERE public_id = ?",
                (started["recorded_turn_id"],),
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO move_legs(
                        turn_id, leg_index, uci, before_fen, after_fen,
                        active, created_at_ms
                    ) VALUES (?, 2, 'a3a4+', ?, ?, 1, ?)
                    """,
                    (
                        turn_id,
                        FEN_1,
                        FEN_2,
                        int(self.clock() * 1000),
                    ),
                )
        with self.assertRaises(ValidationError):
            self.store.append_turn_leg(
                self.token,
                game["id"],
                started["recorded_turn_id"],
                expected_revision=1,
                uci="a3a4+",
                after_fen=FEN_2,
                clock_state={},
            )
        finished = self.store.append_turn_leg(
            self.token,
            game["id"],
            started["recorded_turn_id"],
            expected_revision=1,
            uci="a3a4",
            after_fen=FEN_2,
            clock_state={},
        )
        self.assertEqual(finished["active_moves"], ["a2a3+", "a3a4"])

    def test_database_history_guards_block_direct_shape_mutation(self):
        game = self.create_game()
        recorded = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[
                {"uci": "a2a3+", "after_fen": FEN_1},
                {"uci": "a3a4", "after_fen": FEN_2},
            ],
            clock_state={},
        )
        turn_id = recorded["recorded_turn_id"]

        with sqlite3.connect(self.path) as connection:
            first_id, second_id = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT l.id
                    FROM move_legs AS l
                    JOIN turns AS t ON t.id = l.turn_id
                    WHERE t.public_id = ?
                    ORDER BY l.leg_index
                    """,
                    (turn_id,),
                )
            ]
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "immutable"
            ):
                connection.execute(
                    "UPDATE move_legs SET uci = 'a2a3' WHERE id = ?",
                    (first_id,),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "immutable"
            ):
                connection.execute(
                    "UPDATE move_legs SET uci = 'a3a4+' WHERE id = ?",
                    (second_id,),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "active incomplete drafts"
            ):
                connection.execute(
                    """
                    INSERT INTO turns(
                        public_id, game_id, turn_index, actor, color,
                        before_fen, after_fen,
                        before_moves_json, after_moves_json,
                        before_clock_json, after_clock_json,
                        completed, active, created_at_ms, completed_at_ms,
                        undone_at_ms
                    )
                    SELECT
                        ?, game_id, turn_index + 1000, actor, color,
                        before_fen, after_fen,
                        before_moves_json, after_moves_json,
                        before_clock_json, after_clock_json,
                        1, 1, created_at_ms, created_at_ms, NULL
                    FROM turns WHERE public_id = ?
                    """,
                    ("f" * 32, turn_id),
                )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "invalid turn history update"
            ):
                connection.execute(
                    "UPDATE turns SET completed = 0, completed_at_ms = NULL "
                    "WHERE public_id = ?",
                    (turn_id,),
                )

        history = self.store.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertEqual(
            [leg["uci"] for leg in history["turns"][0]["legs"]],
            ["a2a3+", "a3a4"],
        )

    def test_valid_v2_history_is_rebuilt_by_v3_migration(self):
        path = os.path.join(self.temp.name, "valid-v2.sqlite3")
        store = GameStore(path, clock=self.clock)
        token = store.create_session(
            token="valid-v2-session-" + "v" * 24
        )["token"]
        game = store.create_game(
            token,
            start_fen=START_FEN,
            human_color="white",
            model_id="v37-test",
            model_sha256=MODEL_SHA,
            preset="policy-only",
            nodes=1,
            temperature=0.8,
            backend="sycl",
            clock={"mode": "unlimited"},
        )
        store.record_turn(
            token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[
                {"uci": "a2a3+", "after_fen": FEN_1},
                {"uci": "a3a4", "after_fen": FEN_2},
            ],
            clock_state={},
        )
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version=2")

        migrated = GameStore(path, clock=self.clock)

        history = migrated.get_game_for_owner(
            token, game["id"], include_history=True
        )
        self.assertEqual(
            [leg["uci"] for leg in history["turns"][0]["legs"]],
            ["a2a3+", "a3a4"],
        )
        with sqlite3.connect(path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 4
            )
            table_sql = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = 'move_legs'
                """
            ).fetchone()[0]
            self.assertIn("substr(uci, -1) <> '+'", table_sql)

    def test_v1_and_v2_invalid_reversed_history_fail_migration(self):
        for version in (1, 2):
            with self.subTest(version=version):
                path = os.path.join(
                    self.temp.name, f"invalid-v{version}.sqlite3"
                )
                store = GameStore(path, clock=self.clock)
                token = store.create_session(
                    token=f"invalid-v{version}-session-" + "i" * 24
                )["token"]
                game = store.create_game(
                    token,
                    start_fen=START_FEN,
                    human_color="white",
                    model_id="v37-test",
                    model_sha256=MODEL_SHA,
                    preset="policy-only",
                    nodes=1,
                    temperature=0.8,
                    backend="sycl",
                    clock={"mode": "unlimited"},
                )
                recorded = store.record_turn(
                    token,
                    game["id"],
                    expected_revision=0,
                    actor="human",
                    color="white",
                    legs=[
                        {"uci": "a2a3+", "after_fen": FEN_1},
                        {"uci": "a3a4", "after_fen": FEN_2},
                    ],
                    clock_state={},
                )
                with sqlite3.connect(path) as connection:
                    for trigger in (
                        "turns_shape_before_completion",
                        "move_legs_second_requires_activation",
                        "turns_reject_precompleted_insert",
                        "move_legs_update_guard",
                        "turns_update_guard",
                    ):
                        connection.execute(
                            f"DROP TRIGGER IF EXISTS {trigger}"
                        )
                    connection.execute(
                        "ALTER TABLE move_legs RENAME TO move_legs_current"
                    )
                    connection.execute(
                        """
                        CREATE TABLE move_legs (
                            id INTEGER PRIMARY KEY,
                            turn_id INTEGER NOT NULL
                                REFERENCES turns(id) ON DELETE CASCADE,
                            leg_index INTEGER NOT NULL
                                CHECK(leg_index IN (1, 2)),
                            uci TEXT NOT NULL,
                            before_fen TEXT NOT NULL,
                            after_fen TEXT NOT NULL,
                            active INTEGER NOT NULL,
                            created_at_ms INTEGER NOT NULL,
                            undone_at_ms INTEGER,
                            UNIQUE(turn_id, leg_index)
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO move_legs(
                            id, turn_id, leg_index, uci,
                            before_fen, after_fen, active,
                            created_at_ms, undone_at_ms
                        )
                        SELECT
                            id, turn_id, leg_index,
                            CASE leg_index
                                WHEN 1 THEN rtrim(uci, '+')
                                ELSE uci || '+'
                            END,
                            before_fen, after_fen, active,
                            created_at_ms, undone_at_ms
                        FROM move_legs_current
                        """
                    )
                    connection.execute("DROP TABLE move_legs_current")
                    connection.execute(f"PRAGMA user_version={version}")

                with self.assertRaisesRegex(
                    GameStoreError,
                    recorded["recorded_turn_id"]
                    + " has an invalid move-leg shape",
                ):
                    GameStore(path, clock=self.clock)
                with sqlite3.connect(path) as connection:
                    self.assertEqual(
                        connection.execute(
                            "PRAGMA user_version"
                        ).fetchone()[0],
                        version,
                    )
                    moves = [
                        row[0]
                        for row in connection.execute(
                            "SELECT uci FROM move_legs ORDER BY leg_index"
                        )
                    ]
                self.assertEqual(moves, ["a2a3", "a3a4+"])

    def test_takeback_soft_undoes_engine_and_human_and_restores_clock(self):
        game = self.timed_game()
        initial_clock = game["clock_state"]
        human = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[
                {"uci": "a2a3+", "after_fen": FEN_1},
                {"uci": "a3a4", "after_fen": FEN_2},
            ],
            clock_state={
                "white_ms": 291_000,
                "black_ms": 300_000,
                "running_color": "black",
                "anchor_at_ms": int(self.clock() * 1000),
            },
        )
        engine = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=human["revision"],
            actor="engine",
            color="black",
            legs=[
                {"uci": "h7h6+", "after_fen": FEN_3},
                {"uci": "h6h5", "after_fen": FEN_4},
            ],
            clock_state={
                "white_ms": 291_000,
                "black_ms": 294_000,
                "running_color": "white",
                "anchor_at_ms": int(self.clock() * 1000),
            },
        )
        self.clock.advance(17)

        restored = self.store.takeback(
            self.token,
            game["id"],
            expected_revision=engine["revision"],
            received_at_ms=int(self.clock() * 1000),
        )

        self.assertEqual(restored["revision"], 3)
        self.assertEqual(restored["takeback_count"], 1)
        self.assertEqual(restored["current_fen"], START_FEN)
        self.assertEqual(restored["active_moves"], [])
        self.assertEqual(
            restored["clock_state"],
            {
                **initial_clock,
                "anchor_at_ms": int(self.clock() * 1000),
            },
        )
        self.assertEqual(len(restored["reverted_turn_ids"]), 2)

        history = self.store.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertTrue(all(not turn["active"] for turn in history["turns"]))
        self.assertTrue(
            all(
                not leg["active"]
                for turn in history["turns"]
                for leg in turn["legs"]
            )
        )
        takeback_events = [
            event for event in history["events"] if event["type"] == "takeback"
        ]
        self.assertEqual(len(takeback_events), 1)
        self.assertEqual(
            takeback_events[0]["payload"]["reverted_turn_ids"],
            restored["reverted_turn_ids"],
        )

    def test_takeback_of_unfinished_human_turn_only(self):
        game = self.create_game()
        started = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3+", "after_fen": FEN_1}],
            clock_state={},
            complete=False,
        )
        restored = self.store.takeback(
            self.token,
            game["id"],
            expected_revision=started["revision"],
            received_at_ms=int(self.clock() * 1000),
        )
        self.assertEqual(restored["current_fen"], START_FEN)
        self.assertEqual(restored["active_moves"], [])
        self.assertEqual(len(restored["reverted_turn_ids"]), 1)

    def test_takeback_uses_receipt_time_at_clock_boundary(self):
        cases = (
            ("before", 999, "active"),
            ("exact", 1_000, "completed"),
            ("after", 1_001, "completed"),
        )
        for label, offset_ms, expected_status in cases:
            with self.subTest(label=label):
                token = self.store.create_session(
                    token=f"takeback-{label}-session-" + "q" * 24
                )["token"]
                game, anchor_ms = self.timed_takeback_position(token)
                self.clock.seconds = (anchor_ms + 2_000) / 1000

                result = self.store.takeback(
                    token,
                    game["id"],
                    expected_revision=game["revision"],
                    received_at_ms=anchor_ms + offset_ms,
                )

                self.assertEqual(result["status"], expected_status)
                if expected_status == "active":
                    self.assertEqual(result["takeback_count"], 1)
                    self.assertEqual(result["clock_state"]["white_ms"], 1_000)
                    self.assertEqual(
                        result["clock_state"]["anchor_at_ms"],
                        anchor_ms + 2_000,
                    )
                else:
                    self.assertEqual(result["termination"], "timeout")
                    self.assertEqual(result["result"], "0-1")
                    self.assertEqual(result["clock_state"]["white_ms"], 0)
                    self.assertEqual(result["takeback_count"], 0)
                    history = self.store.get_game_for_owner(
                        token, game["id"], include_history=True
                    )
                    self.assertNotIn(
                        "takeback",
                        [event["type"] for event in history["events"]],
                    )

    def test_expired_unfinished_takeback_completes_timeout_command(self):
        token = self.store.create_session(
            token="unfinished-timeout-session-" + "u" * 24
        )["token"]
        game, anchor_ms = self.timed_takeback_position(
            token, incomplete=True
        )
        key = "takeback.timeout-0001"
        request = {
            "operation": "takeback",
            "expected_revision": game["revision"],
        }
        self.clock.seconds = (anchor_ms + 1_000) / 1000
        begun = self.store.begin_command(
            token,
            game["id"],
            key,
            request,
            received_at_ms=anchor_ms + 1_000,
        )
        self.clock.seconds = (anchor_ms + 2_000) / 1000

        timed_out = self.store.takeback(
            token,
            game["id"],
            expected_revision=game["revision"],
            received_at_ms=anchor_ms + 1_000,
            idempotency_key=key,
            command_lease=begun["lease_token"],
            command_stage="takeback_recorded",
        )

        self.assertEqual(timed_out["status"], "completed")
        self.assertEqual(timed_out["termination"], "timeout")
        self.assertEqual(timed_out["current_fen"], START_FEN)
        self.assertEqual(timed_out["active_moves"], [])
        self.assertEqual(timed_out["takeback_count"], 0)
        staged = self.store.begin_command(
            token, game["id"], key, request
        )
        self.assertEqual(staged["status"], "in_progress")
        self.assertEqual(staged["stage"], "game_completed")
        self.assertEqual(staged["stage_data"]["termination"], "timeout")
        self.assertEqual(
            staged["stage_data"]["revision"], timed_out["revision"]
        )
        history = self.store.get_game_for_owner(
            token, game["id"], include_history=True
        )
        self.assertFalse(history["turns"][0]["active"])
        self.assertFalse(history["turns"][0]["legs"][0]["active"])
        self.assertEqual(
            history["events"][-1]["payload"][
                "voided_unfinished_turn_id"
            ],
            game["recorded_turn_id"],
        )

        completed = self.store.complete_command(
            token,
            game["id"],
            key,
            {"game": timed_out},
            lease_token=begun["lease_token"],
        )
        self.assertEqual(completed["status"], "completed")
        replay = self.store.begin_command(
            token, game["id"], key, request
        )
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["response"], {"game": timed_out})

    def test_receipt_timestamp_validation_precedes_clock_mutation(self):
        game = self.create_game()
        moved = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3", "after_fen": FEN_1}],
            clock_state={},
        )
        for invalid in (-1, True, 1.5, None):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ValidationError, "non-negative integer"
                ):
                    self.store.takeback(
                        self.token,
                        game["id"],
                        expected_revision=moved["revision"],
                        received_at_ms=invalid,
                    )
        with self.assertRaisesRegex(
            ValidationError, "later than transaction time"
        ):
            self.store.resign_game(
                self.token,
                game["id"],
                expected_revision=moved["revision"],
                resigning_color="white",
                received_at_ms=int(self.clock() * 1000) + 1,
            )
        unchanged = self.store.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertEqual(unchanged["revision"], moved["revision"])
        self.assertTrue(unchanged["turns"][0]["active"])

    def test_resignation_charges_receipt_clock_and_timeout_precedes(self):
        cases = (
            ("before", 999, "resignation", 1),
            ("exact", 1_000, "timeout", 0),
            ("after", 1_001, "timeout", 0),
        )
        for label, offset_ms, termination, remaining_ms in cases:
            with self.subTest(label=label):
                token = self.store.create_session(
                    token=f"resign-{label}-session-" + "r" * 24
                )["token"]
                anchor_ms = int(self.clock() * 1000)
                game = self.create_game(
                    token,
                    clock={
                        "mode": "timed",
                        "initial_ms": 1_000,
                        "increment_ms": 0,
                    },
                    clock_state={
                        "white_ms": 1_000,
                        "black_ms": 1_000,
                        "running_color": "white",
                        "anchor_at_ms": anchor_ms,
                    },
                )
                self.clock.seconds = (anchor_ms + 2_000) / 1000

                result = self.store.resign_game(
                    token,
                    game["id"],
                    expected_revision=0,
                    resigning_color="white",
                    received_at_ms=anchor_ms + offset_ms,
                )

                self.assertEqual(result["termination"], termination)
                self.assertEqual(
                    result["clock_state"]["white_ms"], remaining_ms
                )
                self.assertIsNone(
                    result["clock_state"]["running_color"]
                )
                self.assertIsNone(result["clock_state"]["anchor_at_ms"])

    def test_resign_mid_double_archives_charged_clock(self):
        token = self.store.create_session(
            token="resign-mid-double-session-" + "d" * 24
        )["token"]
        game, anchor_ms = self.timed_takeback_position(
            token, incomplete=True
        )
        self.clock.seconds = (anchor_ms + 2_000) / 1000

        resigned = self.store.resign_game(
            token,
            game["id"],
            expected_revision=game["revision"],
            resigning_color="white",
            received_at_ms=anchor_ms + 750,
        )

        self.assertEqual(resigned["termination"], "resignation")
        self.assertEqual(resigned["clock_state"]["white_ms"], 250)
        self.assertEqual(resigned["current_fen"], START_FEN)
        self.assertEqual(resigned["active_moves"], [])

    def test_expired_engine_clock_overrides_human_resignation(self):
        token = self.store.create_session(
            token="engine-timeout-resign-session-" + "e" * 24
        )["token"]
        anchor_ms = int(self.clock() * 1000)
        game = self.create_game(
            token,
            clock={
                "mode": "timed",
                "initial_ms": 1_000,
                "increment_ms": 0,
            },
            clock_state={
                "white_ms": 1_000,
                "black_ms": 1_000,
                "running_color": "black",
                "anchor_at_ms": anchor_ms,
            },
        )
        self.clock.seconds = (anchor_ms + 2_000) / 1000

        result = self.store.resign_game(
            token,
            game["id"],
            expected_revision=0,
            resigning_color="white",
            received_at_ms=anchor_ms + 1_000,
        )

        self.assertEqual(result["termination"], "timeout")
        self.assertEqual(result["result"], "1-0")
        self.assertEqual(result["clock_state"]["black_ms"], 0)

    def test_expired_resignation_command_replays_timeout(self):
        token = self.store.create_session(
            token="resign-timeout-command-session-" + "m" * 24
        )["token"]
        anchor_ms = int(self.clock() * 1000)
        game = self.create_game(
            token,
            clock={
                "mode": "timed",
                "initial_ms": 1_000,
                "increment_ms": 0,
            },
            clock_state={
                "white_ms": 1_000,
                "black_ms": 1_000,
                "running_color": "white",
                "anchor_at_ms": anchor_ms,
            },
        )
        key = "resign.timeout-0001"
        request = {"operation": "resign", "expected_revision": 0}
        self.clock.seconds = (anchor_ms + 1_000) / 1000
        begun = self.store.begin_command(
            token,
            game["id"],
            key,
            request,
            received_at_ms=anchor_ms + 1_000,
        )
        self.clock.seconds = (anchor_ms + 2_000) / 1000

        timed_out = self.store.resign_game(
            token,
            game["id"],
            expected_revision=0,
            resigning_color="white",
            received_at_ms=anchor_ms + 1_000,
            idempotency_key=key,
            command_lease=begun["lease_token"],
            command_stage="resignation_recorded",
        )

        self.assertEqual(timed_out["termination"], "timeout")
        staged = self.store.begin_command(
            token, game["id"], key, request
        )
        self.assertEqual(staged["stage"], "game_completed")
        self.assertEqual(staged["stage_data"]["termination"], "timeout")
        response = {"game": timed_out}
        self.store.complete_command(
            token,
            game["id"],
            key,
            response,
            lease_token=begun["lease_token"],
        )
        replay = self.store.begin_command(
            token, game["id"], key, request
        )
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["response"], response)

    def test_human_can_resign_mid_turn_without_counting_takeback(self):
        game = self.create_game()
        started = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3+", "after_fen": FEN_1}],
            clock_state={},
            complete=False,
        )

        resigned = self.store.resign_game(
            self.token,
            game["id"],
            expected_revision=started["revision"],
            resigning_color="white",
            received_at_ms=int(self.clock() * 1000),
        )

        self.assertEqual(resigned["status"], "completed")
        self.assertEqual(resigned["result"], "0-1")
        self.assertEqual(resigned["termination"], "resignation")
        self.assertEqual(resigned["current_fen"], START_FEN)
        self.assertEqual(resigned["active_moves"], [])
        self.assertEqual(resigned["takeback_count"], 0)
        history = self.store.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertFalse(history["turns"][0]["active"])
        self.assertFalse(history["turns"][0]["completed"])
        self.assertFalse(history["turns"][0]["legs"][0]["active"])
        completion = history["events"][-1]
        self.assertEqual(completion["type"], "game_completed")
        self.assertEqual(
            completion["payload"]["voided_unfinished_turn_id"],
            started["recorded_turn_id"],
        )

    def test_timeout_mid_double_voids_leg_and_archives_zero_clock(self):
        game = self.timed_game()
        started = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3+", "after_fen": FEN_1}],
            clock_state={
                "white_ms": 0,
                "black_ms": 300_000,
                "running_color": "white",
                "anchor_at_ms": int(self.clock() * 1000),
            },
            complete=False,
        )

        timed_out = self.store.timeout_game(
            self.token,
            game["id"],
            expected_revision=started["revision"],
            timed_out_color="white",
        )

        self.assertEqual(timed_out["status"], "completed")
        self.assertEqual(timed_out["result"], "0-1")
        self.assertEqual(timed_out["termination"], "timeout")
        self.assertEqual(timed_out["current_fen"], START_FEN)
        self.assertEqual(timed_out["active_moves"], [])
        self.assertEqual(timed_out["clock_state"]["white_ms"], 0)
        self.assertEqual(timed_out["clock_state"]["black_ms"], 300_000)
        self.assertIsNone(timed_out["clock_state"]["running_color"])
        self.assertIsNone(timed_out["clock_state"]["anchor_at_ms"])
        self.assertEqual(timed_out["takeback_count"], 0)
        history = self.store.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertFalse(history["turns"][0]["active"])
        self.assertFalse(history["turns"][0]["completed"])
        self.assertEqual(
            history["events"][-1]["payload"]["timed_out_color"], "white"
        )

    def test_timeout_rejects_unlimited_or_nonrunning_side(self):
        unlimited = self.create_game()
        with self.assertRaisesRegex(InvalidTransition, "unlimited"):
            self.store.timeout_game(
                self.token,
                unlimited["id"],
                expected_revision=0,
                timed_out_color="white",
            )

    def test_timeout_requires_authoritative_elapsed_clock_boundary(self):
        anchor_ms = int(self.clock() * 1000)
        game = self.create_game(
            clock={
                "mode": "timed",
                "initial_ms": 1_000,
                "increment_ms": 0,
            },
            clock_state={
                "white_ms": 1_000,
                "black_ms": 1_000,
                "running_color": "white",
                "anchor_at_ms": anchor_ms,
            },
        )

        self.clock.seconds = (anchor_ms + 999) / 1000
        with self.assertRaisesRegex(InvalidTransition, "has not expired"):
            self.store.timeout_game(
                self.token,
                game["id"],
                expected_revision=0,
                timed_out_color="white",
            )
        with self.assertRaisesRegex(InvalidTransition, "clock is running"):
            self.store.timeout_game(
                self.token,
                game["id"],
                expected_revision=0,
                timed_out_color="black",
            )

        self.clock.seconds = (anchor_ms + 1_000) / 1000
        exact = self.store.timeout_game(
            self.token,
            game["id"],
            expected_revision=0,
            timed_out_color="white",
        )
        self.assertEqual(exact["clock_state"]["white_ms"], 0)

        second_token = self.store.create_session(
            token="timeout-after-session-" + "t" * 24
        )["token"]
        second_anchor_ms = int(self.clock() * 1000)
        after_game = self.create_game(
            second_token,
            clock={
                "mode": "timed",
                "initial_ms": 1_000,
                "increment_ms": 0,
            },
            clock_state={
                "white_ms": 1_000,
                "black_ms": 1_000,
                "running_color": "white",
                "anchor_at_ms": second_anchor_ms,
            },
        )
        self.clock.seconds = (second_anchor_ms + 1_001) / 1000
        after = self.store.timeout_game(
            second_token,
            after_game["id"],
            expected_revision=0,
            timed_out_color="white",
        )
        self.assertEqual(after["clock_state"]["white_ms"], 0)

    def test_pending_timely_command_blocks_unbound_timeout(self):
        token = self.store.create_session(
            token="pending-timeout-session-" + "p" * 24
        )["token"]
        anchor_ms = int(self.clock() * 1000)
        game = self.create_game(
            token,
            clock={
                "mode": "timed",
                "initial_ms": 1_000,
                "increment_ms": 0,
            },
            clock_state={
                "white_ms": 1_000,
                "black_ms": 1_000,
                "running_color": "white",
                "anchor_at_ms": anchor_ms,
            },
        )
        receipt_ms = anchor_ms + 999
        self.clock.seconds = receipt_ms / 1000
        request = {
            "operation": "move",
            "move": "a2a3",
            "expected_revision": 0,
        }
        begun = self.store.begin_command(
            token,
            game["id"],
            "move.pending-timeout-0001",
            request,
            received_at_ms=receipt_ms,
        )
        self.clock.seconds = (anchor_ms + 2_000) / 1000

        with self.assertRaises(PendingCommand) as raised:
            self.store.timeout_game(
                token,
                game["id"],
                expected_revision=0,
                timed_out_color="white",
            )
        self.assertGreater(raised.exception.retry_after_ms, 0)
        unchanged = self.store.get_game_for_owner(token, game["id"])
        self.assertEqual(unchanged["status"], "active")
        self.assertEqual(unchanged["revision"], 0)

        bound = self.store.timeout_game(
            token,
            game["id"],
            expected_revision=0,
            timed_out_color="white",
            idempotency_key="move.pending-timeout-0001",
            command_lease=begun["lease_token"],
            command_stage="game_completed",
        )
        self.assertEqual(bound["termination"], "timeout")

    def test_later_bound_command_cannot_overtake_oldest_pending(self):
        token = self.store.create_session(
            token="pending-order-session-" + "o" * 24
        )["token"]
        anchor_ms = int(self.clock() * 1000)
        game = self.create_game(
            token,
            clock={
                "mode": "timed",
                "initial_ms": 1_000,
                "increment_ms": 0,
            },
            clock_state={
                "white_ms": 1_000,
                "black_ms": 1_000,
                "running_color": "white",
                "anchor_at_ms": anchor_ms,
            },
        )
        receipt_ms = anchor_ms + 999
        self.clock.seconds = receipt_ms / 1000
        first = self.store.begin_command(
            token,
            game["id"],
            "pending.order-first-0001",
            {
                "operation": "move",
                "move": "a2a3",
                "expected_revision": 0,
            },
            received_at_ms=receipt_ms,
        )
        second = self.store.begin_command(
            token,
            game["id"],
            "pending.order-second-0001",
            {"operation": "resign", "expected_revision": 0},
            received_at_ms=receipt_ms,
        )
        self.clock.seconds = (anchor_ms + 2_000) / 1000

        with self.assertRaises(PendingCommand):
            self.store.timeout_game(
                token,
                game["id"],
                expected_revision=0,
                timed_out_color="white",
                idempotency_key="pending.order-second-0001",
                command_lease=second["lease_token"],
                command_stage="game_completed",
            )
        self.assertEqual(
            self.store.get_game_for_owner(token, game["id"])["status"],
            "active",
        )
        completed = self.store.timeout_game(
            token,
            game["id"],
            expected_revision=0,
            timed_out_color="white",
            idempotency_key="pending.order-first-0001",
            command_lease=first["lease_token"],
            command_stage="game_completed",
        )
        self.assertEqual(completed["termination"], "timeout")

    def test_malformed_pending_command_blocks_timeout_fail_closed(self):
        token = self.store.create_session(
            token="pending-malformed-timeout-session-" + "x" * 24
        )["token"]
        anchor_ms = int(self.clock() * 1000)
        game = self.create_game(
            token,
            clock={
                "mode": "timed",
                "initial_ms": 1_000,
                "increment_ms": 0,
            },
            clock_state={
                "white_ms": 1_000,
                "black_ms": 1_000,
                "running_color": "white",
                "anchor_at_ms": anchor_ms,
            },
        )
        receipt_ms = anchor_ms + 999
        self.clock.seconds = receipt_ms / 1000
        self.store.begin_command(
            token,
            game["id"],
            "pending.bad-timeout-0001",
            {
                "operation": "move",
                "move": "a2a3",
                "expected_revision": 0,
            },
            received_at_ms=receipt_ms,
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE idempotent_commands
                SET stage_json = '{"received_at_ms":0,"request":{}}'
                WHERE idempotency_key = 'pending.bad-timeout-0001'
                """
            )
        self.clock.seconds = (anchor_ms + 2_000) / 1000

        with self.assertRaisesRegex(
            GameStoreError, "recovery data is invalid"
        ):
            self.store.timeout_game(
                token,
                game["id"],
                expected_revision=0,
                timed_out_color="white",
            )
        self.assertEqual(
            self.store.get_game_for_owner(token, game["id"])["status"],
            "active",
        )

    def test_session_owner_cannot_resign_engine_color(self):
        game = self.create_game()
        with self.assertRaisesRegex(
            InvalidTransition, "human player's color"
        ):
            self.store.resign_game(
                self.token,
                game["id"],
                expected_revision=0,
                resigning_color="black",
                received_at_ms=int(self.clock() * 1000),
            )

    def test_hidden_name_and_active_game_are_not_public(self):
        game = self.create_game(
            player_name="Secret Name",
            name_visible=False,
        )
        with self.assertRaises(NotFoundError):
            self.store.get_public_game(game["id"])

        completed = self.store.complete_game(
            self.token,
            game["id"],
            expected_revision=0,
            result="1-0",
            termination="resignation",
            actor="human",
        )
        self.assertTrue(completed["public_visible"])
        public = self.store.get_public_game(game["id"])
        self.assertNotIn("player_name", public)
        self.assertFalse(public["name_visible"])
        self.assertNotIn("publish_on_completion", public)
        self.assertNotIn("public_visible", public)
        self.assertNotIn("owner_session_id", repr(public))
        owner = self.store.get_game_for_owner(self.token, game["id"])
        self.assertEqual(owner["player_name"], "Secret Name")
        archive = self.store.sanitized_export(game["id"])
        self.assertNotIn("Secret Name", repr(archive))
        self.assertNotIn(self.token, repr(archive))

    def test_completed_public_listing_is_bounded_and_cursor_based(self):
        identifiers = []
        for index in range(3):
            token = (
                self.token
                if index == 0
                else self.store.create_session(
                    token=f"listing-session-{index}-" + "y" * 24
                )["token"]
            )
            game = self.create_game(token=token, player_name=f"P{index}")
            self.clock.advance(1)
            self.store.complete_game(
                token,
                game["id"],
                expected_revision=0,
                result="1/2-1/2",
                termination="agreement",
            )
            identifiers.append(game["id"])

        first = self.store.list_completed_public_games(limit=2)
        self.assertEqual(len(first["games"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        second = self.store.list_completed_public_games(
            limit=2, cursor=first["next_cursor"]
        )
        listed = first["games"] + second["games"]
        seen = [game["id"] for game in listed]
        self.assertCountEqual(seen, identifiers)
        self.assertTrue(all(game["final_fen"] == START_FEN for game in listed))
        self.assertTrue(all("current_fen" not in game for game in listed))
        with self.assertRaises(ValidationError):
            self.store.list_completed_public_games(limit=101)

    def test_cursor_rejects_malformed_and_out_of_sqlite_range_values(self):
        public_id = "a" * 32
        oversized = base64.urlsafe_b64encode(
            f"{2**80}:{public_id}".encode("ascii")
        ).decode("ascii").rstrip("=")
        negative = base64.urlsafe_b64encode(
            f"-1:{public_id}".encode("ascii")
        ).decode("ascii").rstrip("=")
        for cursor in ("!!!!", oversized, negative):
            with self.subTest(cursor=cursor):
                with self.assertRaises(ValidationError):
                    self.store.list_completed_public_games(cursor=cursor)

    def test_cleanup_abandons_expired_games_then_purges_old_abandoned(self):
        store = GameStore(
            self.path,
            clock=self.clock,
            abandon_after_seconds=30,
            abandoned_retention_seconds=120,
        )
        expiring = store.create_session(
            token="expiring-session-" + "z" * 24,
            ttl_seconds=60,
        )["token"]
        game = store.create_game(
            expiring,
            start_fen=START_FEN,
            human_color="white",
            model_id="v37-test",
            model_sha256=MODEL_SHA,
            preset="policy-only",
            nodes=1,
            temperature=0.8,
            backend="sycl",
            clock={"mode": "unlimited"},
            player_name="Forget Me",
            name_visible=False,
        )

        self.clock.advance(61)
        cleaned = store.cleanup()
        self.assertEqual(cleaned["abandoned_games"], 1)
        self.assertEqual(cleaned["expired_sessions"], 1)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT status, result, termination, public_visible,
                       owner_session_id, revision, player_name
                FROM games WHERE public_id = ?
                """,
                (game["id"],),
            ).fetchone()
        self.assertEqual(
            row, ("abandoned", "*", "abandoned", 0, None, 1, None)
        )

        self.clock.advance(121)
        purged = store.cleanup()
        self.assertEqual(purged["purged_abandoned_games"], 1)
        with sqlite3.connect(self.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM games WHERE public_id = ?",
                (game["id"],),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_default_cleanup_abandons_after_two_inactive_days(self):
        game = self.create_game()

        self.clock.advance(2 * 24 * 60 * 60 - 1)
        before_boundary = self.store.cleanup()
        self.assertEqual(before_boundary["abandoned_games"], 0)
        self.assertEqual(
            self.store.get_game_for_owner(self.token, game["id"])["status"],
            "active",
        )

        self.clock.advance(2)
        after_boundary = self.store.cleanup()
        self.assertEqual(after_boundary["abandoned_games"], 1)
        self.assertEqual(
            self.store.get_game_for_owner(self.token, game["id"])["status"],
            "abandoned",
        )

    def test_cleanup_scrubs_hidden_names_and_arbitrary_command_caches(self):
        secret_name = "PRIVATE_DISPLAY_NAME_92"
        secret_response = "PRIVATE_COMMAND_RESPONSE_73"
        game = self.create_game(
            player_name=secret_name,
            name_visible=False,
            idempotency_key="create.private-0001",
        )
        begun = self.store.begin_command(
            self.token,
            game["id"],
            "private.command-0001",
            {"operation": "probe"},
        )
        self.store.complete_command(
            self.token,
            game["id"],
            "private.command-0001",
            {"message": secret_response},
            lease_token=begun["lease_token"],
        )
        self.assertTrue(self.store.revoke_session(self.token))

        cleaned = self.store.cleanup()

        self.assertGreaterEqual(cleaned["scrubbed_command_caches"], 1)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM idempotent_commands"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM session_commands"
                ).fetchone()[0],
                0,
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT player_name FROM games WHERE public_id = ?",
                    (game["id"],),
                ).fetchone()[0]
            )
        with open(self.path, "rb") as database:
            contents = database.read()
        self.assertNotIn(secret_name.encode("utf-8"), contents)
        self.assertNotIn(secret_response.encode("utf-8"), contents)

    def test_health_check_is_read_only_and_requires_current_schema(self):
        with open(self.path, "rb") as database:
            before = database.read()
        self.assertTrue(self.store.health_check())
        with open(self.path, "rb") as database:
            self.assertEqual(database.read(), before)

        with sqlite3.connect(self.path) as connection:
            connection.execute("PRAGMA user_version=2")
        with self.assertRaisesRegex(GameStoreError, "schema 2"):
            self.store.health_check()

    def test_health_check_rejects_missing_database_without_recreating_it(self):
        os.unlink(self.path)
        with self.assertRaisesRegex(NotFoundError, "missing"):
            self.store.health_check()
        self.assertFalse(os.path.exists(self.path))

        incomplete_path = os.path.join(
            self.temp.name, "missing-table.sqlite3"
        )
        incomplete = GameStore(incomplete_path, clock=self.clock)
        with sqlite3.connect(incomplete_path) as connection:
            connection.execute("DROP TABLE session_commands")
        with self.assertRaisesRegex(GameStoreError, "required schema"):
            incomplete.health_check()

    def test_health_check_probes_core_columns_of_every_required_table(self):
        required_tables = (
            "sessions",
            "games",
            "turns",
            "move_legs",
            "game_events",
            "idempotent_commands",
            "session_commands",
        )
        for table in required_tables:
            with self.subTest(table=table):
                path = os.path.join(
                    self.temp.name, f"malformed-{table}.sqlite3"
                )
                store = GameStore(path, clock=self.clock)
                with sqlite3.connect(path) as connection:
                    connection.execute("PRAGMA foreign_keys=OFF")
                    connection.execute(f"DROP TABLE {table}")
                    connection.execute(
                        f"CREATE TABLE {table}(id INTEGER PRIMARY KEY)"
                    )
                with self.assertRaisesRegex(
                    GameStoreError, "health check failed"
                ):
                    store.health_check()

    def test_health_check_rejects_corrupt_or_unopenable_database(self):
        corrupt_path = os.path.join(self.temp.name, "corrupt.sqlite3")
        corrupt = GameStore(corrupt_path, clock=self.clock)
        with open(corrupt_path, "wb") as database:
            database.write(b"not a sqlite database")
        with self.assertRaisesRegex(GameStoreError, "health check failed"):
            corrupt.health_check()

        unopenable_path = os.path.join(
            self.temp.name, "unopenable.sqlite3"
        )
        unopenable = GameStore(unopenable_path, clock=self.clock)
        saved_path = unopenable_path + ".saved"
        os.replace(unopenable_path, saved_path)
        os.mkdir(unopenable_path)
        try:
            with self.assertRaisesRegex(
                GameStoreError, "health check failed"
            ):
                unopenable.health_check()
        finally:
            os.rmdir(unopenable_path)
            os.replace(saved_path, unopenable_path)

    def test_health_check_busy_timeout_is_bounded(self):
        path = os.path.join(self.temp.name, "locked.sqlite3")
        store = GameStore(
            path,
            clock=self.clock,
            busy_timeout_ms=25,
        )
        locker = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(locker.close)
        locker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                GameStoreError, "health check failed"
            ):
                store.health_check()
        finally:
            locker.rollback()
        self.assertLess(time.monotonic() - started, 1)

    def test_backup_restores_complete_history(self):
        game = self.create_game(player_name="Published", name_visible=True)
        moved = self.store.record_turn(
            self.token,
            game["id"],
            expected_revision=0,
            actor="human",
            color="white",
            legs=[{"uci": "a2a3", "after_fen": FEN_1}],
            clock_state={},
        )
        self.store.complete_game(
            self.token,
            game["id"],
            expected_revision=moved["revision"],
            result="1-0",
            termination="resignation",
            actor="engine",
        )
        backup_path = os.path.join(self.temp.name, "backup.sqlite3")

        metadata = self.store.backup(backup_path)

        self.assertEqual(metadata["integrity_check"], ["ok"])
        self.assertGreater(metadata["size_bytes"], 0)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(backup_path).st_mode & 0o777, 0o600)
        restored = GameStore(backup_path, clock=self.clock)
        public = restored.get_public_game(game["id"], include_history=True)
        self.assertEqual(public["player_name"], "Published")
        self.assertEqual(public["active_moves"], ["a2a3"])
        self.assertEqual(len(public["turns"]), 1)
        self.assertEqual(restored.integrity_check(), ["ok"])
        with self.assertRaises(ValidationError):
            self.store.backup(backup_path)

    def test_backup_rejects_hardlink_alias_of_source(self):
        alias_path = os.path.join(self.temp.name, "database-alias.sqlite3")
        os.link(self.path, alias_path)

        with self.assertRaisesRegex(ValidationError, "aliases"):
            self.store.backup(alias_path, overwrite=True)
        self.assertTrue(os.path.samefile(self.path, alias_path))


if __name__ == "__main__":
    unittest.main()
