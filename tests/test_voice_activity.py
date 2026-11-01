import json
import queue
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from board_relay import BoardRelay, cloud_activity_id, cloud_voice_epoch
from voice_activity import publish_activity
from voice_gateway import _make_handler


class VoiceActivityTests(unittest.TestCase):
    def test_gateway_reload_preserves_only_a_valid_local_control_credential(self):
        import tempfile
        from pathlib import Path
        from gateway_control import stored_control_token

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            with patch("gateway_control.STATE", path):
                self.assertIsNone(stored_control_token(8767))
                token = "example_" + "a" * 40
                path.write_text(json.dumps({"port": 8767, "token": token}))
                self.assertEqual(stored_control_token(8767), token)
                self.assertIsNone(stored_control_token(8768))
                path.write_text(json.dumps({"port": 8767, "token": "short"}))
                self.assertIsNone(stored_control_token(8767))

    def test_activity_wakes_poll_without_creating_hardware_command(self):
        relay = BoardRelay()
        received = []
        thread = threading.Thread(target=lambda: received.append(relay.poll(timeout=3)))
        thread.start()
        self.assertTrue(relay.publish_activity("123-4", 7, "writing"))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(
            received,
            [{"activity": [{"activity_id": "123-4", "voice_epoch": 7, "stage": "writing"}]}],
        )
        self.assertEqual(relay.pending, {})

    def test_coalesces_bounds_replays_and_expires_cosmetic_updates(self):
        relay = BoardRelay()
        for n in range(12):
            relay.publish_activity(str(n), 7, "matching")
        relay.publish_activity("11", 7, "writing")
        packet = relay.poll(timeout=0)
        self.assertEqual(len(packet["activity"]), 8)
        self.assertEqual(packet["activity"][-1]["stage"], "writing")
        self.assertEqual({}, relay.poll(timeout=0))
        relay.activity_dirty = True  # Socket reconnect republishes the latest state once.
        self.assertEqual(packet, relay.poll(timeout=0))
        with patch("board_relay.time.monotonic", return_value=time.monotonic() + 91):
            self.assertEqual(relay.poll(timeout=0), {})

    def test_activity_piggybacks_on_normal_command_without_changing_ack(self):
        relay = BoardRelay()
        results = []
        relay.publish_activity("capture", 3, "executing")
        thread = threading.Thread(
            target=lambda: results.append(relay.request("GET", "/ping", timeout=3))
        )
        thread.start()
        deadline = time.monotonic() + 1
        while relay.commands.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        packet = relay.poll(timeout=1)
        self.assertEqual(packet["path"], "/ping")
        self.assertEqual(packet["activity"][0]["stage"], "executing")
        self.assertTrue(relay.complete({"id": packet["id"], "body": "pong"}))
        thread.join(timeout=2)
        self.assertEqual(results, ["pong"])

    def test_rejects_invalid_event_metadata(self):
        relay = BoardRelay()
        for capture, epoch, stage in [
            ("", 1, "writing"),
            ("../bad", 1, "writing"),
            ("ok", True, "done"),
            ("ok", -1, "done"),
            ("ok", 1, "fake"),
            ("ok", 1, []),
        ]:
            self.assertFalse(relay.publish_activity(capture, epoch, stage))
        self.assertEqual(relay.poll(timeout=0), {})

    def test_activity_context_is_bound_to_this_voice_capture(self):
        relay = BoardRelay()
        epoch = cloud_voice_epoch.set(5)
        capture = cloud_activity_id.set("only-this-capture")
        try:
            with (
                patch("voice_activity.relay", relay),
                patch.dict("os.environ", {"UNIHIKER_CONNECTION": "relay"}),
            ):
                publish_activity("writing")
        finally:
            cloud_voice_epoch.reset(epoch)
            cloud_activity_id.reset(capture)
        event = relay.poll(timeout=0)["activity"][0]
        self.assertEqual(event["activity_id"], "only-this-capture")
        self.assertEqual(event["voice_epoch"], 5)

    def test_authenticated_voice_accepts_optional_activity_id_and_rejects_bad_id(self):
        work = queue.Queue()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(None, "token", work))
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def post(activity):
            payload = {"text": "show sensors", "voice_epoch": 4}
            if activity is not None:
                payload["activity_id"] = activity
            request = urllib.request.Request(
                "http://127.0.0.1:%d/voice" % server.server_port,
                data=json.dumps(payload).encode(),
                headers={"X-Voice-Gateway-Token": "token"},
            )
            return urllib.request.urlopen(request, timeout=3)

        try:
            for bad in ("../other", 41 * "x", 123):
                with self.assertRaises(urllib.error.HTTPError) as exc:
                    post(bad)
                self.assertEqual(exc.exception.code, 400)
            with post("1234-5") as response:
                self.assertTrue(json.load(response)["accepted"])
            self.assertEqual(work.get_nowait()["activity_id"], "1234-5")
            with post(None) as response:
                self.assertTrue(json.load(response)["accepted"])
            self.assertIsNone(work.get_nowait()["activity_id"])
        finally:
            server.shutdown()
            server.server_close()
