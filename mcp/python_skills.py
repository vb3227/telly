"""Persistent laptop Python skills and bounded child-to-MCP execution."""

import ast
import contextvars
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from RestrictedPython import compile_restricted
from program_validation import validate_program

PYTHON_API = """Python skill: write source using functions, loops, conditions, lists/dicts and
provided math/statistics. No imports, files, OS, shell, sockets, eval or exec.
str.format/format_map are unavailable: use f-strings or string concatenation.
telly.run({'steps': [...]}) submits validated board operations and returns a job ID.
telly.wait(job_id, timeout=10) returns status/result (only jobs this script started).
telly.sensors() returns a dict of current sensors.
telly.show(text) displays text; telly.rgb(r=0,g=0,b=0,brightness=5) sets LEDs.
telly.sleep(seconds) pauses (0..5 per call); telly.load()/save(JSON_value) persist
up to 16 KB per skill locally. Set result to a JSON value to report a result.
print() is captured. Max source 16000 UTF-8 bytes; max runtime 60 seconds;
max 128 hardware calls. No new firmware drivers or arbitrary Wi-Fi frames.
The board has only four shared job slots: wait for short jobs before submitting more.
Use Python for sensor-dependent branches, computation, repeated sampling and logic
that cannot be expressed well by a short existing JSON program. Prefer JSON otherwise.
Never claim a laptop Python skill is saved to or runs offline on the board."""


def validate_source(source):
    if not isinstance(source, str) or not 1 <= len(source.encode()) <= 16000:
        raise ValueError("Python source must contain 1..16000 UTF-8 bytes")
    try:
        tree = ast.parse(source)
        if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)):
            raise ValueError("Imports unavailable; use supplied telly, math and statistics")
        if any(
            isinstance(n, ast.Attribute) and n.attr in ("format", "format_map")
            for n in ast.walk(tree)
        ):
            raise ValueError("str.format/format_map unavailable; use f-strings or concatenation")
        compile_restricted(source, filename="<telly-skill>", mode="exec")
    except SyntaxError as exc:
        raise ValueError("Invalid Python source: " + str(exc)) from exc
    return source


def read_python_skill(folder, name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,31}", name):
        raise ValueError("Invalid Python skill name")
    folder = Path(folder).resolve()
    manifest = folder / (name + ".json")
    source = folder / (name + ".py")
    if manifest.is_symlink() or source.is_symlink():
        raise ValueError("Symlink skills are unavailable")
    item = json.loads(manifest.read_text(encoding="utf-8"))
    code = source.read_text(encoding="utf-8")
    if not isinstance(item, dict) or (
        item.get("name") != name
        or item.get("runtime") != "python"
        or hashlib.sha256(code.encode()).hexdigest() != item.get("sha256")
    ):
        raise ValueError("Python source and manifest do not match")
    phrase = item.get("voice_phrase")
    aliases = item.get("aliases", [])
    if (
        not isinstance(phrase, str)
        or not 1 <= len(phrase.encode()) <= 63
        or not isinstance(aliases, list)
        or len(aliases) > 16
        or any(not isinstance(a, str) or len(a) > 300 for a in aliases)
    ):
        raise ValueError("Invalid Python skill metadata")
    validate_source(code)
    return item, code


class PythonSkills:
    def __init__(self, project, mcp_call, finished=None):
        self.project = Path(project).resolve()
        self.folder = self.project / "skills" / "python"
        self.state = self.project / ".telly" / "python_state"
        self.mcp = mcp_call
        self.finished = finished
        self.lock = threading.RLock()
        self.jobs = {}
        self.processes = {}
        self.slots = threading.BoundedSemaphore(4)

    def save(self, name, phrase, source, aliases=None):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]{1,31}", name):
            raise ValueError("Invalid Python skill name")
        if not isinstance(phrase, str) or not 1 <= len(phrase.encode()) <= 63:
            raise ValueError("Voice phrase must contain 1..63 UTF-8 bytes")
        if (self.project / "skills" / (name + ".json")).exists() or name in (
            "camera_on",
            "camera_off",
            "show_sensors",
        ):
            raise ValueError("Name already belongs to a board skill")
        validate_source(source)
        item = {
            "name": name,
            "runtime": "python",
            "voice_phrase": phrase,
            "aliases": [a[:300] for a in (aliases or []) if isinstance(a, str)][:16],
            "sha256": hashlib.sha256(source.encode()).hexdigest(),
        }
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            for extension, content in (
                ("py", source),
                ("json", json.dumps(item, ensure_ascii=False, indent=2)),
            ):
                path = self.folder / (name + "." + extension)
                if path.is_symlink():
                    raise ValueError("Symlink skills are unavailable")
                temporary = self.folder / (uuid.uuid4().hex + ".tmp")
                temporary.write_text(content, encoding="utf-8")
                temporary.replace(path)
        return item

    def list(self):
        items = []
        for path in sorted(self.folder.glob("*.json")):
            try:
                item, _ = read_python_skill(self.folder, path.stem)
                items.append(item)
            except (OSError, ValueError, SyntaxError):
                continue
        return items

    def run(self, name, timeout=60):
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0.2 <= timeout <= 60
        ):
            raise ValueError("Python timeout must be 0.2..60 seconds")
        _, source = read_python_skill(self.folder, name)
        if not self.slots.acquire(blocking=False):
            raise RuntimeError("All four Python workers are busy")
        job_id = uuid.uuid4().hex
        with self.lock:
            # Bound retained results; never evict active work.
            completed = [k for k, v in self.jobs.items() if v["status"] != "running"]
            for old in completed[:-63]:
                self.jobs.pop(old, None)
            self.jobs[job_id] = {
                "job_id": job_id,
                "name": name,
                "status": "running",
                "started": time.time(),
            }
        context = contextvars.copy_context()
        threading.Thread(
            target=lambda: context.run(self._run, job_id, name, source, timeout),
            daemon=True,
            name="telly-python",
        ).start()
        return {
            "job_id": job_id,
            "name": name,
            "status": "running",
            "runtime": "python",
            "location": "laptop",
        }

    def status(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError("Unknown Python job")
            return dict(self.jobs[job_id])

    def stop(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise ValueError("Unknown Python job")
            if job["status"] == "running":
                job["cancelled"] = True
                process = self.processes.get(job_id)
                if process and process.poll() is None:
                    process.kill()
        return self.status(job_id)

    def _run(self, job_id, name, source, timeout):
        process = None
        timer = None
        owned = set()
        deadline = time.monotonic() + timeout
        calls = 0

        def check():
            if self.status(job_id).get("cancelled"):
                raise RuntimeError("Python skill cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("Python skill timed out")

        def call(tool, args):
            nonlocal calls
            check()
            calls += 1
            if calls > 128:
                raise ValueError("Python hardware-call budget exceeded")
            value = self.mcp(tool, args)
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, dict) or value.get("error"):
                raise RuntimeError(
                    str(value.get("error", "Invalid MCP result"))
                    if isinstance(value, dict)
                    else "Invalid MCP result"
                )
            # Preserve ownership even if the deadline expires while awaiting
            # the board acknowledgment; error cleanup must stop this job too.
            if tool == "execute_board_code" and isinstance(value.get("job_id"), str):
                owned.add(value["job_id"])
            check()
            return value

        def run(program):
            validate_program(program)
            value = call("execute_board_code", {"code": json.dumps(program)})
            if not isinstance(value.get("job_id"), str):
                raise RuntimeError("Board returned no job ID")
            owned.add(value["job_id"])
            return value["job_id"]

        def wait(board_job, seconds):
            if (
                board_job not in owned
                or type(seconds) not in (int, float)
                or not math.isfinite(seconds)
                or not 0 < seconds <= 20
            ):
                raise ValueError("Invalid or unowned board job/timeout")
            until = min(deadline, time.monotonic() + seconds)
            while time.monotonic() < until:
                value = call("board_job_status", {"job_id": board_job})
                if value.get("status") in ("done", "stopped", "error"):
                    if value.get("status") == "error":
                        raise RuntimeError(value.get("error") or "Board job failed")
                    if isinstance(value.get("result"), str):
                        value["result"] = json.loads(value["result"])
                    return value
                time.sleep(0.1)
            raise TimeoutError("Board job wait timed out")

        def rpc(method, args):
            if method == "run":
                return run(args["program"])
            if method == "wait":
                return wait(args["job_id"], args.get("timeout", 10))
            if method == "sensors":
                return wait(run({"steps": [{"op": "sensor.read"}]}), 10)["result"]["sensors"]
            if method == "show":
                text = args["text"]
                if not isinstance(text, str) or not 1 <= len(text.encode()) <= 512:
                    raise ValueError("Display text must contain 1..512 UTF-8 bytes")
                return call("display_voice_result", {"text": text})
            if method == "sleep":
                seconds = args["seconds"]
                if (
                    type(seconds) not in (int, float)
                    or not math.isfinite(seconds)
                    or not 0 <= seconds <= 5
                ):
                    raise ValueError("Sleep must be 0..5 seconds")
                time.sleep(seconds)
                check()
                return None
            if method in ("load", "save"):
                path = self.state / (name + ".json")
                if path.is_symlink():
                    raise ValueError("Symlink state is unavailable")
                if method == "load":
                    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                raw = json.dumps(args["value"], ensure_ascii=False, allow_nan=False)
                if len(raw.encode()) > 16384:
                    raise ValueError("Skill state exceeds 16 KB")
                with self.lock:
                    self.state.mkdir(parents=True, exist_ok=True)
                    temporary = self.state / (uuid.uuid4().hex + ".tmp")
                    temporary.write_text(raw, encoding="utf-8")
                    temporary.replace(path)
                return None
            raise ValueError("Unknown Python capability")

        try:
            env = {
                k: v
                for k, v in os.environ.items()
                if k.upper() in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            }
            process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).with_name("python_skill_child.py"))],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self.lock:
                self.processes[job_id] = process

            def expire():
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass

            timer = threading.Timer(timeout, expire)
            timer.daemon = True
            timer.start()
            lines = queue.Queue(maxsize=4)

            def read():
                try:
                    while True:
                        line = process.stdout.readline(65537)
                        lines.put(line)
                        if not line:
                            break
                except (OSError, ValueError):
                    lines.put("")

            threading.Thread(target=read, daemon=True).start()
            process.stdin.write(json.dumps({"source": source}) + "\n")
            process.stdin.flush()
            while True:
                check()
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not line:
                    check()
                    raise RuntimeError(
                        "Python process exited before returning a result (possible memory limit)"
                    )
                if len(line) > 65536:
                    raise ValueError("Python protocol message too large")
                packet = json.loads(line)
                if packet.get("done"):
                    if packet.get("error"):
                        raise RuntimeError(packet["error"])
                    with self.lock:
                        self.jobs[job_id].update(
                            status="done",
                            result=packet.get("value"),
                            stdout=packet.get("stdout", ""),
                        )
                    break
                try:
                    response = {"value": rpc(packet.get("call"), packet.get("arguments", {}))}
                except Exception as exc:
                    response = {"error": str(exc)[:400]}
                if process.poll() is None:
                    process.stdin.write(json.dumps(response, allow_nan=False) + "\n")
                    process.stdin.flush()
        except Exception as exc:
            with self.lock:
                self.jobs[job_id].update(
                    status="stopped" if self.jobs[job_id].get("cancelled") else "error",
                    error=str(exc)[:500],
                )
            for board_job in owned:
                try:
                    self.mcp("stop_board_job", {"job_id": board_job})
                except Exception:
                    pass
        finally:
            if timer:
                timer.cancel()
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdin.close()
                process.stdout.close()
            with self.lock:
                self.processes.pop(job_id, None)
                self.jobs[job_id]["elapsed_ms"] = round(
                    (time.monotonic() - (deadline - timeout)) * 1000
                )
            self.slots.release()
            print(
                "[telly-python] " + json.dumps(self.status(job_id), ensure_ascii=True),
                file=sys.stderr,
                flush=True,
            )
            if self.finished:
                try:
                    self.finished(self.status(job_id))
                except Exception:
                    pass  # Lost feedback never retries an executed program.
