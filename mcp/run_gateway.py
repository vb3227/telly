"""Run Telly's gateway independently of a desktop MCP connection.
Environment matches server.py. --codex-config imports only this project's MCP env.
"""

import argparse
import asyncio
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-config", action="store_true")
    parser.add_argument(
        "--local-stt", action="store_true", help="Warm CPU Whisper before serving audio uploads"
    )
    args = parser.parse_args()
    if args.local_stt:
        os.environ["TELLY_LOCAL_STT"] = "1"
    if args.codex_config:
        import tomllib

        config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8"))
        env = config.get("mcp_servers", {}).get("esp32-school-robot", {}).get("env", {})
        for key, value in env.items():
            if key in ("UNIHIKER_CONNECTION", "UNIHIKER_BOARD_TOKEN", "TELLY_VOICE_GATEWAY_TOKEN"):
                os.environ.setdefault(key, value)
    import server
    from voice_gateway import start_voice_gateway

    async def run():
        shared = start_voice_gateway(server.mcp, asyncio.get_running_loop())
        if shared:
            raise RuntimeError("A gateway is already running")
        print("Telly gateway running; planners=fast+codex; transport=websocket", flush=True)
        await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
