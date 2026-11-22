import asyncio
import hashlib
import itertools
import json
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .protocol import Kind, hex_bytes, wire_json
from .room import RoomError

log = logging.getLogger("multicast.socket")
CHUNK = 1000
INDEX = struct.Struct("!I")
SPAN = struct.Struct("!II")
MAX_REPAIR_QUEUE = 8192
MAX_CONCURRENT = 12
MAX_LISTED = 60
IDLE_TIMEOUT = 25


def filename(value):
    if (not isinstance(value, str) or not 1 <= len(value.encode()) <= 240
            or value in (".", "..") or any(char in "/\\" or ord(char) < 32 or ord(char) == 127
                                           for char in value)):
        raise ValueError("文件名必须为 1–240 个 UTF-8 字节，且不能包含路径分隔符或控制字符")
    return value


def verify_file(path):
    with path.open("rb") as stream:
        return os.fstat(stream.fileno()).st_size, hashlib.file_digest(stream, "sha256").digest()


@dataclass(slots=True)
class FileInfo:
    sender: bytes
    id: bytes
    name: str
    size: int
    sha: bytes
    path: Path
    status: str
    sent_bytes: int = 0
    error: str = ""
    valid: bool = False
    created: float = field(default_factory=time.monotonic)

    @property
    def key(self):
        return self.sender.hex() + "-" + self.id.hex()

    @property
    def count(self):
        return (self.size + CHUNK - 1) // CHUNK

    @property
    def mime(self):
        return {".wav": "audio/wav", ".webm": "video/webm"}.get(Path(self.name).suffix.lower())

    def manifest(self):
        return wire_json([self.id.hex(), self.name, self.size, self.sha.hex(), CHUNK])


@dataclass(slots=True)
class Incoming:
    info: FileInfo
    stream: BinaryIO
    received: bytearray
    last_progress: float = field(default_factory=time.monotonic)
    next_request: float = 0
    last_receipt: float = -float("inf")
    ended: bool = False
    verifying: bool = False


@dataclass(slots=True)
class Outgoing:
    info: FileInfo
    expected: set
    received: set = field(default_factory=set)
    repair: set = field(default_factory=set)
    task: asyncio.Task | None = None


def missing_spans(received):
    for missing, group in itertools.groupby(enumerate(received), key=lambda pair: not pair[1]):
        if missing:
            first, _ = next(group)
            yield first, 1 + sum(1 for _ in group)


class Transfers:
    def __init__(self, room):
        self.room = room
        self.root = room.storage.files
        self.entries = {}
        self.incoming = {}
        self.outgoing = {}
        self.tasks = set()
        self.interval = CHUNK / (room.config.rate_kib * 1024)
        self.pace_lock = asyncio.Lock()
        self.next_send = 0

    def schedule(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def interrupt(self):
        for transfer in self.outgoing.values():
            if transfer.info.status in ("sending", "waiting"):
                self.fail(transfer.info, "成员状态或密钥已变更，请重新发送")
            transfer.task.cancel()
        self.outgoing.clear()
        for transfer in list(self.incoming.values()):
            if transfer.info.status in ("receiving", "verifying"):
                self.fail_incoming(transfer, "成员状态或密钥已变更，请重新接收")

    def reset(self):
        self.interrupt()
        self.incoming.clear()
        self.entries.clear()

    async def stop(self):
        self.reset()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def snapshot(self):
        return [{
            "id": info.key, "name": info.name, "size": info.size, "bytes": info.sent_bytes,
            "status": info.status, "error": info.error, "mime": info.mime,
            "outgoing": info.sender == self.room.storage.id, "download": info.valid,
            "sha256": info.sha.hex() if info.valid else None,
            "confirmed": len(outgoing.expected & outgoing.received) if outgoing else None,
            "expected": len(outgoing.expected) if outgoing else None,
        } for info, outgoing in reversed(list(self.entries.values()))]

    def list_file(self, info, outgoing=None):
        self.entries[info.key] = info, outgoing
        if len(self.entries) > MAX_LISTED:
            for key, (previous, _) in self.entries.items():
                if previous.status in ("complete", "failed") and key not in self.outgoing:
                    self.entries.pop(key)
                    self.incoming.pop(key, None)
                    break
        self.room.notify()

    def get(self, key):
        entry = self.entries.get(key)
        if entry and entry[0].valid:
            return entry[0]
        return None

    async def upload(self, name, chunks):
        self.room.require_ready()
        name = filename(name)
        if len(self.outgoing) >= MAX_CONCURRENT:
            raise RoomError("发送任务过多，请等待当前任务结束")
        room_epoch = self.room.room_id, self.room.epoch
        identity = os.urandom(16)
        path = self.root / f"sent-{identity.hex()}.bin"
        partial = path.with_suffix(".part")
        sha = hashlib.sha256()
        size = 0
        try:
            with partial.open("xb") as stream:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.room.config.file_limit:
                        raise RoomError("文件超过此节点配置的大小上限")
                    stream.write(chunk)
                    sha.update(chunk)
            self.room.require_ready()
            if (self.room.room_id, self.room.epoch) != room_epoch:
                raise RoomError("上传期间房间状态已变更")
            if len(self.outgoing) >= MAX_CONCURRENT:
                raise RoomError("发送任务过多，请等待当前任务结束")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
        info = FileInfo(self.room.storage.id, identity, name, size, sha.digest(), path, "sending",
                        valid=True)
        transfer = Outgoing(info, self.room.active_peers())
        self.outgoing[info.key] = transfer
        self.list_file(info, transfer)
        transfer.task = self.schedule(self.send_file(transfer))
        return info.key

    async def pace(self):
        async with self.pace_lock:
            now = time.monotonic()
            if self.next_send - now > self.interval * 7:
                await asyncio.sleep(self.next_send - now)
            self.next_send = max(self.next_send, time.monotonic()) + self.interval

    def announce(self, info):
        self.room.send_data(Kind.MANIFEST, info.manifest(), f"file={info.id.hex()[:12]}")

    async def block(self, info, stream, index, *, repair=False, duplicate=False):
        await self.pace()
        stream.seek(index * CHUNK)
        data = stream.read(CHUNK)
        detail = ""
        if index in (0, 2, 17) or repair and index < 25:
            detail = f"file={info.id.hex()[:12]} block={index} phase={'repair' if repair else 'initial'}"
        self.room.send_data(Kind.BLOCK, info.id + INDEX.pack(index) + data, detail, duplicate)
        return len(data)

    async def send_file(self, transfer):
        info = transfer.info
        fault = self.room.config.fault_demo
        try:
            self.announce(info)
            with info.path.open("rb") as stream:
                for start in range(0, info.count, 8):
                    group = range(start, min(start + 8, info.count))
                    if fault:
                        group = reversed(group)
                        if start == 0:
                            log.info("FAULT pid=%d file=%s reversing block order in batches of 8",
                                     os.getpid(), info.id.hex()[:12])
                    for index in group:
                        if fault and index in (2, 17):
                            info.sent_bytes += min(CHUNK, info.size - index * CHUNK)
                            log.info("FAULT pid=%d file=%s dropped initial block=%d",
                                     os.getpid(), info.id.hex()[:12], index)
                            continue
                        info.sent_bytes += await self.block(info, stream, index, duplicate=fault and index == 1)
                    if start and start % 256 == 0:
                        self.announce(info)
                self.announce(info)
                self.room.send_data(Kind.END, info.id)
                info.status = "waiting"
                self.check_receipts(transfer)
                self.room.notify()
                expiry = time.monotonic() + max(40, min(300, 30 + info.count * self.interval * 2))
                next_announce = time.monotonic() + 1.3
                while time.monotonic() < expiry:
                    now = time.monotonic()
                    if now >= next_announce and info.status == "waiting":
                        self.announce(info)
                        next_announce = now + 1.3
                    if transfer.repair:
                        batch = sorted(transfer.repair)
                        for index in batch:
                            await self.block(info, stream, index, repair=True)
                            transfer.repair.discard(index)
                    else:
                        await asyncio.sleep(0.12)
                if info.status == "waiting":
                    self.fail(info, "等待接收校验确认超时")
        except (OSError, RoomError) as exc:
            if info.status != "complete":
                self.fail(info, str(exc))
        finally:
            self.outgoing.pop(info.key, None)

    def check_receipts(self, transfer):
        if transfer.info.status == "waiting" and transfer.expected <= transfer.received:
            transfer.info.status = "complete"

    def fail(self, info, reason):
        info.status, info.error = "failed", reason
        self.room.notify()

    def fail_incoming(self, transfer, reason):
        self.fail(transfer.info, reason)
        try:
            transfer.stream.close()
            if not transfer.verifying:
                transfer.info.path.with_suffix(".part").unlink(missing_ok=True)
        except OSError:
            log.exception("Could not clean up incomplete file")

    def receive(self, header, body):
        kind = header.kind
        if kind == Kind.MANIFEST:
            self.manifest(header.sender, body)
            return
        if len(body) < 16:
            raise ValueError("missing transfer ID")
        identity, payload = body[:16], body[16:]
        if kind in (Kind.MISSING, Kind.RECEIPT):
            key = self.room.storage.id.hex() + "-" + identity.hex()
            transfer = self.outgoing.get(key)
            if transfer is not None:
                self.handle_response(header.sender, transfer, kind, payload)
            return
        transfer = self.incoming.get(header.sender.hex() + "-" + identity.hex())
        if transfer is None:
            return
        if kind == Kind.BLOCK and transfer.info.status == "receiving":
            self.receive_block(transfer, header, payload)
        elif kind == Kind.END and not payload:
            transfer.ended = True
            self.receipt(transfer)

    def manifest(self, sender, body):
        identity, name, size, sha, chunk = json.loads(body)
        identity = hex_bytes(identity, 16)
        sha = hex_bytes(sha, 32)
        name = filename(name)
        if type(size) is not int or not 0 <= size <= self.room.config.file_limit or chunk != CHUNK:
            raise ValueError("invalid or oversized manifest")
        key = sender.hex() + "-" + identity.hex()
        if key in self.incoming:
            transfer = self.incoming[key]
            if (name, size, sha) == (transfer.info.name, transfer.info.size, transfer.info.sha):
                self.receipt(transfer)
            return
        if sum(item.info.status in ("receiving", "verifying") for item in self.incoming.values()) >= MAX_CONCURRENT:
            return
        path = self.root / f"recv-{key}.bin"
        info = FileInfo(sender, identity, name, size, sha, path, "receiving")
        try:
            stream = path.with_suffix(".part").open("wb")
        except OSError as exc:
            self.fail(info, str(exc))
            self.list_file(info)
            return
        transfer = Incoming(info, stream, bytearray(info.count),
                            next_request=time.monotonic() + 1.4)
        self.incoming[key] = transfer
        self.list_file(info)
        if not size:
            self.complete(transfer)

    def receive_block(self, transfer, header, payload):
        info = transfer.info
        if len(payload) < INDEX.size:
            return
        index, = INDEX.unpack_from(payload)
        if not 0 <= index < info.count or transfer.received[index]:
            return
        data = payload[INDEX.size:]
        if len(data) != min(CHUNK, info.size - index * CHUNK):
            return
        try:
            transfer.stream.seek(index * CHUNK)
            transfer.stream.write(data)
        except OSError as exc:
            self.fail_incoming(transfer, str(exc))
            return
        transfer.received[index] = 1
        transfer.last_progress = time.monotonic()
        info.sent_bytes += len(data)
        if index in (0, 2, 17):
            log.info("RX_BLOCK pid=%d sender=%s seq=%d epoch=%d joined=%s:%d file=%s block=%d",
                     os.getpid(), header.sender.hex()[:12], header.seq, header.epoch,
                     *self.room.data.destination, info.id.hex()[:12], index)
        if info.sent_bytes == info.size:
            self.complete(transfer)

    def complete(self, transfer):
        info = transfer.info
        try:
            transfer.stream.close()
        except OSError as exc:
            self.fail_incoming(transfer, str(exc))
            return
        info.status = "verifying"
        transfer.verifying = True
        self.schedule(self.verify(transfer))
        self.room.notify()

    async def verify(self, transfer):
        info = transfer.info
        partial = info.path.with_suffix(".part")
        try:
            size_sha = await asyncio.to_thread(verify_file, partial)
            if info.status != "verifying":
                return
            if size_sha != (info.size, info.sha):
                self.fail(info, "长度或 SHA-256 校验失败")
                return
            partial.replace(info.path)
            info.valid, info.status = True, "complete"
            log.info("FILE_VERIFIED pid=%d file=%s size=%d sha256=%s",
                     os.getpid(), info.id.hex()[:12], info.size, info.sha.hex())
            self.receipt(transfer)
            self.room.notify()
        except OSError as exc:
            self.fail(info, str(exc))
        finally:
            transfer.verifying = False
            partial.unlink(missing_ok=True)

    def receipt(self, transfer):
        now = time.monotonic()
        if transfer.info.valid and self.room.ready and now - transfer.last_receipt >= 0.7:
            info = transfer.info
            try:
                self.room.send_data(Kind.RECEIPT, info.id + info.sha)
            except RoomError:
                return
            transfer.last_receipt = now

    def handle_response(self, sender, transfer, kind, payload):
        if kind == Kind.RECEIPT:
            if payload == transfer.info.sha:
                transfer.received.add(sender)
                self.check_receipts(transfer)
                self.room.notify()
            return
        if not payload or len(payload) % SPAN.size:
            raise ValueError("invalid missing ranges")
        for start, count in SPAN.iter_unpack(payload):
            if count == 0 or start + count > transfer.info.count:
                raise ValueError("invalid missing range")
            for index in range(start, start + count):
                if len(transfer.repair) >= MAX_REPAIR_QUEUE:
                    return
                transfer.repair.add(index)

    def tick(self):
        now = time.monotonic()
        for transfer in self.incoming.values():
            info = transfer.info
            if info.status != "receiving":
                continue
            if (now - transfer.last_progress >= IDLE_TIMEOUT
                    or now - info.created > 60 + 2 * info.size / (16 * 1024)):
                self.fail_incoming(transfer, "缺少数据块，接收超时")
            elif (self.room.ready and now >= transfer.next_request
                  and (transfer.ended or now - transfer.last_progress >= 1.4)):
                batches = itertools.batched(missing_spans(transfer.received), 120)
                for batch in itertools.islice(batches, 4):
                    payload = info.id + b"".join(SPAN.pack(*span) for span in batch)
                    try:
                        self.room.send_data(Kind.MISSING, payload,
                                            f"file={info.id.hex()[:12]} spans={len(batch)}")
                    except RoomError:
                        break
                transfer.next_request = now + 1.2 + self.room.storage.id[0] / 512
                transfer.ended = False
