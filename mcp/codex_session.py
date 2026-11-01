"""Persistent stdio Codex App Server for schema-constrained plans only."""

from __future__ import annotations
import atexit
from collections import deque
import json
import hashlib
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


class CodexSession:
    def __init__(
        self, executable, project, instructions, state_name="codex-planner.json", ephemeral=False
    ):
        self.executable = executable
        self.project = Path(project)
        self.instructions = instructions
        self.state = self.project / ".telly" / state_name
        self.ephemeral = ephemeral
        self.process = None
        self.messages = queue.Queue()
        self.events = deque()
        self.sequence = 0
        self.thread_id = None
        self.turn_id = None
        atexit.register(self.close)

    def close(self):
        process = self.process
        self.process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process:
            for stream in (process.stdin, process.stdout):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def _send(self, message):
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("Codex planner stopped")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex planner timed out")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("Codex planner timed out") from exc
        if message is None:
            raise RuntimeError("Codex planner connection closed")
        if "method" in message and "id" in message:
            # Unattended planning never grants tool/permission approval.
            self._send(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Planner cannot grant approvals or execute client tools",
                    },
                }
            )
        return message

    def _rpc(self, method, params, timeout=30):
        self.sequence += 1
        request_id = self.sequence
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self._receive(deadline)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(str(message["error"].get("message", "Codex RPC failed")))
                return message.get("result", {})
            if "method" in message and "id" not in message:
                self.events.append(message)

    def start(self):
        if self.process and self.process.poll() is None:
            return
        self.close()
        self.messages = queue.Queue()
        self.events.clear()
        env = os.environ.copy()
        for key in (
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "GROQ_API_KEY",
            "UNIHIKER_BOARD_TOKEN",
            "TELLY_VOICE_GATEWAY_TOKEN",
            "TELLY_LOCAL_CONTROL_TOKEN",
            "TELLY_MCP_PROXY_URL",
        ):
            env.pop(key, None)
        command = [
            self.executable,
            "-c",
            "mcp_servers.esp32-school-robot.enabled=false",
            "-c",
            "mcp_servers.node_repl.enabled=false",
            "app-server",
            "--listen",
            "stdio://",
        ]
        process = subprocess.Popen(
            command,
            cwd=self.project,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            bufsize=1,
        )
        self.process = process
        target = self.messages

        def read():
            try:
                for line in process.stdout:
                    try:
                        target.put(json.loads(line))
                    except ValueError:
                        continue
            except (OSError, ValueError):
                pass
            finally:
                target.put(None)

        threading.Thread(target=read, name="telly-codex-protocol", daemon=True).start()
        self._rpc("initialize", {"clientInfo": {"name": "telly_worker", "version": "1.0.0"}})
        self._send({"method": "initialized", "params": {}})
        params = {
            "cwd": str(self.project),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "developerInstructions": self.instructions,
        }
        if os.environ.get("TELLY_CODEX_MODEL"):
            params["model"] = os.environ["TELLY_CODEX_MODEL"]
        if self.ephemeral:
            params["ephemeral"] = True
        instructions_hash = hashlib.sha256(self.instructions.encode()).hexdigest()
        try:
            saved = {} if self.ephemeral else json.loads(self.state.read_text(encoding="utf-8"))
            stored = (
                saved.get("thread_id")
                if saved.get("instructions_hash") == instructions_hash
                else None
            )
        except (OSError, ValueError):
            stored = None
        if stored:
            result = self._rpc("thread/resume", dict(params, threadId=stored, excludeTurns=True))
        else:
            result = self._rpc("thread/start", params)
        self.thread_id = result["thread"]["id"]
        if not self.ephemeral:
            self.state.parent.mkdir(exist_ok=True)
            self.state.write_text(
                json.dumps({"thread_id": self.thread_id, "instructions_hash": instructions_hash}),
                encoding="utf-8",
            )
        self.events.clear()

    def generate(self, prompt, schema, effort="low", images=()):
        self.start()
        self.events.clear()
        inputs = [{"type": "text", "text": prompt}]
        inputs.extend({"type": "localImage", "path": str(Path(path).resolve())} for path in images)
        response = self._rpc(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": inputs,
                "effort": effort,
                "outputSchema": schema,
                "approvalPolicy": "never",
            },
        )
        self.turn_id = response["turn"]["id"]
        deadline = time.monotonic() + int(os.environ.get("TELLY_CODEX_TIMEOUT", "120"))
        answer = ""
        try:
            while True:
                event = self.events.popleft() if self.events else self._receive(deadline)
                params = event.get("params", {})
                if params.get("threadId") not in (None, self.thread_id):
                    continue
                if params.get("turnId") not in (None, self.turn_id):
                    continue
                if event.get("method") == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        answer = item.get("text", "")
                if event.get("method") == "turn/completed":
                    turn = params.get("turn", {})
                    if turn.get("id") != self.turn_id:
                        continue
                    if turn.get("status") != "completed":
                        raise RuntimeError(
                            (turn.get("error") or {}).get("message", "Codex turn did not complete")
                        )
                    if not answer:
                        for item in turn.get("items", []):
                            if item.get("type") == "agentMessage":
                                answer = item.get("text", "")
                    return json.loads(answer)
        except (TimeoutError, RuntimeError, ValueError):
            try:
                self._rpc(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self.turn_id},
                    timeout=3,
                )
            except Exception:
                pass
            self.close()
            raise
        finally:
            self.turn_id = None
