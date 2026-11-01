"""Local credentials for sharing one board queue between MCP clients."""

import json
import os
from pathlib import Path
import re
from urllib.request import urlopen

STATE = Path(__file__).resolve().parent.parent / ".telly" / "gateway-control.json"


def save_gateway(port, token):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(
        json.dumps({"port": port, "token": token, "pid": os.getpid()}), encoding="utf-8"
    )


def stored_control_token(port):
    """Keep local MCP clients connected across a gateway-only code reload."""
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        token = value.get("token")
        if (
            value.get("port") == port
            and isinstance(token, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
        ):
            return token
    except (OSError, ValueError, AttributeError):
        pass
    return None


def existing_gateway(port):
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        if value["port"] != port or not isinstance(value["token"], str):
            return None
        with urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as response:
            health = json.load(response)
        if health.get("provider") not in ("codex", "groq"):
            return None
        return {"url": "http://127.0.0.1:%d" % port, "token": value["token"]}
    except (OSError, ValueError, KeyError):
        return None
