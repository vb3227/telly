# Host services

`server.py` is the stable stdio MCP facade; keep its configured path stable. `run_gateway.py` starts the shared runtime independently of the desktop client. `realtime_gateway.py` owns the persistent board connection, `speech_service.py` selects host recognition, and `skill_router.py`/`skill_composition.py` reuse capabilities before invoking Codex.

See [setup](../docs/SETUP.md), [protocol](../docs/PROTOCOL.md) and [architecture](../docs/ARCHITECTURE.md). Install root requirements; local speech dependencies are optional. Never publish runtime tokens, audio, media, model caches or `.telly/`.
