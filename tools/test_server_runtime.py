#!/usr/bin/env python3

from dataclasses import replace
import http.client
import json
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tools import dm_cli
from tools import server


class UciAnalysisLineTest(unittest.TestCase):
    def test_multipv_lines_are_white_relative_and_keep_double_moves(self):
        lines = server._parse_uci_analysis_lines(
            [
                "info depth 4 seldepth 8 nodes 21 nps 7 score cp 52 "
                "multipv 1 pv e7e5+ e5e4 g1f3",
                "info depth 3 nodes 21 score mate -3 multipv 2 pv c7c5",
            ],
            "b",
        )

        self.assertEqual(
            lines,
            [
                {
                    "multipv": 1,
                    "white_cp": -52,
                    "white_mate": None,
                    "depth": 4,
                    "seldepth": 8,
                    "nodes": 21,
                    "nps": 7,
                    "pv": ["e7e5+", "e5e4", "g1f3"],
                },
                {
                    "multipv": 2,
                    "white_cp": None,
                    "white_mate": 3,
                    "depth": 3,
                    "seldepth": None,
                    "nodes": 21,
                    "nps": None,
                    "pv": ["c7c5"],
                },
            ],
        )


class DmCliProtocolTest(unittest.TestCase):
    def bare_engine(self, responder):
        engine = object.__new__(dm_cli.Lc0)
        engine._output = dm_cli._OutputBuffer(max_lines=32)
        engine._lock = threading.RLock()
        engine._send = lambda command: responder(engine, command)
        return engine

    def test_output_buffer_is_bounded_and_keeps_tail(self):
        output = dm_cli._OutputBuffer(max_lines=3)
        for index in range(5):
            output.append(f"line {index}")

        self.assertEqual(len(output), 3)
        self.assertEqual(
            output.tail(10),
            ["line 2", "line 3", "line 4"],
        )

    def test_output_buffer_waiter_detects_evicted_start(self):
        output = dm_cli._OutputBuffer(max_lines=2)
        start = output.mark()
        output.append("one")
        output.append("two")
        output.append("three")

        with self.assertRaisesRegex(RuntimeError, "buffer overrun"):
            output.wait_for(lambda line: line == "never", start, 0.1)

    def test_output_buffer_strict_read_detects_post_marker_eviction(self):
        output = dm_cli._OutputBuffer(max_lines=2)
        start = output.mark()
        output.append("dminfo fen startpos")
        output.append("dminfo ready")
        output.append("later diagnostic")

        with self.assertRaisesRegex(RuntimeError, "buffer overrun"):
            output.read(start, 1, require_full=True)

    def test_output_buffer_condition_wakes_for_marker(self):
        output = dm_cli._OutputBuffer(max_lines=4)
        start = output.mark()
        observed = []

        waiter = threading.Thread(
            target=lambda: observed.append(
                output.wait_for(
                    lambda line: line == "readyok",
                    start,
                    1,
                )
            )
        )
        waiter.start()
        output.append("info string loading")
        output.append("readyok")
        waiter.join(timeout=1)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(observed, [(1, "readyok")])

    def test_go_nodes_rejects_output_evicted_after_marker(self):
        engine = object.__new__(dm_cli.Lc0)
        engine._output = dm_cli._OutputBuffer(max_lines=2)
        engine._lock = threading.RLock()
        engine._send = lambda _command: None

        def wait_for_marker(*_args, **_kwargs):
            engine._output.append("info depth 1 pv e2e4")
            engine._output.append("bestmove e2e4")
            engine._output.append("later diagnostic")
            return 1, "bestmove e2e4"

        engine._wait_for = wait_for_marker

        with self.assertRaisesRegex(RuntimeError, "buffer overrun"):
            engine.go_nodes(1)

    def test_set_position_turns_engine_error_into_position_error(self):
        def respond(engine, command):
            if command.startswith("position "):
                engine._output.append("error illegal move e2e5")
            elif command == "isready":
                engine._output.append("readyok")

        engine = self.bare_engine(respond)

        with self.assertRaisesRegex(
            dm_cli.Lc0PositionError, "illegal move e2e5"
        ):
            engine.set_position("startpos", ["e2e5"])

    def test_position_info_parses_exact_double_move_protocol(self):
        response = [
            "dminfo fen 8/8/8/8/4P3/8/8/4K2k w - - 0 1 Wm",
            "dminfo turn w",
            "dminfo ability 0 1 1",
            "dminfo result ongoing",
            "dminfo legal e4e5 e4e6 g1f3+",
            "dminfo ready",
        ]

        def respond(engine, command):
            self.assertEqual(command, "dminfo")
            for line in response:
                engine._output.append(line)

        info = self.bare_engine(respond).position_info()

        self.assertEqual(
            info,
            {
                "fen": "8/8/8/8/4P3/8/8/4K2k w - - 0 1 Wm",
                "turn": "w",
                "ability": {
                    "white": False,
                    "black": True,
                    "mid": True,
                },
                "result": "ongoing",
                "legal": ["e4e5", "e4e6", "g1f3+"],
            },
        )

    def test_position_info_rejects_partial_response(self):
        def respond(engine, _command):
            engine._output.append("dminfo fen startpos")
            engine._output.append("dminfo ready")

        with self.assertRaisesRegex(RuntimeError, "incomplete dminfo"):
            self.bare_engine(respond).position_info()

    def test_verbose_mode_is_disabled_when_search_fails(self):
        commands = []

        def respond(_engine, command):
            commands.append(command)

        engine = self.bare_engine(respond)
        engine._wait_for = lambda *args, **kwargs: (_ for _ in ()).throw(
            TimeoutError("search failed")
        )

        with self.assertRaisesRegex(TimeoutError, "search failed"):
            engine.verbose_stats(10)
        self.assertEqual(
            commands[-1],
            "setoption name VerboseMoveStats value false",
        )

    def test_live_verbose_search_streams_complete_cumulative_snapshots(self):
        commands = []

        def respond(engine, command):
            commands.append(command)
            if command == "go nodes 8":
                for line in (
                    "info depth 1 nodes 2 score cp 1 pv e2e4",
                    "info string e2e4 N: 1",
                    "info string node N: 2",
                    "info depth 2 nodes 8 score cp 2 pv d2d4",
                    "info string e2e4 N: 3",
                    "info string d2d4 N: 4",
                    "info string node N: 8",
                    "bestmove d2d4 ponder d7d5",
                ):
                    engine._output.append(line)

        snapshots = []
        result = self.bare_engine(respond).verbose_stats_stream(
            8,
            lambda nodes, verbose, info: snapshots.append(
                (nodes, verbose, info)
            ),
        )

        self.assertEqual([item[0] for item in snapshots], [2, 8])
        self.assertEqual(
            snapshots[-1][1],
            [
                "info string e2e4 N: 3",
                "info string d2d4 N: 4",
                "info string node N: 8",
            ],
        )
        self.assertEqual(
            result,
            (
                snapshots[-1][1],
                "bestmove d2d4 ponder d7d5",
                8,
                False,
            ),
        )
        self.assertEqual(
            commands,
            [
                "setoption name VerboseMoveStats value true",
                "setoption name LogLiveStats value true",
                "go nodes 8",
                "setoption name LogLiveStats value false",
                "setoption name VerboseMoveStats value false",
            ],
        )

    def test_live_multipv_uses_native_wdl_mu_scores(self):
        commands = []

        def respond(engine, command):
            commands.append(command)
            if command == "go nodes 8":
                for line in (
                    "info depth 1 multipv 1 nodes 8 score cp 50 pv e2e4",
                    "info string e2e4 N: 8",
                    "info string node N: 8",
                    "bestmove e2e4",
                ):
                    engine._output.append(line)

        self.bare_engine(respond).verbose_stats_stream(
            8,
            lambda _nodes, _verbose, _info: True,
            multipv=3,
        )

        self.assertEqual(commands[0:2], [
            "setoption name MultiPV value 3",
            "setoption name ScoreType value WDL_mu",
        ])
        self.assertEqual(commands[-2:], [
            "setoption name MultiPV value 1",
            "setoption name ScoreType value WDL_mu",
        ])
        self.assertNotIn(
            "setoption name ScoreType value centipawn_with_drawscore",
            commands,
        )

    def test_live_verbose_cancellation_stops_once_and_drains_bestmove(self):
        commands = []

        def respond(engine, command):
            commands.append(command)
            if command == "go nodes 800":
                for line in (
                    "info depth 1 nodes 12 pv e2e4",
                    "info string e2e4 N: 9",
                    "info string node N: 12",
                ):
                    engine._output.append(line)
            elif command == "stop":
                for line in (
                    "info depth 2 nodes 13 pv e2e4",
                    "info string e2e4 N: 10",
                    "info string node N: 13",
                    "bestmove e2e4",
                ):
                    engine._output.append(line)

        callback_calls = []
        result = self.bare_engine(respond).verbose_stats_stream(
            800,
            lambda nodes, verbose, info: (
                callback_calls.append((nodes, verbose, info)) or False
            ),
        )

        self.assertEqual(len(callback_calls), 1)
        self.assertEqual(commands.count("stop"), 1)
        self.assertEqual(result[1:], ("bestmove e2e4", 13, True))
        self.assertEqual(
            commands[-2:],
            [
                "setoption name LogLiveStats value false",
                "setoption name VerboseMoveStats value false",
            ],
        )

    def test_supersession_event_stops_before_an_info_snapshot(self):
        commands = []
        cancel_event = threading.Event()

        def respond(engine, command):
            commands.append(command)
            if command == "go nodes 800":
                threading.Timer(0.02, cancel_event.set).start()
            elif command == "stop":
                engine._output.append("bestmove e2e4")

        result = self.bare_engine(respond).verbose_stats_stream(
            800,
            lambda *_args: self.fail("no snapshot should be required"),
            cancel_event=cancel_event,
        )

        self.assertEqual(commands.count("stop"), 1)
        self.assertEqual(result, ([], "bestmove e2e4", None, True))

    def test_initial_snapshot_dumps_retained_root_before_target_search(self):
        commands = []

        def respond(engine, command):
            commands.append(command)
            if command == "go nodes 1":
                for line in (
                    "info depth 4 nodes 37 score cp 12 pv c7c5",
                    "info string c7c5 N: 21",
                    "info string node N: 37",
                    "bestmove c7c5",
                ):
                    engine._output.append(line)
            elif command == "go nodes 800":
                for line in (
                    "info depth 8 nodes 800 score cp 9 pv c7c5",
                    "info string c7c5 N: 521",
                    "info string node N: 800",
                    "bestmove c7c5",
                ):
                    engine._output.append(line)

        snapshots = []
        result = self.bare_engine(respond).verbose_stats_stream(
            800,
            lambda nodes, _verbose, _info: snapshots.append(nodes),
            initial_snapshot=True,
        )

        self.assertEqual(snapshots, [37, 800])
        self.assertEqual(
            [command for command in commands if command.startswith("go nodes")],
            ["go nodes 1", "go nodes 800"],
        )
        self.assertEqual(result[2:], (800, False))

    def test_visit_budget_is_cumulative_even_if_caller_requests_playouts(self):
        options = dm_cli._lc0_uci_options(
            smart_pruning_factor=0,
            opts={"NodesAsPlayouts": "true"},
        )

        self.assertEqual(options["NodesAsPlayouts"], "false")


class AnalysisRequestCoordinatorTest(unittest.TestCase):
    def test_new_request_cancels_active_request_in_same_context(self):
        coordinator = server._AnalysisRequestCoordinator()
        first = coordinator.begin("a" * 32, 1)
        second = coordinator.begin("a" * 32, 2)

        self.assertTrue(first.cancel_event.is_set())
        self.assertFalse(second.cancel_event.is_set())
        self.assertTrue(second.accepted)

        coordinator.finish(first)
        third = coordinator.begin("a" * 32, 3)
        self.assertTrue(second.cancel_event.is_set())
        self.assertFalse(third.cancel_event.is_set())

    def test_late_older_request_cannot_supersede_newer_request(self):
        coordinator = server._AnalysisRequestCoordinator()
        current = coordinator.begin("a" * 32, 8)
        stale = coordinator.begin("a" * 32, 7)

        self.assertFalse(current.cancel_event.is_set())
        self.assertFalse(stale.accepted)
        self.assertTrue(stale.cancel_event.is_set())

    def test_different_analysis_contexts_do_not_cancel_each_other(self):
        coordinator = server._AnalysisRequestCoordinator()
        first = coordinator.begin("a" * 32, 1)
        second = coordinator.begin("b" * 32, 1)

        self.assertFalse(first.cancel_event.is_set())
        self.assertFalse(second.cancel_event.is_set())

    def test_explicit_cancel_only_matches_the_active_request_id(self):
        coordinator = server._AnalysisRequestCoordinator()
        current = coordinator.begin("a" * 32, 5)

        self.assertFalse(coordinator.cancel("a" * 32, 4))
        self.assertFalse(current.cancel_event.is_set())
        self.assertTrue(coordinator.cancel("a" * 32, 5))
        self.assertTrue(current.cancel_event.is_set())

    def test_cancel_before_begin_rejects_the_late_request(self):
        coordinator = server._AnalysisRequestCoordinator()

        self.assertFalse(coordinator.cancel("a" * 32, 5))
        cancelled = coordinator.begin("a" * 32, 5)
        newer = coordinator.begin("a" * 32, 6)

        self.assertFalse(cancelled.accepted)
        self.assertTrue(cancelled.cancel_event.is_set())
        self.assertTrue(newer.accepted)


class GameMaintenanceTest(unittest.TestCase):
    def test_loop_runs_cleanup_once_per_two_day_interval(self):
        store = mock.Mock()
        stopping = mock.Mock()
        stopping.wait.side_effect = (False, True)

        with mock.patch.object(server, "_run_game_maintenance") as run:
            server._game_maintenance_loop(store, stopping)

        self.assertEqual(
            stopping.wait.call_args_list,
            [
                mock.call(server.GAME_MAINTENANCE_INTERVAL_SECONDS),
                mock.call(server.GAME_MAINTENANCE_INTERVAL_SECONDS),
            ],
        )
        self.assertEqual(
            server.GAME_MAINTENANCE_INTERVAL_SECONDS,
            2 * 24 * 60 * 60,
        )
        run.assert_called_once_with(store)


class ServerRuntimeConfigTest(unittest.TestCase):
    def parse(self, *arguments):
        return server._build_arg_parser().parse_args(arguments)

    def test_auto_profile_stays_local_with_unrelated_host_paths(self):
        environ = {
            "WORK": "/other/work",
            "HPCWORK": "/other/data",
        }
        config = server._resolve_runtime_config(
            self.parse(),
            environ=environ,
            path_exists=lambda _path: False,
        )

        self.assertEqual(config.profile, "local")
        self.assertEqual(config.lc0_path, server.LOCAL_LC0)
        self.assertEqual(config.runs_root, server.LOCAL_RUNS_ROOT)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.backend, "blas")
        self.assertEqual(config.available_backends, ("blas",))
        self.assertEqual(config.nodes, 800)
        self.assertEqual(config.minibatch_size, 32)
        self.assertFalse(config.use_modules)
        self.assertEqual(config.weights_path, server.LOCAL_WEIGHTS)

    def test_auto_profile_uses_portable_cpu_defaults(self):
        config = server._resolve_runtime_config(
            self.parse(),
            environ={},
            path_exists=lambda _path: False,
        )

        self.assertEqual(config.profile, "local")
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.lc0_path, server.LOCAL_LC0)
        self.assertEqual(config.runs_root, server.LOCAL_RUNS_ROOT)
        self.assertEqual(config.weights_path, server.LOCAL_WEIGHTS)
        self.assertEqual(config.backend, "blas")
        self.assertEqual(config.available_backends, ("blas",))
        self.assertEqual(config.nodes, 800)
        self.assertEqual(config.minibatch_size, 32)
        self.assertFalse(config.use_modules)

    def test_cli_overrides_environment_and_profile_defaults(self):
        args = self.parse(
            "--profile",
            "local",
            "--host",
            "127.0.0.2",
            "--port",
            "9000",
            "--runs-root",
            "/explicit/runs",
            "--lc0",
            "/explicit/lc0",
            "--weights",
            "/explicit/network.pb.gz",
            "--backend",
            "blas",
            "--available-backends",
            "blas,cuda",
            "--nodes",
            "10",
            "--minibatch-size",
            "8",
            "--no-use-modules",
        )
        environ = {
            "WORK": "/other/work",
            "HPCWORK": "/other/data",
            "DM_HOST": "ignored",
            "DM_NODES": "123",
            "DM_USE_MODULES": "true",
        }

        config = server._resolve_runtime_config(args, environ=environ)

        self.assertEqual(config.profile, "local")
        self.assertEqual(config.host, "127.0.0.2")
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.runs_root, "/explicit/runs")
        self.assertEqual(config.lc0_path, "/explicit/lc0")
        self.assertEqual(config.weights_path, "/explicit/network.pb.gz")
        self.assertEqual(config.backend, "blas")
        self.assertEqual(config.available_backends, ("blas", "cuda"))
        self.assertEqual(config.nodes, 10)
        self.assertEqual(config.minibatch_size, 8)
        self.assertFalse(config.use_modules)

    def test_environment_overrides_local_defaults(self):
        environ = {
            "DM_PROFILE": "local",
            "DM_LC0_PATH": "/env/lc0",
            "DM_WEIGHTS_PATH": "/env/network.pb.gz",
            "DM_RUNS_ROOT": "/env/runs",
            "DM_BACKEND": "custom",
            "DM_AVAILABLE_BACKENDS": "custom,blas,custom",
            "DM_NODES": "17",
            "DM_MINIBATCH_SIZE": "4",
            "DM_USE_MODULES": "yes",
            "DM_HOST": "localhost",
            "DM_PORT": "8123",
        }

        config = server._resolve_runtime_config(self.parse(), environ=environ)

        self.assertEqual(config.profile, "local")
        self.assertEqual(config.lc0_path, "/env/lc0")
        self.assertEqual(config.weights_path, "/env/network.pb.gz")
        self.assertEqual(config.runs_root, "/env/runs")
        self.assertEqual(config.backend, "custom")
        self.assertEqual(config.available_backends, ("custom", "blas"))
        self.assertEqual(config.nodes, 17)
        self.assertEqual(config.minibatch_size, 4)
        self.assertTrue(config.use_modules)
        self.assertEqual(config.host, "localhost")
        self.assertEqual(config.port, 8123)

    def test_hosted_profile_has_exact_locked_cpu_defaults(self):
        config = server._resolve_runtime_config(
            self.parse("--profile", "hosted"),
            environ={},
            path_exists=lambda _path: False,
        )

        self.assertEqual(config.profile, "hosted")
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, server.DEFAULT_PORT)
        self.assertEqual(config.runs_root, server.LOCAL_RUNS_ROOT)
        self.assertEqual(config.lc0_path, server.LOCAL_LC0)
        self.assertEqual(config.weights_path, server.LOCAL_WEIGHTS)
        self.assertEqual(config.backend, "blas")
        self.assertEqual(config.available_backends, ("blas",))
        self.assertEqual(config.nodes, 800)
        self.assertEqual(config.minibatch_size, 32)
        self.assertFalse(config.use_modules)
        self.assertTrue(config.restricted)

    def test_invalid_environment_value_has_clear_error(self):
        with self.assertRaisesRegex(ValueError, "Invalid DM_NODES"):
            server._resolve_runtime_config(
                self.parse(),
                environ={"DM_PROFILE": "local", "DM_NODES": "many"},
            )

    def test_nodes_must_fit_lc0_signed_integer_parser(self):
        with self.assertRaisesRegex(ValueError, "Nodes must be between"):
            server._resolve_runtime_config(
                self.parse(
                    "--profile",
                    "local",
                    "--nodes",
                    str(server.MAX_SEARCH_NODES + 1),
                ),
                environ={},
            )

    def test_available_backends_must_include_default_backend(self):
        with self.assertRaisesRegex(
            ValueError, "Configured backend 'sycl-auto' is not in"
        ):
            server._resolve_runtime_config(
                self.parse(
                    "--profile",
                    "local",
                    "--backend",
                    "sycl-auto",
                    "--available-backends",
                    "blas,sycl",
                ),
                environ={},
            )

    def test_backend_override_is_added_to_implicit_choices(self):
        config = server._resolve_runtime_config(
            self.parse("--profile", "local", "--backend", "sycl-auto"),
            environ={},
        )

        self.assertEqual(config.available_backends, ("sycl-auto", "blas"))

    def test_backend_name_must_be_valid(self):
        for value in ("", "sycl auto", "../sycl"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError, "Backend must be a valid lc0 backend name"
                ):
                    server._resolve_runtime_config(
                        self.parse("--profile", "local", "--backend", value),
                        environ={},
                    )

    def test_public_play_model_catalog_is_server_owned_and_path_free(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            configured = os.path.join(temp_dir, "v37-latest.pb.gz")
            run_model = os.path.join(
                temp_dir,
                "runs",
                "v36",
                "exported_models",
                "net-00012345.pb.gz",
            )
            os.makedirs(os.path.dirname(run_model))
            with open(configured, "wb") as handle:
                handle.write(b"configured model")
            with open(run_model, "wb") as handle:
                handle.write(b"run model")
            config = replace(
                server._profile_defaults("local", {}),
                weights_path=configured,
                runs_root=os.path.join(temp_dir, "runs"),
                restricted=False,
            )

            models, default_id = server._resolve_public_models(config)

            self.assertEqual(default_id, "v37-latest.pb.gz")
            self.assertEqual(
                [model.choice.id for model in models],
                ["v37-latest.pb.gz", "v36:net-00012345"],
            )
            safe_payload = [
                {
                    "id": model.choice.id,
                    "label": model.choice.label,
                }
                for model in models
            ]
            self.assertNotIn(temp_dir, json.dumps(safe_payload))

            restricted, restricted_default = server._resolve_public_models(
                replace(config, restricted=True)
            )
            self.assertEqual(restricted_default, "v37-latest.pb.gz")
            self.assertEqual(len(restricted), 1)

    def test_optional_model_failure_is_skipped_and_labels_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            configured = os.path.join(temp_dir, "v37-latest.pb.gz")
            long_run = "r" * 150
            optional = os.path.join(
                temp_dir,
                "runs",
                long_run,
                "exported_models",
                "net-" + ("9" * 80) + ".pb.gz",
            )
            os.makedirs(os.path.dirname(optional))
            for path, content in (
                (configured, b"configured model"),
                (optional, b"optional model"),
            ):
                with open(path, "wb") as handle:
                    handle.write(content)
            config = replace(
                server._profile_defaults("local", {}),
                weights_path=configured,
                runs_root=os.path.join(temp_dir, "runs"),
                restricted=False,
            )

            models, _default_id = server._resolve_public_models(config)
            self.assertEqual(len(models), 2)
            self.assertLessEqual(len(models[1].choice.label), 160)

            real_sha256 = server._sha256_file

            def fail_optional(path):
                if path == optional:
                    raise FileNotFoundError(path)
                return real_sha256(path)

            with mock.patch.object(
                server, "_sha256_file", side_effect=fail_optional
            ):
                models, default_id = server._resolve_public_models(config)

            self.assertEqual(default_id, "v37-latest.pb.gz")
            self.assertEqual(
                [model.choice.id for model in models],
                ["v37-latest.pb.gz"],
            )

    def test_resume_only_manifest_model_is_hidden_compatibility_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            models_dir = os.path.join(temp_dir, "models")
            os.makedirs(models_dir)
            legacy_path = os.path.join(models_dir, "v37-latest.pb.gz")
            with open(legacy_path, "wb") as handle:
                handle.write(b"legacy model")
            legacy_hash = server._sha256_file(legacy_path)
            manifest_path = os.path.join(temp_dir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "engine": {
                            "supported_backends": [
                                "blas", "sycl", "sycl-auto"
                            ]
                        },
                        "models": {
                            "v38-latest": {
                                "installed_filename": "v38-latest.pb.gz",
                                "size_bytes": 12,
                                "sha256": "a" * 64,
                            },
                            "v37-latest": {
                                "installed_filename": "v37-latest.pb.gz",
                                "size_bytes": os.path.getsize(legacy_path),
                                "sha256": legacy_hash,
                                "resume_backends": ["sycl-auto", "blas"],
                                "resume_only": True,
                            },
                        }
                    },
                    handle,
                )
            current = server._PublicPlayModelRuntime(
                choice=server.ModelChoice(
                    id="v38-latest.pb.gz",
                    label="v38-latest",
                    sha256="a" * 64,
                ),
                path=os.path.join(models_dir, "v38-latest.pb.gz"),
                size=12,
                mtime_ns=1,
            )

            with mock.patch.object(server, "MANIFEST_PATH", manifest_path), \
                    mock.patch.object(server, "MODELS_DIR", models_dir):
                compatibility = server._resolve_compatibility_models((current,))

            self.assertEqual(len(compatibility), 1)
            self.assertEqual(
                compatibility[0].choice.id, "v37-latest.pb.gz"
            )
            self.assertEqual(compatibility[0].choice.sha256, legacy_hash)
            self.assertEqual(
                compatibility[0].choice.compatibility_backends,
                ("sycl-auto", "blas"),
            )

    def test_selectable_manifest_models_are_verified_and_ordered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            models_dir = os.path.join(temp_dir, "models")
            os.makedirs(models_dir)
            current_path = os.path.join(models_dir, "current.pb.gz")
            sparring_path = os.path.join(models_dir, "sparring.pb.gz")
            invalid_path = os.path.join(models_dir, "invalid.pb.gz")
            for path, contents in (
                (current_path, b"current"),
                (sparring_path, b"sparring"),
                (invalid_path, b"wrong contents"),
            ):
                with open(path, "wb") as handle:
                    handle.write(contents)
            manifest_path = os.path.join(temp_dir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "default_model": "current",
                        "models": {
                            "current": {
                                "installed_filename": "current.pb.gz",
                            },
                            "sparring": {
                                "display_name": "Sparring · test",
                                "selectable": True,
                                "installed_filename": "sparring.pb.gz",
                                "size_bytes": os.path.getsize(sparring_path),
                                "sha256": server._sha256_file(sparring_path),
                            },
                            "invalid": {
                                "selectable": True,
                                "installed_filename": "invalid.pb.gz",
                                "size_bytes": os.path.getsize(invalid_path),
                                "sha256": "f" * 64,
                            },
                        },
                    },
                    handle,
                )
            config = replace(
                server._profile_defaults("hosted", {}),
                weights_path=current_path,
            )

            with mock.patch.object(server, "MANIFEST_PATH", manifest_path), \
                    mock.patch.object(server, "MODELS_DIR", models_dir):
                models, default_id = server._resolve_public_models(config)

            self.assertEqual(default_id, "current.pb.gz")
            self.assertEqual(
                [model.choice.id for model in models],
                ["current.pb.gz", "sparring.pb.gz"],
            )
            self.assertEqual(models[1].choice.label, "Sparring · test")


class BoardServerRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.original_config = server.BoardServer.runtime_config
        self.original_runs_root = server.BoardServer.runs_root
        self.original_model_runtimes = server.BoardServer.model_runtimes
        server.BoardServer.model_runtimes = ()
        self.handler = object.__new__(server.BoardServer)

    def tearDown(self):
        server.BoardServer.runtime_config = self.original_config
        server.BoardServer.runs_root = self.original_runs_root
        server.BoardServer.model_runtimes = self.original_model_runtimes

    def test_empty_engine_request_uses_existing_local_runtime_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            weights_path = os.path.join(temp_dir, "v37-latest.pb.gz")
            with open(weights_path, "wb"):
                pass
            config = replace(
                server._profile_defaults("local", {}),
                runs_root=os.path.join(temp_dir, "runs"),
                weights_path=weights_path,
            )
            server.BoardServer.runtime_config = config
            server.BoardServer.runs_root = config.runs_root

            lc0_path, weights, backend, minibatch, modules, _opts = (
                self.handler._engine_config_from_body({})
            )

            self.assertEqual(lc0_path, server.LOCAL_LC0)
            self.assertEqual(weights, weights_path)
            self.assertEqual(backend, "blas")
            self.assertEqual(minibatch, 32)
            self.assertFalse(modules)

    def test_missing_local_weights_fail_clearly(self):
        config = replace(
            server._profile_defaults("local", {}),
            runs_root="/missing/runs",
            weights_path="/missing/v37-latest.pb.gz",
        )
        server.BoardServer.runtime_config = config
        server.BoardServer.runs_root = config.runs_root

        with self.assertRaisesRegex(
            ValueError, "No weights configured and no exported models found"
        ):
            self.handler._engine_config_from_body({})

    def test_client_settings_still_override_local_defaults(self):
        config = server._profile_defaults("local", {})
        server.BoardServer.runtime_config = config
        server.BoardServer.runs_root = config.runs_root

        lc0_path, weights, backend, minibatch, modules, _opts = (
            self.handler._engine_config_from_body(
                {
                    "lc0": "/other/lc0",
                    "weights": "/other/network.pb.gz",
                    "backend": "blas",
                    "minibatch_size": 7,
                    "use_modules": False,
                }
            )
        )

        self.assertEqual(lc0_path, "/other/lc0")
        self.assertEqual(weights, "/other/network.pb.gz")
        self.assertEqual(backend, "blas")
        self.assertEqual(minibatch, 7)
        self.assertFalse(modules)

    def test_configured_model_precedes_discovered_run_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runs_root = os.path.join(temp_dir, "runs")
            exported = os.path.join(runs_root, "v36", "exported_models")
            models_root = os.path.join(temp_dir, "models")
            os.makedirs(exported)
            os.makedirs(models_root)
            discovered_path = os.path.join(exported, "net-00000001.pb.gz")
            configured_path = os.path.join(models_root, "v37-latest.pb.gz")
            for path in (discovered_path, configured_path):
                with open(path, "wb"):
                    pass

            config = replace(
                server._profile_defaults("local", {}),
                runs_root=runs_root,
                weights_path=configured_path,
            )
            server.BoardServer.runtime_config = config
            server.BoardServer.runs_root = runs_root

            models = self.handler._discover_weights()

            self.assertEqual(
                [model["path"] for model in models],
                [configured_path, discovered_path],
            )

    def test_get_engine_discards_and_relaunches_matching_dead_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lc0_path = os.path.join(temp_dir, "lc0")
            weights_path = os.path.join(temp_dir, "network.pb.gz")
            for path in (lc0_path, weights_path):
                with open(path, "wb"):
                    pass
            os.chmod(lc0_path, 0o755)
            config = replace(
                server._profile_defaults("local", {}),
                lc0_path=lc0_path,
                weights_path=weights_path,
            )
            self.handler.runtime_config = config
            (
                resolved_lc0,
                resolved_weights,
                backend,
                minibatch,
                modules,
                options,
            ) = self.handler._engine_config_from_body({})
            matching_key = (
                resolved_lc0,
                resolved_weights,
                backend,
                minibatch,
                modules,
                tuple(sorted(options.items())),
            )
            replacement = SimpleNamespace()
            dead = SimpleNamespace(
                proc=SimpleNamespace(poll=lambda: 9)
            )
            original = (
                server.ENGINE,
                server.ENGINE_KEY,
                server.ENGINE_CONTEXT_ID,
                server.Lc0,
            )
            server.ENGINE = dead
            server.ENGINE_KEY = matching_key
            server.ENGINE_CONTEXT_ID = "public:" + "1" * 32
            server.Lc0 = lambda *_args, **_kwargs: replacement
            try:
                launched = self.handler._get_engine({})
            finally:
                (
                    server.ENGINE,
                    server.ENGINE_KEY,
                    server.ENGINE_CONTEXT_ID,
                    server.Lc0,
                ) = original

            self.assertIs(launched, replacement)

    def test_public_engine_session_relaunches_matching_dead_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lc0_path = os.path.join(temp_dir, "lc0")
            weights_path = os.path.join(temp_dir, "network.pb.gz")
            for path in (lc0_path, weights_path):
                with open(path, "wb"):
                    pass
            os.chmod(lc0_path, 0o755)
            config = replace(
                server._profile_defaults("hosted", {}),
                lc0_path=lc0_path,
                weights_path=weights_path,
            )
            options = {
                "HistoryFill": "repeat_current",
                "CPuct": "1.2",
                "PolicyTemperature": "1.0",
                "DisableAbilityEdges": "false",
                "SmartPruningFactor": "0",
            }
            requested_backend = "sycl-auto"
            matching_key = (
                config.lc0_path,
                weights_path,
                requested_backend,
                config.minibatch_size,
                config.use_modules,
                tuple(sorted(options.items())),
            )
            replacement = SimpleNamespace()
            dead = SimpleNamespace(
                proc=SimpleNamespace(poll=lambda: 3)
            )
            original = (
                server.ENGINE,
                server.ENGINE_KEY,
                server.ENGINE_CONTEXT_ID,
                server.Lc0,
            )
            server.ENGINE = dead
            server.ENGINE_KEY = matching_key
            server.ENGINE_CONTEXT_ID = "public:" + "2" * 32
            launched_with = {}

            def launch(*_args, **kwargs):
                launched_with.update(kwargs)
                return replacement

            server.Lc0 = launch
            try:
                metadata = os.stat(weights_path)
                model = server._PublicPlayModelRuntime(
                    choice=server.ModelChoice(
                        id="v37-latest.pb.gz",
                        label="v37-latest",
                        sha256="a" * 64,
                        compatibility_backends=(requested_backend,),
                    ),
                    path=weights_path,
                    size=metadata.st_size,
                    mtime_ns=metadata.st_mtime_ns,
                )
                sessions = server._PublicPlayEngineSessions(
                    config, (model,)
                )
                with sessions(
                    "public:" + "2" * 32,
                    "v37-latest.pb.gz",
                    requested_backend,
                ) as launched:
                    self.assertIs(launched, replacement)
                self.assertEqual(launched_with["backend"], requested_backend)
            finally:
                (
                    server.ENGINE,
                    server.ENGINE_KEY,
                    server.ENGINE_CONTEXT_ID,
                    server.Lc0,
                ) = original

    def test_unrestricted_models_response_exposes_backend_choices(self):
        config = replace(
            server._profile_defaults("local", {}),
            backend="sycl-auto",
            available_backends=("sycl-auto", "sycl", "sycl-fp16", "blas"),
        )
        server.BoardServer.runtime_config = config
        server.BoardServer.runs_root = config.runs_root
        response = {}
        self.handler._discover_weights = lambda: []
        self.handler._send_json = lambda body, status=200: response.update(
            body=body, status=status
        )

        self.handler._handle_models()

        self.assertEqual(response["status"], 200)
        self.assertEqual(
            response["body"]["available_backends"],
            ["sycl-auto", "sycl", "sycl-fp16", "blas"],
        )
        self.assertEqual(response["body"]["default_backend"], "sycl-auto")

    def test_hosted_models_response_uses_configured_sycl_backend(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            weights_path = os.path.join(temp_dir, "v37-latest.pb.gz")
            with open(weights_path, "wb"):
                pass
            config = replace(
                server._profile_defaults("hosted", {}),
                weights_path=weights_path,
                backend="sycl-auto",
                available_backends=(
                    "sycl-auto",
                    "sycl",
                    "sycl-fp16",
                    "blas",
                ),
            )
            server.BoardServer.runtime_config = config
            server.BoardServer.runs_root = config.runs_root
            response = {}
            self.handler._send_json = (
                lambda body, status=200: response.update(
                    body=body,
                    status=status,
                )
            )

            self.handler._handle_models()

        self.assertEqual(response["status"], 200)
        self.assertFalse(response["body"]["locked"])
        self.assertTrue(response["body"]["managed"])
        self.assertEqual(
            response["body"]["default_backend"],
            "sycl-auto",
        )
        self.assertEqual(
            response["body"]["available_backends"],
            ["sycl-auto", "sycl", "sycl-fp16", "blas"],
        )

    def test_unrestricted_engine_request_enforces_backend_choices(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            weights_path = os.path.join(temp_dir, "v37-latest.pb.gz")
            with open(weights_path, "wb"):
                pass
            config = replace(
                server._profile_defaults("local", {}),
                weights_path=weights_path,
                available_backends=("blas", "sycl"),
            )
            server.BoardServer.runtime_config = config
            server.BoardServer.runs_root = config.runs_root

            result = self.handler._engine_config_from_body(
                {"backend": "sycl"}
            )
            self.assertEqual(result[2], "sycl")

            for backend in ("sycl-auto", "", 7, None):
                with self.subTest(backend=backend):
                    with self.assertRaisesRegex(
                        server.RequestError, "Backend is not available"
                    ):
                        self.handler._engine_config_from_body(
                            {"backend": backend}
                        )

    def test_hosted_engine_configuration_is_fixed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lc0_path = os.path.join(temp_dir, "lc0")
            weights_path = os.path.join(temp_dir, "v37-latest.pb.gz")
            for path in (lc0_path, weights_path):
                with open(path, "wb"):
                    pass
            os.chmod(lc0_path, 0o755)
            config = replace(
                server._profile_defaults("hosted", {}),
                runs_root=os.path.join(temp_dir, "runs"),
                lc0_path=lc0_path,
                weights_path=weights_path,
            )
            server.BoardServer.runtime_config = config
            server.BoardServer.runs_root = config.runs_root

            result = self.handler._engine_config_from_body(
                {
                    "fen": "startpos",
                    "moves": ["e2e4"],
                    "engine_context_id": "a" * 32,
                    "position_revision": 4,
                    "analysis_request_id": 7,
                }
            )

            self.assertEqual(
                result,
                (
                    lc0_path,
                    weights_path,
                    "blas",
                    32,
                    False,
                    {
                        "HistoryFill": "repeat_current",
                        "CPuct": "1.2",
                        "PolicyTemperature": "1.0",
                        "DisableAbilityEdges": "false",
                        "SmartPruningFactor": "0",
                    },
                ),
            )

    def test_hosted_engine_configuration_allows_only_bounded_safe_controls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lc0_path = os.path.join(temp_dir, "lc0")
            weights_path = os.path.join(temp_dir, "v37-latest.pb.gz")
            for path in (lc0_path, weights_path):
                with open(path, "wb"):
                    pass
            os.chmod(lc0_path, 0o755)
            config = replace(
                server._profile_defaults("hosted", {}),
                lc0_path=lc0_path,
                weights_path=weights_path,
                backend="sycl",
                available_backends=("sycl", "blas"),
            )
            server.BoardServer.runtime_config = config

            result = self.handler._engine_config_from_body({
                "backend": "blas",
                "nodes": 1234,
                "disable_ability_edges": True,
                "smart_pruning_factor": 1.5,
                "options": {
                    "HistoryFill": "fen_only",
                    "CPuct": "2.4",
                    "PolicyTemperature": "0.75",
                },
            })
            self.assertEqual(result[:5], (
                lc0_path,
                weights_path,
                "blas",
                32,
                False,
            ))
            self.assertEqual(result[5], {
                "HistoryFill": "fen_only",
                "CPuct": "2.4",
                "PolicyTemperature": "0.75",
                "DisableAbilityEdges": "true",
                "SmartPruningFactor": "1.5",
            })

            unsafe_overrides = {
                "weights": "/tmp/other.pb.gz",
                "lc0": "/tmp/other-lc0",
                "minibatch_size": 1,
                "use_modules": True,
                "options": {"FpuValue": 99},
            }
            for name, value in unsafe_overrides.items():
                with self.subTest(name=name):
                    with self.assertRaises(server.RequestError):
                        self.handler._engine_config_from_body({name: value})

            invalid_safe_overrides = (
                {"backend": "cuda"},
                {"disable_ability_edges": "true"},
                {"smart_pruning_factor": -1},
                {"smart_pruning_factor": 11},
                {"smart_pruning_factor": "nan"},
                {"options": []},
                {"options": {"HistoryFill": "unknown"}},
                {"options": {"CPuct": 101}},
                {"options": {"PolicyTemperature": 0}},
            )
            for body in invalid_safe_overrides:
                with self.subTest(body=body):
                    with self.assertRaises(server.RequestError):
                        self.handler._engine_config_from_body(body)

    def test_hosted_engine_selects_only_a_declared_model_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lc0_path = os.path.join(temp_dir, "lc0")
            default_path = os.path.join(temp_dir, "default.pb.gz")
            sparring_path = os.path.join(temp_dir, "sparring.pb.gz")
            for path in (lc0_path, default_path, sparring_path):
                with open(path, "wb") as handle:
                    handle.write(path.encode("utf-8"))
            os.chmod(lc0_path, 0o755)
            config = replace(
                server._profile_defaults("hosted", {}),
                lc0_path=lc0_path,
                weights_path=default_path,
            )
            runtimes = []
            for model_id, label, path in (
                ("default.pb.gz", "Default", default_path),
                ("sparring.pb.gz", "Sparring", sparring_path),
            ):
                metadata = os.stat(path)
                runtimes.append(server._PublicPlayModelRuntime(
                    choice=server.ModelChoice(
                        id=model_id,
                        label=label,
                        sha256=server._sha256_file(path),
                    ),
                    path=path,
                    size=metadata.st_size,
                    mtime_ns=metadata.st_mtime_ns,
                ))
            server.BoardServer.runtime_config = config
            server.BoardServer.model_runtimes = tuple(runtimes)

            default = self.handler._engine_config_from_body({})
            sparring = self.handler._engine_config_from_body({
                "model_id": "sparring.pb.gz",
            })

            self.assertEqual(default[1], default_path)
            self.assertEqual(sparring[1], sparring_path)
            with self.assertRaisesRegex(
                server.RequestError, "Model is not available"
            ):
                self.handler._engine_config_from_body({
                    "model_id": "../../arbitrary.pb.gz",
                })

    def test_hosted_position_limits_have_explicit_boundaries(self):
        server.BoardServer.runtime_config = server._profile_defaults("hosted", {})

        fen, moves = self.handler._position_from_body(
            {
                "fen": "x" * 256,
                "moves": ["a2a4"] * server.HOSTED_MAX_MOVES,
            }
        )
        self.assertEqual(fen, "x" * 256)
        self.assertEqual(len(moves), server.HOSTED_MAX_MOVES)

        with self.assertRaisesRegex(server.RequestError, "FEN is too long"):
            self.handler._position_from_body({"fen": "x" * 257})
        with self.assertRaisesRegex(server.RequestError, "FEN is too long"):
            self.handler._position_from_body({"fen": "start\npos"})
        with self.assertRaisesRegex(server.RequestError, "invalid characters"):
            self.handler._position_from_body({"fen": "\ud800"})
        with self.assertRaisesRegex(server.RequestError, "At most 2048 moves"):
            self.handler._position_from_body(
                {"moves": ["a2a4"] * (server.HOSTED_MAX_MOVES + 1)}
            )

    def test_hosted_position_rejects_non_uci_move_tokens(self):
        server.BoardServer.runtime_config = server._profile_defaults("hosted", {})

        for move in ("e2-e4", "e9e4", "e2e4qq", "e2e4 e7e5", ""):
            with self.subTest(move=move):
                with self.assertRaisesRegex(server.RequestError, "Invalid UCI move"):
                    self.handler._position_from_body({"moves": [move]})
        with self.assertRaisesRegex(
            server.RequestError, "Every move must be a UCI string"
        ):
            self.handler._position_from_body({"moves": [1234]})


class EngineContextRuntimeTest(unittest.TestCase):
    CONTEXT_A = "a" * 32
    CONTEXT_B = "b" * 32

    class FakeEngine:
        def __init__(self):
            self.new_game_calls = 0
            self.positions = []
            self.searches = []
            self.next_moves = []
            self.position_error = None

        def new_game(self):
            self.new_game_calls += 1

        def set_position(self, fen, moves):
            if self.position_error is not None:
                raise self.position_error
            self.positions.append((fen, list(moves)))

        def position_info(self):
            moves = self.positions[-1][1]
            mid = bool(moves and moves[-1].endswith("+"))
            if mid:
                legal = ["g1f3"]
            elif not moves:
                legal = ["e2e4", "e2e4+"]
            else:
                legal = ["e7e5", "e7e5+"]
            return {
                "fen": "authoritative fen",
                "turn": "w" if mid else "b",
                "ability": {
                    "white": not bool(moves),
                    "black": True,
                    "mid": mid,
                },
                "result": "ongoing",
                "legal": legal,
            }

        def verbose_stats(self, nodes):
            self.searches.append(("eval", nodes))
            return ["info string node N: 10"], "bestmove e2e4"

        def verbose_stats_stream(
            self,
            nodes,
            callback,
            multipv=1,
            cancel_event=None,
            initial_snapshot=False,
        ):
            self.searches.append((
                "eval-stream",
                nodes,
                initial_snapshot,
            ))
            if cancel_event is not None and cancel_event.is_set():
                return [], None, None, True
            info = dm_cli.UciInfoSnapshot(
                "info depth 2 nodes 10 score cp 1 multipv 1 pv e2e4",
                (
                    "info depth 2 nodes 10 score cp 1 multipv 1 pv e2e4",
                    "info depth 2 nodes 10 score cp -2 multipv 2 pv d2d4",
                )[:multipv],
            )
            verbose = [
                "info string e2e4 N: 9",
                "info string node N: 10",
            ]
            keep_searching = callback(10, verbose, info)
            return verbose, "bestmove e2e4", 10, keep_searching is False

        def go_nodes(self, nodes):
            self.searches.append(("move", nodes))
            move = self.next_moves.pop(0)
            return move, None, f"info depth 1 pv {move}"

    def setUp(self):
        self.original_engine = server.ENGINE
        self.original_engine_key = server.ENGINE_KEY
        self.original_context = server.ENGINE_CONTEXT_ID
        server.ENGINE = None
        server.ENGINE_KEY = None
        server.ENGINE_CONTEXT_ID = None
        self.handler = object.__new__(server.BoardServer)
        self.handler.runtime_config = server._profile_defaults("local", {})
        self.handler.analysis_requests = server._AnalysisRequestCoordinator()

    def tearDown(self):
        server.ENGINE = self.original_engine
        server.ENGINE_KEY = self.original_engine_key
        server.ENGINE_CONTEXT_ID = self.original_context

    def install_fake_engine(self, engine):
        server.ENGINE = engine
        server.ENGINE_KEY = ("fake",)
        self.handler._get_engine = lambda _body: engine

    def capture_response(self):
        responses = []
        self.handler._send_json = (
            lambda body, status=200: responses.append((status, body))
        )
        return responses

    def capture_stream(self):
        records = []
        starts = []
        self.handler._begin_engine_stream = lambda: starts.append(True)
        self.handler._send_engine_stream_record = records.append
        return starts, records

    def test_streamed_eval_reports_live_visits_and_final_result(self):
        engine = self.FakeEngine()
        self.install_fake_engine(engine)
        server.ENGINE_CONTEXT_ID = (
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_A
        )
        body = {
            "fen": "startpos",
            "moves": [],
            "nodes": 12,
            "engine_context_id": self.CONTEXT_A,
            "position_revision": 7,
            "analysis_request_id": 19,
        }
        self.handler._read_json = lambda: body
        starts, records = self.capture_stream()

        self.handler._handle_engine_eval_stream()

        self.assertEqual(starts, [True])
        self.assertEqual([record["type"] for record in records], [
            "progress",
            "complete",
        ])
        self.assertEqual(
            (
                records[0]["nodes"],
                records[0]["target_nodes"],
                records[0]["bestmove"],
            ),
            (10, 12, "e2e4"),
        )
        self.assertEqual(records[1]["actual_nodes"], 10)
        self.assertEqual(records[1]["nodes"], 12)
        self.assertEqual(
            records[1]["engine_context_id"], self.CONTEXT_A
        )
        self.assertEqual(records[1]["position_revision"], 7)
        self.assertEqual(records[1]["analysis_request_id"], 19)
        self.assertEqual(engine.searches, [("eval-stream", 12, True)])
        self.assertEqual(engine.new_game_calls, 0)

    def test_terminal_root_completes_without_searching_or_killing_engine(self):
        class TerminalEngine(self.FakeEngine):
            def position_info(self):
                info = super().position_info()
                info.update({
                    "result": "draw",
                    "legal": [],
                })
                return info

        engine = TerminalEngine()
        self.install_fake_engine(engine)
        server.ENGINE_CONTEXT_ID = (
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_A
        )
        body = {
            "fen": "startpos",
            "moves": [],
            "nodes": 800,
            "engine_context_id": self.CONTEXT_A,
            "position_revision": 7,
            "analysis_request_id": 19,
        }
        self.handler._read_json = lambda: body
        starts, records = self.capture_stream()

        self.handler._handle_engine_eval_stream()

        self.assertEqual(starts, [True])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], "complete")
        self.assertTrue(records[0]["terminal"])
        self.assertEqual(records[0]["game_result"], "draw")
        self.assertEqual(records[0]["actual_nodes"], 0)
        self.assertEqual(records[0]["target_nodes"], 800)
        self.assertEqual(engine.searches, [])
        self.assertIs(server.ENGINE, engine)
        self.assertEqual(
            server.ENGINE_CONTEXT_ID,
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_A,
        )

        responses = self.capture_response()
        self.handler._read_json = lambda: {
            key: value
            for key, value in body.items()
            if key != "analysis_request_id"
        }
        self.handler._handle_engine_eval()

        self.assertEqual(responses[0][0], 200)
        self.assertTrue(responses[0][1]["terminal"])
        self.assertEqual(responses[0][1]["game_result"], "draw")
        self.assertEqual(responses[0][1]["actual_nodes"], 0)
        self.assertEqual(engine.searches, [])

    def test_internal_stream_failure_quarantines_context_for_recovery(self):
        class FailingEngine(self.FakeEngine):
            def verbose_stats_stream(self, *_args, **_kwargs):
                raise RuntimeError("engine protocol failed")

        engine = FailingEngine()
        self.install_fake_engine(engine)
        server.ENGINE_CONTEXT_ID = (
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_A
        )
        self.handler._read_json = lambda: {
            "fen": "startpos",
            "moves": [],
            "nodes": 12,
            "engine_context_id": self.CONTEXT_A,
            "position_revision": 7,
            "analysis_request_id": 19,
        }
        _starts, records = self.capture_stream()

        self.handler._handle_engine_eval_stream()

        self.assertEqual(records[-1]["type"], "error")
        self.assertTrue(records[-1]["retriable"])
        self.assertIsNone(server.ENGINE_CONTEXT_ID)
        self.handler._engine_for_context({}, self.CONTEXT_A)
        self.assertEqual(engine.new_game_calls, 1)

    def test_disconnected_eval_stream_cancels_without_clearing_tree(self):
        engine = self.FakeEngine()
        self.install_fake_engine(engine)
        server.ENGINE_CONTEXT_ID = (
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_A
        )
        self.handler._read_json = lambda: {
            "nodes": 800,
            "engine_context_id": self.CONTEXT_A,
            "position_revision": 3,
        }
        self.handler._begin_engine_stream = lambda: None
        writes = []

        def disconnect(record):
            writes.append(record)
            raise BrokenPipeError

        self.handler._send_engine_stream_record = disconnect

        self.handler._handle_engine_eval_stream()

        self.assertEqual([record["type"] for record in writes], ["progress"])
        self.assertEqual(engine.searches, [("eval-stream", 800, True)])
        self.assertEqual(engine.new_game_calls, 0)
        self.assertTrue(server.ENGINE_LOCK.acquire(blocking=False))
        server.ENGINE_LOCK.release()

    def test_stream_rejects_invalid_visits_before_sending_headers(self):
        engine = self.FakeEngine()
        self.install_fake_engine(engine)
        self.handler._read_json = lambda: {
            "nodes": -1,
            "engine_context_id": self.CONTEXT_A,
        }
        starts, records = self.capture_stream()
        responses = self.capture_response()

        self.handler._handle_engine_eval_stream()

        self.assertEqual(starts, [])
        self.assertEqual(records, [])
        self.assertEqual(responses[0][0], 400)
        self.assertIn("Visits", responses[0][1]["error"])
        self.assertEqual(engine.searches, [])

    def test_superseded_stream_does_not_spend_engine_rate_limit(self):
        newest = self.handler.analysis_requests.begin(self.CONTEXT_A, 2)
        self.handler.analysis_requests.finish(newest)
        self.handler.engine_rate_limiter = server.TokenBucketLimiter(
            capacity=1,
            refill_per_second=1,
            maximum_buckets=1,
            clock=lambda: 0,
        )
        self.handler._read_json = lambda: {
            "nodes": 800,
            "engine_context_id": self.CONTEXT_A,
            "position_revision": 3,
            "analysis_request_id": 1,
        }
        responses = self.capture_response()

        self.handler._handle_engine_eval_stream()

        self.assertEqual(responses[0][0], 409)
        self.handler.engine_rate_limiter.consume("global")

    def test_cancel_endpoint_stops_only_the_named_analysis_request(self):
        ticket = self.handler.analysis_requests.begin(self.CONTEXT_A, 27)
        self.handler._read_json = lambda: {
            "engine_context_id": self.CONTEXT_A,
            "position_revision": 9,
            "analysis_request_id": 27,
        }
        responses = self.capture_response()

        self.handler._handle_engine_eval_cancel()

        self.assertTrue(ticket.cancel_event.is_set())
        self.assertEqual(responses, [(
            202,
            {
                "cancelled": True,
                "engine_context_id": self.CONTEXT_A,
                "position_revision": 9,
                "analysis_request_id": 27,
            },
        )])

    def test_visit_validation_accepts_exact_integer_or_digit_string(self):
        self.assertEqual(self.handler._search_nodes_from_body({}), 800)
        self.assertEqual(
            self.handler._search_nodes_from_body({"nodes": 17}), 17
        )
        self.assertEqual(
            self.handler._search_nodes_from_body({"nodes": "18"}), 18
        )
        for value in (True, 0, -1, 1.5, "1.5", "lots"):
            with self.subTest(value=value):
                with self.assertRaises(server.RequestError):
                    self.handler._search_nodes_from_body({"nodes": value})
        with self.assertRaises(server.RequestError):
            self.handler._search_nodes_from_body(
                {"nodes": server.MAX_SEARCH_NODES + 1}
            )

    def test_hosted_visit_override_is_bounded_for_public_analysis(self):
        self.handler.runtime_config = server._profile_defaults("hosted", {})
        self.assertEqual(
            self.handler._search_nodes_from_body({"nodes": 25}),
            25,
        )
        self.assertEqual(
            self.handler._search_nodes_from_body(
                {"nodes": server.HOSTED_MAX_SEARCH_NODES}
            ),
            server.HOSTED_MAX_SEARCH_NODES,
        )
        with self.assertRaisesRegex(server.RequestError, "between 1 and"):
            self.handler._search_nodes_from_body(
                {"nodes": server.HOSTED_MAX_SEARCH_NODES + 1}
            )

    def test_same_context_reuses_tree_and_echoes_revision(self):
        engine = self.FakeEngine()
        self.install_fake_engine(engine)
        server.ENGINE_CONTEXT_ID = (
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_A
        )
        bodies = [
            {
                "fen": "startpos",
                "moves": [],
                "nodes": 12,
                "engine_context_id": self.CONTEXT_A,
                "position_revision": 7,
            },
            {
                "fen": "startpos",
                "moves": ["e2e4"],
                "nodes": 12,
                "engine_context_id": self.CONTEXT_A,
                "position_revision": 8,
            },
        ]
        responses = self.capture_response()

        for body in bodies:
            self.handler._read_json = lambda body=body: body
            self.handler._handle_engine_eval()

        self.assertEqual(engine.new_game_calls, 0)
        self.assertEqual(
            [response[1]["position_revision"] for response in responses],
            [7, 8],
        )
        self.assertTrue(
            all(
                response[1]["engine_context_id"] == self.CONTEXT_A
                for response in responses
            )
        )
        self.assertEqual(
            [response[1]["actual_nodes"] for response in responses],
            [10, 10],
        )

    def test_context_switch_and_explicit_clear_reset_tree(self):
        engine = self.FakeEngine()
        self.install_fake_engine(engine)
        server.ENGINE_CONTEXT_ID = (
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_A
        )

        self.handler._engine_for_context({}, self.CONTEXT_A)
        self.assertEqual(engine.new_game_calls, 0)

        self.handler._engine_for_context({}, self.CONTEXT_B)
        self.assertEqual(engine.new_game_calls, 1)
        self.assertEqual(
            server.ENGINE_CONTEXT_ID,
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_B,
        )

        self.handler._engine_for_context(
            {}, self.CONTEXT_B, force_clear=True
        )
        self.assertEqual(engine.new_game_calls, 2)

    def test_public_looking_client_context_can_only_own_analysis_namespace(self):
        engine = self.FakeEngine()
        self.install_fake_engine(engine)
        public_id = "1" * 32
        client_context = "public_play_" + public_id
        server.ENGINE_CONTEXT_ID = "public:" + public_id
        self.handler.engine_rate_limiter = server.TokenBucketLimiter(
            capacity=10,
            refill_per_second=1,
        )
        responses = self.capture_response()
        body = {
            "fen": "startpos",
            "moves": [],
            "nodes": 1,
            "engine_context_id": client_context,
            "position_revision": 4,
        }
        self.handler._read_json = lambda: body

        self.handler._handle_engine_eval()

        self.assertEqual(engine.new_game_calls, 1)
        self.assertEqual(
            server.ENGINE_CONTEXT_ID,
            server.ANALYSIS_CONTEXT_PREFIX + client_context,
        )
        self.assertEqual(
            responses[-1][1]["engine_context_id"], client_context
        )

        self.handler._handle_engine_newgame()

        self.assertEqual(engine.new_game_calls, 2)
        self.assertEqual(
            server.ENGINE_CONTEXT_ID,
            server.ANALYSIS_CONTEXT_PREFIX + client_context,
        )
        self.assertNotEqual(server.ENGINE_CONTEXT_ID, "public:" + public_id)

    def test_engine_endpoint_global_limit_is_bounded_and_recovers(self):
        engine = self.FakeEngine()
        self.install_fake_engine(engine)
        now = [0.0]
        self.handler.engine_rate_limiter = server.TokenBucketLimiter(
            capacity=1,
            refill_per_second=1,
            maximum_buckets=1,
            clock=lambda: now[0],
        )
        body = {
            "fen": "startpos",
            "moves": [],
            "nodes": 1,
            "engine_context_id": self.CONTEXT_A,
            "position_revision": 0,
        }
        self.handler._read_json = lambda: body
        responses = self.capture_response()

        self.handler._handle_engine_eval()
        self.handler._handle_engine_eval()
        now[0] += 1
        self.handler._handle_engine_eval()

        self.assertEqual(
            [status for status, _body in responses],
            [200, 429, 200],
        )
        self.assertEqual(responses[1][1]["code"], "rate_limited")
        self.assertEqual(engine.searches, [("eval", 1), ("eval", 1)])

    def test_missing_context_gets_unique_non_reusing_legacy_ids(self):
        first_context, first_revision = (
            self.handler._engine_request_metadata({})
        )
        second_context, second_revision = (
            self.handler._engine_request_metadata({})
        )

        self.assertNotEqual(first_context, second_context)
        self.assertRegex(first_context, server.ENGINE_CONTEXT_RE)
        self.assertRegex(second_context, server.ENGINE_CONTEXT_RE)
        self.assertEqual((first_revision, second_revision), (0, 0))

    def test_invalid_context_and_revision_are_rejected(self):
        for context in ("short", "../not-opaque", 42):
            with self.subTest(context=context):
                with self.assertRaisesRegex(
                    server.RequestError, "engine_context_id"
                ):
                    self.handler._engine_request_metadata(
                        {"engine_context_id": context}
                    )
        for revision in (-1, True, 1.5, "4"):
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(
                    server.RequestError, "position_revision"
                ):
                    self.handler._engine_request_metadata(
                        {"position_revision": revision}
                    )
        self.assertIsNone(self.handler._analysis_request_id({}))
        self.assertEqual(
            self.handler._analysis_request_id({"analysis_request_id": 4}),
            4,
        )
        for request_id in (-1, True, 1.5, "4"):
            with self.subTest(request_id=request_id):
                with self.assertRaisesRegex(
                    server.RequestError, "analysis_request_id"
                ):
                    self.handler._analysis_request_id(
                        {"analysis_request_id": request_id}
                    )

    def test_rejected_engine_position_is_http_400_without_search(self):
        engine = self.FakeEngine()
        engine.position_error = dm_cli.Lc0PositionError("bad history")
        self.install_fake_engine(engine)
        server.ENGINE_CONTEXT_ID = (
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_A
        )
        self.handler._read_json = lambda: {
            "engine_context_id": self.CONTEXT_A,
            "position_revision": 3,
        }
        responses = self.capture_response()

        self.handler._handle_engine_eval()

        self.assertEqual(
            responses,
            [(400, {"error": "Invalid position: bad history"})],
        )
        self.assertEqual(engine.searches, [])

    def test_double_engine_move_is_validated_at_every_half(self):
        engine = self.FakeEngine()
        engine.next_moves = ["e2e4+", "g1f3"]
        self.install_fake_engine(engine)
        server.ENGINE_CONTEXT_ID = (
            server.ANALYSIS_CONTEXT_PREFIX + self.CONTEXT_A
        )
        self.handler._read_json = lambda: {
            "fen": "startpos",
            "moves": [],
            "nodes": 9,
            "engine_context_id": self.CONTEXT_A,
            "position_revision": 11,
        }
        responses = self.capture_response()

        self.handler._handle_engine_move()

        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1]["moves"], ["e2e4+", "g1f3"])
        self.assertEqual(responses[0][1]["position_revision"], 11)
        self.assertEqual(
            engine.positions,
            [
                ("startpos", []),
                ("startpos", ["e2e4+"]),
                ("startpos", ["e2e4+", "g1f3"]),
            ],
        )


class HostedHTTPRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        lc0_path = os.path.join(self.temp_dir.name, "lc0")
        weights_path = os.path.join(self.temp_dir.name, "v37-latest.pb.gz")
        with open(lc0_path, "wb"):
            pass
        with open(weights_path, "wb"):
            pass
        os.chmod(lc0_path, 0o755)

        config = replace(
            server._profile_defaults("hosted", {}),
            host="127.0.0.1",
            port=0,
            runs_root=os.path.join(self.temp_dir.name, "runs"),
            lc0_path=lc0_path,
            weights_path=weights_path,
        )

        class FakePlayService:
            token = "A" * 43

            def __init__(fake_self):
                fake_self.calls = []
                fake_self.owner_pending = False
                fake_self.store = SimpleNamespace(
                    health_check=lambda: True
                )

            def parse_session_cookie(fake_self, header):
                return server.PublicPlayService.parse_session_cookie(header)

            def session_cookie_header(fake_self, token):
                return (
                    f"dm_session={token}; Path=/; Max-Age=2592000; "
                    "HttpOnly; SameSite=Lax"
                )

            def get_config(fake_self, token):
                fake_self.calls.append(("config", token))
                return SimpleNamespace(
                    payload={
                        "csrf_token": "csrf",
                        "presets": [],
                        "clocks": [],
                        "active_game_id": None,
                    },
                    session_token=fake_self.token,
                )

            def get_owner_game(fake_self, token, game_id):
                fake_self.calls.append(("owner", token, game_id))
                return {
                    "game": {
                        "id": game_id,
                        "pending_command": fake_self.owner_pending,
                    }
                }

            def create_game(fake_self, *arguments):
                fake_self.calls.append(("create", *arguments))
                return {"game": {"id": "1" * 32}}

            def move(fake_self, *arguments):
                fake_self.calls.append(("move", *arguments))
                return {"game": {"id": arguments[2]}}

            takeback = move
            resign = move

            def list_public_games(fake_self, *, limit, cursor):
                fake_self.calls.append(("list", limit, cursor))
                return {"games": [], "next_cursor": None}

            def get_public_game(fake_self, game_id):
                fake_self.calls.append(("public", game_id))
                return {"game": {"id": game_id, "status": "completed"}}

        self.play_service = FakePlayService()

        class TestHandler(server.BoardServer):
            def _handle_engine_newgame(handler_self):
                handler_self._send_json({"stage": "accepted"}, 202)

        TestHandler.runtime_config = config
        TestHandler.runs_root = config.runs_root
        TestHandler.play_service = self.play_service
        self.access_store = server.AccessStore(
            os.path.join(self.temp_dir.name, "access.sqlite3")
        )
        TestHandler.access_store = self.access_store
        TestHandler.public_origins = ()
        TestHandler.canonical_origin = ""
        TestHandler.legacy_origins = ()
        TestHandler.engine_rate_limiter = server.TokenBucketLimiter(
            capacity=1000,
            refill_per_second=1000,
            maximum_buckets=1,
        )
        TestHandler.archive_rate_limiter = server.TokenBucketLimiter(
            capacity=1000,
            refill_per_second=1000,
            maximum_buckets=1,
        )
        TestHandler.access_redeem_rate_limiter = server.TokenBucketLimiter(
            capacity=1000,
            refill_per_second=1000,
            maximum_buckets=1,
        )
        self.httpd = server.BoardHTTPServer(("127.0.0.1", 0), TestHandler)
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, headers=None):
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
        headers = dict(headers or {})
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=5
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            response_headers = {
                key.lower(): value for key, value in response.getheaders()
            }
            return response.status, response_headers, payload
        finally:
            connection.close()

    def json_request(self, method, path, body=None, headers=None):
        status, response_headers, payload = self.request(
            method, path, body=body, headers=headers
        )
        return status, response_headers, json.loads(payload.decode("utf-8"))

    def test_root_and_vendored_assets_are_served_from_web_directory(self):
        status, _headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body)
        self.assertIn(b"Once per game, move twice.", body)

        status, _headers, body = self.request(
            "GET", "/vendor/jquery/jquery-3.7.1.min.js"
        )
        self.assertEqual(status, 200)
        self.assertIn(b"jQuery v3.7.1", body)

        status, _headers, body = self.request("GET", "/invite")
        self.assertEqual(status, 200)
        self.assertIn(b"Checking your invitation", body)

        status, _headers, body = self.request("GET", "/invite.js")
        self.assertEqual(status, 200)
        self.assertIn(b"takeTokenAndCleanLocation", body)

    def test_first_party_assets_are_not_stored_while_vendored_assets_remain_stable(self):
        for path in (
            "/analysis",
            "/analysis-ui.js?v=20260805c",
            "/public-site.css?v=20260805c",
        ):
            with self.subTest(path=path):
                status, headers, _body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(
                    headers.get("cache-control"),
                    "no-store",
                )
                self.assertEqual(
                    headers.get("cloudflare-cdn-cache-control"),
                    "no-store",
                )

        status, headers, _body = self.request(
            "GET",
            "/vendor/jquery/jquery-3.7.1.min.js",
        )
        self.assertEqual(status, 200)
        self.assertNotIn("cache-control", headers)

    def test_public_static_routes_use_history_fallbacks(self):
        for path, marker in (
            ("/analysis", b"id=\"board\""),
            ("/play", b"id=\"setupForm\""),
            ("/play/" + "1" * 32, b"id=\"playBoard\""),
            ("/games", b"data-page=\"games\""),
            ("/games/" + "2" * 32, b"id=\"detailContent\""),
            ("/rules", b"data-page=\"rules\""),
        ):
            with self.subTest(path=path):
                status, _headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(marker, body)

    def test_invite_only_redirects_pages_and_rejects_protected_apis(self):
        self.access_store.set_mode(server.MODE_INVITE_ONLY)

        status, headers, body = self.request(
            "GET", "/analysis?fen=startpos"
        )
        self.assertEqual(status, 302)
        self.assertEqual(
            headers["location"],
            "/invite?next=%2Fanalysis%3Ffen%3Dstartpos",
        )
        self.assertEqual(body, b"")

        status, _headers, body = self.json_request(
            "GET", "/api/models"
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "access_required")

        for public_path in ("/", "/rules", "/invite", "/api/access/status"):
            with self.subTest(path=public_path):
                status, _headers, _body = self.request("GET", public_path)
                self.assertEqual(status, 200)

    def test_invitation_redeems_to_http_only_cookie_and_unlocks_routes(self):
        invitation = self.access_store.create_invitation("Browser test")
        self.access_store.set_mode(server.MODE_INVITE_ONLY)

        status, headers, body = self.json_request(
            "POST",
            "/api/access/redeem",
            body={"token": invitation.token},
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["authorized"])
        self.assertEqual(body["remaining_uses"], 4)
        cookie = headers["set-cookie"].split(";", 1)[0]
        self.assertTrue(cookie.startswith("dm_access="))
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("SameSite=Lax", headers["set-cookie"])

        status, _headers, page = self.request(
            "GET", "/play", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertIn(b"id=\"setupForm\"", page)

        status, _headers, model_data = self.json_request(
            "GET", "/api/models", headers={"Cookie": cookie}
        )
        self.assertEqual(status, 200)
        self.assertFalse(model_data["locked"])
        self.assertTrue(model_data["managed"])

    def test_legacy_host_transfers_an_existing_invited_browser_once(self):
        invitation = self.access_store.create_invitation("Domain migration")
        source = self.access_store.redeem(invitation.token)
        source_cookie = "dm_access=" + source.session_token
        self.access_store.set_mode(server.MODE_INVITE_ONLY)
        handler = self.httpd.RequestHandlerClass
        handler.public_origins = (
            "https://doublestrikechess.com",
            self.origin,
        )
        handler.canonical_origin = "https://doublestrikechess.com"
        handler.legacy_origins = (self.origin,)

        status, headers, body = self.request(
            "GET",
            "/play?from=legacy",
            headers={"Cookie": source_cookie},
        )
        self.assertEqual(status, 302)
        self.assertEqual(body, b"")
        redirect = urlsplit(headers["location"])
        self.assertEqual(redirect.scheme, "https")
        self.assertEqual(redirect.netloc, "doublestrikechess.com")
        self.assertEqual(redirect.path, "/invite")
        self.assertEqual(
            parse_qs(redirect.query)["next"],
            ["/play?from=legacy"],
        )
        self.assertTrue(redirect.fragment.startswith("transfer="))
        transfer_token = redirect.fragment.removeprefix("transfer=")

        status, transfer_headers, payload = self.json_request(
            "POST",
            "/api/access/transfer",
            body={"token": transfer_token},
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"authorized": True})
        self.assertTrue(
            transfer_headers["set-cookie"].startswith("dm_access=")
        )
        self.assertEqual(
            self.access_store.list_invitations()[0]["redemption_count"],
            1,
        )

        replay_status, _headers, replay = self.json_request(
            "POST",
            "/api/access/transfer",
            body={"token": transfer_token},
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
            },
        )
        self.assertEqual(replay_status, 403)
        self.assertEqual(replay["code"], "transfer_invalid")

    def test_mode_changes_and_revocation_take_effect_without_restart(self):
        invitation = self.access_store.create_invitation()
        redemption = self.access_store.redeem(invitation.token)
        cookie = "dm_access=" + redemption.session_token

        self.access_store.set_mode(server.MODE_INVITE_ONLY)
        self.assertEqual(
            self.request("GET", "/play", headers={"Cookie": cookie})[0],
            200,
        )
        self.access_store.revoke_invitation(invitation.id)
        self.assertEqual(
            self.request("GET", "/play", headers={"Cookie": cookie})[0],
            302,
        )
        self.access_store.set_mode(server.MODE_OPEN)
        self.assertEqual(self.request("GET", "/play")[0], 200)

    def test_missing_access_storage_fails_closed_only_for_protected_routes(self):
        handler = self.httpd.RequestHandlerClass
        original_store = handler.access_store
        handler.access_store = None
        try:
            page_status = self.request("GET", "/play")[0]
            api_status, _headers, api_body = self.json_request(
                "GET", "/api/models"
            )
            public_status = self.request("GET", "/rules")[0]
        finally:
            handler.access_store = original_store

        self.assertEqual(page_status, 503)
        self.assertEqual(api_status, 503)
        self.assertEqual(api_body["code"], "access_unavailable")
        self.assertEqual(public_status, 200)

    def test_play_config_sets_an_http_only_same_site_cookie(self):
        status, headers, body = self.json_request(
            "GET", "/api/play/config"
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["csrf_token"], "csrf")
        self.assertIn("dm_session=" + "A" * 43, headers["set-cookie"])
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("SameSite=Lax", headers["set-cookie"])
        self.assertNotIn("access-control-allow-origin", headers)

    def test_public_play_write_requires_same_origin_and_forwards_guards(self):
        body = {
            "display_name": "",
            "publish_name": False,
            "color": "white",
            "preset_id": "club",
            "clock_id": "unlimited",
        }
        status, _headers, rejected = self.json_request(
            "POST",
            "/api/play/games",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(rejected["error"], "Origin header is required.")

        status, headers, accepted = self.json_request(
            "POST",
            "/api/play/games",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
                "Cookie": "dm_session=" + "A" * 43,
                "X-CSRF-Token": "csrf",
                "Idempotency-Key": "request-key-0001",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(accepted["game"]["id"], "1" * 32)
        self.assertNotIn("access-control-allow-origin", headers)
        call = self.play_service.calls[-1]
        self.assertEqual(call[0], "create")
        self.assertEqual(call[1], "A" * 43)
        self.assertEqual(call[2:4], ("csrf", "request-key-0001"))

    def test_owner_and_public_archive_routes_are_distinct(self):
        game_id = "3" * 32
        status, _headers, owner = self.json_request(
            "GET",
            "/api/play/games/" + game_id,
            headers={"Cookie": "dm_session=" + "A" * 43},
        )
        self.assertEqual(status, 200)
        self.assertEqual(owner["game"]["id"], game_id)
        self.assertFalse(owner["game"]["pending_command"])

        status, _headers, page = self.json_request(
            "GET", "/api/games?limit=20"
        )
        self.assertEqual(status, 200)
        self.assertEqual(page, {"games": [], "next_cursor": None})
        self.assertEqual(self.play_service.calls[-1], ("list", 20, None))

        status, _headers, public = self.json_request(
            "GET", "/api/games/" + game_id
        )
        self.assertEqual(status, 200)
        self.assertEqual(public["game"]["status"], "completed")
        self.assertNotIn("pending_command", public["game"])

    def test_owner_route_preserves_pending_command_recovery_signal(self):
        game_id = "4" * 32
        self.play_service.owner_pending = True

        status, _headers, owner = self.json_request(
            "GET",
            "/api/play/games/" + game_id,
            headers={"Cookie": "dm_session=" + "A" * 43},
        )

        self.assertEqual(status, 200)
        self.assertTrue(owner["game"]["pending_command"])

    def test_public_archive_global_limit_is_bounded_and_recovers(self):
        now = [0.0]
        self.httpd.RequestHandlerClass.archive_rate_limiter = (
            server.TokenBucketLimiter(
                capacity=1,
                refill_per_second=1,
                maximum_buckets=1,
                clock=lambda: now[0],
            )
        )

        first = self.json_request("GET", "/api/games")
        limited = self.json_request("GET", "/api/games")
        now[0] += 1
        recovered = self.json_request("GET", "/api/games")

        self.assertEqual(
            [first[0], limited[0], recovered[0]], [200, 429, 200]
        )
        self.assertEqual(limited[2]["code"], "rate_limited")

    def test_health_reports_ready_engine_and_model(self):
        status, _headers, body = self.json_request("GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "status": "ok",
                "profile": "hosted",
                "engine": "ready",
                "model": "ready",
                "public_play": "ready",
                "storage": "ready",
                "access": "ready",
            },
        )

    def test_health_is_unavailable_when_a_runtime_artifact_is_missing(self):
        os.unlink(self.httpd.RequestHandlerClass.runtime_config.weights_path)

        status, _headers, body = self.json_request("GET", "/api/health")

        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(body["engine"], "ready")
        self.assertEqual(body["model"], "missing")

    def test_health_is_unavailable_when_public_play_storage_is_unavailable(self):
        handler = self.httpd.RequestHandlerClass
        original_service = handler.play_service
        handler.play_service = None
        try:
            status, _headers, body = self.json_request("GET", "/api/health")
        finally:
            handler.play_service = original_service

        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(body["public_play"], "unavailable")
        self.assertEqual(body["storage"], "unavailable")

    def test_health_probes_post_start_storage_failure(self):
        store = self.play_service.store
        original_check = store.health_check

        def unavailable():
            raise RuntimeError("database became unavailable")

        store.health_check = unavailable
        try:
            status, _headers, body = self.json_request(
                "GET", "/api/health"
            )
        finally:
            store.health_check = original_check

        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(body["public_play"], "unavailable")
        self.assertEqual(body["storage"], "unavailable")

    def test_health_reports_an_exited_engine_process(self):
        original = (
            server.ENGINE,
            server.ENGINE_KEY,
            server.ENGINE_CONTEXT_ID,
        )
        server.ENGINE = SimpleNamespace(
            proc=SimpleNamespace(poll=lambda: 17)
        )
        server.ENGINE_KEY = ("dead",)
        server.ENGINE_CONTEXT_ID = "public:" + "4" * 32
        try:
            status, _headers, body = self.json_request(
                "GET", "/api/health"
            )
        finally:
            (
                server.ENGINE,
                server.ENGINE_KEY,
                server.ENGINE_CONTEXT_ID,
            ) = original

        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(body["engine"], "unavailable")

    def test_hosted_run_browsing_apis_are_disabled(self):
        for path in (
            "/api/training/games",
            "/api/latest-game",
            "/api/runs",
        ):
            with self.subTest(path=path):
                status, _headers, body = self.json_request("GET", path)
                self.assertEqual(status, 404)
                self.assertEqual(body, {"error": "Not found"})

    def test_models_response_exposes_no_filesystem_paths(self):
        status, headers, body = self.json_request("GET", "/api/models")

        self.assertEqual(status, 200)
        self.assertFalse(body["locked"])
        self.assertTrue(body["managed"])
        self.assertEqual(
            body["models"],
            [{
                "id": "v37-latest.pb.gz",
                "name": "v37-latest",
            }],
        )
        self.assertNotIn("default_lc0", body)
        self.assertNotIn("default_weights", body)
        self.assertEqual(body["available_backends"], ["blas"])
        self.assertNotIn("access-control-allow-origin", headers)
        serialized = json.dumps(body)
        self.assertNotIn(self.temp_dir.name, serialized)
        self.assertNotIn(server.LOCAL_LC0, serialized)
        self.assertNotIn(server.LOCAL_WEIGHTS, serialized)

    def test_malformed_and_oversized_json_are_rejected_before_engine_use(self):
        headers = {
            "Content-Type": "application/json",
            "Origin": self.origin,
        }
        status, _response_headers, body = self.json_request(
            "POST",
            "/api/engine/eval",
            body=b"{broken",
            headers=headers,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "Malformed JSON request"})

        status, _response_headers, body = self.json_request(
            "POST",
            "/api/engine/eval",
            body=b"x" * (server.HOSTED_MAX_BODY + 1),
            headers=headers,
        )
        self.assertEqual(status, 413)
        self.assertEqual(body, {"error": "Request body is too large"})

    def test_cross_origin_is_rejected_without_wildcard_cors(self):
        status, headers, body = self.json_request(
            "POST",
            "/api/engine/newgame",
            body={},
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
            },
        )

        self.assertEqual(status, 403)
        self.assertEqual(
            body, {"error": "Cross-origin requests are not allowed"}
        )
        self.assertNotIn("access-control-allow-origin", headers)

        status, headers, body = self.json_request("OPTIONS", "/api/engine/eval")
        self.assertEqual(status, 405)
        self.assertEqual(body, {"error": "Method not allowed"})
        self.assertNotIn("access-control-allow-origin", headers)

    def test_same_origin_request_reaches_mocked_handler_stage(self):
        status, headers, body = self.json_request(
            "POST",
            "/api/engine/newgame",
            body={},
            headers={
                "Content-Type": "application/json",
                "Origin": self.origin,
            },
        )

        self.assertEqual(status, 202)
        self.assertEqual(body, {"stage": "accepted"})
        self.assertNotIn("access-control-allow-origin", headers)

    def test_busy_engine_returns_service_unavailable_without_launching_lc0(self):
        acquired = server.ENGINE_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with mock.patch.object(server, "ENGINE_LOCK_WAIT_SECONDS", 0.01):
                status, _headers, body = self.json_request(
                    "POST",
                    "/api/engine/eval",
                    body={},
                    headers={
                        "Content-Type": "application/json",
                        "Origin": self.origin,
                    },
                )
        finally:
            server.ENGINE_LOCK.release()

        self.assertEqual(status, 503)
        self.assertEqual(
            body, {"error": "Engine is busy; try again shortly"}
        )


if __name__ == "__main__":
    unittest.main()
