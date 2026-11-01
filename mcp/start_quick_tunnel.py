"""Start a Cloudflare Quick Tunnel and give its temporary URL to K10 over USB."""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\b")
DEFAULT_CLOUDFLARED = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"


def find_port(requested: str | None) -> str | None:
    if requested:
        return requested
    try:
        from serial.tools import list_ports

        ports = [
            p.device
            for p in list_ports.comports()
            if "USB" in p.description.upper() or "ESP" in p.description.upper()
        ]
        return ports[0] if len(ports) == 1 else None
    except ImportError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="K10 USB serial port, for example COM5")
    parser.add_argument("--gateway-port", type=int, default=8767)
    parser.add_argument(
        "--protocol",
        choices=("auto", "quic", "http2"),
        default="auto",
        help="Use http2 if the network blocks UDP",
    )
    parser.add_argument("--cloudflared", default=shutil.which("cloudflared") or DEFAULT_CLOUDFLARED)
    args = parser.parse_args()
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:{}/health".format(args.gateway_port), timeout=3
        ) as response:
            if response.status != 200:
                raise RuntimeError("gateway health check failed")
    except Exception as exc:
        print("Local MCP voice gateway is unavailable: {}".format(exc), file=sys.stderr)
        print(
            "Start Codex MCP with TELLY_VOICE_GATEWAY_TOKEN and UNIHIKER_CONNECTION=relay first.",
            file=sys.stderr,
        )
        return 2
    if not os.path.isfile(args.cloudflared):
        print("cloudflared.exe not found: {}".format(args.cloudflared), file=sys.stderr)
        return 2

    process = subprocess.Popen(
        [
            args.cloudflared,
            "tunnel",
            "--protocol",
            args.protocol,
            "--url",
            "http://127.0.0.1:{}".format(args.gateway_port),
            "--no-autoupdate",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    lines: queue.Queue[str] = queue.Queue(maxsize=256)

    def read_output():
        assert process.stderr
        for line in process.stderr:
            try:
                lines.put_nowait(line)
            except queue.Full:
                pass

    threading.Thread(target=read_output, daemon=True).start()
    url = None
    registered = False
    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline and process.poll() is None:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                continue
            match = URL_PATTERN.search(line)
            if match:
                url = match.group(0)
            if "Registered tunnel connection" in line:
                registered = True
            if url and registered:
                break
            if "error" in line.lower():
                print(line.rstrip(), file=sys.stderr)
        if not url or not registered:
            print(
                "Cloudflare did not establish a tunnel. Try --protocol http2 or check outbound port 7844.",
                file=sys.stderr,
            )
            return 2
        print("Temporary relay URL: {}".format(url), flush=True)
        port = find_port(args.port)
        if port:
            try:
                import serial

                board = serial.Serial(port=None, baudrate=115200, timeout=2, write_timeout=3)
                board.dtr = False
                board.rts = False
                board.port = port
                with board:
                    time.sleep(5)
                    board.write(
                        (
                            "TELLY_TIME "
                            + str(int(time.time()))
                            + "\nTELLY_RELAY_URL "
                            + url
                            + "\n"
                        ).encode("ascii")
                    )
                    board.flush()
                    reply = ""
                    ack_deadline = time.monotonic() + 12
                    while (
                        time.monotonic() < ack_deadline and ("Relay URL saved: " + url) not in reply
                    ):
                        reply += board.readline().decode("utf-8", errors="replace")
                if ("Relay URL saved: " + url) not in reply:
                    print(
                        "USB command sent to {}; verify the board Serial Monitor acknowledged it.".format(
                            port
                        ),
                        file=sys.stderr,
                    )
                else:
                    print("Board saved URL through {}.".format(port), flush=True)
            except Exception as exc:
                print("Could not send URL through {}: {}".format(port, exc), file=sys.stderr)
                print(
                    "Send this line manually at 115200 baud: TELLY_RELAY_URL {}".format(url),
                    flush=True,
                )
        else:
            print(
                "Send this line to board Serial Monitor at 115200 baud: TELLY_RELAY_URL {}".format(
                    url
                ),
                flush=True,
            )
            print("Use --port COMx for automatic USB setup.", file=sys.stderr)
        print("Tunnel is active; press Ctrl+C to stop it.", flush=True)
        return process.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
