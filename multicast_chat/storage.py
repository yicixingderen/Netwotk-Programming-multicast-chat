import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from .protocol import b64, digest, unb64, wire_json


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("wb") as stream:
        stream.write(wire_json(value))
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


class Storage:
    def __init__(self, location):
        self.path = Path(location).resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.files = self.path / "files"
        self.files.mkdir(exist_ok=True)
        self.lock = (self.path / "node.lock").open("a+b")
        try:
            self.lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            raise RuntimeError(f"Data directory is already in use: {self.path}") from exc

        for partial in self.files.glob("*.part"):
            partial.unlink()
        path = self.path / "identity.json"
        if path.exists():
            record = json.loads(path.read_bytes())
            self.signing = ed25519.Ed25519PrivateKey.from_private_bytes(unb64(record["ed"]))
            self.rsa = serialization.load_der_private_key(unb64(record["rsa"]), password=None)
        else:
            self.signing = ed25519.Ed25519PrivateKey.generate()
            self.rsa = rsa.generate_private_key(65537, 2048)
            write_json(path, {
                "ed": b64(self.signing.private_bytes_raw()),
                "rsa": b64(self.rsa.private_bytes(serialization.Encoding.DER,
                                                 serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption())),
            })
        self.public = self.signing.public_key().public_bytes_raw()
        self.id = digest(self.public)
        self.rsa_public = self.rsa.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

    def read_room(self):
        path = self.path / "room.json"
        return json.loads(path.read_bytes()) if path.exists() else None

    def write_room(self, record):
        write_json(self.path / "room.json", record)

    def clear_room(self):
        (self.path / "room.json").unlink(missing_ok=True)

    def close(self):
        self.lock.close()
