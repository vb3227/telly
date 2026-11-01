"""Authenticated command exchange for a board behind NAT or another network."""

from __future__ import annotations

import queue
import threading
import uuid
import re
import time
from contextvars import ContextVar

cloud_voice_epoch = ContextVar("cloud_voice_epoch", default=None)
cloud_activity_id = ContextVar("cloud_activity_id", default=None)
ACTIVITY_STAGES = {
    "matching",
    "thinking",
    "writing",
    "validating",
    "executing",
    "started",
    "done",
    "error",
}

ALLOWED_PATHS = {
    "/voice/activity",
    "/voice/activity/preview",
    "/ping",
    "/status",
    "/stop",
    "/reset",
    "/run",
    "/voice/latest",
    "/voice/result",
    "/skills/list",
    "/skills/delete",
    "/voice/start",
    "/voice/finish",
    "/media/start",
    "/media/stop",
    "/media/status",
    "/skills/install",
    "/skills/run",
}


class BoardRelay:
    def __init__(self):
        self.commands: queue.Queue = queue.Queue(maxsize=16)
        self.pending: dict[str, tuple[threading.Event, dict]] = {}
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.activity = {}
        self.activity_dirty = False
        self.online = None

    def disconnect(self):
        self.online = False
        with self.lock:
            for ready, result in self.pending.values():
                result["error"] = (
                    "Device disconnected; execution may be incomplete. No automatic replay."
                )
                ready.set()

    def publish_activity(self, capture_id, epoch, stage):
        if (
            not isinstance(capture_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", capture_id)
            or type(epoch) is not int
            or not 0 <= epoch <= 0xFFFFFFFF
            or not isinstance(stage, str)
            or stage not in ACTIVITY_STAGES
        ):
            return False
        with self.lock:
            self.activity.pop(capture_id, None)
            self.activity[capture_id] = (
                {"activity_id": capture_id, "voice_epoch": epoch, "stage": stage},
                time.monotonic(),
            )
            while len(self.activity) > 8:
                self.activity.pop(next(iter(self.activity)))
            self.activity_dirty = True
        self.changed.set()
        return True

    def request(
        self, method: str, path: str, body: str = "", timeout: float = 30, voice_epoch=None
    ) -> str:
        if self.online is False:
            raise ConnectionError("Device WebSocket is offline")
        route = path.split("?", 1)[0]
        if route not in ALLOWED_PATHS or method not in ("GET", "POST"):
            raise ValueError("Unsupported board relay command")
        if len(body.encode("utf-8")) > 6000:
            raise ValueError("Board command is too large")
        command_id = uuid.uuid4().hex
        ready = threading.Event()
        result: dict = {}
        with self.lock:
            self.pending[command_id] = (ready, result)
        try:
            command = {"id": command_id, "method": method, "path": path, "body": body}
            capture_id = cloud_activity_id.get()
            if capture_id:
                command["activity_id"] = capture_id
            epoch = cloud_voice_epoch.get() if voice_epoch is None else voice_epoch
            if epoch is not None:
                command["voice_epoch"] = epoch
            self.commands.put_nowait(command)
            self.changed.set()
            if not ready.wait(timeout):
                raise TimeoutError(
                    "Board did not answer through the HTTPS relay within {} seconds".format(timeout)
                )
            if result.get("error"):
                raise RuntimeError(str(result["error"]))
            return str(result.get("body", ""))
        finally:
            with self.lock:
                self.pending.pop(command_id, None)

    def poll(self, timeout: float = 15) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            self.changed.clear()
            command = {}
            try:
                while True:
                    candidate = self.commands.get_nowait()
                    with self.lock:
                        if candidate["id"] in self.pending:
                            command = candidate
                            break
            except queue.Empty:
                pass
            with self.lock:
                self.activity = {
                    key: (value, created)
                    for key, (value, created) in self.activity.items()
                    if time.monotonic() - created < 90
                }
                events = [
                    value
                    for value, created in self.activity.values()
                    if time.monotonic() - created < 90
                ]
                fresh = self.activity_dirty
                self.activity_dirty = False
            if events and (command or fresh):
                command["activity"] = events
            if command:
                return command
            if time.monotonic() >= deadline:
                return {}
            self.changed.wait(max(0, deadline - time.monotonic()))

    def complete(self, payload: dict) -> bool:
        command_id = payload.get("id", "")
        if not isinstance(command_id, str) or not isinstance(payload.get("body"), str):
            return False
        with self.lock:
            pending = self.pending.get(command_id)
            if pending is None:
                return False
            ready, result = pending
            result.update(payload)
            ready.set()
        return True


relay = BoardRelay()
