"""Version 2 wire format and cryptographic primitives."""
import base64
import binascii
import hashlib
import json
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .network import MAX_DATAGRAM

VERSION = 2
HEADER = struct.Struct("!2sBB16s32s16sIQQ")
SIGNATURE = 64
MAX_BODY = MAX_DATAGRAM - HEADER.size - SIGNATURE - 16
FRESH_SECONDS = 10
CLOCK_SKEW = 0.5
WINDOW = 8192
WINDOW_MASK = (1 << WINDOW) - 1


class Kind(IntEnum):
    JOIN = 1
    BEACON = 2
    ROSTER = 3
    SYNC = 4
    KEY = 5
    HEARTBEAT = 6
    LEAVE = 7
    CLOSE = 8
    EXCLUDED = 9
    CONTACT = 10
    CHAT = 16
    MANIFEST = 17
    BLOCK = 18
    END = 19
    MISSING = 20
    RECEIPT = 21

    @property
    def encrypted(self):
        return self in (Kind.ROSTER, Kind.HEARTBEAT) or self >= Kind.CHAT


def data_destination(room, epoch, rendezvous):
    """Cycle through the administratively scoped IPv4 local groups for this room."""
    group, port = rendezvous
    size = 254 * 254
    start = int.from_bytes(digest(room)[:4], "big") % size
    offset = epoch - 1
    octets = group.split(".")
    if octets[:2] == ["239", "255"] and all(1 <= int(octet) <= 254 for octet in octets[2:]):
        reserved = (int(octets[2]) - 1) * 254 + int(octets[3]) - 1
        start %= size - 1
        slot = (start + offset) % (size - 1)
        slot += slot >= reserved
    else:
        slot = (start + offset) % size
    return f"239.255.{slot // 254 + 1}.{slot % 254 + 1}", port


def wire_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64(value):
    if not isinstance(value, str):
        raise ValueError("expected base64 text")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64") from exc
    if b64(decoded) != value:
        raise ValueError("non-canonical base64")
    return decoded


def hex_bytes(value, size):
    if not isinstance(value, str) or len(value) != 2 * size:
        raise ValueError("invalid hex length")
    decoded = bytes.fromhex(value)
    if decoded.hex() != value:
        raise ValueError("non-canonical hex")
    return decoded


def digest(value):
    return hashlib.sha256(value).digest()


@lru_cache(maxsize=128)
def signer(public):
    return ed25519.Ed25519PublicKey.from_public_bytes(public)


def oaep(room, epoch, owner_boot, recipient, recipient_boot, challenge):
    label = b"multicast-chat/v2/room-key/" + room + struct.pack("!I", epoch)
    label += owner_boot + recipient + recipient_boot + challenge
    return padding.OAEP(padding.MGF1(hashes.SHA256()), hashes.SHA256(), label)


@dataclass(frozen=True, slots=True)
class Header:
    kind: Kind
    room: bytes
    sender: bytes
    boot: bytes
    epoch: int
    seq: int
    millis: int

    def encode(self):
        return HEADER.pack(b"MC", VERSION, self.kind, self.room, self.sender, self.boot,
                           self.epoch, self.seq, self.millis)

    @classmethod
    def read(cls, packet):
        if not HEADER.size + SIGNATURE <= len(packet) <= MAX_DATAGRAM:
            raise ValueError("invalid packet size")
        magic, version, kind, *fields = HEADER.unpack_from(packet)
        if magic != b"MC" or version != VERSION:
            raise ValueError("unsupported packet version")
        return cls(Kind(kind), *fields)

    @property
    def age(self):
        return time.time() - self.millis / 1000

    @property
    def nonce(self):
        return self.seq.to_bytes(12, "big")


def sender_cipher(room_key, header):
    salt = header.room + struct.pack("!I", header.epoch)
    info = b"multicast-chat/v2/data/" + header.sender + header.boot
    return AESGCM(HKDF(hashes.SHA256(), 32, salt, info).derive(room_key))


def encode(header, body, private, cipher=None):
    associated = header.encode()
    if header.kind.encrypted:
        body = cipher.encrypt(header.nonce, body, associated)
    signed = associated + body
    packet = signed + private.sign(signed)
    if len(packet) > MAX_DATAGRAM:
        raise ValueError("UDP payload exceeds 1200 bytes")
    return packet


def verify(packet, public):
    signer(public).verify(packet[-SIGNATURE:], packet[:-SIGNATURE])
    return packet[HEADER.size:-SIGNATURE]


def decrypt(header, packet_body, cipher):
    return cipher.decrypt(header.nonce, packet_body, header.encode())


class ReplayWindow:
    def __init__(self):
        self.highest = 0
        self.bits = 0

    def accepts(self, seq):
        if seq > self.highest:
            return True
        behind = self.highest - seq
        return seq > 0 and behind < WINDOW and not self.bits & (1 << behind)

    def record(self, seq):
        if seq > self.highest:
            advance = seq - self.highest
            self.bits = 0 if advance >= WINDOW else (self.bits << advance) & WINDOW_MASK
            self.highest = seq
        self.bits |= 1 << (self.highest - seq)
