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
METRICS_PATTERN = re.compile(r"127\.0\.0\.1:(\d+)/metrics")
RESTART = 75


class TunnelWatchdog:
    def __init__(self, grace=45):
        self.grace = grace
        self.failed_at = None

    def observe(self, ready, now):
        if ready:
            self.failed_at = None
        elif self.failed_at is None:
            self.failed_at = now
        return self.failed_at is not None and now - self.failed_at >= self.grace


def endpoint_ready(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def send_board_url(port, url):
    if not port:
        return False
    try:
        import serial

        board = serial.Serial(port=None, baudrate=115200, timeout=0.3, write_timeout=3)
        board.dtr = False
        board.rts = False
        board.port = port
        with board:
            time.sleep(5)
            board.write(
                ("TELLY_TIME " + str(int(time.time())) + "\nTELLY_RELAY_URL " + url + "\n").encode(
                    "ascii"
                )
            )
            board.flush()
            reply = ""
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                reply = (reply + board.readline().decode("utf-8", errors="replace"))[-4096:]
                if "Relay URL saved: " + url in reply:
                    print("Board saved URL through {}.".format(port), flush=True)
                    return True
        print("USB update not acknowledged; will retry.", file=sys.stderr, flush=True)
    except Exception as exc:
        print(
            "USB update pending (close Serial Monitor): {}".format(type(exc).__name__),
            file=sys.stderr,
            flush=True,
        )
    return False


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


def run_tunnel(args) -> int:
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
            "--metrics",
            "127.0.0.1:0",
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
    readiness = None
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
            metrics = METRICS_PATTERN.search(line)
            if metrics and 1 <= int(metrics.group(1)) <= 65535:
                readiness = "http://127.0.0.1:" + metrics.group(1) + "/ready"
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
            return RESTART
        print("Temporary relay URL: {}".format(url), flush=True)
        port = find_port(args.port)
        configured = send_board_url(port, url)
        if not port:
            print(
                "Send this line to board Serial Monitor at 115200 baud: TELLY_RELAY_URL {}".format(
                    url
                ),
                flush=True,
            )
            print("Use --port COMx for automatic USB setup.", file=sys.stderr)
        print("Tunnel watchdog active; press Ctrl+C to stop it.", flush=True)
        watchdog = TunnelWatchdog()
        next_check = time.monotonic() + 10
        while process.poll() is None:
            try:
                line = lines.get(timeout=1)
                if "error" in line.lower():
                    print(line.rstrip(), file=sys.stderr, flush=True)
            except queue.Empty:
                pass
            now = time.monotonic()
            if now < next_check:
                continue
            next_check = now + 10
            if watchdog.observe(endpoint_ready(readiness or url + "/health"), now):
                print(
                    "Tunnel lost its edge connection; restarting and refreshing board URL.",
                    flush=True,
                )
                return RESTART
            if not configured:
                configured = send_board_url(port or find_port(args.port), url)
        return RESTART
    except KeyboardInterrupt:
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="K10 USB serial port")
    parser.add_argument("--gateway-port", type=int, default=8767)
    parser.add_argument("--protocol", choices=("auto", "quic", "http2"), default="auto")
    parser.add_argument("--cloudflared", default=shutil.which("cloudflared") or DEFAULT_CLOUDFLARED)
    args = parser.parse_args()
    if not 1024 <= args.gateway_port <= 65534:
        parser.error("Gateway port must be 1024..65534")
    try:
        while True:
            result = run_tunnel(args)
            if result != RESTART:
                return result
            time.sleep(5)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
