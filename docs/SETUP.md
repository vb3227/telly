# Setup

For a first installation from GitHub, use the [complete Russian walkthrough](GETTING_STARTED_RU.md), including the credential matrix, fresh-board skill provisioning and acceptance checklist. This page is a compact reference.

Use a UNIHIKER K10, data USB cable, compatible 2.4 GHz Wi-Fi, Python 3.11+ and VS Code with PlatformIO. Complex generation requires a working, signed-in Codex CLI. The default speech/router provider uses a laptop Groq key; optional local speech uses faster-whisper.

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Copy `include/board_config.example.h` to `include/board_config.h`. Set Wi-Fi, board token and gateway token. Keep these local. The laptop and board must agree on their tokens. Board firmware no longer sends requests to Groq; configure `GROQ_API_KEY` on the laptop. An existing ignored private header can still supply that key for compatibility.

Merge [the MCP example](../config/mcp.example.toml) into the client configuration. Point its command at the chosen Python interpreter and its script path at `mcp/server.py`. With the gateway running, MCP clients share it instead of starting another device queue.

```powershell
.venv/Scripts/python.exe mcp/run_gateway.py --codex-config
```

This imports the relevant token/connection environment from the named Codex MCP server. Alternatively set the environment directly and omit `--codex-config`. Default public port: 8767; loopback control port: 8768. Health reports the transport, selected speech provider and device connection state. Stop an existing gateway before replacing it.

For different networks or Wi-Fi client isolation, keep `cloudflared` installed and run with Serial Monitor closed:

```powershell
.venv/Scripts/python.exe mcp/start_quick_tunnel.py --port COM5 --protocol http2
```

Replace COM5 with the board's actual serial port. The helper writes the temporary HTTPS hostname and current clock to the board. Keep it running; a replacement tunnel needs its new address provisioned again over USB. The board upgrades `/board/ws` to WSS through this same hostname.

Build through VS Code PlatformIO, authorize Upload and check success/hash verification. Serial Monitor uses 115200 baud. Normal `unihiker` uploads use `Model=None` and preserve the vendor ASR model partition. For a fresh board or `[asr] English wake/command model missing`, select `unihiker_model_en` in the PlatformIO environment switcher and Upload once, then return to `unihiker`. This uses model files from the installed vendor framework; they are not bundled in the repository. Missing models are checked before entering the vendor recognizer so the board can keep network control available.

For a trusted local LAN without a tunnel, set the private `VOICE_GATEWAY_URL` to `http://<laptop-address>:8767` and host `TELLY_VOICE_GATEWAY_BIND=0.0.0.0`. This selects plain WebSocket on the LAN and needs the board to reach the laptop directly. An NVS tunnel URL takes priority. Local MCP/control HTTP remains loopback-only even when the public frontend binds the LAN.

Check `/health`, MCP `ping_unihiker`, sensor display and a parameterized LED command. A/A submits manual speech; B is unassigned. Camera media starts through a voice command or MCP. Recognition is clip-based after stopping, not streaming. See [speech](SPEECH.md) and [performance](PERFORMANCE.md).
