# Contributing

Use Python 3.11+ and install `requirements-dev.txt`. Run `python -m unittest discover -s tests -t . -v` and `python scripts/check_repository.py` before submitting changes. Run `ruff check mcp tests scripts` and `ruff format --check mcp tests scripts` for Python style. Build firmware through PlatformIO in VS Code when C++ changes.

Keep device credentials in the ignored board header and user environment. Use placeholder values in examples. Do not commit runtime data, camera frames, audio, API responses, firmware binaries, virtual environments or IDE-generated files.

Validate new JSON operations on both host and firmware. Preserve local wake priority, four-job limits and bounded media capture and WebSocket message ownership. Include meaningful regression tests for changed behavior. Keep `mcp/server.py` at its current path: installed MCP clients reference it directly. Tests live in `tests/`, not beside bridge modules.

Hardware uploads require explicit authorization. Record whether a change was only unit tested, compiled, or actually exercised on a device. Do not report target latency as a measurement.

The repository does not currently declare an open-source license. Agree on contribution and distribution terms with the project owner before external contributions.
