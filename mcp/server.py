import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from mcp.server.mcpserver import MCPServer
from board_relay import relay, cloud_voice_epoch
from program_validation import validate_program
from urllib.parse import urlparse

mcp = MCPServer("unihiker-control")

BOARD_URL = os.environ.get("UNIHIKER_BOARD_URL", "http://192.0.2.1:8765").rstrip("/")
RESCUE_URL = os.environ.get("UNIHIKER_RESCUE_URL", BOARD_URL.rsplit(":", 1)[0] + ":8766").rstrip(
    "/"
)
BOARD_TOKEN = os.environ.get("UNIHIKER_BOARD_TOKEN", "")
SHARED_GATEWAY = None
CONNECTION = os.environ.get("UNIHIKER_CONNECTION", "direct").lower()


def request_to(base_url, path, method="GET", payload=None, timeout=5):
    if not BOARD_TOKEN:
        raise RuntimeError(
            "Set UNIHIKER_BOARD_TOKEN in the environment before starting the MCP server"
        )
    proxy = os.environ.get("TELLY_MCP_PROXY_URL", "") or (SHARED_GATEWAY or {}).get("url", "")
    if proxy:
        parsed = urlparse(proxy)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise RuntimeError("Codex MCP proxy must be a local HTTP address")
        epoch_value = os.environ.get("TELLY_CLOUD_VOICE_EPOCH")
        epoch = int(epoch_value) if epoch_value is not None else cloud_voice_epoch.get()
        raw = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if payload is not None
            else ""
        )
        packet = {"method": method, "path": path, "body": raw, "voice_epoch": epoch}
        req = Request(
            proxy.rstrip("/") + "/board/request",
            data=json.dumps(packet).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Local-Control-Token": os.environ.get("TELLY_LOCAL_CONTROL_TOKEN", "")
                or (SHARED_GATEWAY or {}).get("token", ""),
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=55) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
            except (ValueError, OSError):
                detail = str(exc)
            raise RuntimeError(str(detail)[:600]) from exc
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["body"]
    data = None
    headers = {"X-Board-Token": BOARD_TOKEN}
    if cloud_voice_epoch.get() is not None:
        headers["X-Voice-Epoch"] = str(cloud_voice_epoch.get())
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if CONNECTION == "relay":
        return relay.request(
            method, path, data.decode("utf-8") if data else "", timeout=max(timeout, 45)
        )
    req = Request(base_url + path, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def rescue_request(path, method="GET", payload=None, timeout=5):
    errors = []
    for base_url in (BOARD_URL,) if CONNECTION == "relay" else (BOARD_URL, RESCUE_URL):
        try:
            return request_to(base_url, path, method, payload, timeout)
        except Exception as exc:
            errors.append(str(exc))
    return "Ошибка связи с UNIHIKER по основному и резервному каналам: " + " | ".join(errors)


@mcp.tool()
def ping_unihiker() -> str:
    """Проверить основной и резервный каналы связи с UNIHIKER."""
    return rescue_request("/ping", timeout=3)


@mcp.tool()
def execute_board_code(code: str, feedback: str = "") -> str:
    """Асинхронно запустить JSON-сценарий на UNIHIKER. Прошивка поддерживает до четырёх независимых задач; вернёт ID этой задачи."""
    if not code.strip():
        return "JSON-сценарий пустой."
    try:
        program = validate_program(json.loads(code), max_bytes=4096, allow_voice=True)
    except Exception as exc:
        return 'Ожидается JSON-сценарий, например {"steps":[{"op":"rgb.blink"}]}. Ошибка: ' + str(
            exc
        )
    if not isinstance(program, dict) or not isinstance(program.get("steps"), list):
        return "Неверный формат: ожидается объект с массивом steps."
    if not 1 <= len(program["steps"]) <= 32:
        return "Сценарий должен содержать от 1 до 32 шагов."
    if len(code.encode("utf-8")) > 4096:
        return "JSON-сценарий слишком большой: максимум 4096 байт."
    try:
        return request_to(
            BOARD_URL,
            "/run",
            "POST",
            {"program": program, "feedback": feedback[:480]} if feedback else program,
            timeout=5,
        )
    except Exception as exc:
        return "Не удалось отправить задачу по основному каналу: {}".format(exc)


@mcp.tool()
def board_job_status(job_id: str) -> str:
    """Получить состояние конкретной задачи на UNIHIKER по её job_id."""
    try:
        # После перепрошивки текущий сервер платы сообщает статус на основном порту.
        return request_to(BOARD_URL, "/status?job_id=" + str(job_id), timeout=3)
    except Exception as exc:
        return "Не удалось получить состояние задачи: {}".format(exc)


@mcp.tool()
def stop_board_job(job_id: str) -> str:
    """Остановить только задачу с указанным job_id; остальные задачи продолжат работу."""
    return rescue_request("/stop", "POST", {"job_id": str(job_id)}, timeout=3)


@mcp.tool()
def reset_unihiker() -> str:
    """Аварийно перезагрузить UNIHIKER через резервный канал."""
    try:
        return request_to(RESCUE_URL, "/reset", "POST", {}, timeout=3)
    except Exception as rescue_error:
        try:
            return request_to(BOARD_URL, "/reset", "POST", {}, timeout=3)
        except Exception as primary_error:
            return "Не удалось перезагрузить плату: резервный канал: {}; основной: {}".format(
                rescue_error, primary_error
            )


@mcp.tool()
def latest_voice_transcript() -> str:
    """Получить последнюю расшифровку, которую K10 сделал через облачное распознавание."""
    try:
        return request_to(BOARD_URL, "/voice/latest", timeout=4)
    except Exception as exc:
        return "Не удалось получить голосовую расшифровку: {}".format(exc)


@mcp.tool()
def list_unihiker_skills() -> str:
    """Показать навыки, сохранённые в постоянной памяти UNIHIKER."""
    try:
        return request_to(BOARD_URL, "/skills/list", timeout=4)
    except Exception as exc:
        return "Не удалось получить список навыков: {}".format(exc)


def read_skill_file(name: str) -> dict:
    if not re.fullmatch(r"[a-z0-9_-]{1,31}", name):
        raise ValueError(
            "Имя навыка должно содержать только строчные латинские буквы, цифры, дефис и подчёркивание."
        )
    path = Path(__file__).resolve().parent.parent / "skills" / (name + ".json")
    with path.open("r", encoding="utf-8") as source:
        skill = json.load(source)
    if skill.get("name") != name or not isinstance(skill.get("program"), dict):
        raise ValueError("Файл навыка должен содержать совпадающее поле name и объект program.")
    steps = skill["program"].get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise ValueError("Навык должен содержать от 1 до 32 шагов.")
    validate_program(skill["program"])
    return skill


@mcp.tool()
def install_unihiker_skill(name: str) -> str:
    """Скопировать готовый JSON-навык из skills/ в постоянную память K10 для автономного запуска."""
    try:
        skill = read_skill_file(name)
        payload = {
            "name": name,
            "voice_phrase": skill.get("voice_phrase", ""),
            "program": skill["program"],
        }
        return request_to(BOARD_URL, "/skills/install", "POST", payload, timeout=6)
    except Exception as exc:
        return "Не удалось установить навык {}: {}".format(name, exc)


@mcp.tool()
def run_unihiker_skill(name: str) -> str:
    """Установить JSON-навык из skills/ в постоянную память K10 и запустить его."""
    try:
        skill = read_skill_file(name)
        install = request_to(
            BOARD_URL,
            "/skills/install",
            "POST",
            {
                "name": name,
                "voice_phrase": skill.get("voice_phrase", ""),
                "program": skill["program"],
            },
            timeout=6,
        )
        result = request_to(BOARD_URL, "/skills/run", "POST", {"name": name}, timeout=6)
        return json.dumps(
            {"installed": json.loads(install), "run": json.loads(result)}, ensure_ascii=False
        )
    except Exception as exc:
        return "Не удалось запустить навык {}: {}".format(name, exc)


@mcp.tool()
def save_unihiker_skill_payload(
    name: str,
    voice_phrase: str,
    program_json: str,
    run: bool = False,
    short_commands: list[str] | None = None,
) -> str:
    """Save a newly generated JSON skill directly to board NVS; no local skill file is needed."""
    if not re.fullmatch(r"[a-z0-9_-]{1,31}", name):
        return "Имя навыка: только строчные латинские буквы, цифры, дефис и подчёркивание (до 31 символа)."
    try:
        program = validate_program(json.loads(program_json))
        from skill_commands import command_aliases

        aliases = command_aliases(name, voice_phrase, program, short_commands or [])
    except Exception as exc:
        return "Неверный JSON программы: {}".format(exc)
    steps = program.get("steps") if isinstance(program, dict) else None
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        return "Программа должна содержать от 1 до 32 шагов."
    payload = {"name": name, "voice_phrase": voice_phrase[:63], "program": program}
    if run:
        payload.update(
            run=True, feedback="Сохранено. Команда: " + ((short_commands or [voice_phrase])[0])
        )
    try:
        result = request_to(BOARD_URL, "/skills/install", "POST", payload, timeout=6)
        # Keep a reusable laptop copy after NVS acknowledges the save. Failure to
        # write locally does not undo a successful board save.
        try:
            folder = Path(__file__).resolve().parent.parent / "skills"
            folder.mkdir(exist_ok=True)
            local = folder / (name + ".json")
            temporary = folder / (name + ".json.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        **{key: payload[key] for key in ("name", "voice_phrase", "program")},
                        "aliases": aliases,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(local)
        except OSError:
            pass
        return result
    except Exception as exc:
        return "Не удалось сохранить навык на плате: {}".format(exc)


@mcp.tool()
def display_voice_result(text: str) -> str:
    """Show the voice agent's short result on the K10 display."""
    try:
        return request_to(BOARD_URL, "/voice/result", "POST", {"text": text[:480]}, timeout=4)
    except Exception as exc:
        return "Не удалось вывести ответ на плату: {}".format(exc)


@mcp.tool()
def set_voice_activity(activity_id: str, voice_epoch: int, stage: str) -> str:
    """Update an existing capture's activity overlay; stale capture IDs are rejected."""
    from board_relay import ACTIVITY_STAGES

    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", activity_id)
        or type(voice_epoch) is not int
        or not 0 <= voice_epoch <= 0xFFFFFFFF
        or stage not in ACTIVITY_STAGES
    ):
        raise ValueError("Invalid voice activity update")
    return request_to(
        BOARD_URL,
        "/voice/activity",
        "POST",
        {"activity_id": activity_id, "voice_epoch": voice_epoch, "stage": stage},
    )


@mcp.tool()
def preview_loading_animation() -> str:
    """Play a labeled green loading animation preview without recording or API calls."""
    return request_to(BOARD_URL, "/voice/activity/preview", "POST", {})


@mcp.tool()
def run_stored_unihiker_skill(name: str, feedback: str = "") -> str:
    """Запустить навык по имени из постоянной памяти платы без доступа к файлам проекта."""
    try:
        return request_to(
            BOARD_URL,
            "/skills/run",
            "POST",
            {"name": name, "feedback": feedback[:480]} if feedback else {"name": name},
            timeout=6,
        )
    except Exception as exc:
        return "Не удалось запустить сохранённый навык {}: {}".format(name, exc)


@mcp.tool()
def take_photo() -> str:
    """Capture one JPEG and save it on the laptop; no image analysis."""
    return request_to(BOARD_URL, "/media/start", "POST", {"mode": "photo"})


@mcp.tool()
def delete_stored_unihiker_skill(name: str) -> str:
    """Remove one explicitly named device skill; reboot refreshes native phrases."""
    if not re.fullmatch(r"[a-z0-9_-]{1,31}", name):
        raise ValueError("Invalid skill name")
    return request_to(BOARD_URL, "/skills/delete", "POST", {"name": name})


@mcp.tool()
def start_voice_capture() -> str:
    """Explicitly start one bounded voice command capture, equivalent to A."""
    if cloud_voice_epoch.get() is not None:
        raise ValueError("A voice task cannot recursively start recording")
    return request_to(BOARD_URL, "/voice/start", "POST", {})


@mcp.tool()
def finish_voice_capture() -> str:
    """Finish the explicitly started recording and submit its audio once."""
    return request_to(BOARD_URL, "/voice/finish", "POST", {})


@mcp.tool()
def record_video() -> str:
    """Record a silent bounded MJPEG AVI clip on the laptop; no analysis."""
    return request_to(BOARD_URL, "/media/start", "POST", {"mode": "video"})


@mcp.tool()
def stop_video() -> str:
    """Finish only the active camera recording."""
    return request_to(BOARD_URL, "/media/stop", "POST", {})


@mcp.tool()
def media_capture_status() -> str:
    return request_to(BOARD_URL, "/media/status")


def _local_control(path, payload, timeout=10):
    from gateway_control import existing_gateway

    gateway = existing_gateway(int(os.environ.get("TELLY_VOICE_GATEWAY_PORT", "8767")))
    if not gateway:
        raise RuntimeError("Start the Telly gateway first")
    request = Request(
        gateway["url"] + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Local-Control-Token": gateway["token"]},
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _python_control(payload):
    payload = dict(payload, voice_epoch=cloud_voice_epoch.get())
    return _local_control("/python/control", payload)


@mcp.tool()
def save_python_skill(name: str, voice_phrase: str, source: str, run: bool = False) -> str:
    """Save real Python source on laptop; optionally start it. No firmware upload."""
    return _python_control(
        {"action": "save", "name": name, "voice_phrase": voice_phrase, "source": source, "run": run}
    )


@mcp.tool()
def list_python_skills() -> str:
    """List persistent laptop Python skills; these do not run offline on K10."""
    return _python_control({"action": "list"})


@mcp.tool()
def run_python_skill(name: str) -> str:
    """Start a saved Python skill asynchronously on laptop, controlling K10 via MCP."""
    return _python_control({"action": "run", "name": name})


@mcp.tool()
def python_skill_status(job_id: str) -> str:
    """Read Python execution result, captured print output, or error."""
    return _python_control({"action": "status", "job_id": job_id})


@mcp.tool()
def stop_python_skill(job_id: str) -> str:
    """Cancel only this Python process and stop board jobs it started where reachable."""
    return _python_control({"action": "stop", "job_id": job_id})


if __name__ == "__main__":
    import asyncio
    from voice_gateway import start_voice_gateway

    async def run_services():
        global SHARED_GATEWAY
        if not os.environ.get("TELLY_MCP_PROXY_URL"):
            SHARED_GATEWAY = start_voice_gateway(mcp, asyncio.get_running_loop())
        await mcp.run_stdio_async()

    asyncio.run(run_services())
