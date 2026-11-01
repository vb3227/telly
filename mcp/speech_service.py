"""One host-side speech provider; no device-side API keys or REST relay."""

import time
import httpx
from local_stt import LocalSpeech, validate_wav


class SpeechService:
    def __init__(self, provider, api_key):
        if provider not in ("local", "groq"):
            raise ValueError("TELLY_STT_PROVIDER must be local or groq")
        self.provider = provider
        self.last = {}
        self.model = "tiny" if provider == "local" else "whisper-large-v3-turbo"
        self.local = LocalSpeech.load() if provider == "local" else None
        self.api_key = api_key
        if provider == "groq" and not api_key:
            raise ValueError("Groq recognition requires a laptop GROQ_API_KEY")
        self.client = httpx.Client(trust_env=False, timeout=httpx.Timeout(3, connect=1))

    def transcribe(self, raw):
        frames = validate_wav(raw)
        if self.local:
            result = self.local.transcribe(raw)
        else:
            started = time.monotonic()
            response = self.client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + self.api_key},
                data={"model": self.model},
                files={"file": ("command.wav", raw, "audio/wav")},
            )
            if response.status_code != 200:
                raise RuntimeError("Speech provider HTTP " + str(response.status_code))
            text = response.json().get("text")
            if not isinstance(text, str) or len(text) > 3000:
                raise ValueError("Invalid transcript")
            result = {
                "text": text.strip(),
                "provider": "groq",
                "model": self.model,
                "inference_ms": round((time.monotonic() - started) * 1000, 2),
                "audio_ms": frames / 16,
            }
        self.last = {key: value for key, value in result.items() if key != "text"}
        return result
