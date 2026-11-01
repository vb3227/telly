# Performance and validation

Telly is an experimental prototype. The current persistent WebSocket architecture removes the former audio/transcript round trip, but a complete speech-to-visible-action latency distribution has not yet been measured. Targets are not service guarantees.

## Current evidence — 2026-11-02

| Check | Observed result | What it establishes |
| --- | --- | --- |
| Automated host suite | 69 tests passed | Routing, validation, composition, Python workers, media and transport contracts |
| Python style | Ruff lint and format checks | Source consistency; not hardware correctness |
| Normal firmware build | PlatformIO `unihiker` succeeded | Current C++ compiled for K10 |
| Model recovery build | `unihiker_model_en` succeeded | Provisioning environment compiled with the vendor English ASR model |
| Hardware recovery | Uploaded through VS Code; board replied to MCP ping | The tested development board booted and accepted commands |
| Boot observation | Same voice epoch across two status samples, no reset markers during a 15-second Serial observation | No reboot observed in that short window |
| Credential/publication checks | Explicit source manifest; local credential and common-key-pattern scans | Checked publication contents; not a comprehensive security certification |

Tests use simulated hardware/provider responses unless explicitly described as a hardware or service check. The full suite needs local sockets and child processes; a restricted execution environment can prevent these tests from working.

## Speech-provider comparison

Three runs used the same freshly generated, non-sensitive 3.34-second English phrase: “Show all sensors and blink red three times.” The audio came from synthetic speech on the development laptop, not a recording of a private conversation.

| Run | Groq Whisper Turbo | Local multilingual Whisper tiny |
| --- | --- | --- |
| 1 | 404 ms | 1046 ms |
| 2 | 310 ms | 906 ms |
| 3 | 342 ms | 969 ms |

Both paths returned the intended words in these samples. Local inference used CPU INT8 with up to eight threads. Groq is the default on the host; local recognition remains optional. These timings are small synthetic samples, not a noisy-microphone accuracy benchmark or a universal ranking of providers. They exclude board recording, board-to-host audio transfer and final action delivery.

## What changed in the transport

The old implementation could serialize transcription and command forwarding behind shared HTTPS activity. A historical human test exposed approximately 20.54 seconds from completed capture to transcript-submission acknowledgment, despite much faster recognition itself. That measurement belongs to the retired transport.

The current path uploads one WAV through the persistent authenticated connection. The host transcribes and dispatches the text immediately; the board receives the transcript for logging and does not upload it again. Commands and results share that connection. A separate complex-work queue keeps saved commands available while Codex plans a new program.

This is an architectural reduction in work. It must not be presented as a measured under-five-second result for every spoken request.

## Reading the metrics

`GET /health` reports `last_request` with:

- `elapsed_ms`: host dispatch processing, beginning when a request is queued. For WebSocket audio, this queue timestamp is after transcription.
- `lookup_ms`: skill routing, including lightweight inference where applicable.
- `queue_ms`: time waiting for the selected dispatcher.
- `planner_ms`: Codex planning where applicable.
- `delivery_ms`: separately delivered final feedback, or null for combined operations.
- `route`, `success` and `router_error`: the actual routing/result category.

Host timings exclude at least part of the board upload and must not be described as complete speech-to-action latency. The firmware diagnostic `[voice timing] stop_to_action_ms` tracks a matching capture through action/feedback delivery; use it together with host logs and visible device behavior. Receiving a job ID means acceptance, not completion of every step in that job.

The green activity percentage is a UI progress estimate, not a measured fraction of model computation or a remaining-time guarantee.

## Acceptance targets and remaining work

Targets remain an existing command visible in roughly two seconds and a simple new task in roughly four to five seconds after recording stops. Network conditions, cold startup, provider load, command duration and complex code generation can exceed these targets. Codex has a longer planning deadline and cannot promise five-second completion for arbitrary programs.

Before reporting a release latency guarantee:

1. Measure several real A / speech / A requests in both Russian and English.
2. Separate exact saved commands, parameter changes, paraphrases, ordinary answers and genuinely new programs.
3. Record p50/p95 timings and failures from stop-of-recording to the first visible action, plus full job completion when relevant.
4. Include cold and warm starts, LAN and tunnel operation, and commands while another job is active.
5. Check microphone accuracy and false activation independently of synthetic provider timings.

No complete clean-machine installation or broad end-to-end hardware acceptance run is claimed. See [setup](GETTING_STARTED_RU.md), [testing](TESTING.md) and [architecture](ARCHITECTURE.md) for reproducible steps and boundaries.
