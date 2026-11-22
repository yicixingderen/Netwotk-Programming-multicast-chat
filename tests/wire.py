"""Observe real datagrams and generate defensive validation fixtures for the test room."""
import json
import os
import select
import threading
import time
from collections import deque
from contextlib import AbstractContextManager

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from multicast_chat.network import open_multicast, open_sender
from multicast_chat.protocol import (
    HEADER, SIGNATURE, Header, data_destination, decrypt, encode, oaep, sender_cipher, unb64, verify,
)

DESTINATION = ("239.255.42.99", 50420)


class WireTap(AbstractContextManager):
    def __init__(self, destination=DESTINATION):
        self.destination = destination
        self.sockets = {}
        self.packets = deque(maxlen=8192)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.capture, daemon=True)
        self.watch(destination)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.thread.join(2)
        for sock in self.sockets:
            sock.close()

    def watch(self, destination):
        with self.lock:
            if destination not in self.sockets.values():
                self.sockets[open_multicast(*destination, "127.0.0.1", 1)] = destination
        return destination

    def watch_data(self, room, epoch):
        return self.watch(data_destination(room, epoch, self.destination))

    def capture(self):
        while not self.stop.is_set():
            with self.lock:
                sockets = dict(self.sockets)
            readable, _, _ = select.select(list(sockets), [], [], 0.05)
            for sock in readable:
                try:
                    packet, _ = sock.recvfrom(65535)
                    header = Header.read(packet)
                except (BlockingIOError, ValueError):
                    continue
                with self.lock:
                    self.packets.append((sockets[sock], header, packet))

    def matching(self, *, kind=None, sender=None, epoch=None, destination=None):
        with self.lock:
            return [(header, packet) for dest, header, packet in self.packets
                    if (kind is None or header.kind == kind) and (sender is None or header.sender == sender)
                    and (epoch is None or header.epoch == epoch)
                    and (destination is None or dest == destination)]

    def transmit(self, packet, destination=None):
        if destination is None:
            header = Header.read(packet)
            destination = (data_destination(header.room, header.epoch, self.destination)
                           if header.kind.encrypted else self.destination)
        with open_sender("127.0.0.1", 1) as sender:
            sender.sendto(packet, destination)


def private_keys(node):
    record = json.loads((node.data / "identity.json").read_bytes())
    return (ed25519.Ed25519PrivateKey.from_private_bytes(unb64(record["ed"])),
            serialization.load_der_private_key(unb64(record["rsa"]), password=None))


def key_target(packet):
    return json.loads(packet[HEADER.size:-SIGNATURE])[0]


def unseal(owner, receiver, header, packet):
    public = private_keys(owner)[0].public_key().public_bytes_raw()
    target, boot, challenge, ciphertext, _ = json.loads(verify(packet, public))
    label = oaep(header.room, header.epoch, header.boot, bytes.fromhex(target),
                 bytes.fromhex(boot), bytes.fromhex(challenge))
    return private_keys(receiver)[1].decrypt(unb64(ciphertext), label)


def plaintext(header, packet, key, sender):
    record = json.loads((sender.data / "identity.json").read_bytes())
    public = ed25519.Ed25519PrivateKey.from_private_bytes(unb64(record["ed"])).public_key().public_bytes_raw()
    return decrypt(header, verify(packet, public), sender_cipher(key, header))


def fixture(node, room, epoch, seq, kind, key, body, *, boot=None, tamper=None):
    private, _ = private_keys(node)
    identity = bytes.fromhex(node.state()["id"])
    header = Header(kind, room, identity, boot or os.urandom(16), epoch, seq, time.time_ns() // 1_000_000)
    packet = encode(header, body, private, sender_cipher(key, header))
    if tamper == "signature":
        return header, packet[:-1] + bytes([packet[-1] ^ 1])
    if tamper == "gcm":
        signed = packet[:-SIGNATURE - 1] + bytes([packet[-SIGNATURE - 1] ^ 1])
        return header, signed + private.sign(signed)
    return header, packet
