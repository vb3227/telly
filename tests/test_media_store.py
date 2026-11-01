import io
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from media_store import MediaStore


def jpeg():
    stream = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(stream, format="JPEG")
    return stream.getvalue()


class MediaTests(unittest.TestCase):
    def test_photo_is_saved_without_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MediaStore(folder)
            result = store.frame(
                {"session": "photo", "mode": "photo", "index": 0, "at_ms": 0}, jpeg()
            )
            self.assertEqual(result["filename"], "photo.jpg")
            self.assertEqual((Path(folder) / "photo.jpg").read_bytes(), jpeg())

    def test_video_is_real_mjpeg_avi_with_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MediaStore(folder)
            for i in range(3):
                store.frame(
                    {"session": "clip", "mode": "video", "index": i, "at_ms": i * 200}, jpeg()
                )
            result = store.finish({"session": "clip"})
            raw = (Path(folder) / result["filename"]).read_bytes()
            self.assertEqual(raw[:4], b"RIFF")
            self.assertEqual(raw[8:12], b"AVI ")
            self.assertIn(b"MJPG", raw)
            self.assertIn(b"idx1", raw)
            self.assertEqual(result["frame_times_ms"], [0, 200, 400])
            self.assertFalse(result["audio"])

    def test_path_traversal_bad_jpeg_order_and_quota_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MediaStore(folder, quota=1)
            for sid, raw in [("../outside", jpeg()), ("a", b"not jpeg"), ("a", jpeg())]:
                with self.assertRaises((ValueError, OSError)):
                    store.frame({"session": sid, "mode": "photo", "index": 0, "at_ms": 0}, raw)
