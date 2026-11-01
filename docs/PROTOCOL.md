# Protocol

The board connects outward to `/board/ws` using TLS certificate validation and `X-Voice-Gateway-Token`. Only one device connection is active per gateway. Heartbeats detect a dead link; reconnect never replays an uncertain action.

- `audio`: JSON metadata with `id`, `epoch`, `bytes`, immediately followed by one WAV binary message (maximum 320044 bytes). IDs are bounded and checked for recent replay.
- `reply`: upload/transcription result. Transcript text is for device feedback only; the host already dispatches accepted commands.
- `command`: host-to-device MCP call with an opaque request ID, allowlisted path, method, bounded JSON body, optional voice epoch/capture ID and coalesced UI events.
- `result`: board response for a currently pending call. Unknown response IDs cannot complete another call.
- `media`: metadata with mode, session, index, offset and length followed by one JPEG (maximum 65536 bytes).
- `media.finish`: seal a video file. Media is stored without model analysis.

Authenticated device-local endpoints provide `/run`, `/stop`, `/status`, `/skills/install`, `/skills/run`, `/skills/list`, `/voice/start`, `/voice/finish`, `/voice/latest`, `/voice/result`, `/voice/activity` and `/media/start|stop|status`. Direct LAN control remains available. No analysis/video-understanding endpoints remain.

`/run` accepts a validated steps object or `{program: <steps object>, feedback: <short text>}` to combine execution and screen acknowledgment in one exchange. Both host and firmware validate the program. Voice epochs reject stale actions. Four device slots remain shared by every caller.

Laptop `/board/request` and `/python/control` require a separate local control token and reject tunnel-originated requests. Text `/voice` remains an authenticated API input for explicit commands. HTTP is not the board's transport anymore. Public WebSocket/media/audio sizes and queues are bounded; invalid protocol input closes or rejects the request.
