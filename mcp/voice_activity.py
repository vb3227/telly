"""Coalesced voice UI events; never block planning on a status update."""

import os
import threading
from board_relay import relay, cloud_activity_id, cloud_voice_epoch, ACTIVITY_STAGES

_direct = None
_pending = {}
_lock = threading.Lock()
_wake = threading.Event()


def configure_activity(direct_callback):
    global _direct
    _direct = direct_callback
    if os.environ.get("UNIHIKER_CONNECTION") == "relay":
        return

    def deliver():
        while True:
            _wake.wait()
            _wake.clear()
            with _lock:
                events = list(_pending.values())
                _pending.clear()
            for event in events:
                try:
                    _direct(event)
                except Exception:
                    pass  # Cosmetic updates never retry hardware.

    threading.Thread(target=deliver, daemon=True, name="telly-activity-ui").start()


def publish_activity(stage):
    capture_id = cloud_activity_id.get()
    epoch = cloud_voice_epoch.get()
    if stage not in ACTIVITY_STAGES or not capture_id:
        return
    if os.environ.get("UNIHIKER_CONNECTION") == "relay":
        relay.publish_activity(capture_id, epoch, stage)
    elif _direct:
        with _lock:
            _pending[capture_id] = {"activity_id": capture_id, "voice_epoch": epoch, "stage": stage}
            while len(_pending) > 8:
                _pending.pop(next(iter(_pending)))
        _wake.set()
