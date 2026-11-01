import json
import queue
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch, Mock
from pathlib import Path
from program_validation import validate_program
from voice_policy import classify_transcript, known_skill
from skill_router import SkillCatalog
from voice_gateway import _make_handler, ExclusiveHTTPServer, BoardTools
from codex_worker import CodexRunner
from board_relay import BoardRelay, cloud_voice_epoch


class VoiceControlTests(unittest.TestCase):
    def test_local_wake_and_background_never_enter_agent_queue(self):
        for text in ("Hi Telly, turn on camera", "Hey Telly! show sensors", "high telli blink"):
            self.assertEqual(classify_transcript(text), "local")
        self.assertEqual(classify_transcript("today is a nice day"), "ignore")
        self.assertEqual(classify_transcript("show all sensors"), "command")
        self.assertEqual(known_skill("start camera!"), "camera_on")

    def test_learned_phrase_uses_saved_skill_without_model(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "blue.json").write_text(
                json.dumps(
                    {
                        "name": "blue",
                        "voice_phrase": "blink blue twice",
                        "aliases": ["make the lights blue twice"],
                        "program": {"steps": [{"op": "rgb.blink", "b": 255, "count": 2}]},
                    }
                ),
                encoding="utf-8",
            )
            catalog = SkillCatalog(folder)
            self.assertEqual(catalog.find("Blink blue twice!"), "blue")
            self.assertEqual(catalog.find("Make the lights blue twice!"), "blue")
            self.assertIsNone(catalog.find("something else"))

    def test_mcp_invalid_json_returns_message_without_board_request(self):
        from server import execute_board_code

        result = execute_board_code("not valid JSON")
        self.assertIn("JSON", result)

    def test_program_rejects_unbounded_or_unknown_input(self):
        for step in (
            {"op": "exec", "code": "x"},
            {"op": "rgb.set", "r": 256},
            {"op": "rgb.set", "r": True},
            {"op": "delay", "duration_ms": 1.5},
            {"op": "display.text", "text": "x" * 513},
            {"op": "camera.preview", "extra": 1},
            {"op": "voice.transcribe"},
        ):
            with self.subTest(step=step), self.assertRaises(ValueError):
                validate_program({"steps": [step]})
        validate_program(
            {
                "steps": [
                    {"op": "rgb.blink", "count": 3, "r": 10},
                    {"op": "display.text", "text": "ready", "color": "#00ff00"},
                ]
            }
        )

    def test_relay_propagates_epoch(self):
        relay = BoardRelay()
        errors = []

        def complete():
            command = relay.poll(timeout=2)
            if command.get("voice_epoch") != 123:
                errors.append(command)
            relay.complete({"id": command["id"], "body": "ok"})

        context = cloud_voice_epoch.set(123)
        thread = threading.Thread(target=complete)
        thread.start()
        try:
            self.assertEqual(relay.request("GET", "/ping", timeout=3), "ok")
        finally:
            cloud_voice_epoch.reset(context)
        thread.join()
        self.assertFalse(errors)

    def test_http_proxy_auth_validation_and_epoch(self):
        class Agent:
            api_key = "test"

        work = queue.Queue()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _make_handler(Agent(), "gateway", work, "admin")
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def post(path, payload, headers):
            request = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (server.server_port, path),
                data=json.dumps(payload).encode(),
                headers=headers,
            )
            return urllib.request.urlopen(request, timeout=3)

        item = {"method": "GET", "path": "/ping", "voice_epoch": 123}
        try:
            for headers in ({}, {"X-Local-Control-Token": "admin", "CF-Connecting-IP": "1.2.3.4"}):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    post("/board/request", item, headers)
                self.assertEqual(ctx.exception.code, 403)
            with patch("voice_gateway.relay.request", return_value="pong") as request:
                with post("/board/request", item, {"X-Local-Control-Token": "admin"}) as result:
                    self.assertEqual(json.load(result), {"body": "pong"})
                self.assertEqual(request.call_args.kwargs["voice_epoch"], 123)
            with post(
                "/voice",
                {"text": "Hi Telly, show sensors", "voice_epoch": 123},
                {"X-Voice-Gateway-Token": "gateway"},
            ) as result:
                self.assertFalse(json.load(result)["accepted"])
            self.assertTrue(work.empty())
            with post(
                "/voice",
                {"text": "show sensors", "voice_epoch": 123},
                {"X-Voice-Gateway-Token": "gateway"},
            ) as result:
                self.assertTrue(json.load(result)["accepted"])
            self.assertEqual(work.get_nowait()["epoch"], 123)
        finally:
            server.shutdown()
            server.server_close()

    def test_new_skill_save_run_is_one_mcp_request(self):
        agent = BoardTools(None, None)
        with patch.object(
            agent, "_mcp_json", return_value={"stored": True, "job_id": "test", "status": "running"}
        ) as mcp:
            result = agent._save_and_run(
                "blue", "blink blue", '{"steps":[{"op":"rgb.blink","b":255}]}'
            )
        mcp.assert_called_once()
        self.assertEqual(mcp.call_args.args[0], "save_unihiker_skill_payload")
        self.assertTrue(mcp.call_args.args[1]["run"])
        self.assertEqual(result["execution"]["job_id"], "test")

    def test_gateway_port_is_exclusive(self):
        server = ExclusiveHTTPServer(
            ("127.0.0.1", 0),
            _make_handler(type("Agent", (), {"api_key": ""})(), "x", queue.Queue()),
        )
        try:
            with self.assertRaises(OSError):
                duplicate = ExclusiveHTTPServer(
                    ("127.0.0.1", server.server_port), server.RequestHandlerClass
                )
                duplicate.server_close()
        finally:
            server.server_close()

    def test_invalid_generated_plan_is_repaired_before_execution(self):
        invalid = {
            "kind": "skill",
            "name": "screen",
            "voice_phrase": "show green",
            "program_json": '{"steps":[{"op":"display.clear","color":"green"}]}',
            "reply": "",
        }
        valid = dict(invalid, program_json='{"steps":[{"op":"display.clear","color":"#00FF00"}]}')
        runner = CodexRunner(Path.cwd(), "", "")
        runner.session = Mock()
        runner.session.generate.side_effect = [invalid, valid]
        with patch.dict("os.environ", {"TELLY_CODEX_TRANSPORT": "app-server"}):
            self.assertEqual(runner.answer("show green", 1), valid)
        self.assertEqual(runner.session.generate.call_count, 2)
        self.assertEqual(runner.session.generate.call_args.args[-1], "medium")

    def test_valid_generated_plan_is_not_regenerated(self):
        plan = {
            "kind": "skill",
            "name": "blue",
            "voice_phrase": "blink blue",
            "program_json": '{"steps":[{"op":"rgb.blink","b":255}]}',
            "reply": "",
        }
        runner = CodexRunner(Path.cwd(), "", "")
        runner.session = Mock()
        runner.session.generate.return_value = plan
        with patch.dict("os.environ", {"TELLY_CODEX_TRANSPORT": "app-server"}):
            self.assertEqual(runner.answer("blink blue", 1), plan)
        runner.session.generate.assert_called_once()

    def test_worker_uses_structured_plan_and_persistent_session(self):
        runner = CodexRunner(Path.cwd(), "http://127.0.0.1:8767", "unused")
        runner.executable = "codex"
        command = runner.command("session-test")
        self.assertEqual(command[-3:], ["resume", "session-test", "-"])
        self.assertIn("workspace-write", command)
        self.assertIn("mcp_servers.esp32-school-robot.enabled=false", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--output-schema", command)


if __name__ == "__main__":
    unittest.main()
