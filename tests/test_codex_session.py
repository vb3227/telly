"""A failed Codex startup must never produce a turn with threadId=null."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from codex_session import CodexSession


class CodexSessionTests(unittest.TestCase):
    def test_failed_thread_start_is_closed_and_next_attempt_reinitializes(self):
        with tempfile.TemporaryDirectory() as folder:
            session = CodexSession("codex", Path(folder), "test", ephemeral=True)
            failed = Mock()
            failed.poll.return_value = None
            failed.stdout = []
            healthy = Mock()
            healthy.poll.return_value = None
            healthy.stdout = []
            session._rpc = Mock(
                side_effect=[
                    {},
                    RuntimeError("startup failed"),
                    {},
                    {"thread": {"id": "ready-thread"}},
                ]
            )
            try:
                with patch("codex_session.subprocess.Popen", side_effect=[failed, healthy]):
                    with self.assertRaisesRegex(RuntimeError, "startup failed"):
                        session.start()
                    self.assertIsNone(session.process)
                    self.assertIsNone(session.thread_id)
                    failed.terminate.assert_called_once()
                    session.start()
                    self.assertEqual(session.thread_id, "ready-thread")
                    self.assertEqual(
                        [call.args[0] for call in session._rpc.call_args_list],
                        ["initialize", "thread/start", "initialize", "thread/start"],
                    )
            finally:
                session.close()

    def test_live_process_without_thread_is_not_reused(self):
        session = CodexSession("codex", Path.cwd(), "test", ephemeral=True)
        broken = Mock()
        broken.poll.return_value = None
        session.process = broken
        with patch("codex_session.subprocess.Popen", side_effect=OSError("test restart")) as launch:
            with self.assertRaisesRegex(OSError, "test restart"):
                session.start()
        broken.terminate.assert_called_once()
        launch.assert_called_once()
        self.assertIsNone(session.process)

    def test_null_thread_result_is_rejected_during_initialization(self):
        session = CodexSession("codex", Path.cwd(), "test", ephemeral=True)
        process = Mock()
        process.poll.return_value = None
        process.stdout = []
        session._rpc = Mock(side_effect=[{}, {"thread": {"id": None}}])
        with patch("codex_session.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "valid thread ID"):
                session.start()
        self.assertIsNone(session.process)
        process.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
