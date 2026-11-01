# Architecture

The K10 supervisor provides four cooperative JSON job slots, resource arbitration, NVS skills and native phrase ASR. Audio capture reads copies of the existing driver PCM stream. A is the only assigned capture button; B and combined capture have no action.

A single dedicated network task owns one authenticated WSS client. It sends bounded audio/media messages and services MCP commands through local device endpoints. The same connection returns transcripts for the console and execution acknowledgments. It reconnects, but never replays an ambiguously delivered action. No REST polling or shared HTTPS gate remains in the command path.

The laptop authenticates the socket before accepting payloads, checks WAV/JPEG metadata and bounds, and dispatches recognized speech directly. The transcript is not re-uploaded by the board. Local wake detection and voice epochs prevent stale cloud commands from overriding device-local commands.

The in-memory catalog indexes short English/Russian aliases. Exact matches, supported parameter changes and clear compositions use no model. The lightweight tool-free router receives the complete catalog once per inference request. It can answer, select, compose or propose a bounded new behavior. The host validates every proposed plan. Changes to counts/colors and compositions of saved behavior execute without saving a duplicate.

Complex tasks run in a separate bounded queue using one persistent Codex session. This leaves fast commands available during code generation. Genuinely new JSON skills save/run atomically on the board; laptop Python skills run in separate restricted processes and remain stored on the host. Short commands are saved with each new skill.

Media capture only saves files. Photos are JPEGs; video is silent MJPEG AVI with actual frame timestamps in a JSON sidecar. Sampling is capped at 5 FPS / 50 frames / ten seconds and may be lower on a slow link. There is no visual model, person memory, ambient recording service or analysis endpoint.

The external ASGI service and loopback-only MCP/control service are in the same gateway process. Local control routes require a separate token and reject requests arriving through the public tunnel. Source publication is independent from runtime data and device credentials.
