"""One authenticated persistent socket for audio, board calls and saved media."""

import asyncio
import contextlib
import hmac
import json
import os
import re
import threading
import time

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from board_relay import relay
from local_stt import MAX_WAV_BYTES, validate_wav
from voice_policy import classify_transcript


def valid_metadata(item):
    if not isinstance(item, dict) or item.get("type") not in ("audio", "media"):
        raise ValueError("Unknown binary message")
    if not isinstance(item.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", item["id"]):
        raise ValueError("Invalid capture ID")
    limit = MAX_WAV_BYTES if item["type"] == "audio" else 65536
    if type(item.get("bytes")) is not int or not 0 < item["bytes"] <= limit:
        raise ValueError("Invalid binary size")
    if item["type"] == "audio" and (
        type(item.get("epoch")) is not int or not 0 <= item["epoch"] <= 0xFFFFFFFF
    ):
        raise ValueError("Invalid voice epoch")
    return item


def create_app(port, token, speech, accept_text, media):
    connection = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
            app.state.upstream = client
            yield

    async def board_socket(ws: WebSocket):
        if not token or not hmac.compare_digest(ws.headers.get("x-voice-gateway-token", ""), token):
            await ws.close(code=1008)
            return
        if connection.locked():
            await ws.close(code=1013)
            return
        async with connection:
            protocol = (
                "arduino"
                if "arduino" in ws.headers.get("sec-websocket-protocol", "").split(", ")
                else None
            )
            await ws.accept(subprotocol=protocol)
            relay.online = True
            relay.activity_dirty = True
            relay.changed.set()
            sending = asyncio.Lock()
            jobs = set()
            seen = {}
            pending = None

            async def send(value):
                async with sending:
                    await ws.send_json(value)

            async def commands():
                while True:
                    command = await asyncio.to_thread(relay.poll, 0.15)
                    if command:
                        await send({"type": "command", **command})

            async def audio(meta, raw):
                try:
                    started = time.monotonic()
                    result = await asyncio.to_thread(speech.transcribe, raw)
                    text = result["text"]
                    usable = classify_transcript(text, manual=True) == "command"
                    # Transcript is returned only for the device console. The same
                    # host immediately dispatches it; the board never posts it back.
                    await send(
                        {
                            "type": "reply",
                            "id": meta["id"],
                            "text": text,
                            "ok": True,
                            "accepted": usable,
                            "inference_ms": result["inference_ms"],
                        }
                    )
                    if usable:
                        accept_text(text, meta["epoch"], meta["id"], started)
                    print("[speech] " + json.dumps(result, ensure_ascii=True), flush=True)
                except Exception as exc:
                    await send(
                        {
                            "type": "reply",
                            "id": meta["id"],
                            "ok": False,
                            "error": "Speech failed: " + type(exc).__name__,
                        }
                    )

            writer = asyncio.create_task(commands())
            try:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    if message.get("bytes") is not None:
                        raw = message["bytes"]
                        if pending is None or len(raw) != pending["bytes"]:
                            raise ValueError("Unexpected binary payload")
                        meta, pending = pending, None
                        if meta["type"] == "audio":
                            validate_wav(raw)
                            if len(jobs) >= 1:
                                await send(
                                    {
                                        "type": "reply",
                                        "id": meta["id"],
                                        "ok": False,
                                        "error": "Speech busy",
                                    }
                                )
                                continue
                            task = asyncio.create_task(audio(meta, raw))
                            jobs.add(task)
                            task.add_done_callback(jobs.discard)
                        else:
                            result = await asyncio.to_thread(media.frame, meta, raw)
                            await send({"type": "reply", "id": meta["id"], "ok": True, **result})
                    elif message.get("text") is not None:
                        if len(message["text"]) > 16000:
                            raise ValueError("Oversized control message")
                        data = json.loads(message["text"])
                        if not isinstance(data, dict):
                            raise ValueError("Expected object")
                        kind = data.get("type")
                        if kind in ("audio", "media"):
                            if pending is not None:
                                raise ValueError("Previous payload incomplete")
                            valid_metadata(data)
                            if data["id"] in seen:
                                raise ValueError("Capture replay")
                            if len(seen) >= 512:
                                seen.pop(next(iter(seen)))
                            seen[data["id"]] = True
                            pending = data
                        elif kind == "result":
                            if len(data.get("body", "")) > 16000:
                                raise ValueError("Oversized board result")
                            relay.complete(data)
                        elif kind == "media.finish":
                            result = await asyncio.to_thread(media.finish, data)
                            await send({"type": "reply", "id": data["id"], "ok": True, **result})
                        else:
                            raise ValueError("Unknown control message")
            except (WebSocketDisconnect, OSError):
                pass
            except (ValueError, KeyError, TypeError):
                with contextlib.suppress(Exception):
                    await ws.close(code=1008)
            finally:
                relay.disconnect()
                writer.cancel()
                for task in jobs:
                    task.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError, WebSocketDisconnect, RuntimeError, OSError
                ):
                    await writer

    async def proxy(request: Request):
        path = request.url.path
        if path in ("/board/request", "/python/control"):
            if request.client.host not in ("127.0.0.1", "::1") or request.headers.get(
                "cf-connecting-ip"
            ):
                return Response(status_code=403)
        size = request.headers.get("content-length", "0")
        if not size.isdigit() or int(size) > 65536 or request.headers.get("transfer-encoding"):
            return Response(status_code=413)
        raw = await request.body()
        if len(raw) > 65536:
            return Response(status_code=413)
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "connection", "content-length")
        }
        response = await request.app.state.upstream.request(
            request.method, f"http://127.0.0.1:{port + 1}" + path, content=raw, headers=headers
        )
        return Response(
            response.content, status_code=response.status_code, media_type="application/json"
        )

    return Starlette(
        lifespan=lifespan,
        routes=[
            WebSocketRoute("/board/ws", board_socket),
            Route("/{path:path}", proxy, methods=["GET", "POST"]),
        ],
    )


def start_realtime(port, token, speech, accept_text, media):
    relay.online = False
    app = create_app(port, token, speech, accept_text, media)
    config = uvicorn.Config(
        app,
        host=os.environ.get("TELLY_VOICE_GATEWAY_BIND", "127.0.0.1"),
        port=port,
        ws="websockets",
        ws_max_size=MAX_WAV_BYTES,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True, name="telly-realtime").start()
    return server
