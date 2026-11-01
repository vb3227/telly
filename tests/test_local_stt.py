import io
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import Mock

from local_stt import LocalSpeech, SpeechBusy, validate_wav


def wav(frames=16000, rate=16000, channels=1):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * frames * channels)
    return stream.getvalue()


class LocalSpeechTests(unittest.TestCase):
    def test_invalid_or_truncated_audio_rejected_before_inference(self):
        engine = Mock()
        speech = LocalSpeech(engine)
        for raw in (
            b"not wav",
            wav(rate=8000),
            wav(channels=2),
            wav(frames=160001),
            wav()[:-2],
            wav(frames=100),
        ):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                speech.transcribe(raw)
        engine.transcribe.assert_not_called()

    def test_generator_is_consumed_and_timings_include_inference(self):
        def segments():
            yield SimpleNamespace(text=" Turn off ")
            yield SimpleNamespace(text="the camera. ")

        engine = Mock()
        engine.transcribe.return_value = (segments(), SimpleNamespace(language="en"))
        speech = LocalSpeech(engine)
        result = speech.transcribe(wav())
        self.assertEqual(result["text"], "Turn off the camera.")
        self.assertEqual(result["audio_ms"], 1000)
        self.assertNotIn("text", speech.last)
        self.assertEqual(validate_wav(wav()), 16000)

    def test_busy_and_failed_inference_do_not_leave_worker_locked(self):
        engine = Mock()
        engine.transcribe.side_effect = RuntimeError("engine failed")
        speech = LocalSpeech(engine)
        speech.available.acquire()
        with self.assertRaises(SpeechBusy):
            speech.transcribe(wav())
        speech.available.release()
        with self.assertRaises(RuntimeError):
            speech.transcribe(wav())
        self.assertTrue(speech.available.acquire(blocking=False))
        speech.available.release()
