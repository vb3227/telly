import concurrent.futures
import queue
import tempfile
import unittest
from unittest.mock import Mock, patch
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from board_relay import BoardRelay
from realtime_gateway import create_app, valid_metadata
from media_store import MediaStore
from tests.test_local_stt import wav


class RealtimeTests(unittest.TestCase):
    def test_metadata_rejects_oversize_and_bool_epoch(self):
        for values in ({"bytes": 320045}, {"epoch": True}, {"id": "../secret"}):
            with self.assertRaises(ValueError):
                valid_metadata({"type": "audio", "id": "a", "epoch": 1, "bytes": 100, **values})

    def test_socket_auth_audio_dispatch_and_no_repeated_transcript_post(self):
        relay = BoardRelay()
        accepted = queue.Queue()
        speech = Mock()
        speech.transcribe.return_value = {"text": "show sensors", "inference_ms": 10}
        with tempfile.TemporaryDirectory() as folder, patch("realtime_gateway.relay", relay):
            app = create_app(
                10000, "secret", speech, lambda *args: accepted.put(args), MediaStore(folder)
            )
            with TestClient(app) as client:
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/board/ws"):
                        pass
                with client.websocket_connect(
                    "/board/ws", headers={"X-Voice-Gateway-Token": "secret"}
                ) as ws:
                    raw = wav()
                    ws.send_json({"type": "audio", "id": "voice1", "bytes": len(raw), "epoch": 123})
                    ws.send_bytes(raw)
                    result = ws.receive_json()
                    self.assertEqual(result["text"], "show sensors")
                    text, epoch, capture, _ = accepted.get(timeout=3)
                    self.assertEqual((text, epoch, capture), ("show sensors", 123, "voice1"))
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(relay.request, "GET", "/ping", timeout=3)
                        command = ws.receive_json()
                        self.assertEqual(command["path"], "/ping")
                        ws.send_json({"type": "result", "id": command["id"], "body": "pong"})
                        self.assertEqual(future.result(timeout=3), "pong")
                    ws.send_json({"type": "audio", "id": "voice1", "bytes": len(raw), "epoch": 123})
                    with self.assertRaises(WebSocketDisconnect):
                        ws.receive_json()
            self.assertFalse(relay.online)

    def test_disconnect_fails_pending_calls_without_replay(self):
        relay = BoardRelay()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(relay.request, "GET", "/ping", timeout=3)
            self.assertTrue(relay.poll(0.5).get("id"))
            relay.disconnect()
            with self.assertRaisesRegex(RuntimeError, "No automatic replay"):
                future.result(timeout=2)
        with self.assertRaises(ConnectionError):
            relay.request("GET", "/ping")
