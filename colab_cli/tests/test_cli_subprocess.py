"""CLI 输出必须进入任务日志，失败原因必须传到任务页面；不访问云端。"""
import sys
import unittest
from unittest.mock import patch

from colab_transport import CliTransport, TransportError
from colab_transport.cli import ColabCli


class ColabCliOutputTests(unittest.TestCase):
    def command(self, code):
        return patch.object(
            ColabCli, "build", return_value=[sys.executable, "-u", "-c", code],
        )

    def test_success_forwards_stdout_and_stderr(self):
        logs = []
        with self.command(
            "import sys; print('ready'); print('diagnostic', file=sys.stderr)"
        ):
            result = ColabCli(log=logs.append).run("new")
        self.assertEqual(result.returncode, 0)
        self.assertIn("ready", logs)
        self.assertIn("diagnostic", logs)

    def test_session_failure_reaches_log_sink_and_exception(self):
        events = []
        transport = CliTransport(lambda *event: events.append(event))
        with self.command(
            "import sys; print('quota rejected', file=sys.stderr); sys.exit(7)"
        ):
            with self.assertRaises(TransportError) as caught:
                transport.create_session("test", "gpu", "H100")
        self.assertIn("exit 7", str(caught.exception))
        self.assertIn("quota rejected", str(caught.exception))
        self.assertTrue(any(
            event[0] == "log.line" and event[2] == "quota rejected"
            for event in events
        ))

    def test_windows_import_failure_has_actionable_hint(self):
        with self.command(
            "import sys; print(\"ModuleNotFoundError: No module named 'termios'\", "
            "file=sys.stderr); sys.exit(1)"
        ):
            with self.assertRaises(TransportError) as caught:
                ColabCli(log=lambda _line: None).run("new")
        self.assertIn("termios", str(caught.exception))
        self.assertIn("WSL/Linux", str(caught.exception))

    def test_check_false_returns_failure_and_still_logs(self):
        logs = []
        with self.command("import sys; print('failed'); sys.exit(2)"):
            result = ColabCli(log=logs.append).run("stop", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("failed", logs)


if __name__ == "__main__":
    unittest.main()
