"""Bounded JSON programs accepted by the voice agent and MCP bridge."""

import json
import re

FIELDS = {
    "delay": {"duration_ms": (0, 3600000)},
    "rgb.set": {"r": (0, 255), "g": (0, 255), "b": (0, 255), "brightness": (0, 9)},
    "rgb.off": {},
    "rgb.blink": {
        "r": (0, 255),
        "g": (0, 255),
        "b": (0, 255),
        "brightness": (0, 9),
        "count": (1, 60),
        "period_ms": (40, 2000),
    },
    "display.clear": {"color": "color"},
    "display.text": {"text": "text", "x": (-320, 640), "y": (-320, 960), "color": "color"},
    "display.square": {
        "x": (-320, 640),
        "y": (-320, 960),
        "w": (1, 640),
        "h": (1, 640),
        "size": (1, 640),
        "color": "color",
    },
    "display.rect": {
        "x": (-320, 640),
        "y": (-320, 960),
        "w": (1, 640),
        "h": (1, 640),
        "size": (1, 640),
        "color": "color",
    },
    "display.line": {
        "x1": (-320, 640),
        "y1": (-320, 960),
        "x2": (-320, 640),
        "y2": (-320, 960),
        "color": "color",
    },
    "sensor.read": {},
    "sensor.read.display": {},
    "mic.sample": {},
    "asr.start": {},
    "asr.last": {},
    "camera.preview": {"duration_ms": (0, 120000)},
    "camera.stop": {},
    "camera.status": {},
    "camera.photo": {},
    "camera.record": {},
    "camera.finish": {},
}


def validate_program(program, max_bytes=3800, allow_voice=False):
    if not isinstance(program, dict) or set(program) != {"steps"}:
        raise ValueError("Program must contain only steps")
    steps = program["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise ValueError("Program needs 1-32 steps")
    if len(json.dumps(program, ensure_ascii=False, separators=(",", ":")).encode()) > max_bytes:
        raise ValueError("Program is too large")
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("op"), str):
            raise ValueError("Each step needs an operation")
        op = step["op"]
        fields = {} if allow_voice and op == "voice.transcribe" else FIELDS.get(op)
        if fields is None or set(step) - {"op"} - set(fields):
            raise ValueError("Unsupported operation or field")
        for key, value in step.items():
            if key == "op":
                continue
            rule = fields[key]
            if isinstance(rule, tuple):
                if type(value) is not int or not rule[0] <= value <= rule[1]:
                    raise ValueError("Invalid numeric field: " + key)
            elif rule == "text":
                if not isinstance(value, str) or len(value.encode()) > 512:
                    raise ValueError("Display text exceeds 512 bytes")
            elif rule == "color":
                if not (
                    (type(value) is int and 0 <= value <= 0xFFFFFF)
                    or (isinstance(value, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", value))
                ):
                    raise ValueError("Invalid RGB color")
    return program
