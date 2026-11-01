# Laptop Python skills

Complex skills can now contain Python functions, branches, loops and computations, instead of a fixed JSON sequence. Python executes on the laptop in a separate process and requests hardware actions through the existing MCP bridge. It requires a running laptop, gateway and board connection; it is not MicroPython on K10. No firmware update is needed for this feature, and it cannot add a missing native hardware driver.

## Voice workflow

A/A capture and Groq transcription stay the same. The in-memory catalog contains both board JSON skills and laptop Python skills. Exact phrases and aliases bypass generation; the small router can match paraphrases. Familiar JSON tasks retain the existing direct path. For complex logic Codex can return a `python_skill` plan with a source string. The gateway validates, saves and launches it asynchronously, freeing the voice queue for subsequent requests. The currently selected Codex model is controlled by `TELLY_CODEX_MODEL` or the resumed/default Codex session; do not assume it matches the desktop chat model.

The coding worker receives the API contract below and returns code, not executable shell commands. A rejected plan gets one validation-repair attempt before anything runs. Runtime failure is reported; hardware actions are not automatically replayed.

The current API contract is repeated on every planning turn. Saved planner sessions carry an instruction hash; after a contract change a fresh planning session is created instead of resuming stale instructions. Existing skill files remain available through the in-memory catalog.

## Files and MCP tools

Each skill has `skills/python/<name>.py` and `<name>.json`. The manifest records its name, `runtime=python`, voice phrase, aliases and source SHA-256. Mismatched source/manifest pairs are rejected. Use `save_python_skill` to update source and hash together. Skill names use 1–31 lowercase letters, digits, `_` or `-` and cannot replace a board skill. Local state is private under `.telly/python_state/<name>.json`.

The five MCP tools are:

- `save_python_skill(name, voice_phrase, source, run=False)` saves source and optionally starts it.
- `list_python_skills()` lists saved laptop programs.
- `run_python_skill(name)` returns a Python job ID immediately.
- `python_skill_status(job_id)` returns status, result, captured stdout or error.
- `stop_python_skill(job_id)` terminates that Python process and attempts to stop only board jobs it created.

Install the pinned root requirements, then restart the gateway/MCP process to load changed host code. No PlatformIO upload is required. `/python/control` is local-only and requires the gateway control token; the public tunnel token does not grant access. Voice-triggered jobs inherit their voice epoch so local wake cancellation remains effective for their board calls. An in-flight network action may already have executed when cancellation/timeout occurs; failed transport is never grounds to retry it automatically.

## Python interface

The execution namespace provides `telly`, selected `math` functions (`sqrt`, `sin`, `cos`, `pi`, `floor`, `ceil`, `isfinite`) and `statistics` (`mean`, `median`, `stdev`, `pstdev`). Ordinary functions, collections, comprehensions and conditions are supported. Imports, type annotations and `str.format/format_map` are unavailable; use f-strings or string concatenation. Unsupported formatting is rejected before hardware execution. Set a top-level `result` to a JSON-compatible value; `print()` output is captured separately.

| Call | Result |
| --- | --- |
| `telly.run({"steps": [...]})` | Starts a validated board program and returns its board job ID |
| `telly.wait(job_id, timeout=10)` | Waits for a job created by this script; returns its parsed status/result |
| `telly.sensors()` | Reads and returns the sensors dictionary |
| `telly.show(text)` | Displays text through the board feedback endpoint |
| `telly.rgb(r=0, g=0, b=0, brightness=5)` | Starts an RGB set job and returns its ID |
| `telly.sleep(seconds)` | Pauses this worker for 0–5 seconds per call |
| `telly.load()` / `telly.save(value)` | Loads/replaces this skill's JSON state on the laptop |

For example, the saved [sensor-average program](../skills/python/python_sensor_average.py) reads actual sensor values three times and computes their mean. Its branches and computation execute in Python; each sensor read and final display use MCP. 

## Execution limits

At most four Python processes run concurrently, each with a 60-second wall-time limit, 256 MiB process memory cap, 16000 UTF-8 bytes of source, 4096 captured-output characters, a bounded result protocol and 128 hardware calls. The memory cap uses Windows Job Objects or POSIX resource limits. Only the most recent 64 completed/running job records are retained approximately; process restart discards job records, but saved source and state remain. Skills have 16 KiB of persistent JSON state each. The four board job slots and hardware remain shared across all callers; wait for short jobs before submitting more. Four Python processes do not create four extra board slots.

The child process receives no API keys or board/gateway tokens. RestrictedPython compilation, guarded builtins and the supplied capability API exclude direct host files, shell/process execution, arbitrary imports, raw sockets and arbitrary Wi-Fi frames. This is a restricted Python environment, not unrestricted CPython and not a hardened OS security sandbox. Restrictions reduce accidental damage; do not treat them as proof that hostile arbitrary code is safely contained. See [RestrictedPython's security model](https://restrictedpython.readthedocs.io/en/latest/usage/security_considerations.html).

Gateway restart interrupts Python workers; it does not resume or replay their hardware actions. Cleanup and result feedback can fail when Wi-Fi/epoch authorization is unavailable. Camera preview jobs intentionally started with indefinite duration can continue after successful script completion; explicitly stop them when needed.

