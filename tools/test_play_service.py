#!/usr/bin/env python3
"""Application-level tests for the anonymous public play service."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import os
import sqlite3
import tempfile
import unittest

try:
    from .game_store import GameStore, PendingCommand
    from .play_service import (
        ModelChoice,
        PRESETS,
        PublicPlayError,
        PublicPlayService,
        TokenBucketLimiter,
    )
except ImportError:
    from game_store import GameStore, PendingCommand
    from play_service import (
        ModelChoice,
        PRESETS,
        PublicPlayError,
        PublicPlayService,
        TokenBucketLimiter,
    )


class FakeClock:
    def __init__(self, seconds=1_800_000_000.0):
        self.value = float(seconds)

    def __call__(self):
        return self.value

    def advance_ms(self, milliseconds):
        self.value += milliseconds / 1000


def fake_fen(turn, index):
    return (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
        f"{turn} KQkq - {index} 1"
    )


def position(turn, legal, *, mid=False, white=True, black=True, result="ongoing", index=0):
    return {
        "fen": fake_fen(turn, index),
        "turn": turn,
        "ability": {"white": white, "black": black, "mid": mid},
        "result": result,
        "legal": list(legal),
    }


class ScriptedEngine:
    def __init__(self, clock):
        self.clock = clock
        self.states = {
            (): position("w", ["e2e4", "e2e4+", "d2d4"], index=0),
            ("e2e4",): position(
                "b", ["e7e5", "e7e5+"], index=1
            ),
            ("e2e4+",): position(
                "w", ["g1f3"], mid=True, white=False, index=1
            ),
            ("e2e4+", "g1f3"): position(
                "b", ["e7e5", "e7e5+"], white=False, index=2
            ),
            ("e2e4", "e7e5"): position(
                "w", ["g1f3", "g1f3+"], index=2
            ),
            ("e2e4", "e7e5+"): position(
                "b", ["b8c6"], mid=True, black=False, index=2
            ),
            ("e2e4", "e7e5+", "b8c6"): position(
                "w", ["g1f3", "g1f3+"], black=False, index=3
            ),
            ("e2e4+", "g1f3", "e7e5"): position(
                "w", ["f1c4"], white=False, index=3
            ),
            ("e2e4+", "g1f3", "e7e5+"): position(
                "b", ["b8c6"], mid=True, white=False, black=False, index=3
            ),
            ("e2e4+", "g1f3", "e7e5+", "b8c6"): position(
                "w", ["f1c4"], white=False, black=False, index=4
            ),
            ("d2d4",): position(
                "b",
                [],
                result="white",
                index=1,
            ),
        }
        self.current_moves = ()
        self.next_moves = []
        self.search_advance_ms = []
        self.options = []
        self.searches = []
        self.positions = []
        self.error = None

    def set_position(self, _fen, moves):
        self.current_moves = tuple(moves)
        self.positions.append(self.current_moves)
        if self.error is not None:
            raise self.error
        if self.current_moves not in self.states:
            raise AssertionError(f"missing scripted state {self.current_moves!r}")

    def position_info(self):
        return copy.deepcopy(self.states[self.current_moves])

    def set_option(self, name, value):
        self.options.append((name, str(value)))

    def go_nodes(self, nodes):
        self.searches.append(nodes)
        if self.search_advance_ms:
            self.clock.advance_ms(self.search_advance_ms.pop(0))
        if not self.next_moves:
            raise AssertionError("test did not provide an engine move")
        move = self.next_moves.pop(0)
        return move, None, f"info pv {move}"


class EngineSessions:
    def __init__(self, engine):
        self.engine = engine
        self.contexts = []

    @contextmanager
    def __call__(self, context_id, model_id, backend):
        self.contexts.append((context_id, model_id, backend))
        yield self.engine


class PublicPlayServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = FakeClock()
        self.database_path = os.path.join(self.temporary.name, "games.sqlite3")
        self.store = GameStore(
            self.database_path,
            clock=self.clock,
            command_lease_seconds=300,
        )
        self.engine = ScriptedEngine(self.clock)
        self.sessions = EngineSessions(self.engine)
        self.csrf_secret = b"test csrf secret".ljust(32, b"!")
        self.service = self.make_service()
        config = self.service.get_config(None)
        self.token = config.session_token
        self.csrf = config.payload["csrf_token"]
        self.key_index = 0

    def make_service(
        self,
        *,
        stage_hook=None,
        config_rate_limiter=None,
        global_config_refresh_rate_limiter=None,
        global_create_rate_limiter=None,
        global_mutation_rate_limiter=None,
        global_owner_read_rate_limiter=None,
        models=None,
        compatibility_models=(),
        default_model_id="v37-public",
        backend="sycl",
    ):
        limiter = TokenBucketLimiter(
            capacity=1000,
            refill_per_second=1000,
            clock=self.clock,
        )
        return PublicPlayService(
            self.store,
            self.sessions,
            models=models or (
                ModelChoice(
                    id="v37-public",
                    label="v37 public",
                    sha256="a" * 64,
                ),
            ),
            compatibility_models=compatibility_models,
            default_model_id=default_model_id,
            backend=backend,
            csrf_secret=self.csrf_secret,
            clock=self.clock,
            rate_limiter=limiter,
            config_rate_limiter=config_rate_limiter,
            global_config_refresh_rate_limiter=(
                global_config_refresh_rate_limiter
            ),
            global_create_rate_limiter=global_create_rate_limiter,
            global_mutation_rate_limiter=global_mutation_rate_limiter,
            global_owner_read_rate_limiter=global_owner_read_rate_limiter,
            stage_hook=stage_hook,
        )

    def key(self):
        self.key_index += 1
        return f"request-key-{self.key_index:04d}"

    def create(
        self,
        *,
        color="white",
        preset="club",
        clock="unlimited",
        name="",
        publish=False,
        key=None,
    ):
        return self.service.create_game(
            self.token,
            self.csrf,
            key or self.key(),
            {
                "display_name": name,
                "publish_name": publish,
                "color": color,
                "preset_id": preset,
                "clock_id": clock,
            },
        )["game"]

    def create_advanced(
        self,
        *,
        color="white",
        model_id="v37-public",
        nodes=37,
        temperature=0.4,
        temp_decay_moves=12,
        clock=None,
        start_fen=None,
        key=None,
    ):
        return self.service.create_game(
            self.token,
            self.csrf,
            key or self.key(),
            {
                "display_name": "",
                "publish_name": False,
                "color": color,
                "preset_id": "policy",
                "clock_id": "unlimited",
                "advanced": {
                    "model_id": model_id,
                    "nodes": nodes,
                    "temperature": temperature,
                    "temp_decay_moves": temp_decay_moves,
                    "clock": clock or {"mode": "unlimited"},
                    **(
                        {"start_fen": start_fen}
                        if start_fen is not None
                        else {}
                    ),
                },
            },
        )["game"]

    def move(self, game, move, *, key=None):
        return self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key or self.key(),
            {"move": move, "expected_revision": game["revision"]},
        )["game"]

    def test_config_issues_token_and_exposes_safe_advanced_choices(self):
        config = self.service.get_config(None)
        cookie = self.service.session_cookie_header(config.session_token)

        self.assertNotIn(config.session_token, repr(config.payload))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertEqual(
            [preset["id"] for preset in config.payload["presets"]],
            [
                "policy",
                "policy-deterministic",
                "casual",
                "club",
                "full-temperature",
                "full",
            ],
        )
        self.assertEqual(
            config.payload["models"],
            [{"id": "v37-public", "label": "v37 public"}],
        )
        self.assertEqual(config.payload["default_model_id"], "v37-public")
        self.assertEqual(
            [
                (
                    preset["nodes"],
                    preset["temperature"],
                    preset["temp_decay_moves"],
                )
                for preset in config.payload["presets"]
            ],
            [
                (1, 0.5, 10),
                (1, 0.0, 0),
                (25, 0.5, 10),
                (100, 0.5, 10),
                (800, 0.5, 10),
                (800, 0.0, 0),
            ],
        )
        with open(self.database_path, "rb") as database:
            self.assertNotIn(config.session_token.encode("ascii"), database.read())

    def test_config_refreshes_session_and_surfaces_active_game(self):
        game = self.create()
        refreshed = self.service.get_config(self.token)

        self.assertFalse(refreshed.set_cookie)
        self.assertEqual(refreshed.session_token, self.token)
        self.assertEqual(refreshed.payload["active_game_id"], game["id"])

    def test_unchanged_owner_poll_uses_cached_authoritative_position(self):
        game = self.create()
        inspected_positions = len(self.engine.positions)
        engine_contexts = len(self.sessions.contexts)

        first = self.service.get_owner_game(self.token, game["id"])["game"]
        second = self.service.get_owner_game(self.token, game["id"])["game"]

        self.assertEqual(first["revision"], game["revision"])
        self.assertEqual(second["legal_moves"], game["legal_moves"])
        self.assertEqual(len(self.engine.positions), inspected_positions)
        self.assertEqual(len(self.sessions.contexts), engine_contexts)

    def test_csrf_origin_and_rate_limit_guards(self):
        with self.assertRaisesRegex(PublicPlayError, "CSRF"):
            self.service.verify_csrf(self.token, "wrong")
        PublicPlayService.validate_write_origin(
            host="double.example",
            origin="https://double.example",
            sec_fetch_site="same-origin",
        )
        with self.assertRaisesRegex(PublicPlayError, "Cross-origin"):
            PublicPlayService.validate_write_origin(
                host="double.example",
                origin="https://evil.example",
                sec_fetch_site="cross-site",
            )
        limiter = TokenBucketLimiter(
            capacity=1, refill_per_second=0.01, clock=self.clock
        )
        limiter.consume("session-digest")
        with self.assertRaisesRegex(PublicPlayError, "Too many"):
            limiter.consume("session-digest")

    def test_exhausted_config_limit_creates_no_session_and_recovers(self):
        active = self.create()
        limiter = TokenBucketLimiter(
            capacity=1,
            refill_per_second=1,
            maximum_buckets=1,
            clock=self.clock,
        )
        bounded = self.make_service(config_rate_limiter=limiter)

        bounded.get_config(None)
        with sqlite3.connect(self.database_path) as connection:
            sessions_after_first = connection.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0]

        with self.assertRaises(PublicPlayError) as raised:
            bounded.get_config(None)
        self.assertEqual(raised.exception.code, "rate_limited")
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0],
                sessions_after_first,
            )

        existing = bounded.get_config(self.token)
        self.assertEqual(existing.session_token, self.token)
        self.assertEqual(existing.payload["active_game_id"], active["id"])
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0],
                sessions_after_first,
            )

        self.clock.advance_ms(1_000)
        bounded.get_config(None)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0],
                sessions_after_first + 1,
            )

    def test_fresh_sessions_cannot_bypass_global_create_limit(self):
        limiter = TokenBucketLimiter(
            capacity=2,
            refill_per_second=1,
            maximum_buckets=1,
            clock=self.clock,
        )
        bounded = self.make_service(global_create_rate_limiter=limiter)
        sessions = [bounded.get_config(None) for _ in range(3)]

        def create_for(config, key):
            return bounded.create_game(
                config.session_token,
                config.payload["csrf_token"],
                key,
                {
                    "display_name": "",
                    "publish_name": False,
                    "color": "white",
                    "preset_id": "club",
                    "clock_id": "unlimited",
                },
            )

        create_for(sessions[0], "global-create-0001")
        create_for(sessions[1], "global-create-0002")
        with self.assertRaises(PublicPlayError) as raised:
            create_for(sessions[2], "global-create-0003")
        self.assertEqual(raised.exception.code, "rate_limited")

        self.clock.advance_ms(1_000)
        recovered = create_for(sessions[2], "global-create-0003")
        self.assertEqual(recovered["game"]["status"], "active")

    def test_existing_sessions_share_recovering_config_work_limit(self):
        other = self.service.get_config(None)
        limiter = TokenBucketLimiter(
            capacity=1,
            refill_per_second=1,
            maximum_buckets=1,
            clock=self.clock,
        )
        bounded = self.make_service(
            global_config_refresh_rate_limiter=limiter
        )

        first = bounded.get_config(self.token)
        self.assertEqual(first.session_token, self.token)
        with self.assertRaises(PublicPlayError) as raised:
            bounded.get_config(other.session_token)
        self.assertEqual(raised.exception.code, "rate_limited")

        self.clock.advance_ms(1_000)
        recovered = bounded.get_config(other.session_token)
        self.assertEqual(recovered.session_token, other.session_token)

    def test_distinct_sessions_share_recovering_global_mutation_limit(self):
        first = self.create()
        other = self.service.get_config(None)
        second = self.service.create_game(
            other.session_token,
            other.payload["csrf_token"],
            "other-create-0001",
            {
                "display_name": "",
                "publish_name": False,
                "color": "white",
                "preset_id": "club",
                "clock_id": "unlimited",
            },
        )["game"]
        limiter = TokenBucketLimiter(
            capacity=1,
            refill_per_second=1,
            maximum_buckets=1,
            clock=self.clock,
        )
        bounded = self.make_service(
            global_mutation_rate_limiter=limiter
        )

        bounded.move(
            self.token,
            self.csrf,
            first["id"],
            "global-move-0001",
            {"move": "e2e4+", "expected_revision": first["revision"]},
        )
        with self.assertRaises(PublicPlayError) as raised:
            bounded.move(
                other.session_token,
                other.payload["csrf_token"],
                second["id"],
                "global-move-0002",
                {
                    "move": "e2e4+",
                    "expected_revision": second["revision"],
                },
            )
        self.assertEqual(raised.exception.code, "rate_limited")

        self.clock.advance_ms(1_000)
        recovered = bounded.move(
            other.session_token,
            other.payload["csrf_token"],
            second["id"],
            "global-move-0002",
            {
                "move": "e2e4+",
                "expected_revision": second["revision"],
            },
        )
        self.assertEqual(recovered["game"]["moves"], ["e2e4+"])

    def test_distinct_sessions_share_recovering_owner_work_limit(self):
        first = self.create()
        other = self.service.get_config(None)
        second = self.service.create_game(
            other.session_token,
            other.payload["csrf_token"],
            "owner-read-create-0001",
            {
                "display_name": "",
                "publish_name": False,
                "color": "white",
                "preset_id": "club",
                "clock_id": "unlimited",
            },
        )["game"]
        limiter = TokenBucketLimiter(
            capacity=1,
            refill_per_second=1,
            maximum_buckets=1,
            clock=self.clock,
        )
        bounded = self.make_service(
            global_owner_read_rate_limiter=limiter
        )

        bounded.get_owner_game(self.token, first["id"])
        with self.assertRaises(PublicPlayError) as raised:
            bounded.get_owner_game(other.session_token, second["id"])
        self.assertEqual(raised.exception.code, "rate_limited")

        self.clock.advance_ms(1_000)
        recovered = bounded.get_owner_game(
            other.session_token, second["id"]
        )
        self.assertEqual(recovered["game"]["id"], second["id"])

    def test_create_shapes_state_and_stores_exact_private_settings(self):
        game = self.create(
            color="white",
            preset="casual",
            clock="5+3",
            name="Alice",
            publish=False,
        )

        self.assertEqual(game["moves"], [])
        self.assertEqual(game["to_move"], "white")
        self.assertEqual(game["legal_moves"], ["e2e4", "e2e4+", "d2d4"])
        self.assertEqual(game["players"]["white"]["name"], "Alice")
        self.assertEqual(game["players"]["black"]["type"], "engine")
        self.assertEqual(game["players"]["black"]["name"], "Double Strike Chess Engine")
        self.assertEqual(game["clocks"]["white_ms"], 300_000)
        self.assertEqual(game["model_id"], "v37-public")
        self.assertEqual(game["nodes"], 25)
        self.assertEqual(game["temperature"], 0.5)
        self.assertEqual(game["temp_decay_moves"], 10)
        self.assertEqual(game["backend"], "sycl")
        self.assertNotIn("model", game)

        stored = self.store.get_game_for_owner(self.token, game["id"])
        self.assertEqual(stored["nodes"], 25)
        self.assertEqual(stored["temperature"], 0.5)
        self.assertEqual(stored["backend"], "sycl")

    def test_deterministic_policy_preset_uses_exact_engine_settings(self):
        game = self.create(color="white", preset="policy-deterministic")

        self.assertEqual(game["preset_id"], "policy-deterministic")
        self.assertEqual(game["preset_label"], "Policy only · deterministic")
        self.assertEqual(game["nodes"], 1)
        self.assertEqual(game["temperature"], 0.0)
        self.assertEqual(game["temp_decay_moves"], 0)
        stored = self.store.get_game_for_owner(self.token, game["id"])
        self.assertEqual(stored["preset"], "policy-deterministic")
        self.assertEqual(stored["temperature"], 0.0)
        self.assertEqual(stored["temp_decay_moves"], 0)

        self.engine.next_moves = ["e7e5"]
        self.move(game, "e2e4")
        self.assertEqual(self.engine.searches, [1])
        self.assertIn(("Temperature", "0"), self.engine.options)
        self.assertIn(("TempDecayMoves", "0"), self.engine.options)
        self.assertIn(("PolicyOnlyAvoidTwofold", "true"), self.engine.options)

    def test_varied_full_strength_preset_uses_exact_engine_settings(self):
        game = self.create(color="white", preset="full-temperature")

        self.assertEqual(game["preset_id"], "full-temperature")
        self.assertEqual(game["preset_label"], "Full strength · varied")
        self.assertEqual(game["nodes"], 800)
        self.assertEqual(game["temperature"], 0.5)
        self.assertEqual(game["temp_decay_moves"], 10)
        stored = self.store.get_game_for_owner(self.token, game["id"])
        self.assertEqual(stored["preset"], "full-temperature")
        self.assertEqual(stored["temperature"], 0.5)
        self.assertEqual(stored["temp_decay_moves"], 10)

        self.engine.next_moves = ["e7e5"]
        self.move(game, "e2e4")
        self.assertEqual(self.engine.searches, [800])
        self.assertIn(("Temperature", "0.5"), self.engine.options)
        self.assertIn(("TempDecayMoves", "10"), self.engine.options)
        self.assertNotIn(("PolicyOnlyAvoidTwofold", "true"), self.engine.options)
        self.assertIn(("PolicyOnlyAvoidTwofold", "false"), self.engine.options)

    def test_advanced_create_persists_and_uses_exact_settings(self):
        self.service = self.make_service(
            models=(
                ModelChoice(
                    id="v37-public",
                    label="v37 public",
                    sha256="a" * 64,
                ),
                ModelChoice(
                    id="probe-model",
                    label="Probe model",
                    sha256="b" * 64,
                ),
            )
        )
        game = self.create_advanced(
            model_id="probe-model",
            nodes=37,
            temperature=0.4,
            temp_decay_moves=12,
            clock={
                "mode": "timed",
                "initial_ms": 123_000,
                "increment_ms": 4_500,
            },
        )

        stored = self.store.get_game_for_owner(self.token, game["id"])
        self.assertEqual(stored["model"]["id"], "probe-model")
        self.assertEqual(stored["model"]["sha256"], "b" * 64)
        self.assertEqual(stored["preset"], "advanced")
        self.assertEqual(stored["nodes"], 37)
        self.assertEqual(stored["temperature"], 0.4)
        self.assertEqual(stored["temp_decay_moves"], 12)
        self.assertEqual(
            stored["clock"],
            {
                "mode": "timed",
                "initial_ms": 123_000,
                "increment_ms": 4_500,
            },
        )
        self.assertEqual(game["preset_label"], "Advanced")
        self.assertEqual(game["clock_id"], "custom-123000-4500")
        self.assertEqual(game["clock_label"], "2:03 + 4.5")

        self.engine.next_moves = ["e7e5"]
        self.move(game, "e2e4")
        self.assertEqual(self.engine.searches, [37])
        self.assertIn(("Temperature", "0.4"), self.engine.options)
        self.assertIn(("TempDecayMoves", "12"), self.engine.options)
        self.assertIn(
            ("public:" + game["id"], "probe-model", "sycl"),
            self.sessions.contexts,
        )
        self.assertEqual(
            self.engine.options[-2:],
            [("Temperature", "0"), ("TempDecayMoves", "0")],
        )

    def test_advanced_create_accepts_and_persists_variant_fen(self):
        start_fen = (
            "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1 W-"
        )
        game = self.create_advanced(start_fen=start_fen)

        stored = self.store.get_game_for_owner(self.token, game["id"])
        self.assertEqual(game["initial_fen"], start_fen)
        self.assertEqual(stored["start_fen"], start_fen)
        self.assertTrue(
            any(
                context.startswith("validation:")
                for context, _model, _backend in self.sessions.contexts
            )
        )

    def test_advanced_create_rejects_unsafe_or_out_of_range_values(self):
        base = {
            "model_id": "v37-public",
            "nodes": 37,
            "temperature": 0.4,
            "temp_decay_moves": 12,
            "clock": {"mode": "unlimited"},
        }
        invalid = (
            {"model_id": "../../weights.pb.gz"},
            {"nodes": 0},
            {"nodes": 1_000_001},
            {"temperature": float("inf")},
            {"temperature": -0.1},
            {"temp_decay_moves": 641},
            {
                "clock": {
                    "mode": "timed",
                    "initial_ms": 999,
                    "increment_ms": 0,
                }
            },
            {
                "clock": {
                    "mode": "timed",
                    "initial_ms": 60_000,
                    "increment_ms": 3_600_001,
                }
            },
        )
        for override in invalid:
            with self.subTest(override=override):
                advanced = dict(base)
                advanced.update(override)
                with self.assertRaises(PublicPlayError):
                    self.service.create_game(
                        self.token,
                        self.csrf,
                        self.key(),
                        {
                            "display_name": "",
                            "publish_name": False,
                            "color": "white",
                            "preset_id": "policy",
                            "clock_id": "unlimited",
                            "advanced": advanced,
                        },
                    )

    def test_display_name_rejects_surrogates_but_accepts_unicode(self):
        with self.assertRaises(PublicPlayError) as raised:
            self.create(name="\ud800")
        self.assertEqual(raised.exception.status, 400)

        game = self.create(name="Éric ♞")
        self.assertEqual(game["players"]["white"]["name"], "Éric ♞")

    def test_random_color_is_stable_for_durable_create_retry(self):
        key = self.key()
        self.engine.next_moves = ["e2e4"]
        first = self.create(color="random", key=key)
        self.service = self.make_service()
        second = self.create(color="random", key=key)

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["human_color"], first["human_color"])

    def test_black_creation_runs_engine_before_returning(self):
        self.engine.next_moves = ["e2e4"]
        game = self.create(color="black", preset="policy")

        self.assertEqual(game["moves"], ["e2e4"])
        self.assertEqual(game["to_move"], "black")
        self.assertEqual(game["revision"], 1)
        self.assertEqual(self.engine.searches, [1])
        self.assertIn(("Temperature", "0.5"), self.engine.options)
        self.assertIn(("TempDecayMoves", "10"), self.engine.options)

    def test_human_double_turn_persists_each_leg_then_runs_engine(self):
        game = self.create(color="white")
        first = self.move(game, "e2e4+")

        self.assertEqual(first["moves"], ["e2e4+"])
        self.assertTrue(first["ability"]["mid"])
        self.assertEqual(first["to_move"], "white")
        self.assertEqual(first["revision"], 1)
        self.assertEqual(self.engine.searches, [])

        self.engine.next_moves = ["e7e5"]
        final = self.move(first, "g1f3")
        self.assertEqual(final["moves"], ["e2e4+", "g1f3", "e7e5"])
        self.assertFalse(final["ability"]["mid"])
        self.assertEqual(final["to_move"], "white")
        self.assertEqual(final["revision"], 3)

        stored = self.store.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertEqual(
            [
                [leg["uci"] for leg in turn["legs"]]
                for turn in stored["turns"]
                if turn["active"]
            ],
            [["e2e4+", "g1f3"], ["e7e5"]],
        )

    def test_engine_double_turn_validates_and_persists_both_legs(self):
        game = self.create(color="white")
        self.engine.next_moves = ["e7e5+", "b8c6"]
        final = self.move(game, "e2e4")

        self.assertEqual(final["moves"], ["e2e4", "e7e5+", "b8c6"])
        self.assertEqual(final["revision"], 2)
        self.assertEqual(self.engine.searches, [100, 100])
        stored = self.store.get_game_for_owner(
            self.token, game["id"], include_history=True
        )
        self.assertEqual(
            [leg["uci"] for leg in stored["turns"][-1]["legs"]],
            ["e7e5+", "b8c6"],
        )

    def test_clock_runs_through_both_legs_and_increments_once_per_turn(self):
        game = self.create(color="white", clock="3+2")
        self.clock.advance_ms(1_000)
        first = self.move(game, "e2e4+")
        self.assertEqual(first["clocks"]["white_ms"], 179_000)

        self.clock.advance_ms(1_000)
        self.engine.search_advance_ms = [2_000]
        self.engine.next_moves = ["e7e5"]
        final = self.move(first, "g1f3")

        self.assertEqual(final["clocks"]["white_ms"], 180_000)
        self.assertEqual(final["clocks"]["black_ms"], 180_000)
        self.assertEqual(final["clocks"]["running_color"], "white")

    def test_human_timeout_before_move_completes_without_accepting_leg(self):
        game = self.create(color="white", clock="3+2")
        self.clock.advance_ms(180_001)
        final = self.move(game, "e2e4")

        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"], "0-1")
        self.assertEqual(final["termination"], "timeout")
        self.assertEqual(final["moves"], [])
        self.assertEqual(final["clocks"]["white_ms"], 0)

    def test_started_move_reclaim_uses_first_timely_receipt(self):
        game = self.create(clock="3+2")
        key = self.key()
        body = {"move": "e2e4", "expected_revision": game["revision"]}
        self.clock.advance_ms(179_999)
        original = self.service._apply_human_move

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.service._apply_human_move = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.move(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.service._apply_human_move = original

        self.clock.advance_ms(301_000)
        self.engine.next_moves = ["e7e5"]
        recovered = self.service.move(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertEqual(recovered["status"], "active")
        self.assertEqual(recovered["moves"], ["e2e4", "e7e5"])
        self.assertEqual(recovered["clocks"]["white_ms"], 2_001)

    def test_started_move_reclaim_preserves_first_expired_receipt(self):
        game = self.create(clock="3+2")
        key = self.key()
        body = {"move": "e2e4", "expected_revision": game["revision"]}
        self.clock.advance_ms(180_001)
        original = self.service._apply_human_move

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.service._apply_human_move = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.move(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.service._apply_human_move = original

        self.clock.advance_ms(301_000)
        recovered = self.service.move(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["termination"], "timeout")
        self.assertEqual(recovered["moves"], [])

    def test_owner_get_reports_live_started_move_without_advancing_game(self):
        game = self.create(clock="3+2")
        key = self.key()
        body = {"move": "e2e4", "expected_revision": game["revision"]}
        original = self.service._apply_human_move

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.service._apply_human_move = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.move(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.service._apply_human_move = original

        self.clock.advance_ms(180_001)
        inspections_before = len(self.engine.positions)
        pending = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]

        self.assertTrue(pending["pending_command"])
        self.assertEqual(pending["status"], "active")
        self.assertEqual(pending["revision"], game["revision"])
        self.assertEqual(pending["moves"], [])
        self.assertEqual(pending["clocks"]["white_ms"], 0)
        self.assertEqual(len(self.engine.positions), inspections_before)

    def test_owner_get_reclaims_timely_started_move_and_post_replays(self):
        game = self.create(clock="3+2")
        key = self.key()
        body = {"move": "e2e4", "expected_revision": game["revision"]}
        self.clock.advance_ms(179_999)
        original = self.service._apply_human_move

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.service._apply_human_move = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.move(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.service._apply_human_move = original

        self.clock.advance_ms(301_000)
        self.engine.next_moves = ["e7e5"]
        recovered = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        replay = self.service.move(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertFalse(recovered["pending_command"])
        self.assertEqual(recovered["status"], "active")
        self.assertEqual(recovered["moves"], ["e2e4", "e7e5"])
        self.assertEqual(recovered["clocks"]["white_ms"], 2_001)
        self.assertEqual(replay, recovered)
        self.assertEqual(self.engine.searches, [100])

    def test_owner_get_reclaims_expired_started_move_and_post_replays(self):
        game = self.create(clock="3+2")
        key = self.key()
        body = {"move": "e2e4", "expected_revision": game["revision"]}
        self.clock.advance_ms(180_001)
        original = self.service._apply_human_move

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.service._apply_human_move = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.move(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.service._apply_human_move = original

        self.clock.advance_ms(301_000)
        recovered = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        replay = self.service.move(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertFalse(recovered["pending_command"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["termination"], "timeout")
        self.assertEqual(recovered["moves"], [])
        self.assertEqual(replay, recovered)

    def test_owner_get_fails_stale_started_command_without_wedging_game(self):
        game = self.create()
        key = self.key()
        request = {
            "operation": "move",
            "move": "e2e4",
            "expected_revision": game["revision"] + 1,
        }
        self.store.begin_command(
            self.token,
            game["id"],
            key,
            request,
            received_at_ms=self.service._now_ms(),
        )
        self.clock.advance_ms(301_000)

        restored = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]

        self.assertFalse(restored["pending_command"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["revision"], game["revision"])
        with self.assertRaises(PublicPlayError) as raised:
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4", "expected_revision": game["revision"] + 1},
            )
        self.assertEqual(raised.exception.code, "stale_revision")

    def test_owner_get_fails_invalid_started_command_without_wedging_game(self):
        game = self.create()
        key = self.key()
        request = {
            "operation": "resign",
            "expected_revision": 9_007_199_254_740_992,
        }
        self.store.begin_command(
            self.token,
            game["id"],
            key,
            request,
            received_at_ms=self.service._now_ms(),
        )
        self.clock.advance_ms(301_000)

        restored = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        command = self.store.begin_command(
            self.token,
            game["id"],
            key,
            request,
            received_at_ms=self.service._now_ms(),
        )

        self.assertFalse(restored["pending_command"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["revision"], game["revision"])
        self.assertEqual(command["status"], "failed")
        self.assertEqual(command["error"]["code"], "invalid_request")

    def test_owner_get_recovers_multiple_started_commands_oldest_first(self):
        game = self.create()
        stale_key = self.key()
        move_key = self.key()
        stale_request = {
            "operation": "resign",
            "expected_revision": game["revision"] + 1,
        }
        move_request = {
            "operation": "move",
            "move": "e2e4",
            "expected_revision": game["revision"],
        }
        self.store.begin_command(
            self.token,
            game["id"],
            stale_key,
            stale_request,
            received_at_ms=self.service._now_ms(),
        )
        self.clock.advance_ms(1)
        self.store.begin_command(
            self.token,
            game["id"],
            move_key,
            move_request,
            received_at_ms=self.service._now_ms(),
        )
        self.clock.advance_ms(301_000)
        self.engine.next_moves = ["e7e5"]

        after_oldest = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        recovered = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        replay = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            move_key,
            {
                "move": move_request["move"],
                "expected_revision": move_request["expected_revision"],
            },
        )["game"]

        self.assertTrue(after_oldest["pending_command"])
        self.assertEqual(after_oldest["revision"], game["revision"])
        self.assertFalse(recovered["pending_command"])
        self.assertEqual(recovered["moves"], ["e2e4", "e7e5"])
        self.assertEqual(replay, recovered)

    def test_owner_get_stays_locked_after_oldest_command_succeeds(self):
        game = self.create()
        move_key = self.key()
        later_key = self.key()
        move_request = {
            "operation": "move",
            "move": "e2e4",
            "expected_revision": game["revision"],
        }
        later_request = {
            "operation": "resign",
            "expected_revision": game["revision"],
        }
        self.store.begin_command(
            self.token,
            game["id"],
            move_key,
            move_request,
            received_at_ms=self.service._now_ms(),
        )
        self.clock.advance_ms(1)
        self.store.begin_command(
            self.token,
            game["id"],
            later_key,
            later_request,
            received_at_ms=self.service._now_ms(),
        )
        self.clock.advance_ms(301_000)
        self.engine.next_moves = ["e7e5"]

        after_move = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        after_later_failure = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]

        self.assertTrue(after_move["pending_command"])
        self.assertEqual(after_move["moves"], ["e2e4", "e7e5"])
        self.assertFalse(after_later_failure["pending_command"])
        self.assertEqual(after_later_failure["moves"], after_move["moves"])
        self.assertEqual(after_later_failure["status"], "active")

    def test_owner_get_recovers_cross_process_timeout_guard_race(self):
        game = self.create(clock="3+2")
        key = self.key()
        request = {
            "operation": "move",
            "move": "e2e4",
            "expected_revision": game["revision"],
        }
        self.clock.advance_ms(180_001)
        original_timeout = self.store.timeout_game
        injected = False

        def inject_pending_then_signal(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                self.store.begin_command(
                    self.token,
                    game["id"],
                    key,
                    request,
                    received_at_ms=self.service._now_ms() - 2,
                )
                raise PendingCommand(300_000)
            return original_timeout(*args, **kwargs)

        self.store.timeout_game = inject_pending_then_signal
        try:
            pending = self.service.get_owner_game(
                self.token, game["id"]
            )["game"]
        finally:
            self.store.timeout_game = original_timeout

        self.assertTrue(pending["pending_command"])
        self.assertEqual(pending["status"], "active")
        self.assertEqual(pending["revision"], game["revision"])

        self.clock.advance_ms(301_000)
        self.engine.next_moves = ["e7e5"]
        recovered = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]

        self.assertFalse(recovered["pending_command"])
        self.assertEqual(recovered["moves"], ["e2e4", "e7e5"])

    def test_engine_timeout_discards_generated_turn_and_records_loss(self):
        game = self.create(color="white", clock="3+2")
        self.engine.search_advance_ms = [180_001]
        self.engine.next_moves = ["e7e5"]
        final = self.move(game, "e2e4")

        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"], "1-0")
        self.assertEqual(final["termination"], "timeout")
        self.assertEqual(final["moves"], ["e2e4"])
        self.assertEqual(final["clocks"]["black_ms"], 0)

    def test_takeback_restores_previous_human_decision_and_context(self):
        game = self.create(color="white")
        self.engine.next_moves = ["e7e5"]
        played = self.move(game, "e2e4")
        taken = self.service.takeback(
            self.token,
            self.csrf,
            game["id"],
            self.key(),
            {"expected_revision": played["revision"]},
        )["game"]

        self.assertEqual(taken["moves"], [])
        self.assertEqual(taken["to_move"], "white")
        self.assertEqual(taken["takeback_count"], 1)
        self.assertTrue(
            all(
                context == "public:" + game["id"]
                and model_id == "v37-public"
                and backend == "sycl"
                for context, model_id, backend in self.sessions.contexts[1:]
            )
        )

    def test_expired_completed_pair_cannot_be_revived_by_takeback(self):
        game = self.create(clock="3+2")
        self.engine.next_moves = ["e7e5"]
        played = self.move(game, "e2e4")
        self.clock.advance_ms(played["clocks"]["white_ms"] + 1)
        key = self.key()
        body = {"expected_revision": played["revision"]}

        timed_out = self.service.takeback(
            self.token, self.csrf, game["id"], key, body
        )["game"]
        replay = self.service.takeback(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertEqual(timed_out["status"], "completed")
        self.assertEqual(timed_out["termination"], "timeout")
        self.assertEqual(timed_out["result"], "0-1")
        self.assertEqual(timed_out["moves"], ["e2e4", "e7e5"])
        self.assertEqual(timed_out["takeback_count"], 0)
        self.assertEqual(timed_out["clocks"]["white_ms"], 0)
        self.assertEqual(replay, timed_out)

    def test_expired_mid_double_cannot_be_revived_by_takeback(self):
        game = self.create(clock="3+2")
        partial = self.move(game, "e2e4+")
        self.clock.advance_ms(partial["clocks"]["white_ms"] + 1)

        timed_out = self.service.takeback(
            self.token,
            self.csrf,
            game["id"],
            self.key(),
            {"expected_revision": partial["revision"]},
        )["game"]

        self.assertEqual(timed_out["status"], "completed")
        self.assertEqual(timed_out["termination"], "timeout")
        self.assertEqual(timed_out["result"], "0-1")
        self.assertEqual(timed_out["moves"], [])
        self.assertEqual(timed_out["takeback_count"], 0)
        self.assertEqual(timed_out["clocks"]["white_ms"], 0)

    def test_started_takeback_reclaim_uses_first_timely_receipt(self):
        game = self.create(clock="3+2")
        self.engine.next_moves = ["e7e5"]
        played = self.move(game, "e2e4")
        self.clock.advance_ms(played["clocks"]["white_ms"] - 1)
        key = self.key()
        body = {"expected_revision": played["revision"]}
        original = self.store.takeback

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.store.takeback = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.takeback(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.store.takeback = original

        self.clock.advance_ms(301_000)
        recovered = self.service.takeback(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertEqual(recovered["status"], "active")
        self.assertEqual(recovered["moves"], [])
        self.assertEqual(recovered["takeback_count"], 1)

    def test_started_takeback_reclaim_preserves_first_expired_receipt(self):
        game = self.create(clock="3+2")
        self.engine.next_moves = ["e7e5"]
        played = self.move(game, "e2e4")
        self.clock.advance_ms(played["clocks"]["white_ms"] + 1)
        key = self.key()
        body = {"expected_revision": played["revision"]}
        original = self.store.takeback

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.store.takeback = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.takeback(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.store.takeback = original

        self.clock.advance_ms(301_000)
        recovered = self.service.takeback(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["termination"], "timeout")
        self.assertEqual(recovered["takeback_count"], 0)

    def test_owner_get_reclaims_started_takeback_and_post_replays(self):
        game = self.create(clock="3+2")
        self.engine.next_moves = ["e7e5"]
        played = self.move(game, "e2e4")
        self.clock.advance_ms(1_000)
        key = self.key()
        body = {"expected_revision": played["revision"]}
        original = self.store.takeback

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.store.takeback = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.takeback(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.store.takeback = original

        self.clock.advance_ms(301_000)
        recovered = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        replay = self.service.takeback(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertFalse(recovered["pending_command"])
        self.assertEqual(recovered["status"], "active")
        self.assertEqual(recovered["moves"], [])
        self.assertEqual(recovered["takeback_count"], 1)
        self.assertEqual(replay, recovered)

    def test_resign_charges_receipt_time_and_timeout_wins_at_boundary(self):
        before = self.create(clock="3+2")
        self.clock.advance_ms(1_234)
        key = self.key()
        body = {"expected_revision": before["revision"]}
        resigned = self.service.resign(
            self.token, self.csrf, before["id"], key, body
        )["game"]
        self.assertEqual(resigned["termination"], "resignation")
        self.assertEqual(resigned["clocks"]["white_ms"], 178_766)
        self.clock.advance_ms(10_000)
        self.assertEqual(
            self.service.resign(
                self.token, self.csrf, before["id"], key, body
            )["game"]["clocks"]["white_ms"],
            178_766,
        )

        exact = self.create(clock="3+2")
        self.clock.advance_ms(180_000)
        exact_result = self.service.resign(
            self.token,
            self.csrf,
            exact["id"],
            self.key(),
            {"expected_revision": exact["revision"]},
        )["game"]
        self.assertEqual(exact_result["termination"], "timeout")
        self.assertEqual(exact_result["result"], "0-1")
        self.assertEqual(exact_result["clocks"]["white_ms"], 0)

        after = self.create(clock="3+2")
        self.clock.advance_ms(180_001)
        after_result = self.service.resign(
            self.token,
            self.csrf,
            after["id"],
            self.key(),
            {"expected_revision": after["revision"]},
        )["game"]
        self.assertEqual(after_result["termination"], "timeout")
        self.assertEqual(after_result["clocks"]["white_ms"], 0)

    def test_resign_mid_double_charges_clock_while_voiding_partial_leg(self):
        game = self.create(clock="3+2")
        partial = self.move(game, "e2e4+")
        self.clock.advance_ms(1_000)

        resigned = self.service.resign(
            self.token,
            self.csrf,
            game["id"],
            self.key(),
            {"expected_revision": partial["revision"]},
        )["game"]

        self.assertEqual(resigned["termination"], "resignation")
        self.assertEqual(resigned["moves"], [])
        self.assertEqual(resigned["clocks"]["white_ms"], 179_000)

    def test_started_resign_reclaim_uses_first_timely_receipt(self):
        game = self.create(clock="3+2")
        self.clock.advance_ms(179_999)
        key = self.key()
        body = {"expected_revision": game["revision"]}
        original = self.store.resign_game

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.store.resign_game = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.resign(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.store.resign_game = original

        self.clock.advance_ms(301_000)
        recovered = self.service.resign(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["termination"], "resignation")
        self.assertEqual(recovered["clocks"]["white_ms"], 1)

    def test_started_resign_reclaim_preserves_first_expired_receipt(self):
        game = self.create(clock="3+2")
        self.clock.advance_ms(180_001)
        key = self.key()
        body = {"expected_revision": game["revision"]}
        original = self.store.resign_game

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.store.resign_game = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.resign(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.store.resign_game = original

        self.clock.advance_ms(301_000)
        recovered = self.service.resign(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["termination"], "timeout")
        self.assertEqual(recovered["clocks"]["white_ms"], 0)

    def test_owner_get_reclaims_started_resignation_and_post_replays(self):
        game = self.create(clock="3+2")
        self.clock.advance_ms(1_000)
        key = self.key()
        body = {"expected_revision": game["revision"]}
        original = self.store.resign_game

        def crash_before_transition(*_args, **_kwargs):
            raise SystemExit("simulated process death")

        self.store.resign_game = crash_before_transition
        try:
            with self.assertRaises(SystemExit):
                self.service.resign(
                    self.token, self.csrf, game["id"], key, body
                )
        finally:
            self.store.resign_game = original

        self.clock.advance_ms(301_000)
        recovered = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        replay = self.service.resign(
            self.token, self.csrf, game["id"], key, body
        )["game"]

        self.assertFalse(recovered["pending_command"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["termination"], "resignation")
        self.assertEqual(recovered["clocks"]["white_ms"], 179_000)
        self.assertEqual(replay, recovered)

    def test_idempotent_move_replays_and_changed_payload_conflicts(self):
        game = self.create(color="white")
        self.engine.next_moves = ["e7e5"]
        key = self.key()
        first = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4", "expected_revision": 0},
        )
        replay = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4", "expected_revision": 0},
        )

        self.assertEqual(replay, first)
        self.assertEqual(self.engine.searches, [100])
        with self.assertRaises(PublicPlayError) as raised:
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4+", "expected_revision": 0},
            )
        self.assertEqual(raised.exception.code, "idempotency_conflict")

    def test_stale_revision_and_illegal_move_are_safe_cached_failures(self):
        game = self.create(color="white")
        with self.assertRaises(PublicPlayError) as illegal:
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                self.key(),
                {"move": "a2a3", "expected_revision": 0},
            )
        self.assertEqual(illegal.exception.code, "illegal_move")

        with self.assertRaises(PublicPlayError) as stale:
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                self.key(),
                {"move": "e2e4", "expected_revision": 4},
            )
        self.assertEqual(stale.exception.code, "stale_revision")

    def test_active_game_is_owner_only_and_completed_game_is_public(self):
        game = self.create(name="Secret Name", publish=False)
        other = self.service.get_config(None)
        with self.assertRaises(PublicPlayError) as private:
            self.service.get_owner_game(other.session_token, game["id"])
        self.assertEqual(private.exception.status, 404)
        with self.assertRaises(PublicPlayError):
            self.service.get_public_game(game["id"])

        completed = self.service.resign(
            self.token,
            self.csrf,
            game["id"],
            self.key(),
            {"expected_revision": game["revision"]},
        )["game"]
        public = self.service.get_public_game(game["id"])["game"]

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(public["players"]["white"]["name"], "Anonymous challenger")
        self.assertEqual(public["temperature"], 0.5)
        self.assertEqual(public["temp_decay_moves"], 10)
        self.assertNotIn("name_visible", public)
        page = self.service.list_public_games(limit=20)
        self.assertEqual([item["id"] for item in page["games"]], [game["id"]])
        self.assertEqual(page["games"][0]["temperature"], 0.5)
        self.assertEqual(page["games"][0]["temp_decay_moves"], 10)
        self.assertEqual(page["games"][0]["final_fen"], completed["current_fen"])
        self.assertNotIn("current_fen", page["games"][0])

    def test_explicitly_public_name_appears_only_after_completion(self):
        game = self.create(name="Streamer", publish=True)
        with self.assertRaises(PublicPlayError):
            self.service.get_public_game(game["id"])
        self.service.resign(
            self.token,
            self.csrf,
            game["id"],
            self.key(),
            {"expected_revision": game["revision"]},
        )
        public = self.service.get_public_game(game["id"])["game"]
        self.assertEqual(public["players"]["white"]["name"], "Streamer")

    def test_terminal_human_move_is_archived_without_engine_search(self):
        game = self.create()
        final = self.move(game, "d2d4")

        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"], "1-0")
        self.assertEqual(final["termination"], "variant_result")
        self.assertEqual(self.engine.searches, [])

    def test_get_completes_terminal_human_stage_and_retry_reconciles(self):
        game = self.create()
        key = self.key()

        def crash(stage, _game):
            if stage == "human_recorded":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "d2d4", "expected_revision": 0},
            )

        self.service = self.make_service()
        completed = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["termination"], "variant_result")

        self.clock.advance_ms(300_001)
        replay = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "d2d4", "expected_revision": 0},
        )["game"]
        self.assertEqual(replay["revision"], completed["revision"])
        self.assertEqual(replay["result"], completed["result"])

    def test_get_completes_terminal_engine_stage_and_retry_reconciles(self):
        game = self.create()
        key = self.key()
        self.engine.states[("e2e4", "e7e5")] = position(
            "w", [], result="black", index=2
        )
        self.engine.next_moves = ["e7e5"]

        def crash(stage, _game):
            if stage == "engine_recorded":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4", "expected_revision": 0},
            )

        self.service = self.make_service()
        completed = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["termination"], "variant_result")

        self.clock.advance_ms(300_001)
        replay = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4", "expected_revision": 0},
        )["game"]
        self.assertEqual(replay["revision"], completed["revision"])
        self.assertEqual(replay["result"], completed["result"])

    def test_reclaims_crash_after_incomplete_human_first_leg(self):
        game = self.create()
        key = self.key()

        def crash(stage, _game):
            if stage == "human_recorded":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4+", "expected_revision": 0},
            )

        self.clock.advance_ms(301_000)
        self.service = self.make_service()
        recovered = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4+", "expected_revision": 0},
        )["game"]
        self.assertEqual(recovered["moves"], ["e2e4+"])
        self.assertTrue(recovered["ability"]["mid"])
        self.assertEqual(recovered["revision"], 1)

    def test_reclaimed_timed_incomplete_human_stage_settles_timeout(self):
        game = self.create(clock="3+2")
        key = self.key()

        def crash(stage, _game):
            if stage == "human_recorded":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4+", "expected_revision": 0},
            )

        self.clock.advance_ms(301_000)
        self.service = self.make_service()
        recovered = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4+", "expected_revision": 0},
        )["game"]

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["termination"], "timeout")
        self.assertEqual(recovered["result"], "0-1")
        self.assertEqual(recovered["moves"], [])
        self.assertEqual(recovered["clocks"]["white_ms"], 0)

    def test_get_timeout_after_human_stage_reconciles_original_retry(self):
        game = self.create(clock="3+2")
        key = self.key()

        def crash(stage, _game):
            if stage == "human_recorded":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4", "expected_revision": 0},
            )

        self.clock.advance_ms(180_001)
        self.service = self.make_service()
        timed_out = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        self.assertEqual(timed_out["termination"], "timeout")
        self.assertEqual(timed_out["result"], "1-0")

        self.clock.advance_ms(121_000)
        replay = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4", "expected_revision": 0},
        )["game"]
        self.assertEqual(replay["revision"], timed_out["revision"])
        self.assertEqual(replay["result"], timed_out["result"])

    def test_reclaims_crash_after_human_turn_and_resumes_engine_once(self):
        game = self.create()
        key = self.key()

        def crash(stage, _game):
            if stage == "human_recorded":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4", "expected_revision": 0},
            )

        self.clock.advance_ms(301_000)
        self.engine.next_moves = ["e7e5"]
        self.service = self.make_service()
        recovered = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4", "expected_revision": 0},
        )["game"]
        self.assertEqual(recovered["moves"], ["e2e4", "e7e5"])
        self.assertEqual(self.engine.searches, [100])

    def test_reclaims_crash_after_engine_turn_without_searching_twice(self):
        game = self.create()
        key = self.key()
        self.engine.next_moves = ["e7e5"]

        def crash(stage, _game):
            if stage == "engine_recorded":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4", "expected_revision": 0},
            )
        self.assertEqual(self.engine.searches, [100])

        self.clock.advance_ms(301_000)
        self.service = self.make_service()
        recovered = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4", "expected_revision": 0},
        )["game"]
        self.assertEqual(recovered["moves"], ["e2e4", "e7e5"])
        self.assertEqual(self.engine.searches, [100])

    def test_get_timeout_after_engine_stage_reconciles_original_retry(self):
        game = self.create(clock="3+2")
        key = self.key()
        self.engine.next_moves = ["e7e5"]

        def crash(stage, _game):
            if stage == "engine_recorded":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4", "expected_revision": 0},
            )

        self.clock.advance_ms(182_001)
        self.service = self.make_service()
        timed_out = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        self.assertEqual(timed_out["termination"], "timeout")
        self.assertEqual(timed_out["result"], "0-1")

        self.clock.advance_ms(119_000)
        replay = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4", "expected_revision": 0},
        )["game"]
        self.assertEqual(replay["revision"], timed_out["revision"])
        self.assertEqual(replay["result"], timed_out["result"])
        self.assertEqual(self.engine.searches, [100])

    def test_engine_failure_after_human_persistence_is_immediately_retryable(self):
        game = self.create()
        key = self.key()
        with self.assertRaises(PublicPlayError):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4", "expected_revision": 0},
            )

        self.engine.next_moves = ["e7e5"]
        recovered = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4", "expected_revision": 0},
        )["game"]
        self.assertEqual(recovered["moves"], ["e2e4", "e7e5"])
        self.assertEqual(recovered["revision"], 2)

    def test_owner_get_safely_resumes_stranded_engine_turn(self):
        game = self.create()
        key = self.key()
        with self.assertRaises(PublicPlayError):
            self.service.move(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"move": "e2e4", "expected_revision": 0},
            )

        self.engine.next_moves = ["e7e5"]
        resumed = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]
        self.assertEqual(resumed["moves"], ["e2e4", "e7e5"])

        reconciled = self.service.move(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"move": "e2e4", "expected_revision": 0},
        )["game"]
        self.assertEqual(reconciled["moves"], resumed["moves"])
        self.assertEqual(self.engine.searches, [100, 100])

    def test_get_timeout_mid_double_projects_restored_authoritative_state(self):
        game = self.create(clock="3+2")
        first = self.move(game, "e2e4+")
        self.clock.advance_ms(180_001)
        timed_out = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]

        self.assertEqual(timed_out["status"], "completed")
        self.assertEqual(timed_out["result"], "0-1")
        self.assertEqual(timed_out["moves"], [])
        self.assertEqual(timed_out["current_fen"], fake_fen("w", 0))
        self.assertFalse(timed_out["ability"]["mid"])
        self.assertEqual(timed_out["clocks"]["white_ms"], 0)

    def test_sampling_temperature_is_reset_when_engine_search_fails(self):
        game = self.create(preset="casual")
        with self.assertRaises(PublicPlayError):
            self.move(game, "e2e4")
        self.assertEqual(
            self.engine.options[-2:],
            [("Temperature", "0"), ("TempDecayMoves", "0")],
        )

    def test_sampling_temperature_is_reset_when_second_option_fails(self):
        game = self.create(preset="casual")
        original_set_option = self.engine.set_option

        def fail_on_decay(name, value):
            original_set_option(name, value)
            if name == "TempDecayMoves":
                raise RuntimeError("option setup failed")

        self.engine.set_option = fail_on_decay
        with self.assertRaises(PublicPlayError):
            self.move(game, "e2e4")

        self.assertEqual(
            self.engine.options,
            [
                ("Temperature", "0.5"),
                ("TempDecayMoves", "10"),
                ("PolicyOnlyAvoidTwofold", "false"),
                ("Temperature", "0"),
                ("TempDecayMoves", "0"),
            ],
        )

    def test_active_game_rejects_changed_model_configuration(self):
        game = self.create()
        changed = ModelChoice(
            id="v37-public",
            label="v37 public",
            sha256="b" * 64,
        )
        self.service.models = (changed,)
        self.service._models_by_id = {"v37-public": changed}
        with self.assertRaises(PublicPlayError) as raised:
            self.service.get_owner_game(self.token, game["id"])
        self.assertEqual(
            raised.exception.code, "game_engine_unavailable"
        )

    def test_active_game_can_resume_with_hidden_compatibility_model(self):
        self.service = self.make_service(backend="sycl-auto")
        game = self.create()
        legacy = ModelChoice(
            id="v37-public",
            label="v37 compatibility",
            sha256="a" * 64,
            compatibility_backends=("sycl-auto",),
        )
        current = ModelChoice(
            id="v38-public",
            label="v38 public",
            sha256="b" * 64,
        )
        self.service = self.make_service(
            models=(current,),
            compatibility_models=(legacy,),
            default_model_id="v38-public",
        )

        restored = self.service.get_owner_game(self.token, game["id"])["game"]
        config = self.service.get_config(self.token).payload

        self.assertEqual(restored["model_id"], "v37-public")
        self.assertIn(
            ("public:" + game["id"], "v37-public", "sycl-auto"),
            self.sessions.contexts,
        )
        self.assertEqual(config["default_model_id"], "v38-public")
        self.assertEqual(
            config["models"],
            [{"id": "v38-public", "label": "v38 public"}],
        )
        with self.assertRaisesRegex(
            PublicPlayError, "Advanced model selection is invalid"
        ):
            self.service._validate_advanced_settings(
                {
                    "model_id": "v37-public",
                    "nodes": 1,
                    "temperature": 0,
                    "temp_decay_moves": 0,
                    "clock": {"mode": "unlimited"},
                }
            )

    def test_completed_owner_game_remains_readable_after_model_change(self):
        game = self.create()
        completed = self.service.resign(
            self.token,
            self.csrf,
            game["id"],
            self.key(),
            {"expected_revision": game["revision"]},
        )["game"]
        changed = ModelChoice(
            id="v37-public",
            label="v37 public",
            sha256="b" * 64,
        )
        self.service.models = (changed,)
        self.service._models_by_id = {"v37-public": changed}

        restored = self.service.get_owner_game(
            self.token, game["id"]
        )["game"]

        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["result"], completed["result"])
        self.assertEqual(restored["moves"], completed["moves"])

    def test_takeback_stage_reclaims_without_reverting_twice(self):
        game = self.create()
        self.engine.next_moves = ["e7e5"]
        played = self.move(game, "e2e4")
        key = self.key()

        def crash(stage, _game):
            if stage == "takeback_recorded":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.takeback(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"expected_revision": played["revision"]},
            )
        self.clock.advance_ms(301_000)
        self.service = self.make_service()
        recovered = self.service.takeback(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"expected_revision": played["revision"]},
        )["game"]
        self.assertEqual(recovered["moves"], [])
        self.assertEqual(recovered["takeback_count"], 1)

    def test_resignation_stage_reclaims_without_completing_twice(self):
        game = self.create()
        key = self.key()

        def crash(stage, _game):
            if stage == "game_completed":
                raise SystemExit("simulated process death")

        self.service = self.make_service(stage_hook=crash)
        with self.assertRaises(SystemExit):
            self.service.resign(
                self.token,
                self.csrf,
                game["id"],
                key,
                {"expected_revision": game["revision"]},
            )
        self.clock.advance_ms(301_000)
        self.service = self.make_service()
        recovered = self.service.resign(
            self.token,
            self.csrf,
            game["id"],
            key,
            {"expected_revision": game["revision"]},
        )["game"]
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["termination"], "resignation")


if __name__ == "__main__":
    unittest.main()
