# Speech recognition

The device captures 16 kHz mono signed 16-bit PCM, at most ten seconds, then submits a WAV once over its persistent connection. The laptop returns the transcript for the console and immediately dispatches it itself. There is no second transcript upload. Empty/local-wake utterances do not enter the cloud action queue.

Default: Groq `whisper-large-v3-turbo` called from the laptop with a persistent HTTP client. Synthetic comparison on the development machine measured 310–404 ms for Groq versus 906–1046 ms for local multilingual tiny on the same 3.34-second phrase. These are samples, not a universal provider ranking or microphone accuracy benchmark.

For offline inference on the laptop:

```powershell
python -m pip install -r requirements-local-stt.txt
python mcp/local_stt.py
python mcp/run_gateway.py --codex-config --local-stt
```

Whisper tiny uses CPU INT8, greedy decoding, up to eight threads, and warms inference/VAD before serving. Weights cache in ignored `.telly/stt-models`. `TELLY_STT_LANGUAGE=auto|ru|en` and `TELLY_STT_MODEL=tiny|base|small` configure local recognition. Once weights are cached, `HF_HUB_OFFLINE=1` disables model metadata checks. A larger model requires new latency/quality measurements.

The on-device vendor wake/phrase recognizer stays independent. Its installed model determines the wake word. `Jarvis`/`джарвис` are accepted host command prefixes, not a claim that the native wake model has been retrained. Native saved phrases currently use short English triggers; Russian aliases work after host transcription.

Manual audio is processed in memory and not automatically persisted. With Groq selected, it is sent to Groq. With local selected, inference stays on the laptop; cross-network transport still uses the configured tunnel. No silent provider fallback or automatic action replay is performed.
