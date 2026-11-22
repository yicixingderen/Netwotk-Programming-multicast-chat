"""Core HTTP, membership and multicast text checks."""
import json
import tempfile
from pathlib import Path

from tests.nodes import Node, admit, ready, until


def main(root):
    a, b, c = (Node(name, port, root) for name, port in zip("ABC", range(18961, 18964)))
    nodes = (a, b, c)
    try:
        for node in nodes:
            node.start()
        until(lambda: all(node.state()["mode"] == "idle" for node in nodes), description="3 HTTP listeners")
        assert b.request("/")[0] == 200
        a.action("create")
        admit(a, b)
        c_id = admit(a, c)
        until(lambda: ready(*nodes))
        text = "你好，组播！🎉"
        a.action("chat", text=text)
        until(lambda: all([row["text"] for row in node.state()["chat"]] == [text] for node in (b, c)),
              description="one Chinese/Emoji message at B and C")
        c.action("leave")
        until(lambda: all(c_id not in {row["id"] for row in node.state()["members"]} for node in (a, b)),
              description="voluntary leave at both remaining nodes")
        admit(a, c)
        c.stop()
        until(lambda: all(any(row["id"] == c_id and not row["online"] for row in node.state()["members"])
                          for node in (a, b)), description="signed heartbeat timeout at A and B")
        c.start()
        until(lambda: ready(*nodes), description="C restart sync")
        a.action("remove", id=c_id)
        until(lambda: ready(a, b) and c.state()["mode"] == "removed", description="rotation and eviction")
        assert c.request("/api/chat", {"text": "removed"}, allow_error=True)[0] == 409
        b.action("chat", text="批准成员仍可发送")
        until(lambda: a.state()["chat"][-1]["text"] == "批准成员仍可发送")
        old = a.state()["epoch"]
        a.stop()
        until(lambda: not b.state()["ready"], description="B sending lease expires without A")
        assert b.request("/api/chat", {"text": "no lease"}, allow_error=True)[0] == 409
        a.start()
        until(lambda: ready(a, b) and b.state()["epoch"] > old, description="A restart and B fresh key")
        assert c.state()["mode"] == "removed"
        a.action("chat", text="房主重启后已同步")
        until(lambda: b.state()["chat"][-1]["text"] == "房主重启后已同步")
        print("PASS sending resumed through a fresh quorum after owner restart", flush=True)
        check_departures_survive_restart(a, b, c)
        check_saved_room_upgrade(a, b)
        for node in nodes:
            matches = [line for line in node.log.read_text(encoding="utf-8").splitlines() if "kind=CHAT " in line]
            print(node.name, *matches, sep="\n")
        print("PASS room smoke")
    finally:
        for node in nodes:
            node.stop()


def check_departures_survive_restart(owner, first, second):
    admit(owner, second)
    invite = owner.state()["invite"]
    old_roster = {node.state()["id"] for node in (owner, first, second)}
    first.action("leave")
    second.action("leave")
    until(lambda: len(owner.state()["members"]) == 1, description="both departures reached owner")
    record = json.loads((owner.data / "room.json").read_bytes())
    assert not record["committed"]
    assert set(record["fenceBasis"]) == set(record["fenceConfirmed"]) == old_roster
    owner.stop()
    owner.start()
    until(lambda: owner.state()["epoch"] > record["epoch"] and ready(owner),
          description="owner restored both departure confirmations before commit")
    assert owner.state()["invite"] == invite
    assert all(node.state()["mode"] == "idle" for node in (first, second))
    admit(owner, first)
    first.action("chat", text="两位成员退出，房主重启后重新加入")
    until(lambda: owner.state()["chat"][-1]["text"] == "两位成员退出，房主重启后重新加入")
    print("PASS both voluntary confirmations survived owner restart; original invite works", flush=True)


def check_saved_room_upgrade(owner, member):
    identities = owner.state()["id"], member.state()["id"]
    epoch = owner.state()["epoch"]
    for node in (owner, member):
        node.stop()
        path = node.data / "room.json"
        saved = json.loads(path.read_bytes())
        saved["invite"] = "mc1." + saved["invite"][4:]
        for field in ("committed", "closing", "fenceBasis", "fenceConfirmed"):
            saved.pop(field, None)
        path.write_text(json.dumps(saved), encoding="utf-8")
    owner.start()
    member.start()
    until(lambda: ready(owner, member) and owner.state()["epoch"] > epoch,
          description="upgrade from a v1 room record")
    assert (owner.state()["id"], member.state()["id"]) == identities
    assert owner.state()["invite"].startswith("mc2.")
    member.action("chat", text="旧记录已升级")
    until(lambda: owner.state()["chat"][-1]["text"] == "旧记录已升级")
    print("PASS v1 room records restored identities and authorization into v2", flush=True)
    owner.action("leave")
    until(lambda: owner.state()["mode"] == "idle" and member.state()["mode"] == "closed",
          description="signed closing and data group teardown")
    assert owner.state()["dataGroup"] is None and member.state()["dataGroup"] is None
    print("PASS room closing dropped both data group subscriptions", flush=True)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        main(Path(directory))
