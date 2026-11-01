"""Single voice dispatcher: fast tool-free routing and Codex for complex tasks."""

from __future__ import annotations
import asyncio
import hmac
import json
import os
import queue
import re
import threading
import time
import uuid
import secrets
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from pathlib import Path
from board_relay import relay, cloud_voice_epoch, cloud_activity_id
from voice_activity import configure_activity, publish_activity
from program_validation import validate_program
from voice_policy import classify_transcript, remember_alias, conversation_candidate
from codex_worker import CodexRunner
from skill_router import SkillCatalog, FastRouter

LAST_VOICE_METRICS = {}
MAX_PROGRAM_BYTES = 3800


def voice_console(event, **fields):
    line = "[telly-voice] " + json.dumps({"event": event, **fields}, ensure_ascii=False)
    if hasattr(sys.stderr, "buffer"):
        sys.stderr.buffer.write((line + "\n").encode("utf-8"))
        sys.stderr.buffer.flush()
    else:
        print(line, file=sys.stderr, flush=True)


def load_groq_api_key(board_config: Path | None = None) -> str:
    """Environment wins; the private board header lets a local setup paste once."""
    configured = os.environ.get("GROQ_API_KEY", "").strip()
    if configured:
        return configured
    path = board_config or Path(__file__).resolve().parent.parent / "include" / "board_config.h"
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(
        r'^\s*#define\s+GROQ_API_KEY\s+"([^"\r\n]*)"\s*(?://[^\r\n]*)?$', source, re.MULTILINE
    )
    return match.group(1).strip() if match else ""


class BoardTools:
    def __init__(self, mcp_server, loop, api_key=""):
        self.mcp_server = mcp_server
        self.loop = loop
        self.api_key = api_key

    def _mcp(self, name: str, arguments: dict[str, Any]) -> str:
        future = asyncio.run_coroutine_threadsafe(
            self.mcp_server.call_tool(name, arguments), self.loop
        )
        result = future.result(timeout=55)
        if getattr(result, "is_error", getattr(result, "isError", False)):
            raise RuntimeError("MCP tool {} failed".format(name))
        content = getattr(result, "content", []) or []
        texts = [getattr(item, "text", "") for item in content if getattr(item, "text", None)]
        return (
            "\n".join(texts)
            if texts
            else json.dumps(getattr(result, "structuredContent", {}), ensure_ascii=False)
        )

    def _mcp_json(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = self._mcp(name, arguments)
        try:
            value = json.loads(raw)
        except Exception as exc:
            raise RuntimeError(raw[:600] or "MCP returned no data") from exc
        if isinstance(value, dict) and (value.get("error") or value.get("isError")):
            raise RuntimeError(value.get("error") or raw)
        return value

    def _validate_program(self, program_json: str) -> dict[str, Any]:
        if (
            not isinstance(program_json, str)
            or len(program_json.encode("utf-8")) > MAX_PROGRAM_BYTES
        ):
            raise ValueError("Skill program exceeds the 3800-byte limit")
        program = json.loads(program_json)
        return validate_program(program)

    def _save_and_run(
        self, name: str, voice_phrase: str, program_json: str, short_commands=None
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9_-]{1,31}", name):
            raise ValueError("Invalid skill name")
        self._validate_program(program_json)
        saved = self._mcp_json(
            "save_unihiker_skill_payload",
            {
                "name": name,
                "voice_phrase": voice_phrase[:63],
                "program_json": program_json,
                "run": True,
                **({"short_commands": short_commands} if short_commands else {}),
            },
        )
        if not saved.get("job_id"):
            raise RuntimeError("Firmware does not support atomic skill save/run")
        return {
            "saved": saved,
            "execution": {"job_id": saved["job_id"], "status": saved.get("status", "running")},
        }


def _make_handler(
    agent: BoardTools,
    token: str,
    work_queue: queue.Queue,
    control_token="",
    backend="codex",
    python_runtime=None,
    speech=None,
):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *_args):
            return

        def _reply(self, code: int, payload: dict[str, Any]):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _deny_post(self, message, code=403, max_drain=65536):
            # Drain only a bounded body before closing. Otherwise Windows can
            # reset the socket with unread input before the 403 reaches clients.
            self.close_connection = True
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if 0 < size <= max_drain:
                    self.connection.settimeout(1)
                    self.rfile.read(size)
            except (OSError, ValueError):
                pass
            self._reply(code, {"error": message})

        def do_GET(self):
            if self.path != "/health":
                self._reply(404, {"error": "not found"})
                return
            self._reply(
                200,
                {
                    "ready": True,
                    "device_connected": relay.online,
                    "transport": "websocket",
                    "provider": backend,
                    "transcription_provider": speech.provider if speech else "disabled",
                    "local_stt": {"model": speech.model, "last": speech.last} if speech else None,
                    "api_key_configured": bool(agent.api_key),
                    "queued": work_queue.qsize(),
                    "last_request": LAST_VOICE_METRICS,
                },
            )

        def do_POST(self):
            if self.path == "/python/control":
                if (
                    not python_runtime
                    or not control_token
                    or self.client_address[0] not in ("127.0.0.1", "::1")
                    or self.headers.get("CF-Connecting-IP")
                    or not hmac.compare_digest(
                        self.headers.get("X-Local-Control-Token", ""), control_token
                    )
                ):
                    self._deny_post("local Python control denied")
                    return
                context = None
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 65536:
                        raise ValueError("Invalid Python request size")
                    self.connection.settimeout(10)
                    data = json.loads(self.rfile.read(size))
                    epoch = data.get("voice_epoch")
                    if epoch is not None and (
                        type(epoch) is not int or not 0 <= epoch <= 0xFFFFFFFF
                    ):
                        raise ValueError("Invalid epoch")
                    context = cloud_voice_epoch.set(epoch)
                    action = data.get("action")
                    if action == "list":
                        result = {"skills": python_runtime.list()}
                    elif action == "save":
                        result = python_runtime.save(
                            data.get("name"), data.get("voice_phrase"), data.get("source")
                        )
                        if data.get("run"):
                            result = {
                                "saved": result,
                                "execution": python_runtime.run(result["name"]),
                            }
                    elif action == "run":
                        result = python_runtime.run(data.get("name"))
                    elif action == "status":
                        result = python_runtime.status(data.get("job_id"))
                    elif action == "stop":
                        result = python_runtime.stop(data.get("job_id"))
                    else:
                        raise ValueError("Unknown Python action")
                    self._reply(200, result)
                except Exception as exc:
                    self._reply(400, {"error": str(exc)[:500]})
                finally:
                    if context is not None:
                        cloud_voice_epoch.reset(context)
                return
            if self.path == "/board/request":
                if (
                    not control_token
                    or self.client_address[0] not in ("127.0.0.1", "::1")
                    or self.headers.get("CF-Connecting-IP")
                    or not hmac.compare_digest(
                        self.headers.get("X-Local-Control-Token", ""), control_token
                    )
                ):
                    self._deny_post("local control denied")
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= size <= 12000:
                        raise ValueError("invalid size")
                    item = json.loads(self.rfile.read(size))
                    epoch = item.get("voice_epoch")
                    if epoch is not None and (
                        type(epoch) is not int or not 0 <= epoch <= 0xFFFFFFFF
                    ):
                        raise ValueError("invalid voice epoch")
                    path = item["path"]
                    method = item["method"]
                    body = item.get("body", "")
                    route = path.split("?", 1)[0]
                    if not isinstance(body, str) or (route == "/reset" and epoch is not None):
                        raise ValueError("voice cannot reset the board")
                    if route == "/run":
                        validate_program(
                            json.loads(body).get("program", json.loads(body)),
                            max_bytes=4096,
                            allow_voice=epoch is None,
                        )
                    if route == "/skills/install":
                        validate_program(json.loads(body)["program"])
                    result = relay.request(method, path, body, timeout=45, voice_epoch=epoch)
                    self._reply(200, {"body": result})
                except Exception as exc:
                    self._reply(400, {"error": str(exc)[:500]})
                return
            if self.path != "/voice":
                self._reply(404, {"error": "not found"})
                return
            supplied = self.headers.get("X-Voice-Gateway-Token", "")
            if not hmac.compare_digest(supplied, token):
                self._deny_post("invalid gateway token")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 1 <= size <= 12000:
                    self._reply(413, {"error": "invalid request size"})
                    return
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(payload, dict):
                    self._reply(400, {"error": "expected JSON object"})
                    return
                transcript = payload.get("text", "")
                if (
                    not isinstance(transcript, str)
                    or not transcript.strip()
                    or len(transcript) > 3000
                ):
                    self._reply(400, {"error": "text must contain 1–3000 characters"})
                    return
                # Firmware permits /voice recording only from a manual button job.
                # Authenticate above, validate epoch below, preserve local wake;
                # do not impose an imperative-verb whitelist on explicit input.
                route = classify_transcript(transcript, manual=True)
                voice_console("transcript", text=transcript, route=route)
                if route != "command":
                    self._reply(202, {"accepted": False, "ignored": True, "route": route})
                    return
                epoch = payload.get("voice_epoch")
                if type(epoch) is not int or not 0 <= epoch <= 0xFFFFFFFF:
                    self._reply(409, {"error": "voice arbitration firmware update required"})
                    return
                activity_id = payload.get("activity_id")
                if activity_id is not None and (
                    not isinstance(activity_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", activity_id)
                ):
                    self._reply(400, {"error": "invalid activity ID"})
                    return
                request_id = uuid.uuid4().hex[:12]
                work_queue.put_nowait(
                    {
                        "id": request_id,
                        "text": transcript,
                        "epoch": epoch,
                        "activity_id": activity_id,
                        "created_at": time.monotonic(),
                    }
                )
                self._reply(202, {"accepted": True, "request_id": request_id})
            except queue.Full:
                self._reply(503, {"error": "voice queue is full"})
            except Exception:
                self._reply(400, {"error": "invalid JSON request"})

    return Handler


class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def start_voice_gateway(mcp_server, loop):
    token = os.environ.get("TELLY_VOICE_GATEWAY_TOKEN", "")
    if not token:
        import sys

        print("[telly-voice] gateway disabled: set TELLY_VOICE_GATEWAY_TOKEN", file=sys.stderr)
        return
    api_key = load_groq_api_key()
    port = int(os.environ.get("TELLY_VOICE_GATEWAY_PORT", "8767"))
    from gateway_control import existing_gateway

    shared = existing_gateway(port)
    if shared:
        return shared
    from speech_service import SpeechService

    provider = (
        "local"
        if os.environ.get("TELLY_LOCAL_STT") == "1"
        else os.environ.get("TELLY_STT_PROVIDER", "groq")
    )
    speech = SpeechService(provider, api_key)
    voice_console("speech_ready", provider=speech.provider, model=speech.model)
    backend = "codex"
    from gateway_control import stored_control_token

    control_token = stored_control_token(port) or secrets.token_urlsafe(32)
    runner = CodexRunner(
        Path(__file__).resolve().parent.parent, "http://127.0.0.1:" + str(port), control_token
    )
    agent = BoardTools(mcp_server, loop, api_key)
    configure_activity(lambda event: agent._mcp_json("set_voice_activity", event))
    from python_skills import PythonSkills

    def python_finished(job):
        publish_activity("done" if job["status"] == "done" else "error")
        if job["status"] in ("error", "stopped"):
            text = "Python: " + job.get("error", job["status"])
        elif job.get("result") is not None:
            text = (
                job["result"]
                if isinstance(job["result"], str)
                else json.dumps(job["result"], ensure_ascii=False)
            )
        else:
            return
        agent._mcp("display_voice_result", {"text": text[:400]})

    python_runtime = PythonSkills(
        Path(__file__).resolve().parent.parent, agent._mcp_json, python_finished
    )
    from media_store import MediaStore

    media = MediaStore()
    catalog = SkillCatalog()
    catalog.watch()
    router = FastRouter(catalog, api_key)
    work_queue: queue.Queue = queue.Queue(maxsize=8)

    complex_queue: queue.Queue = queue.Queue(maxsize=1)

    def worker(source, complex_lane=False):
        global LAST_VOICE_METRICS
        while True:
            item = source.get()
            context = cloud_voice_epoch.set(item["epoch"])
            activity_context = cloud_activity_id.set(item.get("activity_id"))
            success = False
            deferred = False
            route = "known"
            python_pending = False
            dispatched = False
            worker_started = time.monotonic()
            planner_ms = 0
            delivery_ms = None
            try:
                publish_activity("matching")
                feedback_sent = False
                decision = {}
                if conversation_candidate(item["text"]):
                    publish_activity("thinking")
                decision = item.get("_decision") or router.resolve(item["text"])
                if decision["kind"] == "complex" and not complex_lane:
                    try:
                        complex_queue.put_nowait({**item, "_decision": decision})
                    except queue.Full:
                        raise RuntimeError("Codex is busy; try the new task later")
                    deferred = True
                    continue
                route = decision["route"]
                if decision["kind"] == "answer":
                    publish_activity("thinking")
                    route = decision.get("route", "fast_answer")
                    answer = decision["reply"]
                elif decision["kind"] == "existing":
                    publish_activity("executing")
                    dispatched = True
                    name = decision["name"]
                    if catalog.snapshot()[0][name].get("runtime") == "python":
                        route = "python_saved"
                        agent._mcp(
                            "display_voice_result",
                            {"text": "Python-навык " + name + " запущен на ноутбуке."},
                        )
                        result = python_runtime.run(name)
                        python_pending = True
                        voice_console("python_started", **result)
                        feedback_sent = True
                        success = True
                        continue
                    try:
                        agent._mcp_json(
                            "run_stored_unihiker_skill",
                            {"name": name, "feedback": "Навык " + name + " запущен."},
                        )
                    except RuntimeError as exc:
                        # Repair only an explicit missing-skill response, never
                        # retry an ambiguous timeout that may already have run.
                        if not any(
                            x in str(exc).lower()
                            for x in (
                                "not found in board storage",
                                "not installed",
                                "unknown skill",
                            )
                        ):
                            raise
                        saved = catalog.snapshot()[0][name]
                        agent._save_and_run(
                            name, saved["voice_phrase"], json.dumps(saved["program"])
                        )
                    answer = "Навык " + name + " запущен."
                    feedback_sent = True
                elif decision["kind"] == "program":
                    publish_activity("executing")
                    dispatched = True
                    agent._mcp_json(
                        "execute_board_code",
                        {
                            "code": json.dumps(decision["program"]),
                            "feedback": "Выполнено из готовых навыков.",
                        },
                    )
                    answer = "Выполнено из готовых навыков."
                    feedback_sent = True
                elif decision["kind"] == "new":
                    publish_activity("validating")
                    publish_activity("executing")
                    dispatched = True
                    route = "fast_new"
                    agent._save_and_run(
                        decision["name"],
                        decision["voice_phrase"],
                        json.dumps(decision["program"]),
                        decision.get("short_commands", []),
                    )
                    catalog.add(
                        decision["name"],
                        decision["voice_phrase"],
                        decision["program"],
                        item["text"],
                        decision.get("short_commands", []),
                    )
                    for alias in [item["text"], *decision.get("short_commands", [])]:
                        remember_alias(decision["name"], alias)
                    answer = "Сохранено. Команда: " + decision["voice_phrase"]
                    feedback_sent = True
                elif backend == "codex":
                    route = "codex"
                    publish_activity(
                        "thinking"
                        if re.match(
                            r"^(?:кто|что|почему|зачем|как|what|who|why|how)\b",
                            item["text"].strip().casefold(),
                        )
                        else "writing"
                    )
                    planning_started = time.monotonic()
                    plan = runner.answer(item["text"], item["epoch"], catalog.snapshot()[1])
                    planner_ms = round((time.monotonic() - planning_started) * 1000, 2)
                    publish_activity("validating")
                    if plan["kind"] == "skill":
                        publish_activity("executing")
                        dispatched = True
                        from skill_composition import reuse_program

                        program = json.loads(plan["program_json"])
                        reused = reuse_program(program, catalog.snapshot()[0])
                        if reused is not None:
                            agent._mcp_json(
                                "execute_board_code",
                                {
                                    "code": plan["program_json"],
                                    "feedback": "Выполнено из готовых навыков.",
                                },
                            )
                            answer = "Выполнено из готовых навыков."
                            feedback_sent = True
                        else:
                            agent._save_and_run(
                                plan["name"],
                                plan["voice_phrase"],
                                plan["program_json"],
                                plan.get("short_commands", []),
                            )
                            catalog.add(
                                plan["name"],
                                plan["voice_phrase"],
                                program,
                                item["text"],
                                plan.get("short_commands", []),
                            )
                            for alias in [item["text"], *plan.get("short_commands", [])]:
                                remember_alias(plan["name"], alias)
                            answer = (
                                "Сохранено. Команда: "
                                + (plan.get("short_commands") or [plan["voice_phrase"]])[0]
                            )
                            feedback_sent = True
                    elif plan["kind"] == "python_skill":
                        route = "python_new"
                        saved = python_runtime.save(
                            plan["name"],
                            plan["voice_phrase"],
                            json.loads(plan["program_json"])["source"],
                            [item["text"], *plan.get("short_commands", [])],
                        )
                        catalog.add_python(saved)
                        agent._mcp(
                            "display_voice_result",
                            {
                                "text": "Python сохранён. Команда: "
                                + (plan.get("short_commands") or [plan["voice_phrase"]])[0]
                            },
                        )
                        publish_activity("executing")
                        result = python_runtime.run(plan["name"])
                        python_pending = True
                        voice_console("python_started", **result)
                        feedback_sent = True
                    else:
                        answer = plan["reply"]
                else:
                    raise RuntimeError("Unsupported route")
                if not feedback_sent:
                    delivery_started = time.monotonic()
                    agent._mcp("display_voice_result", {"text": answer[:480]})
                    delivery_ms = round((time.monotonic() - delivery_started) * 1000, 2)
                success = decision.get("route") != "fast_unavailable"
            except Exception as exc:
                try:
                    agent._mcp(
                        "display_voice_result", {"text": ("Задача не выполнена: " + str(exc))[:480]}
                    )
                except Exception:
                    pass
            finally:
                if not deferred:
                    if not success:
                        publish_activity("error")
                    elif not python_pending:
                        publish_activity("started" if dispatched else "done")
                    LAST_VOICE_METRICS = {
                        "id": item["id"],
                        "route": route,
                        "success": success,
                        "elapsed_ms": round((time.monotonic() - item["created_at"]) * 1000),
                        "lookup_ms": decision.get("elapsed_ms", 0) if "decision" in locals() else 0,
                        "queue_ms": round((worker_started - item["created_at"]) * 1000, 2),
                        "planner_ms": planner_ms,
                        "delivery_ms": delivery_ms,
                        "router_error": decision.get("reason"),
                    }
                    voice_console("finished", **LAST_VOICE_METRICS)
                cloud_voice_epoch.reset(context)
                cloud_activity_id.reset(activity_context)
                source.task_done()

    try:
        server = ExclusiveHTTPServer(
            (
                "127.0.0.1",
                port + 1,
            ),
            _make_handler(
                agent,
                token,
                work_queue,
                control_token,
                backend,
                python_runtime,
                speech,
            ),
        )
    except OSError as exc:
        import sys
        from gateway_control import existing_gateway

        shared = existing_gateway(port)
        if not shared:
            raise RuntimeError("Gateway port is occupied by an incompatible process") from exc
        print("[telly-voice] using existing local gateway on port {}".format(port), file=sys.stderr)
        return shared
    from realtime_gateway import start_realtime

    def accept_text(text, epoch, activity_id, created_at=None):
        if classify_transcript(text, manual=True) != "command":
            return None
        request_id = uuid.uuid4().hex[:12]
        work_queue.put_nowait(
            {
                "id": request_id,
                "text": text,
                "epoch": epoch,
                "activity_id": activity_id,
                "created_at": created_at or time.monotonic(),
            }
        )
        return request_id

    start_realtime(port, token, speech, accept_text, media)
    from gateway_control import save_gateway

    save_gateway(port, control_token)

    def prepare_codex():
        if backend == "codex":
            try:
                runner.prepare()
            except Exception:
                pass  # A request reports the failure; no retry of hardware.

    threading.Thread(target=prepare_codex, name="telly-codex-start", daemon=True).start()
    threading.Thread(
        target=worker, args=(work_queue,), name="telly-fast-dispatch", daemon=True
    ).start()
    threading.Thread(
        target=worker, args=(complex_queue, True), name="telly-complex-dispatch", daemon=True
    ).start()
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="telly-voice-http", daemon=True).start()
