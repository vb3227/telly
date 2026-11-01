# Reusable skills

The root contains canonical JSON device skills with short English native phrases and Russian host aliases. Count/color/timing changes and compositions are expanded into a validated program without writing another skill file. Examples: `blink red twice`, `мигни красным два раза`, `show sensors and blink red twice`.

New genuinely complex behaviors save a short trigger and aliases for reuse. JSON skills live in device NVS and on the host; Python skills under `python/` run on the laptop through the restricted `telly` API. The latter are not executable as Python on the board. See [Python skills](../docs/PYTHON_SKILLS.md).

The installed vendor ASR model determines native wake/language behavior. Jarvis prefixes and Russian aliases are host routing features; they do not retrain that model.
