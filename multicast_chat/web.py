import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote_to_bytes

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .protocol import hex_bytes
from .room import Room, RoomError
from .storage import Storage
from .transfers import Transfers

STATIC = Path(__file__).parent / "static"


async def index(request):
    return FileResponse(STATIC / "index.html", headers={
        "Content-Security-Policy": "default-src 'self'; object-src 'none'; frame-ancestors 'none'",
        "Cache-Control": "no-store",
    })


async def state(request):
    return JSONResponse(request.app.state.room.snapshot(), headers={"Cache-Control": "no-store"})


async def events(request):
    room = request.app.state.room

    async def stream():
        previous = None
        while True:
            changed = room.changed
            snapshot = json.dumps(room.snapshot(), ensure_ascii=False, separators=(",", ":"))
            if snapshot != previous:
                yield f"event: state\ndata: {snapshot}\n\n"
                previous = snapshot
            try:
                await asyncio.wait_for(changed.wait(), timeout=12)
            except TimeoutError:
                yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def local_write(request):
    return request.headers.get("x-local-chat") == "1" and request.headers.get("origin") in (
        None, str(request.base_url).rstrip("/"),
    )


async def small_json(request):
    body = bytearray()
    async for part in request.stream():
        if len(body) + len(part) > 8192:
            raise RoomError("请求体过大")
        body.extend(part)
    value = json.loads(body or b"{}")
    if not isinstance(value, dict):
        raise RoomError("请求体必须为 JSON 对象")
    return value


async def action(request):
    if not local_write(request):
        return JSONResponse({"error": "仅接受本地页面的写入请求"}, 403)
    room = request.app.state.room
    operation = request.path_params["operation"]
    try:
        value = await small_json(request)
        if operation == "create":
            room.create()
        elif operation == "join":
            room.join(value["invite"])
        elif operation == "approve":
            room.approve(hex_bytes(value["id"], 32))
        elif operation == "remove":
            room.remove(hex_bytes(value["id"], 32))
        elif operation == "leave":
            room.leave()
        elif operation == "chat":
            room.send_chat(value["text"])
        elif operation == "contact":
            room.send_contact(value.get("invite"), value["text"])
        else:
            return JSONResponse({"error": "操作不存在"}, 404)
        return JSONResponse({"ok": True})
    except RoomError as exc:
        return JSONResponse({"error": str(exc)}, 409)
    except (ValueError, KeyError, TypeError, IndexError, RecursionError, AttributeError):
        return JSONResponse({"error": "请求参数无效"}, 400)
    except OSError:
        return JSONResponse({"error": "无法保存本地房间状态"}, 507)


async def upload(request):
    if not local_write(request):
        return JSONResponse({"error": "仅接受本地页面的写入请求"}, 403)
    room = request.app.state.room
    length = request.headers.get("content-length")
    if length and length.isdecimal() and int(length) > room.config.file_limit:
        return JSONResponse({"error": "文件超过此节点配置的大小上限"}, 413)
    try:
        name = unquote_to_bytes(request.headers["x-file-name"]).decode("utf-8")
        identity = await room.files.upload(name, request.stream())
        return JSONResponse({"id": identity}, 202)
    except RoomError as exc:
        return JSONResponse({"error": str(exc)}, 409)
    except (ValueError, KeyError, ClientDisconnect):
        return JSONResponse({"error": "文件名无效或上传已中断"}, 400)
    except OSError:
        return JSONResponse({"error": "无法写入本地文件"}, 507)


async def download(request):
    info = request.app.state.room.files.get(request.path_params["identity"])
    mode = request.path_params["mode"]
    inline = mode == "play"
    if info is None or mode not in ("play", "download") or (inline and info.mime is None):
        return JSONResponse({"error": "文件尚未校验或当前不可用"}, 404)
    return FileResponse(info.path, filename=info.name,
                        media_type=info.mime if inline else "application/octet-stream",
                        content_disposition_type="inline" if inline else "attachment",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def create_app(config):
    @asynccontextmanager
    async def lifespan(app):
        storage = Storage(config.data_dir)
        try:
            room = Room(config, storage)
            room.files = Transfers(room)
            app.state.room = room
            await room.start()
            try:
                yield
            finally:
                await room.stop()
                await room.files.stop()
        finally:
            storage.close()

    return Starlette(lifespan=lifespan, routes=[
        Route("/", index), Route("/api/state", state), Route("/api/events", events),
        Route("/api/files", upload, methods=["POST"]),
        Route("/api/files/{identity}/{mode}", download, methods=["GET"]),
        Route("/api/{operation}", action, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC)),
    ], middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])])
