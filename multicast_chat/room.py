import asyncio
import hmac
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache

from cryptography.exceptions import InvalidSignature, InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .network import Multicast, open_sender
from .protocol import (
    CLOCK_SKEW, FRESH_SECONDS, HEADER, MAX_BODY, SIGNATURE, Header, Kind, ReplayWindow,
    b64, data_destination, decrypt, digest, encode, hex_bytes, oaep, sender_cipher, unb64, verify, wire_json,
)

log = logging.getLogger("multicast.socket")
TICK = 0.65
LEASE = 3.2
OFFLINE = 5.0
MAX_MEMBERS = 16
CONTACT_LIMIT = 240
FENCE_DELAY = LEASE + CLOCK_SKEW + 0.1
OWNER_KINDS = frozenset((Kind.BEACON, Kind.ROSTER, Kind.KEY, Kind.CLOSE, Kind.EXCLUDED))
TRACE_KINDS = frozenset((Kind.CHAT, Kind.MANIFEST, Kind.END, Kind.MISSING, Kind.KEY,
                         Kind.RECEIPT, Kind.LEAVE, Kind.CLOSE, Kind.CONTACT))


class RoomError(ValueError):
    pass


def name_text(value):
    if (not isinstance(value, str) or not 1 <= len(value.encode()) <= 48
            or not value.strip() or any(ord(char) < 32 for char in value)):
        raise ValueError("名称须为 1–48 个 UTF-8 字节")
    return value


@lru_cache(maxsize=64)
def public_rsa(encoded):
    key = serialization.load_der_public_key(encoded)
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048 or key.public_numbers().e != 65537:
        raise ValueError("expected RSA-2048")
    return key


@dataclass(frozen=True, slots=True)
class Member:
    id: bytes
    name: str
    public: bytes
    attempt: bytes
    rsa_der: bytes = b""

    def row(self):
        return [self.id.hex(), self.name, b64(self.public), self.attempt.hex()]

    @classmethod
    def from_row(cls, row, rsa_der=b""):
        identity, name, public, attempt = row
        public = unb64(public)
        identity = hex_bytes(identity, 32)
        if len(public) != 32 or digest(public) != identity:
            raise ValueError("wrong member fingerprint")
        return cls(identity, name_text(name), public, hex_bytes(attempt, 16), rsa_der)


class Room:
    def __init__(self, config, storage):
        self.config = config
        self.storage = storage
        self.network = Multicast(config, (config.group, config.udp_port), "control", self.receive)
        self.data = None
        self.contact_after = 0
        self.files = None
        self.changed = asyncio.Event()
        self.task = None
        self.bursts = set()
        self.boot = os.urandom(16)
        self.seq = 0
        self.faulted_offers = set()
        self.clear()
        self.restore()

    def clear(self):
        self.drop_data()
        self.mode = "idle"
        self.room_id = None
        self.owner_public = None
        self.owner = None
        self.owner_boot = None
        self.invitation = None
        self.attempt = None
        self.epoch = 0
        self.key = None
        self.members = {}
        self.rows = []
        self.roster_hash = None
        self.pending = {}
        self.consumed = set()
        self.seen = {}
        self.awaiting = set()
        self.committed = False
        self.closing = False
        self.fence_basis = set()
        self.fenced = set()
        self.fence_until = None
        self.votes = {}
        self.rejoin_until = 0
        self.beacon_until = 0
        self.beacon_seq = 0
        self.part_count = 0
        self.parts = {}
        self.challenge = os.urandom(16)
        self.data_floor = 0
        self.replays = {}
        self.public_replays = {}
        self.chat = deque(maxlen=150)
        self.contacts = deque(maxlen=8)
        self.cipher.cache_clear()

    @property
    def is_owner(self):
        return self.mode == "owner"

    @property
    def ready(self):
        return (self.committed and self.data is not None and bool(self.members)
                and self.mode in ("owner", "joined")
                and time.monotonic() < self.quorum_until())

    def quorum_until(self):
        # An installed member counts its own vote. Other votes are distinct signed heartbeats.
        peers_needed = len(self.members) // 2
        if peers_needed == 0:
            return float("inf")
        if len(self.votes) < peers_needed:
            return 0
        return sorted(self.votes.values(), reverse=True)[peers_needed - 1]

    def enforce_quorum(self):
        now = time.monotonic()
        if (not self.is_owner and self.committed and self.key is not None
                and now >= self.rejoin_until and now >= self.quorum_until()):
            log.info("QUORUM_LOST pid=%d epoch=%d", os.getpid(), self.epoch)
            self.reset_sync()
            self.notify()

    def join_data(self):
        if self.data is not None:
            return
        data = Multicast(self.config, data_destination(self.room_id, self.epoch, self.network.destination),
                         "data", self.receive)
        try:
            data.start()
        except OSError:
            log.exception("Could not subscribe to the room data group")
            return
        self.data = data

    def drop_data(self):
        if self.data is not None:
            self.data.close()
            self.data = None

    def notify(self):
        event, self.changed = self.changed, asyncio.Event()
        event.set()

    async def start(self):
        self.network.start()
        try:
            if self.is_owner:
                self.join_data()
            self.task = asyncio.create_task(self.run())
        except BaseException:
            self.network.close()
            raise

    async def stop(self):
        tasks = list(self.bursts)
        if self.task:
            tasks.append(self.task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.drop_data()
        self.network.close()

    def require_ready(self):
        if not self.ready:
            raise RoomError("房间尚未同步或当前无发送权限")

    def restore(self):
        record = self.storage.read_room()
        if record is None:
            return
        self.read_invite(record["invite"])
        self.attempt = hex_bytes(record["attempt"], 16)
        self.mode = record["mode"]
        if self.is_owner:
            self.owner_boot = self.boot
            for row, der in record["members"]:
                member = Member.from_row(row, unb64(der))
                self.members[member.id] = member
            self.consumed = {(hex_bytes(peer, 32), hex_bytes(attempt, 16))
                             for peer, attempt in record["consumed"]}
            self.epoch = record["epoch"]
            # v1 peers depended on the original owner's lease and cannot vote on v2.
            if record["invite"].startswith("mc2."):
                if record.get("committed", True):
                    self.fence_basis = set(self.members)
                else:
                    self.fence_basis = {hex_bytes(peer, 32) for peer in record.get("fenceBasis", [])}
                    self.fenced = {hex_bytes(peer, 32) for peer in record.get("fenceConfirmed", [])}
            self.rotate(closing=record.get("closing", False))

    def persist(self):
        self.save_room(self.epoch, self.members, self.consumed, committed=self.committed,
                       basis=self.fence_basis, fenced=self.fenced, closing=self.closing)

    def save_room(self, epoch, members, consumed, *, committed, basis, fenced, closing):
        record = {"invite": self.invitation, "attempt": self.attempt.hex(), "mode": self.mode}
        if self.is_owner:
            record.update(epoch=epoch, committed=committed, closing=closing,
                          fenceBasis=sorted(peer.hex() for peer in basis),
                          fenceConfirmed=sorted(peer.hex() for peer in fenced),
                          members=[[member.row(), b64(member.rsa_der)] for member in members.values()],
                          consumed=sorted((peer.hex(), attempt.hex()) for peer, attempt in consumed))
        self.storage.write_room(record)

    def parse_invite(self, invitation):
        if not isinstance(invitation, str) or len(invitation) > 600 or not invitation.startswith(("mc1.", "mc2.")):
            raise RoomError("邀请格式无效")
        room, public, fingerprint, group, port = json.loads(unb64(invitation[4:]))
        public = unb64(public)
        if len(public) != 32 or digest(public) != hex_bytes(fingerprint, 32):
            raise RoomError("邀请的房主公钥指纹无效")
        if (group, port) != self.network.destination:
            raise RoomError("邀请的组播地址或端口与此节点配置不一致")
        return hex_bytes(room, 16), public

    def read_invite(self, invitation):
        self.room_id, public = self.parse_invite(invitation)
        self.owner_public = public
        self.owner = digest(public)
        self.invitation = "mc2." + invitation[4:]

    def create(self):
        if self.mode not in ("idle", "removed", "closed"):
            raise RoomError("请先退出当前房间")
        self.clear()
        self.room_id = os.urandom(16)
        self.owner = self.storage.id
        self.owner_public = self.storage.public
        self.owner_boot = self.boot
        self.attempt = os.urandom(16)
        self.invitation = "mc2." + b64(wire_json([
            self.room_id.hex(), b64(self.owner_public), self.owner.hex(), *self.network.destination,
        ]))
        self.mode = "owner"
        self.members[self.owner] = Member(self.owner, self.config.name, self.owner_public, self.attempt)
        try:
            self.rotate(initial=True)
        except OSError:
            self.clear()
            raise

    def join(self, invitation):
        if self.mode not in ("idle", "removed", "closed"):
            raise RoomError("请先退出当前房间")
        self.clear()
        self.read_invite(invitation)
        if self.owner == self.storage.id:
            self.clear()
            raise RoomError("不能用自己的邀请申请加入")
        self.attempt = os.urandom(16)
        self.mode = "waiting"
        try:
            self.persist()
        except OSError:
            self.clear()
            raise
        self.send_join()
        self.notify()

    def approve(self, identity):
        if not self.is_owner or identity not in self.pending:
            raise RoomError("没有这条待批准申请")
        if len(self.members) >= MAX_MEMBERS:
            raise RoomError("房间已达成员上限")
        if not self.committed:
            raise RoomError("请等待当前成员变更完成")
        member, _, _ = self.pending[identity]
        members = self.members | {identity: member}
        self.rotate(members=members, consumed=self.consumed | {(identity, member.attempt)})
        del self.pending[identity]
        self.notify()

    def remove(self, identity, *, voluntary=False):
        if not self.is_owner or identity == self.owner or identity not in self.members:
            raise RoomError("无法移除此成员")
        if not self.committed and not voluntary:
            raise RoomError("请等待当前成员变更完成")
        members = self.members.copy()
        removed = members.pop(identity)
        self.rotate(members=members, withdrawn=identity if voluntary else None)
        self.control(Kind.EXCLUDED, [identity.hex(), removed.attempt.hex()])

    def leave(self):
        if self.mode == "idle":
            raise RoomError("当前没有房间")
        if self.is_owner:
            if not self.closing:
                self.rotate(closing=True)
            return
        self.finish_leave()

    def finish_leave(self):
        outgoing = None
        if self.mode in ("waiting", "joined", "owner"):
            kind = Kind.CLOSE if self.is_owner else Kind.LEAVE
            payload = [] if self.is_owner else [
                self.attempt.hex(), self.owner_boot.hex() if self.owner_boot else None,
            ]
            outgoing = self.pack(kind, wire_json(payload))
        self.clear_local("idle")
        if outgoing is not None:
            packet, header = outgoing
            self.transmit(packet, header)
            task = asyncio.create_task(self.repeat(packet, header))
            self.bursts.add(task)
            task.add_done_callback(self.bursts.discard)

    async def repeat(self, packet, header):
        for _ in range(5):
            await asyncio.sleep(0.32)
            self.transmit(packet, header)

    def clear_local(self, mode):
        self.storage.clear_room()
        if self.files:
            self.files.reset()
        self.clear()
        self.mode = mode
        self.notify()

    def rotate(self, *, initial=False, members=None, consumed=None, withdrawn=None, closing=False):
        if initial:
            basis = set()
        elif self.committed:
            basis = set(self.members)
        else:
            basis = self.fence_basis.copy()
        # A replacement for an uncommitted epoch still fences the same former electorate.
        fenced = set() if self.committed else self.fenced & basis
        fenced.update({self.storage.id, withdrawn} & basis)
        members = self.members if members is None else members
        consumed = self.consumed if consumed is None else consumed
        epoch = self.epoch + 1
        key = os.urandom(32)
        rows = [members[peer].row() for peer in sorted(members)]
        self.save_room(epoch, members, consumed, committed=initial, basis=basis, fenced=fenced,
                       closing=closing)
        self.drop_data()
        self.members, self.consumed = members, consumed
        self.seen = {peer: seen for peer, seen in self.seen.items() if peer in members}
        self.awaiting = self.active_peers()
        self.committed, self.closing = initial, closing
        self.fence_basis, self.fenced = basis, fenced
        self.fence_until = None
        self.votes.clear()
        self.epoch, self.key = epoch, key
        self.cipher.cache_clear()
        self.rows = rows
        self.roster_hash = digest(wire_json(self.rows)).hex()
        if self.files:
            self.files.interrupt()
        log.info("EPOCH pid=%d version=%d waiting=%s", os.getpid(), self.epoch,
                 ",".join(peer.hex()[:12] for peer in sorted(self.awaiting)) or "-")
        self.advance_fence()
        if self.network.socket is not None:
            self.join_data()
            self.broadcast()
        self.notify()

    def online(self, identity):
        if identity == self.storage.id:
            return self.mode in ("owner", "joined")
        return time.monotonic() - self.seen.get(identity, -float("inf")) < OFFLINE

    def active_peers(self):
        return {peer for peer in self.members.keys() - {self.storage.id} if self.online(peer)}

    def pack(self, kind, body, *, room=None):
        self.seq += 1
        header = Header(kind, room or self.room_id, self.storage.id, self.boot, 0 if room else self.epoch, self.seq,
                        time.time_ns() // 1_000_000)
        cipher = self.cipher(header.sender, header.boot) if kind.encrypted else None
        return encode(header, body, self.storage.signing, cipher), header

    @lru_cache(maxsize=64)
    def cipher(self, sender, boot):
        return sender_cipher(self.key, Header(Kind.CHAT, self.room_id, sender, boot, self.epoch, 0, 0))

    def transmit(self, packet, header, detail="", *, outside=None):
        network = self.data if header.kind.encrypted else self.network
        if outside is None:
            network.send(packet)
        else:
            outside.sendto(packet, network.destination)
        if header.kind in TRACE_KINDS or detail:
            log.info("TX pid=%d kind=%s sender=%s seq=%d epoch=%d dest=%s:%d bytes=%d channel=%s %s",
                     os.getpid(), header.kind.name, header.sender.hex()[:12], header.seq,
                     header.epoch, *network.destination, len(packet), network.channel, detail)

    def control(self, kind, value):
        self.transmit(*self.pack(kind, wire_json(value)))

    def send_data(self, kind, body, detail="", duplicate=False):
        self.require_ready()
        packet, header = self.pack(kind, body)
        self.transmit(packet, header, detail)
        if duplicate:
            self.transmit(packet, header, "injected duplicate")
        return header

    def send_chat(self, text):
        self.require_ready()
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_BODY:
            raise RoomError(f"消息须包含文字或表情，且最多 {MAX_BODY} 个 UTF-8 字节")
        header = self.send_data(Kind.CHAT, text.encode())
        self.add_chat(header, text)

    def send_contact(self, invitation, text):
        if self.mode in ("owner", "joined"):
            raise RoomError("组内交流请使用房间聊天")
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > CONTACT_LIMIT:
            raise RoomError(f"组外联系须包含文字或表情，且最多 {CONTACT_LIMIT} 个 UTF-8 字节")
        now = time.monotonic()
        if now < self.contact_after:
            raise RoomError("请稍后再发组外联系")
        room, _ = self.parse_invite(invitation or self.invitation)
        body = wire_json([self.config.name, b64(self.storage.public), text])
        packet, header = self.pack(Kind.CONTACT, body, room=room)
        with open_sender(self.config.interface, self.config.ttl) as sender:
            self.transmit(packet, header, "send-only", outside=sender)
        self.contact_after = now + 1

    def add_contact(self, header, name, text):
        name = name_text(name)
        if (header.epoch or not isinstance(text, str) or not text.strip()
                or len(text.encode()) > CONTACT_LIMIT or header.sender in self.members):
            return
        self.contacts.append({"id": f"{header.sender.hex()}:{header.boot.hex()}:{header.seq}",
                              "sender": name, "time": header.millis, "text": text})
        self.notify()

    def add_chat(self, header, text):
        self.chat.append({"id": f"{header.sender.hex()}:{header.boot.hex()}:{header.seq}",
                          "sender": self.members[header.sender].name,
                          "time": header.millis, "text": text,
                          "mine": header.sender == self.storage.id})
        self.notify()

    def send_join(self):
        self.control(Kind.JOIN, [self.config.name, b64(self.storage.public),
                                 b64(self.storage.rsa_public), self.attempt.hex()])

    def broadcast(self):
        self.control(Kind.BEACON, [self.roster_hash, len(self.rows), self.committed])
        if self.data is not None:
            for index, row in enumerate(self.rows):
                self.control(Kind.ROSTER, [self.roster_hash, index, row])

    def reset_sync(self, *, changing_epoch=False):
        if self.key is not None and self.files:
            self.files.interrupt()
        self.key = None
        self.drop_data()
        self.data_floor = 0
        self.votes.clear()
        self.rejoin_until = 0
        self.challenge = os.urandom(16)
        self.cipher.cache_clear()
        if changing_epoch:
            self.committed = False
            self.parts.clear()
            self.members.clear()

    def proof(self, peer, boot):
        return hmac.digest(self.key, b"multicast-chat/v2/installed/" + self.room_id
                           + self.epoch.to_bytes(4, "big") + self.owner_boot + peer + boot, "sha256")

    def heartbeat(self):
        if self.data is not None and self.storage.id in self.members:
            self.control(Kind.HEARTBEAT, [self.owner_boot.hex(),
                                         b64(self.proof(self.storage.id, self.boot)), self.committed])

    def advance_fence(self):
        # An old majority cannot renew when fewer than a majority can still vote.
        if self.fence_until is None and len(self.fenced) >= (len(self.fence_basis) + 1) // 2:
            self.fence_until = time.monotonic() + FENCE_DELAY
            if self.fence_basis:
                log.info("FENCE pid=%d epoch=%d stopped=%d oldMembers=%d drain=%.1fs", os.getpid(),
                         self.epoch, len(self.fenced), len(self.fence_basis), FENCE_DELAY)

    def confirm_fence(self, identity):
        if self.committed or identity not in self.fence_basis or identity in self.fenced:
            return False
        fenced = self.fenced | {identity}
        self.save_room(self.epoch, self.members, self.consumed, committed=False,
                       basis=self.fence_basis, fenced=fenced, closing=self.closing)
        self.fenced = fenced
        self.advance_fence()
        return True

    def finish_transition(self):
        if self.committed or self.fence_until is None or time.monotonic() < self.fence_until:
            return
        self.awaiting.intersection_update(self.active_peers())
        if self.awaiting:
            return
        if self.closing:
            self.finish_leave()
            return
        self.save_room(self.epoch, self.members, self.consumed, committed=True,
                       basis=set(), fenced=set(), closing=False)
        self.fence_basis.clear()
        self.fenced.clear()
        self.committed = True
        log.info("READY pid=%d epoch=%d", os.getpid(), self.epoch)

    async def run(self):
        while True:
            await asyncio.sleep(TICK)
            self.tick()

    def tick(self):
        now = time.monotonic()
        for sessions in (self.replays, self.public_replays):
            for key, (_, deadline) in list(sessions.items()):
                if now > deadline:
                    del sessions[key]
        if self.key is not None and self.data is None:
            self.join_data()
        if self.is_owner:
            self.pending = {peer: record for peer, record in self.pending.items() if now < record[1]}
            try:
                self.finish_transition()
            except OSError:
                log.exception("Could not persist the room transition")
            if self.is_owner:
                self.broadcast()
                self.heartbeat()
        elif self.mode in ("waiting", "joined"):
            self.enforce_quorum()
            if self.mode == "waiting":
                self.send_join()
            if self.key is None:
                if self.owner_boot is not None:
                    self.control(Kind.SYNC, [self.owner_boot.hex(), self.challenge.hex(),
                                             b64(self.storage.public), self.attempt.hex()])
            else:
                self.heartbeat()
        if self.files:
            self.files.tick()
        self.notify()

    def receive(self, packet, source, network):
        if self.mode in ("idle", "removed", "closed"):
            return
        header = None
        try:
            header = Header.read(packet)
            if (header.room != self.room_id or header.sender == self.storage.id
                    or not -CLOCK_SKEW <= header.age <= FRESH_SECONDS):
                return
            kind = header.kind
            if kind.encrypted != (network is self.data):
                raise ValueError("wrong multicast group")
            if kind == Kind.HEARTBEAT:
                self.enforce_quorum()
                if self.data is not network:
                    return
            body = packet[HEADER.size:-SIGNATURE]
            if kind in OWNER_KINDS:
                if self.is_owner or header.sender != self.owner:
                    return
                public = self.owner_public
            elif (kind == Kind.JOIN and self.is_owner
                  or kind == Kind.CONTACT and self.mode in ("owner", "joined")):
                public = unb64(json.loads(body)[1])
                if len(public) != 32 or digest(public) != header.sender:
                    return
            elif kind == Kind.SYNC and self.is_owner:
                public = unb64(json.loads(body)[2])
                if len(public) != 32 or digest(public) != header.sender:
                    return
            else:
                member = self.members.get(header.sender)
                if kind == Kind.LEAVE and self.is_owner and member is None and header.sender in self.pending:
                    member = self.pending[header.sender][0]
                if member is None:
                    if kind.encrypted:
                        raise ValueError("unauthorized sender")
                    return
                public = member.public
            replay_key = header.sender, header.boot
            sessions = (self.public_replays if kind in (Kind.CONTACT, Kind.JOIN, Kind.SYNC)
                        and header.sender not in self.members else self.replays)
            window, _ = sessions.get(replay_key, (None, 0))
            if not header.seq or (window is not None and not window.accepts(header.seq)):
                raise ValueError("replay")
            body = verify(packet, public)
            if kind.encrypted:
                if (header.epoch != self.epoch or self.key is None
                        or (kind not in (Kind.HEARTBEAT, Kind.ROSTER) and not self.ready)):
                    raise ValueError("old epoch or room not ready")
                if header.millis < self.data_floor:
                    raise ValueError("packet predates key synchronization")
                body = decrypt(header, body, self.cipher(header.sender, header.boot))
            if window is None:
                if len(sessions) >= 256:
                    raise ValueError("too many active packet sessions")
                window = ReplayWindow()
            process_leave = kind == Kind.LEAVE and self.is_owner
            if process_leave:
                # Retransmissions can retry if persisting the departure fails.
                self.handle_control(header, json.loads(body))
            window.record(header.seq)
            sessions[replay_key] = window, time.monotonic() + FRESH_SECONDS + CLOCK_SKEW
            if kind in TRACE_KINDS:
                log.info("RX pid=%d kind=%s sender=%s seq=%d epoch=%d src=%s:%d joined=%s:%d bytes=%d channel=%s",
                         os.getpid(), kind.name, header.sender.hex()[:12], header.seq, header.epoch,
                         *source, *network.destination, len(packet), network.channel)
            if kind == Kind.CHAT:
                text = body.decode("utf-8")
                if text.strip():
                    self.add_chat(header, text)
            elif kind >= Kind.CHAT:
                if self.files:
                    self.files.receive(header, body)
            elif not process_leave:
                self.handle_control(header, json.loads(body))
        except (ValueError, TypeError, IndexError, KeyError, AttributeError, OverflowError,
                InvalidSignature, InvalidTag, UnsupportedAlgorithm, RecursionError) as exc:
            if header is not None:
                log.debug("REJECT pid=%d kind=%s seq=%d reason=%s", os.getpid(), header.kind.name,
                          header.seq, str(exc) or type(exc).__name__)
        except OSError:
            log.exception("Could not save multicast state")

    def handle_control(self, header, value):
        kind = header.kind
        if kind == Kind.JOIN and self.is_owner:
            self.accept_join(header, value)
        elif kind == Kind.BEACON:
            self.accept_beacon(header, value)
        elif kind == Kind.ROSTER and header.epoch == self.epoch and header.boot == self.owner_boot:
            self.accept_roster(value)
        elif kind == Kind.SYNC and self.is_owner:
            self.offer_key(header, value)
        elif kind == Kind.KEY and header.epoch == self.epoch and header.boot == self.owner_boot:
            self.accept_key(header, value)
        elif kind == Kind.HEARTBEAT and header.age < LEASE and header.epoch == self.epoch:
            owner_boot, proof, vote = value
            if (self.key is not None and owner_boot == self.owner_boot.hex()
                    and type(vote) is bool
                    and hmac.compare_digest(unb64(proof), self.proof(header.sender, header.boot))):
                receipt = time.monotonic() - max(header.age, 0)
                self.seen[header.sender] = max(receipt, self.seen.get(header.sender, 0))
                if vote:
                    self.votes[header.sender] = max(receipt + LEASE, self.votes.get(header.sender, 0))
                new_fence = self.is_owner and self.confirm_fence(header.sender)
                if new_fence or header.sender in self.awaiting:
                    self.awaiting.discard(header.sender)
                    log.info("KEY_ACK pid=%d epoch=%d member=%s",
                             os.getpid(), self.epoch, header.sender.hex()[:12])
                self.notify()
        elif kind == Kind.LEAVE and self.is_owner:
            attempt, owner_boot = value
            attempt = hex_bytes(attempt, 16)
            if (owner_boot == self.boot.hex() and header.sender in self.members
                    and self.members[header.sender].attempt == attempt):
                if self.closing:
                    self.confirm_fence(header.sender)
                    self.awaiting.discard(header.sender)
                else:
                    self.remove(header.sender, voluntary=True)
            elif header.sender in self.pending and self.pending[header.sender][0].attempt == attempt:
                consumed = self.consumed | {(header.sender, attempt)}
                self.save_room(self.epoch, self.members, consumed, committed=self.committed,
                               basis=self.fence_basis, fenced=self.fenced, closing=self.closing)
                del self.pending[header.sender]
                self.consumed = consumed
                self.notify()
        elif kind == Kind.CLOSE and header.epoch >= self.epoch:
            self.clear_local("closed")
        elif kind == Kind.EXCLUDED and header.epoch >= self.epoch:
            target, attempt = value
            if target == self.storage.id.hex() and attempt == self.attempt.hex():
                self.clear_local("removed")
        elif kind == Kind.CONTACT and self.mode in ("owner", "joined"):
            name, _, message = value
            self.add_contact(header, name, message)

    def accept_join(self, header, value):
        name, public, encryption, attempt = value
        attempt = hex_bytes(attempt, 16)
        previous = self.pending.get(header.sender)
        if header.sender in self.members:
            return
        if (header.sender, attempt) in self.consumed:
            self.control(Kind.EXCLUDED, [header.sender.hex(), attempt.hex()])
            return
        if (previous is None and len(self.pending) >= 32) or (previous and header.millis < previous[2]):
            return
        der = unb64(encryption)
        public_rsa(der)
        self.pending[header.sender] = (Member(header.sender, name_text(name), unb64(public), attempt, der),
                                       time.monotonic() + OFFLINE + FRESH_SECONDS, header.millis)
        self.notify()

    def accept_beacon(self, header, value):
        roster_hash, count, ready = value
        hex_bytes(roster_hash, 32)
        if (header.age >= LEASE or header.epoch == 0 or header.epoch < self.epoch
                or type(count) is not int or not 1 <= count <= MAX_MEMBERS or type(ready) is not bool):
            return
        if header.epoch == self.epoch:
            if (header.boot != self.owner_boot or roster_hash != self.roster_hash
                    or count != self.part_count or header.seq <= self.beacon_seq):
                return
        else:
            self.reset_sync(changing_epoch=True)
            self.epoch, self.owner_boot = header.epoch, header.boot
            self.roster_hash, self.part_count = roster_hash, count
        now = time.monotonic()
        self.beacon_seq = header.seq
        self.beacon_until = now + LEASE - max(0, header.age)
        if ready and not self.committed:
            self.committed = True
            self.rejoin_until = now + LEASE
        self.notify()

    def accept_roster(self, value):
        roster_hash, index, row = value
        if (self.members or roster_hash != self.roster_hash or type(index) is not int
                or not 0 <= index < self.part_count):
            return
        member = Member.from_row(row)
        self.parts[index] = member
        if len(self.parts) != self.part_count:
            return
        ordered = [self.parts[index] for index in range(self.part_count)]
        if digest(wire_json([entry.row() for entry in ordered])).hex() != self.roster_hash:
            self.parts.clear()
            raise ValueError("roster digest mismatch")
        members = {entry.id: entry for entry in ordered}
        if self.owner not in members or len(members) != self.part_count:
            raise ValueError("invalid roster membership")
        member = members.get(self.storage.id)
        if member is None or member.attempt != self.attempt:
            self.clear_local("removed")
            return
        self.members = members
        self.seen = {peer: seen for peer, seen in self.seen.items() if peer in members}
        self.install_key()
        self.notify()

    def offer_key(self, header, value):
        owner_boot, challenge, _, attempt = value
        if owner_boot != self.boot.hex():
            return
        challenge = hex_bytes(challenge, 16)
        attempt = hex_bytes(attempt, 16)
        member = self.members.get(header.sender)
        if member is None or member.attempt != attempt:
            if (header.sender, attempt) in self.consumed:
                self.control(Kind.EXCLUDED, [header.sender.hex(), attempt.hex()])
            return
        fault = self.epoch, header.sender
        if self.config.fault_demo and self.epoch > 1 and fault not in self.faulted_offers:
            self.faulted_offers.add(fault)
            log.info("FAULT pid=%d dropped first KEY epoch=%d member=%s",
                     os.getpid(), self.epoch, header.sender.hex()[:12])
            return
        label = oaep(self.room_id, self.epoch, self.boot, header.sender, header.boot, challenge)
        sealed = public_rsa(member.rsa_der).encrypt(self.key, label)
        self.control(Kind.KEY, [header.sender.hex(), header.boot.hex(), challenge.hex(), b64(sealed),
                                attempt.hex()])

    def accept_key(self, header, value):
        target, boot, challenge, sealed, attempt = value
        if (self.key is not None or target != self.storage.id.hex() or boot != self.boot.hex()
                or attempt != self.attempt.hex() or challenge != self.challenge.hex()
                or time.monotonic() >= self.beacon_until):
            return
        label = oaep(self.room_id, self.epoch, self.owner_boot, self.storage.id, self.boot, self.challenge)
        key = self.storage.rsa.decrypt(unb64(sealed), label)
        if len(key) != 32:
            raise ValueError("invalid AES-256 key")
        self.key, self.data_floor = key, header.millis
        self.rejoin_until = time.monotonic() + LEASE
        self.join_data()
        self.install_key()

    def install_key(self):
        if self.key is not None and self.storage.id in self.members:
            if self.mode == "waiting":
                self.mode = "joined"
                try:
                    self.persist()
                except OSError:
                    self.mode = "waiting"
                    raise
            self.heartbeat()
            self.notify()

    def snapshot(self):
        members = sorted(self.members.values(), key=lambda member: (
            member.id != self.owner, member.name.casefold(), member.id,
        ))
        return {
            "name": self.config.name, "id": self.storage.id.hex(), "mode": self.mode,
            "epoch": self.epoch, "ready": self.ready, "invite": self.invitation if self.is_owner else None,
            "owner": self.owner.hex() if self.owner is not None else None,
            "group": f"{self.config.group}:{self.config.udp_port}", "interface": self.config.interface,
            "dataGroup": ":".join(map(str, self.data.destination)) if self.data else None,
            "dataReceived": self.data.received if self.data else None, "closing": self.closing,
            "transitioning": self.is_owner and not self.committed,
            "contactLimit": CONTACT_LIMIT, "contacts": list(self.contacts),
            "fileLimit": self.config.file_limit, "chatLimit": MAX_BODY,
            "members": [{"id": member.id.hex(), "name": member.name, "online": self.online(member.id),
                         "owner": member.id == self.owner} for member in members],
            "pending": [{"id": member.id.hex(), "name": member.name}
                        for member, _, _ in self.pending.values()],
            "chat": list(self.chat), "files": self.files.snapshot() if self.files else [],
        }
