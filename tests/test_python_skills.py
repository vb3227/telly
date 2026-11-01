import contextvars
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from codex_worker import CodexRunner
from python_skills import PythonSkills, read_python_skill, validate_source
from skill_router import SkillCatalog, compatible
from voice_gateway import _make_handler


class PythonSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.calls = []
        self.context = contextvars.ContextVar("python_test", default="missing")

        def mcp(name, args):
            self.calls.append((name, args, self.context.get()))
            if name == "execute_board_code":
                return {"job_id": "board-" + str(len(self.calls))}
            if name == "board_job_status":
                return {"status": "done", "result": json.dumps({"sensors": {"temperature_c": 24}})}
            return {"accepted": True}

        self.runtime = PythonSkills(self.project, mcp)

    def tearDown(self):
        for job in list(self.runtime.jobs):
            self.runtime.stop(job)
        deadline = time.monotonic() + 5
        while self.runtime.processes and time.monotonic() < deadline:
            time.sleep(0.02)
        self.temp.cleanup()

    def completed(self, job):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = self.runtime.status(job["job_id"])
            if value["status"] != "running" and "elapsed_ms" in value:
                return value
            time.sleep(0.02)
        self.fail("Python worker did not finish")

    def test_real_python_functions_branches_mcp_state_and_epoch(self):
        source = """
def average():
    readings = []
    for n in range(3):
        readings.append(telly.sensors()["temperature_c"])
    return statistics.mean(readings)
value = average()
if value > 20:
    telly.rgb(r=20)
telly.show("Temperature: " + str(value))
telly.save({"last": value})
result = telly.load()
print("finished", value)
"""
        self.runtime.save("average", "average sensors", source)
        token = self.context.set("current-voice-epoch")
        try:
            job = self.runtime.run("average")
        finally:
            self.context.reset(token)
        result = self.completed(job)
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(result["result"], {"last": 24})
        self.assertIn("finished 24", result["stdout"])
        self.assertTrue(all(c[2] == "current-voice-epoch" for c in self.calls))
        self.assertEqual(len([c for c in self.calls if c[0] == "execute_board_code"]), 4)

    def test_source_is_persistent_and_catalogued(self):
        self.runtime.save(
            "calc", "calculate sample", "result = sum(range(10))", ["сосчитай пример"]
        )
        fresh = PythonSkills(self.project, lambda *_: {})
        self.assertEqual(fresh.list()[0]["name"], "calc")
        catalog = SkillCatalog(self.project / "skills")
        self.assertEqual(catalog.find("сосчитай пример"), "calc")
        item = catalog.snapshot()[0]["calc"]
        self.assertTrue(compatible("calculate the sample", item))
        (fresh.folder / "calc.py").write_text("result = 99", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_python_skill(fresh.folder, "calc")

    def test_bad_source_and_unsafe_access_rejected(self):
        for source in [
            'telly.show("{}".format(1))',
            "import os",
            "from socket import socket",
            "result = telly.__class__",
            "def broken(",
        ]:
            with self.subTest(source=source), self.assertRaises((ValueError, SyntaxError)):
                validate_source(source)
        for name in ["../escape", "UPPER", "camera_on"]:
            with self.assertRaises(ValueError):
                self.runtime.save(name, "test", "result = 1")

    def test_timeout_and_cancel_leave_runtime_usable(self):
        self.runtime.save("loop", "loop test", "while True:\n    pass")
        result = self.completed(self.runtime.run("loop", timeout=0.5))
        self.assertEqual(result["status"], "error")
        job = self.runtime.run("loop")
        time.sleep(0.15)
        self.runtime.stop(job["job_id"])
        self.assertEqual(self.completed(job)["status"], "stopped")
        self.runtime.save("ok", "ok test", "result = 42")
        self.assertEqual(self.completed(self.runtime.run("ok"))["result"], 42)

    def test_memory_limit_is_enforced(self):
        self.runtime.save("memory", "memory test", 'result = "x" * (300 * 1024 * 1024)')
        result = self.completed(self.runtime.run("memory"))
        self.assertEqual(result["status"], "error")
        self.assertTrue(
            "MemoryError" in result["error"] or "memory limit" in result["error"], result
        )

    def test_unowned_job_and_unsupported_operations_never_reach_mcp(self):
        for name, source in [
            ("foreign", 'result = telly.wait("someone-elses-job")'),
            ("unsupported", 'telly.run({"steps":[{"op":"exec","code":"bad"}]})'),
        ]:
            self.runtime.save(name, name + " test", source)
            self.assertEqual(self.completed(self.runtime.run(name))["status"], "error")
        self.assertEqual(self.calls, [])

    def test_four_independent_python_slots(self):
        self.runtime.save("slow", "slow test", "telly.sleep(2)\nresult = 1")
        jobs = [self.runtime.run("slow") for _ in range(4)]
        with self.assertRaises(RuntimeError):
            self.runtime.run("slow")
        for job in jobs:
            self.assertEqual(self.completed(job)["status"], "done")

    def test_python_plan_validation(self):
        plan = {
            "kind": "python_skill",
            "name": "compute",
            "voice_phrase": "compute values",
            "program_json": json.dumps({"source": "result = 1 + 2"}),
            "reply": "",
        }
        self.assertEqual(CodexRunner.validate_plan(plan), plan)
        plan["program_json"] = json.dumps({"source": "import os"})
        with self.assertRaises(ValueError):
            CodexRunner.validate_plan(plan)

    def test_invalid_python_plan_is_repaired_before_execution(self):
        from unittest.mock import Mock, patch

        invalid = {
            "kind": "python_skill",
            "name": "calc",
            "voice_phrase": "calculate",
            "program_json": json.dumps({"source": "def broken("}),
            "reply": "",
        }
        valid = dict(invalid, program_json=json.dumps({"source": "result = 42"}))
        runner = CodexRunner(self.project, "", "")
        runner.session = Mock()
        runner.session.generate.side_effect = [invalid, valid]
        with patch.dict("os.environ", {"TELLY_CODEX_TRANSPORT": "app-server"}):
            self.assertEqual(runner.answer("calculate", 1), valid)
        self.assertEqual(runner.session.generate.call_count, 2)

    def test_current_python_contract_is_sent_on_every_planner_turn(self):
        from unittest.mock import Mock, patch

        plan = {
            "kind": "python_skill",
            "name": "calc",
            "voice_phrase": "calculate",
            "program_json": json.dumps({"source": "result = 42"}),
            "reply": "",
        }
        runner = CodexRunner(self.project, "", "")
        runner.session = Mock()
        runner.session.generate.return_value = plan
        with patch.dict("os.environ", {"TELLY_CODEX_TRANSPORT": "app-server"}):
            runner.answer("calculate", 1)
        self.assertIn("telly.sensors()", runner.session.generate.call_args.args[0])

    def test_planner_resumes_only_matching_instruction_version(self):
        import hashlib
        from unittest.mock import Mock, patch
        from codex_session import CodexSession

        for matches in (False, True):
            session = CodexSession("codex", self.project, "new Python contract")
            session.state.parent.mkdir(exist_ok=True)
            session.state.write_text(
                json.dumps(
                    {
                        "thread_id": "old-thread",
                        "instructions_hash": hashlib.sha256(
                            session.instructions.encode()
                        ).hexdigest()
                        if matches
                        else "old-version",
                    }
                ),
                encoding="utf-8",
            )
            process = Mock()
            process.poll.return_value = None
            process.stdout = []
            session._rpc = Mock(
                side_effect=lambda method, *args, **kwargs: {"thread": {"id": "chosen-thread"}}
                if method.startswith("thread/")
                else {}
            )
            try:
                with patch("codex_session.subprocess.Popen", return_value=process):
                    session.start()
                self.assertEqual(
                    session._rpc.call_args_list[-1].args[0],
                    "thread/resume" if matches else "thread/start",
                )
            finally:
                session.process = None

    def test_malformed_metadata_is_ignored_by_catalog(self):
        self.runtime.save("meta", "metadata test", "result = 1")
        path = self.runtime.folder / "meta.json"
        manifest = json.loads(path.read_text())
        manifest["voice_phrase"] = 123
        path.write_text(json.dumps(manifest), encoding="utf-8")
        catalog = SkillCatalog(self.project / "skills")
        self.assertNotIn("meta", catalog.snapshot()[0])

    def test_late_board_ack_is_still_owned_for_cleanup(self):
        def slow_mcp(name, args):
            self.calls.append((name, args))
            if name == "execute_board_code":
                # Cancel while the board acknowledgment is in flight, rather
                # than assuming CPython starts in less than one wall second.
                active = next(
                    job_id
                    for job_id, job in self.runtime.jobs.items()
                    if job["status"] == "running"
                )
                self.runtime.stop(active)
                return {"job_id": "late-board-job"}
            return {"accepted": True}

        self.runtime.mcp = slow_mcp
        self.runtime.save("late", "late test", "telly.rgb(r=1)")
        result = self.completed(self.runtime.run("late"))
        self.assertEqual(result["status"], "stopped")
        self.assertIn(("stop_board_job", {"job_id": "late-board-job"}), self.calls)

    def test_python_control_rejects_tunnel_and_missing_auth(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _make_handler(None, "gateway", queue.Queue(), "control", python_runtime=self.runtime),
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for headers in (
                {},
                {"X-Local-Control-Token": "control", "CF-Connecting-IP": "1.2.3.4"},
            ):
                request = urllib.request.Request(
                    "http://127.0.0.1:%d/python/control" % server.server_port,
                    data=b'{"action":"list"}',
                    headers=headers,
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(ctx.exception.code, 403)
            request = urllib.request.Request(
                "http://127.0.0.1:%d/python/control" % server.server_port,
                data=b'{"action":"list"}',
                headers={"X-Local-Control-Token": "control"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(json.load(response), {"skills": []})
        finally:
            server.shutdown()
            server.server_close()
