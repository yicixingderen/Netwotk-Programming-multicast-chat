import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def until(check, timeout=15, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, KeyError):
            pass
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


class Node:
    def __init__(self, name, port, root, fault=False, extra=()):
        self.name = name
        self.url = f"http://127.0.0.1:{port}"
        self.data = root / name
        self.log = root / f"{name}.log"
        self.process = None
        self.command = [sys.executable, "-m", "multicast_chat", "--name", name,
                        "--http-port", str(port), "--data-dir", str(self.data), "--verbose", *extra]
        if fault:
            self.command.append("--fault-demo")

    def start(self):
        with self.log.open("ab") as output:
            self.process = subprocess.Popen(self.command, cwd=ROOT, stdout=output,
                                            stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(5)

    def request(self, path, value=None, *, data=None, headers=None, allow_error=False):
        if value is not None:
            data = json.dumps(value).encode()
        headers = headers or {}
        if data is not None:
            headers = {"X-Local-Chat": "1", **headers}
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                if allow_error:
                    return exc.code, exc.headers, exc.read()
                raise AssertionError(f"{self.name} {path}: HTTP {exc.code} {exc.read().decode()}") from exc

    def state(self):
        return json.loads(self.request("/api/state")[2])

    def action(self, action, **value):
        return self.request(f"/api/{action}", value)


def ready(*nodes):
    return all(node.state()["ready"] for node in nodes)


def admit(owner, member):
    identity = member.state()["id"]
    member.action("join", invite=owner.state()["invite"])
    until(lambda: any(row["id"] == identity for row in owner.state()["pending"]),
          description=f"{member.name} approval request")
    until(lambda: not owner.state()["transitioning"], description="previous membership change completed")
    owner.action("approve", id=identity)
    until(lambda: ready(owner, member), description=f"{member.name} authorized and ready")
    return identity
