"""JPEG photos and silent MJPEG AVI clips; no model, profiling or analysis."""

import io
import json
from pathlib import Path
import re
import struct
import threading
import time
from PIL import Image


def chunk(tag, data):
    return tag + struct.pack("<I", len(data)) + data + (b"\0" if len(data) & 1 else b"")


def avi(frames, width, height, duration_ms):
    count = len(frames)
    period = max(1, int(duration_ms * 1000 / max(1, count - 1)))
    largest = max(map(len, frames))
    main = struct.pack("<14I", period, 0, 0, 16, count, 0, 1, largest, width, height, 0, 0, 0, 0)
    stream = struct.pack(
        "<4s4sIHH8I4h",
        b"vids",
        b"MJPG",
        0,
        0,
        0,
        0,
        period,
        1000000,
        0,
        count,
        largest,
        0xFFFFFFFF,
        0,
        0,
        0,
        width,
        height,
    )
    bitmap = struct.pack("<IiiHH4sIiiII", 40, width, height, 1, 24, b"MJPG", largest, 0, 0, 0, 0)
    header = chunk(
        b"LIST",
        b"hdrl"
        + chunk(b"avih", main)
        + chunk(b"LIST", b"strl" + chunk(b"strh", stream) + chunk(b"strf", bitmap)),
    )
    body, index, offset = [], [], 4
    for frame in frames:
        piece = chunk(b"00dc", frame)
        body.append(piece)
        index.append(struct.pack("<4sIII", b"00dc", 16, offset, len(frame)))
        offset += len(piece)
    data = (
        b"AVI "
        + header
        + chunk(b"LIST", b"movi" + b"".join(body))
        + chunk(b"idx1", b"".join(index))
    )
    return b"RIFF" + struct.pack("<I", len(data)) + data


class MediaStore:
    def __init__(self, root=None, quota=512 * 1024 * 1024):
        self.root = Path(root or Path(__file__).resolve().parents[1] / ".telly" / "media")
        self.root.mkdir(parents=True, exist_ok=True)
        self.quota = quota
        self.lock = threading.Lock()
        self.sessions = {}

    @staticmethod
    def session(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", value):
            raise ValueError("Invalid media session")
        return value

    def frame(self, meta, raw):
        sid = self.session(meta.get("session"))
        mode, sequence, at = meta.get("mode"), meta.get("index"), meta.get("at_ms")
        if (
            mode not in ("photo", "video")
            or type(sequence) is not int
            or not 0 <= sequence < 100
            or type(at) is not int
            or not 0 <= at <= 30000
        ):
            raise ValueError("Invalid media frame metadata")
        if not 0 < len(raw) <= 65536:
            raise ValueError("JPEG exceeds 64 KB")
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if image.format != "JPEG" or not 0 < width <= 640 or not 0 < height <= 640:
                raise ValueError("Expected camera JPEG")
            image.verify()
        with self.lock:
            self.sessions = {
                k: v for k, v in self.sessions.items() if time.monotonic() - v["updated"] < 60
            }
            if (
                sum(p.stat().st_size for p in self.root.glob("*") if p.is_file()) + len(raw)
                > self.quota
            ):
                raise ValueError("Media quota reached; remove saved recordings")
            item = self.sessions.get(sid)
            if item is None:
                if sequence != 0 or len(self.sessions) >= 2:
                    raise ValueError("Unknown or busy media session")
                item = self.sessions[sid] = {
                    "mode": mode,
                    "frames": [],
                    "times": [],
                    "size": (width, height),
                    "updated": time.monotonic(),
                }
            if (
                item["mode"] != mode
                or sequence != len(item["frames"])
                or item["size"] != (width, height)
            ):
                raise ValueError("Duplicate/out-of-order frame or changed dimensions")
            if item["times"] and at <= item["times"][-1]:
                raise ValueError("Frame time must increase")
            if sum(map(len, item["frames"])) + len(raw) > 8 * 1024 * 1024:
                raise ValueError("Clip exceeds memory limit")
            item["frames"].append(raw)
            item["times"].append(at)
            item["updated"] = time.monotonic()
            if mode == "photo":
                return self._finish(sid, item)
            return {"frames": len(item["frames"]), "saved": False}

    def _finish(self, sid, item):
        ext = ".jpg" if item["mode"] == "photo" else ".avi"
        target = self.root / (sid + ext)
        if target.exists():
            raise ValueError("Recording already exists")
        frames, times = item["frames"], item["times"]
        raw = (
            frames[0]
            if ext == ".jpg"
            else avi(frames, *item["size"], times[-1] - times[0] if len(times) > 1 else 200)
        )
        if (
            sum(p.stat().st_size for p in self.root.glob("*") if p.is_file()) + len(raw)
            > self.quota
        ):
            raise ValueError("Media quota reached")
        temporary = target.with_suffix(ext + ".part")
        temporary.write_bytes(raw)
        temporary.replace(target)
        result = {
            "saved": True,
            "filename": target.name,
            "mode": item["mode"],
            "frames": len(frames),
            "frame_times_ms": times,
            "audio": False,
        }
        target.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        self.sessions.pop(sid, None)
        return result

    def finish(self, meta):
        sid = self.session(meta.get("session"))
        with self.lock:
            item = self.sessions.get(sid)
            if item is None or not item["frames"]:
                raise ValueError("No frames recorded")
            return self._finish(sid, item)
