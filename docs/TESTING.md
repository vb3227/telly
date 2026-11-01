# Validation

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -t .
ruff check mcp tests scripts
ruff format --check mcp tests scripts
python scripts/check_repository.py
python scripts/package_source.py
```

Tests exercise bounds/validation, phrase matching and composition, short aliases, authenticated WebSocket exchange, duplicate metadata, disconnect/no-replay behavior, JPEG/AVI persistence, Python capability limits, process cancellation/memory caps and publication exclusions. They use fake models where appropriate; passing does not establish speech accuracy, sub-five-second latency or arbitrary hardware compatibility.

Build C++ using VS Code PlatformIO. An upload requires explicit authorization. Real acceptance should check an actual command path, saved-skill reuse without extra files, photo/video persistence and failure recovery. Device `[voice timing] stop_to_action_ms` measures from capture completion to device action/feedback delivery; host-only inference metrics exclude upload time.

Publishing source does not publish firmware, credentials or media. Review `dist/telly-source.zip` and the Git diff. The automated publication checker is a practical preflight rather than a full secret/security audit. Measurements and limitations belong in [performance](PERFORMANCE.md).
