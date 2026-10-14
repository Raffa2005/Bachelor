import unittest
from unittest.mock import patch

from tools.dm_cli import Lc0


class InferenceSettingsTest(unittest.TestCase):
    def test_cpu_backend_threads_are_separate_from_search_threads(self):
        with patch.dict("os.environ", {
            "DM_ENGINE_THREADS": "2", "DM_BACKEND_OPTIONS": "threads=4"
        }), patch("tools.dm_cli.subprocess.Popen") as process, \
                patch("tools.dm_cli.threading.Thread"), \
                patch.object(Lc0, "_send"), \
                patch.object(Lc0, "_wait_for", return_value=(1, "readyok")):
            Lc0("/engine", "/weights", backend="blas")
        arguments = process.call_args.args[0]
        self.assertIn("--threads=2", arguments)
        self.assertIn("--backend-opts=threads=4", arguments)

    def test_invalid_thread_count_does_not_launch_engine(self):
        with patch.dict("os.environ", {"DM_ENGINE_THREADS": "0"}), \
                patch("tools.dm_cli.subprocess.Popen") as process:
            with self.assertRaises(ValueError):
                Lc0("/engine", "/weights")
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
