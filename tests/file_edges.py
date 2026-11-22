"""Exercise the 32 MiB default, empty uploads and timeout cleanup over multicast."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from multicast_chat.protocol import Kind, wire_json
from multicast_chat.transfers import CHUNK
from tests.nodes import Node, admit, ready, until
from tests.wire import WireTap, fixture, key_target, unseal

LIMIT = 32 * 1024 * 1024


def main(root):
    a, b, c = (Node(name, port, root, extra=("--udp-port", "50421", "--rate-kib", "1024"))
               for name, port in zip("ABC", range(18861, 18864)))
    nodes = a, b, c
    try:
        with WireTap(("239.255.42.99", 50421)) as wire:
            for node in nodes:
                node.start()
            until(lambda: all(node.state()["fileLimit"] == LIMIT for node in nodes),
                  description="default upload limit")
            a.action("create")
            b_id = admit(a, b)
            admit(a, c)
            until(lambda: ready(*nodes))
            until(lambda: all(all(row["online"] for row in node.state()["members"]) for node in nodes))
            epoch = a.state()["epoch"]
            a_id = bytes.fromhex(a.state()["id"])
            header, packet = until(lambda: next(((header, packet) for header, packet in
                                   wire.matching(kind=Kind.KEY, sender=a_id, epoch=epoch)
                                   if key_target(packet) == b_id), None))
            key = unseal(a, b, header, packet)
            wire.watch_data(header.room, epoch)
            incomplete = os.urandom(16)
            manifest = wire_json([incomplete.hex(), "never-sent.bin", 997, "00" * 32, CHUNK])
            incomplete_header, packet = fixture(b, header.room, epoch, 1, Kind.MANIFEST, key, manifest)
            wire.transmit(packet)
            size_limited = a.request("/api/files", data=b"", headers={
                "X-File-Name": "too-big.bin", "Content-Length": str(LIMIT + 1)},
                                     allow_error=True)
            assert size_limited[0] == 413
            print("PASS 32 MiB + 1 byte rejected with HTTP 413", flush=True)
            for name, data in (("empty.bin", b""), ("boundary.bin", bytes(range(256)) * (LIMIT // 256))):
                expected = hashlib.sha256(data).hexdigest()
                status, _, response = a.request("/api/files", data=data, headers={"X-File-Name": name})
                assert status == 202
                identity = json.loads(response)["id"]
                for node in nodes:
                    until(lambda: any(file["id"] == identity and file["status"] == "complete"
                                     for file in node.state()["files"]),
                          timeout=180, description=f"{name} completed at {node.name}")
                for node in (b, c):
                    status, _, download = node.request(f"/api/files/{identity}/download")
                    assert status == 200 and len(download) == len(data)
                    assert hashlib.sha256(download).hexdigest() == expected
                print(f"PASS {name} {len(data)} bytes SHA-256 {expected} at B/C", flush=True)
            missing_key = incomplete_header.sender.hex() + "-" + incomplete.hex()
            for node in (a, c):
                until(lambda: any(file["id"] == missing_key and "接收超时" in file["error"]
                                 for file in node.state()["files"]), timeout=30, description="incomplete file timeout")
                assert not (node.data / "files" / f"recv-{missing_key}.part").exists()
            print("PASS incomplete multicast transfer timed out and partials removed at A/C", flush=True)
    except Exception:
        for node in nodes:
            if node.log.exists():
                print(node.name, "\n".join(node.log.read_text(encoding="utf-8").splitlines()[-16:]), sep="\n")
        raise
    finally:
        for node in nodes:
            node.stop()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        main(Path(directory))
