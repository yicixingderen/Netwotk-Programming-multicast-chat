import time
import unittest
from dataclasses import replace

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from multicast_chat.network import MAX_DATAGRAM
from multicast_chat.protocol import (
    MAX_BODY, WINDOW, Header, Kind, ReplayWindow, data_destination, decrypt, digest, encode, oaep,
    sender_cipher, verify,
)
from multicast_chat.transfers import missing_spans


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.signer = ed25519.Ed25519PrivateKey.generate()
        self.public = self.signer.public_key().public_bytes_raw()
        self.header = Header(Kind.CHAT, bytes(range(16)), digest(self.public), b"b" * 16,
                             7, 3, time.time_ns() // 1_000_000)
        self.room_key = b"k" * 32

    def test_maximum_udp_packet_and_signature(self):
        body = b"x" * MAX_BODY
        cipher = sender_cipher(self.room_key, self.header)
        packet = encode(self.header, body, self.signer, cipher)
        self.assertEqual(len(packet), MAX_DATAGRAM)
        self.assertEqual(Header.read(packet), self.header)
        self.assertEqual(decrypt(self.header, verify(packet, self.public), cipher), body)
        with self.assertRaises(ValueError):
            encode(self.header, body + b"x", self.signer, cipher)
        with self.assertRaises(ValueError):
            Header.read(packet[:2] + b"\x01" + packet[3:])
        with self.assertRaises(InvalidSignature):
            verify(packet[:-1] + bytes([packet[-1] ^ 1]), self.public)

    def test_ciphertext_room_epoch_sender_boot_and_nonce_binding(self):
        body = "保密内容.wav 🎉".encode()
        cipher = sender_cipher(self.room_key, self.header)
        packet = encode(self.header, body, self.signer, cipher)
        self.assertNotIn(body, packet)
        ciphertext = verify(packet, self.public)
        with self.assertRaises(InvalidTag):
            decrypt(self.header, ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]), cipher)
        for field, value in [("room", b"r" * 16), ("epoch", 8), ("sender", b"s" * 32),
                             ("boot", b"c" * 16), ("seq", 4), ("millis", self.header.millis + 1)]:
            altered = replace(self.header, **{field: value})
            with self.subTest(field=field), self.assertRaises(InvalidTag):
                decrypt(altered, ciphertext, sender_cipher(self.room_key, altered))
        with self.assertRaises(InvalidTag):
            decrypt(self.header, ciphertext, sender_cipher(b"o" * 32, self.header))

    def test_key_envelope_is_bound_to_recipient_and_challenge(self):
        receiver = rsa.generate_private_key(65537, 2048)
        other = rsa.generate_private_key(65537, 2048)
        args = [self.header.room, self.header.epoch, b"b" * 16, b"r" * 32, b"c" * 16, b"n" * 16]
        label = oaep(*args)
        sealed = receiver.public_key().encrypt(self.room_key, label)
        self.assertEqual(receiver.decrypt(sealed, label), self.room_key)
        with self.assertRaises(ValueError):
            other.decrypt(sealed, label)
        args[-1] = b"d" * 16
        with self.assertRaises(ValueError):
            receiver.decrypt(sealed, oaep(*args))

    def test_replay_window_and_missing_ranges(self):
        window = ReplayWindow()
        for sequence in (3, 8, 5, 1, 7):
            self.assertTrue(window.accepts(sequence))
            window.record(sequence)
            self.assertFalse(window.accepts(sequence))
        window.record(WINDOW + 8)
        self.assertFalse(window.accepts(8))
        self.assertTrue(window.accepts(WINDOW + 7))
        self.assertEqual(list(missing_spans(bytearray([0, 0, 1, 1, 0, 1, 0, 0]))),
                         [(0, 2), (4, 1), (6, 2)])

    def test_epoch_groups_are_private_and_distinct_from_rendezvous(self):
        room = self.header.room
        rendezvous = ("239.255.42.99", 50420)
        groups = {data_destination(room, epoch, rendezvous) for epoch in range(1, 254 * 254)}
        self.assertEqual(len(groups), 254 * 254 - 1)
        self.assertNotIn(rendezvous, groups)
        self.assertTrue(all(group.startswith("239.255.") and port == 50420 for group, port in groups))
