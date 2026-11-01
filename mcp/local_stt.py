"""Bounded, in-memory CPU transcription for manual K10 voice clips."""

import io
import os
from pathlib import Path
import threading
import time
import wave

MAX_WAV_BYTES = 320044


def validate_wav(raw):
    if not 44 < len(raw) <= MAX_WAV_BYTES:
        raise ValueError("WAV must contain at most ten seconds of mono PCM")
    try:
        with wave.open(io.BytesIO(raw), "rb") as audio:
            if (
                audio.getnchannels(),
                audio.getsampwidth(),
                audio.getframerate(),
                audio.getcomptype(),
            ) != (1, 2, 16000, "NONE"):
                raise ValueError("Expected 16 kHz mono signed 16-bit PCM WAV")
            frames = audio.getnframes()
            if not 4000 <= frames <= 160000 or len(audio.readframes(frames)) != frames * 2:
                raise ValueError("Incomplete WAV or invalid duration")
            return frames
    except (wave.Error, EOFError) as exc:
        raise ValueError("Invalid PCM WAV") from exc


class SpeechBusy(RuntimeError):
    pass


class LocalSpeech:
    def __init__(self, engine, model="tiny", language=None):
        self.engine = engine
        self.model = model
        self.language = language
        self.available = threading.BoundedSemaphore(1)
        self.last = {}

    @classmethod
    def load(cls):
        from faster_whisper import WhisperModel

        model = os.environ.get("TELLY_STT_MODEL", "tiny")
        if model not in ("tiny", "base", "small"):
            raise ValueError("TELLY_STT_MODEL must be tiny, base or small")
        language = os.environ.get("TELLY_STT_LANGUAGE", "auto")
        if language not in ("auto", "ru", "en"):
            raise ValueError("TELLY_STT_LANGUAGE must be auto, ru or en")
        engine = WhisperModel(
            model,
            device="cpu",
            compute_type="int8",
            cpu_threads=min(8, os.cpu_count() or 1),
            num_workers=1,
            download_root=str(Path(__file__).resolve().parents[1] / ".telly" / "stt-models"),
        )
        speech = cls(engine, model, None if language == "auto" else language)
        # Execute the generator now: loading weights alone does not warm inference.
        import numpy as np

        segments, _ = engine.transcribe(
            np.zeros(16000, dtype=np.float32),
            language="en",
            beam_size=1,
            max_new_tokens=8,
            vad_filter=False,
        )
        list(segments)
        segments, _ = engine.transcribe(
            np.zeros(16000, dtype=np.float32), language="en", beam_size=1, vad_filter=True
        )
        list(segments)
        return speech

    def transcribe(self, raw):
        frames = validate_wav(raw)
        if not self.available.acquire(blocking=False):
            raise SpeechBusy("Local speech recognizer is busy; retry after the current clip")
        started = time.monotonic()
        try:
            import numpy as np

            with wave.open(io.BytesIO(raw), "rb") as audio:
                samples = np.frombuffer(audio.readframes(frames), dtype="<i2").astype(np.float32)
            samples *= 1.0 / 32768
            segments, info = self.engine.transcribe(
                samples,
                language=self.language,
                beam_size=1,
                best_of=1,
                temperature=0,
                condition_on_previous_text=False,
                word_timestamps=False,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
                max_new_tokens=128,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            if len(text) > 3000:
                raise ValueError("Transcript exceeds the command limit")
            result = {
                "text": text,
                "provider": "local",
                "model": self.model,
                "language": info.language,
                "inference_ms": round((time.monotonic() - started) * 1000, 2),
                "audio_ms": frames / 16,
            }
            self.last = {key: value for key, value in result.items() if key != "text"}
            return result
        finally:
            self.available.release()


if __name__ == "__main__":
    import json
    import sys

    speech = LocalSpeech.load()
    if len(sys.argv) > 1:
        print(json.dumps(speech.transcribe(Path(sys.argv[1]).read_bytes()), ensure_ascii=True))
    else:
        print(json.dumps({"ready": True, "model": speech.model, "compute": "cpu/int8"}))
