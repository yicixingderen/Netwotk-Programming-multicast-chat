"""Windows end-to-end acceptance in installed Chrome or Edge, with a real multicast capture."""
import argparse
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ed25519
from playwright.sync_api import expect, sync_playwright

from multicast_chat.network import MAX_DATAGRAM
from multicast_chat.protocol import Header, Kind, b64, digest, encode, wire_json
from multicast_chat.room import FENCE_DELAY
from multicast_chat.transfers import CHUNK, INDEX
from tests.nodes import ROOT, Node, ready, until
from tests.wire import WireTap, fixture, key_target, plaintext, unseal

SAMPLES = ROOT / "samples"
TEXT = "你好，组播！🎉"
MEDIA = ("multicast-tone.wav", "multicast-motion.webm")
WAIT = 16_000


class Report:
    def __init__(self, root):
        self.path = root / "checks.txt"

    def __call__(self, message):
        print(message, flush=True)
        with self.path.open("a", encoding="utf-8") as stream:
            print(message, file=stream)


def browser_path():
    for directory in [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                      os.environ.get("LocalAppData")]:
        if directory:
            for application in ("Google/Chrome/Application/chrome.exe",
                                "Microsoft/Edge/Application/msedge.exe"):
                candidate = Path(directory) / application
                if candidate.exists():
                    return candidate
    raise RuntimeError("找不到已安装的 Chrome 或 Edge。请传入 --browser 路径。")


def check_fixed_room_layout(browser, node, report):
    initial = node.state()
    history = [{"id": f"chat-{i}", "sender": "测试成员", "time": int(time.time() * 1000),
                "text": "多行测试\n" * 15 + "a" * 240, "mine": i % 2 == 0} for i in range(60)]
    sidebar = {
        "members": initial["members"] + [{"id": f"{i:064x}", "name": f"成员 {i}", "online": True,
                                         "owner": False} for i in range(1, 16)],
        "pending": [{"id": f"{i:064x}", "name": f"申请人 {i}"} for i in range(100, 132)],
        "contacts": [{"id": f"contact-{i}", "sender": "访客", "text": "组外留言测试" * 8}
                     for i in range(8)],
    }
    read_layout = """() => [...document.querySelectorAll(
        '.room, .conversation, .composer, .people, .transfers'
    )].map(element => {
      const box = element.getBoundingClientRect();
      return [box.top + scrollY, box.width, box.height];
    })"""
    publish = """(state) => window.layoutEvents.dispatchEvent(
        new MessageEvent('state', {data: JSON.stringify(state)})
    )"""
    for width, height in ((1200, 840), (390, 760), (920, 360)):
        page = browser.new_page(viewport={"width": width, "height": height})
        try:
            page.add_init_script("""window.EventSource = class extends EventTarget {
              constructor() {
                super();
                window.layoutEvents = this;
              }
            };""")
            page.goto(node.url)
            page.evaluate(publish, initial)
            baseline = page.evaluate(read_layout)
            page.evaluate(publish, {**initial, "chat": history})
            assert page.locator("#messages .message").count() == len(history)
            assert page.evaluate(read_layout) == baseline, "Chat history changed the outer layout"
            page.evaluate(publish, {**initial, **sidebar, "chat": history})
            assert page.evaluate(read_layout) == baseline, "Sidebar content changed the outer layout"
            for region in ("messages", "members", "pending", "contacts"):
                assert page.locator(f"#{region}").evaluate(
                    "(list) => list.clientHeight > 0 && list.scrollHeight > list.clientHeight"
                ), region
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            report(f"LAYOUT {width}x{height}: chat and sidebar content scroll without moving the composer or files")
        finally:
            page.close()


def chat(page, text):
    field = page.locator("#chat-text")
    field.fill(text)
    page.get_by_role("button", name="发送", exact=True).click(timeout=WAIT)
    expect(field).to_have_value("")


def expect_message(page, text):
    expect(page.locator("#messages").get_by_text(text, exact=True)).to_have_count(1, timeout=WAIT)


def request_join(member, invite):
    member.get_by_label("房间邀请", exact=True).fill(invite)
    member.get_by_role("button", name="申请加入").click()
    expect(member.get_by_text("等待房主批准加入")).to_be_visible(timeout=WAIT)


def approve_join(owner, member, name):
    owner.get_by_role("button", name=f"批准 {name}").click(timeout=WAIT)
    expect(owner.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=WAIT)
    expect(member.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=WAIT)


def join(owner, member, name, invite):
    request_join(member, invite)
    approve_join(owner, member, name)


def check_rapid_exits(owner, page, members, invite):
    previous = owner.state()["epoch"]
    for node, member_page in members:
        member_page.get_by_role("button", name="退出房间").click()
    for node, member_page in members:
        expect(member_page.get_by_role("heading", name="进入房间")).to_be_visible(timeout=WAIT)
        assert node.state()["dataGroup"] is None
    until(lambda: set(row["name"] for row in owner.state()["members"]) == {owner.name},
          description="both signed departures received")
    state = owner.state()
    assert state["epoch"] == previous + 2 and state["transitioning"] and not state["ready"]
    expect(page.locator("#room-status")).to_have_text("房间已就绪", timeout=WAIT)
    assert owner.state()["invite"] == invite


def file_row(page, name):
    return page.locator(".file-row").filter(has=page.get_by_text(name, exact=True))


def verify_playback(page, node, row, source, report):
    player = row.locator("audio" if source.suffix == ".wav" else "video")
    player.wait_for()
    player.evaluate("(media) => media.play()")
    page.wait_for_function("""(id) => {
      const media = document.querySelector(`[data-id="${id}"] audio, [data-id="${id}"] video`);
      return media && !media.paused && media.currentTime > 0.25;
    }""", arg=row.get_attribute("data-id"), timeout=WAIT)
    metrics = player.evaluate("""async (media) => {
      const before = media.currentTime;
      media.pause();
      const target = media.duration * 0.74;
      await new Promise((resolve) => {
        media.addEventListener("seeked", resolve, {once: true});
        media.currentTime = target;
      });
      const decoded = media.getVideoPlaybackQuality?.().totalVideoFrames ?? null;
      return {before, after: media.currentTime, duration: media.duration,
        ready: media.readyState, width: media.videoWidth ?? null, decoded};
    }""")
    assert metrics["before"] > 0.25 and metrics["after"] > 1.5 and metrics["ready"] >= 2, metrics
    if source.suffix == ".webm":
        assert metrics["width"] == 320 and metrics["decoded"] > 0, metrics
    route = f"/api/files/{row.get_attribute('data-id')}/play"
    response = page.request.get(node.url + route, headers={"Range": "bytes=1024-2047"})
    assert response.status == 206 and response.body() == source.read_bytes()[1024:2048]
    assert response.headers["content-range"] == f"bytes 1024-2047/{source.stat().st_size}"
    assert response.headers["accept-ranges"] == "bytes"
    report(f"PLAY {node.name} {source.name} advanced={metrics['before']:.3f}s "
           f"seek={metrics['after']:.3f}s decodedFrames={metrics['decoded']} "
           f"HTTP 206 {response.headers['content-range']}")


def browser_transfer(owner, members, source, report):
    previous = owner.locator(".file-row").evaluate_all("(rows) => rows.map((row) => row.dataset.id)")
    owner.locator("#file").set_input_files(source)
    with owner.expect_response(lambda response: response.url.endswith("/api/files")
                               and response.request.method == "POST") as uploading:
        owner.get_by_role("button", name="组播发送").click()
    response = uploading.value
    assert response.status == 202
    exclude = "".join(f':not([data-id="{key}"])' for key in previous)
    sender = owner.locator(f".file-row{exclude}").filter(has=owner.get_by_text(source.name, exact=True))
    sender.wait_for(timeout=WAIT)
    identity = sender.get_attribute("data-id")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    received = []
    for node, page in members:
        row = page.locator(f'.file-row[data-id="{identity}"]')
        expect(row).to_have_attribute("data-status", "complete", timeout=WAIT)
        assert page.evaluate("(id) => window.fileProgress[id].includes('receiving')", identity)
        with page.expect_download() as downloading:
            row.get_by_role("link", name="下载").click()
        download = downloading.value
        assert download.suggested_filename == source.name
        sha = hashlib.sha256(download.path().read_bytes()).hexdigest()
        assert sha == source_hash
        report(f"DOWNLOAD {node.name} {source.name} bytes={source.stat().st_size} sha256={sha} match=YES")
        verify_playback(page, node, row, source, report)
        received.append(row.get_attribute("data-id"))
    expect(sender).to_have_attribute("data-status", "complete", timeout=WAIT)
    assert owner.evaluate("(id) => window.fileProgress[id].includes('sending')", identity)
    expect(sender).to_contain_text(f"{len(members)}/{len(members)} 位在线成员已校验")
    assert len(set(received)) == 1
    return received[0]


def logged(node, fragment):
    return fragment in node.log.read_text(encoding="utf-8")


def target_offer(wire, owner_id, epoch, receiver_id):
    return next(((h, p) for h, p in reversed(wire.matching(kind=Kind.KEY, sender=owner_id, epoch=epoch))
                 if key_target(p) == receiver_id.hex()), None)


def show_outside_contact(page, message):
    expect(page.locator("#contacts").get_by_text(message, exact=True)).to_have_count(1, timeout=WAIT)
    expect(page.locator("#messages").get_by_text(message, exact=True)).to_have_count(0)


def shared_blocks(wire, sender, sender_id, receivers, transfer, key, epoch, destination):
    file_id = bytes.fromhex(transfer.split("-")[1])
    logs = [node.log.read_text(encoding="utf-8").splitlines() for node in receivers]
    accepted = {}
    for header, packet in wire.matching(kind=Kind.BLOCK, sender=sender_id, epoch=epoch, destination=destination):
        body = plaintext(header, packet, key, sender)
        if not body.startswith(file_id):
            continue
        index = INDEX.unpack_from(body, 16)[0]
        if index not in (0, 2, 17):
            continue
        if all(any("RX_BLOCK" in line and f"seq={header.seq} " in line
                   and f"file={file_id.hex()[:12]} block={index}" in line for line in lines)
               for lines in logs):
            accepted[index] = header.seq
    assert accepted.keys() == {0, 2, 17}, accepted
    return accepted


def acceptance(browser, root, nodes, wire, report):
    a, b, c = nodes
    pages = []
    errors = []
    media_responses = []
    for node in nodes:
        context = browser.new_context(viewport={"width": 1200, "height": 960},
                                      permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda response: media_responses.append(response.status)
                if response.url.endswith("/play") else None)
        page.add_init_script("""document.addEventListener("DOMContentLoaded", () => {
          window.fileProgress = {};
          new MutationObserver(() => {
            for (const row of document.querySelectorAll(".file-row")) {
              const stages = window.fileProgress[row.dataset.id] ??= [];
              if (stages.at(-1) !== row.dataset.status) stages.push(row.dataset.status);
            }
          }).observe(document.querySelector("#files"), {
            childList: true, subtree: true, attributes: true, attributeFilter: ["data-status"]
          });
        });""")
        page.goto(node.url)
        expect(page.get_by_role("heading", name="进入房间")).to_be_visible()
        pages.append(page)
    pa, pb, pc = pages
    pids = [re.search(r"Started server process \[(\d+)\]", node.log.read_text(encoding="utf-8"))[1]
            for node in nodes]
    report(f"BROWSER {browser.version} three pages, server PIDs A/B/C={','.join(pids)}")
    pa.get_by_role("button", name="创建房间").click()
    until(lambda: a.state()["ready"])
    check_fixed_room_layout(browser, a, report)
    pa.bring_to_front()
    pa.get_by_role("button", name="复制邀请").click()
    expect(pa.get_by_role("alert")).to_have_text("邀请已复制")
    invite = pa.evaluate("navigator.clipboard.readText()")
    assert invite == a.state()["invite"]
    request_join(pb, invite)
    pb.reload()
    expect(pb.get_by_text("等待房主批准加入")).to_be_visible(timeout=WAIT)
    pb.get_by_text("组外联系", exact=True).click()
    awaiting_message = "B 候审刷新后的公开留言 📨"
    pb.get_by_label("公开留言", exact=True).fill(awaiting_message)
    with pb.expect_request(lambda request: request.url.endswith("/api/contact")) as public_request:
        pb.get_by_role("button", name="发送公开留言").click()
    assert public_request.value.post_data_json == {"text": awaiting_message}
    show_outside_contact(pa, awaiting_message)
    state = b.state()
    assert state["mode"] == "waiting" and state["dataGroup"] is None
    expect(pb.get_by_role("button", name="发送", exact=True)).to_be_disabled()
    expect(pb.get_by_role("button", name="组播发送")).to_be_disabled()
    pb.screenshot(path=root / "B-waiting-contact.png", animations="disabled")
    report("WAITING CONTACT B reloaded while waiting, sent one public message with the saved invite; "
           "A displayed it once, B stayed outside the private data group")
    approve_join(pa, pb, "B")
    expect(pb.get_by_text("组外联系", exact=True)).to_be_hidden()
    a_id, b_id, c_id = (bytes.fromhex(node.state()["id"]) for node in nodes)
    pc.get_by_label("房间邀请", exact=True).fill(invite)
    pc.get_by_text("组外联系", exact=True).click()
    outside = "C 申请前的公开留言 👋"
    pc.get_by_label("公开留言（填写上方房间邀请）").fill(outside)
    pc.get_by_role("button", name="发送公开留言").click()
    for page in (pa, pb):
        show_outside_contact(page, outside)
    pc.get_by_role("button", name="申请加入").click()
    expect(pc.get_by_text("等待房主批准加入")).to_be_visible(timeout=WAIT)
    expect(pa.get_by_role("button", name="批准 C")).to_be_visible(timeout=WAIT)
    prior = a.state()["epoch"]
    prior_key_header, _ = until(lambda: target_offer(wire, a_id, prior, b_id))
    prior_group = wire.watch_data(prior_key_header.room, prior)
    private = "等待批准时的组内文字 🗝"
    chat(pa, private)
    expect_message(pb, private)
    assert c.state()["dataGroup"] is None and c.state()["dataReceived"] is None and not c.state()["chat"]
    assert not any(h.kind.encrypted for h, _ in wire.matching(destination=wire.destination))
    assert until(lambda: wire.matching(kind=Kind.CHAT, sender=a_id, epoch=prior, destination=prior_group))
    pc.screenshot(path=root / "C-waiting-no-data-group.png", animations="disabled")
    report("SCOPE C used a send-only socket for a public message, displayed once on A/B. "
           "While waiting for approval C had no data socket and did not receive the private chat.")
    pa.get_by_role("button", name="批准 C").click()
    expect(pa.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=WAIT)
    expect(pc.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=WAIT)
    expect(pa.locator("#members .online")).to_have_count(3, timeout=WAIT)
    expect(pb.locator("#members .online")).to_have_count(3, timeout=WAIT)
    expect(pc.locator("#members .online")).to_have_count(3, timeout=WAIT)
    initial_epoch = a.state()["epoch"]
    first_offer = until(lambda: target_offer(wire, a_id, initial_epoch, c_id))
    room = first_offer[0].room
    initial_group = wire.watch_data(room, initial_epoch)
    assert initial_group != prior_group
    assert all(node.state()["dataGroup"] == ":".join(map(str, initial_group)) for node in nodes)
    guest_key = ed25519.Ed25519PrivateKey.generate()
    public = guest_key.public_key().public_bytes_raw()
    header = Header(Kind.CONTACT, room, digest(public), os.urandom(16), 0, 1, time.time_ns() // 1_000_000)
    announcement = "同一范围内的组外通知 📣"
    outside_packet = encode(header, wire_json(["外部发送端", b64(public), announcement]), guest_key)
    wire.transmit(outside_packet)
    for page in (pa, pb, pc):
        show_outside_contact(page, announcement)
    report(f"SCOPE one external multicast announcement delivered to all 3 pages at "
           f"{wire.destination}; epoch {initial_epoch} uses separate private group {initial_group}")
    chat(pa, TEXT)
    for page in (pb, pc):
        expect_message(page, TEXT)
        message = page.locator(".message").filter(has=page.get_by_text(TEXT, exact=True))
        expect(message.locator("strong")).to_have_text("A")
        expect(message.locator("time")).to_have_attribute("datetime", re.compile(r"^\d{4}-"), timeout=WAIT)
    chat_header, chat_packet = until(lambda: wire.matching(kind=Kind.CHAT, sender=a_id, epoch=initial_epoch),
                                     description="initial CHAT packet")[0]
    assert TEXT.encode() not in chat_packet
    old_key = unseal(a, c, *first_offer)
    assert plaintext(chat_header, chat_packet, old_key, a).decode() == TEXT
    wire.transmit(chat_packet)
    for node, page in ((b, pb), (c, pc)):
        until(lambda: logged(node, f"kind=CHAT seq={chat_header.seq} reason=replay"))
        expect_message(page, TEXT)
    report(f"CHAT UTF-8 + emoji one TX seq={chat_header.seq} and one display each at B/C; "
           "real replay rejected by both")

    received = [browser_transfer(pa, [(b, pb), (c, pc)], SAMPLES / name, report) for name in MEDIA]
    for page, node in ((pb, b), (pc, c)):
        page.screenshot(path=root / f"{node.name}-media.png", full_page=True, animations="disabled")
    assert 206 in media_responses
    initial_manifests = wire.matching(kind=Kind.MANIFEST, sender=a_id, epoch=initial_epoch)
    blocks = wire.matching(kind=Kind.BLOCK, sender=a_id, epoch=initial_epoch)
    requests = wire.matching(kind=Kind.MISSING, epoch=initial_epoch)
    assert blocks and requests and max(len(packet) for _, packet in wire.matching()) <= MAX_DATAGRAM
    assert len({(header.seq, packet) for header, packet in blocks}) < len(blocks)
    for name in MEDIA:
        assert all(name.encode() not in packet for _, packet in initial_manifests)
    initial_blocks = {key.split("-")[1]: [] for key in received}
    for header, packet in blocks:
        body = plaintext(header, packet, old_key, a)
        initial_blocks[body[:16].hex()].append(INDEX.unpack_from(body, 16)[0])
    for indices in initial_blocks.values():
        assert indices[:8] == [7, 6, 5, 4, 3, 1, 1, 0], indices[:8]
        assert indices[16:23] == [23, 22, 21, 20, 19, 18, 16], indices[16:23]
        assert indices.index(2) >= 23 and indices.index(17) >= 23
    for evidence in ("reversing block order", "dropped initial block=2", "dropped initial block=17",
                     "injected duplicate", "block=2 phase=repair", "block=17 phase=repair"):
        assert logged(a, evidence), evidence
    report(f"FILES browser send/receive progress observed; first blocks 7,6,5,4,3,1,1,0 "
           f"in both captures; encrypted filenames, {len(requests)} captured group MISSING datagrams, "
           f"largest group UDP payload {max(len(packet) for _, packet in wire.matching())} bytes; "
           f"Chrome/Edge media GET status counts 206={media_responses.count(206)}")
    block_proof = shared_blocks(wire, a, a_id, (b, c), received[0], old_key, initial_epoch, initial_group)
    report(f"ONE-TO-MANY A -> B/C accepted identical initial/repair block sequences {block_proof}")

    for index, tamper in enumerate(("signature", "gcm"), 9101):
        header, packet = fixture(b, room, initial_epoch, index, Kind.CHAT, old_key,
                                 b"tampered fixture", tamper=tamper)
        wire.transmit(packet)
        reason = "InvalidSignature" if tamper == "signature" else "InvalidTag"
        until(lambda: logged(a, f"kind=CHAT seq={header.seq} reason={reason}"),
              description=f"socket validation of {reason}")
    expect(pa.get_by_text("tampered fixture", exact=True)).to_have_count(0)
    fake_id = os.urandom(16)
    fake_boot = os.urandom(16)
    name = "corrupted-sample.bin"
    manifest = wire_json([fake_id.hex(), name, 9, hashlib.sha256(b"good data").hexdigest(), CHUNK])
    wire.transmit(fixture(b, room, initial_epoch, 9201, Kind.MANIFEST, old_key, manifest, boot=fake_boot)[1])
    wire.transmit(fixture(b, room, initial_epoch, 9202, Kind.BLOCK, old_key,
                          fake_id + INDEX.pack(0) + b"bad bytes", boot=fake_boot)[1])
    corrupted = file_row(pa, name)
    expect(corrupted).to_have_attribute("data-status", "failed", timeout=WAIT)
    expect(corrupted).to_contain_text("长度或 SHA-256 校验失败")
    assert a.request(f"/api/files/{b_id.hex()}-{fake_id.hex()}/download", allow_error=True)[0] == 404
    assert not (a.data / "files" / f"recv-{b_id.hex()}-{fake_id.hex()}.part").exists()
    report("INTEGRITY valid signature + tampered GCM tag rejected; bad Ed25519 signature rejected; "
           "authenticated but wrong-SHA file rejected and temporary data cleaned")

    transfer = browser_transfer(pb, [(a, pa), (c, pc)], SAMPLES / MEDIA[0], report)
    block_proof = shared_blocks(wire, b, b_id, (a, c), transfer, old_key, initial_epoch, initial_group)
    report(f"ONE-TO-MANY B -> A/C accepted identical initial/repair block sequences {block_proof}; "
           "owner A was a receiver")
    a.stop()
    for page in (pb, pc):
        expect(page.locator(f'.member[data-id="{a_id.hex()}"] .offline')).to_be_visible(timeout=WAIT)
        expect(page.get_by_role("button", name="发送", exact=True)).to_be_enabled()
    assert a.process.poll() is not None and ready(b, c)
    text = "房主进程已停止，成员 B 直接发送 🔗"
    chat(pb, text)
    expect_message(pc, text)
    browser_transfer(pb, [(c, pc)], SAMPLES / MEDIA[1], report)
    assert a.process.poll() is not None
    pc.screenshot(path=root / "C-owner-offline.png", full_page=True, animations="disabled")
    report(f"PEER-TO-PEER owner process stopped and marked offline; B -> C chat and repaired WebM "
           f"stayed on {initial_group} at epoch {initial_epoch}")
    a.start()
    until(lambda: ready(*nodes) and a.state()["epoch"] > initial_epoch, description="owner restart rotation")
    expect(pa.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=WAIT)

    pc.get_by_role("button", name="退出房间").click()
    expect(pc.get_by_role("heading", name="进入房间")).to_be_visible()
    for page in (pa, pb):
        expect(page.locator(f'.member[data-id="{c_id.hex()}"]')).to_have_count(0, timeout=WAIT)
    report("MEMBERS voluntary leave visible in A/B")
    join(pa, pc, "C", invite)
    expect(pa.locator("#members .online")).to_have_count(3, timeout=WAIT)
    before_restart = a.state()["epoch"]
    old_group = wire.watch_data(room, before_restart)
    c_offer = until(lambda: target_offer(wire, a_id, before_restart, c_id))
    last_c_key = unseal(a, c, *c_offer)
    c.stop()
    for page in (pa, pb):
        expect(page.locator(f'.member[data-id="{c_id.hex()}"] .offline')).to_be_visible(timeout=WAIT)
    pa.screenshot(path=root / "A-heartbeat-timeout.png", full_page=True, animations="disabled")
    c.start()
    expect(pc.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=WAIT)
    assert c.state()["epoch"] == before_restart
    expect(pa.locator("#members .online")).to_have_count(3, timeout=WAIT)
    report(f"MEMBERS heartbeat timeout visible in A/B; C fresh RSA sync after restart epoch={before_restart}")

    b.stop()
    expect(pa.locator(f'.member[data-id="{b_id.hex()}"] .offline')).to_be_visible(timeout=WAIT)
    pa.get_by_role("button", name="移除 C").click()
    expect(pc.get_by_role("alert")).to_have_text("房主已移除你。重新加入需要批准。", timeout=WAIT)
    expect(pa.get_by_role("button", name="发送", exact=True)).to_be_disabled()
    after = a.state()["epoch"]
    time.sleep(FENCE_DELAY + 0.5)
    assert not a.state()["ready"]
    assert not re.search(rf"READY pid=\d+ epoch={after}\b", a.log.read_text(encoding="utf-8"))
    b.start()
    expect(pa.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=WAIT)
    expect(pb.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=WAIT)
    new_group = wire.watch_data(room, after)
    assert new_group != old_group and a.state()["epoch"] == after
    assert after > before_restart and ready(a, b) and not c.state()["ready"]
    assert c.state()["dataGroup"] is None
    assert any("DROP" in line and f"channel=data dest={old_group[0]}:{old_group[1]}" in line
               for line in c.log.read_text(encoding="utf-8").splitlines())
    assert c.request("/api/chat", {"text": "removed"}, allow_error=True)[0] == 409
    assert logged(a, f"dropped first KEY epoch={after} member={b_id.hex()[:12]}")
    assert any("KEY_ACK" in line and f"epoch={after} member={b_id.hex()[:12]}" in line
               for line in a.log.read_text(encoding="utf-8").splitlines())
    key_header, key_packet = until(lambda: target_offer(wire, a_id, after, b_id))
    try:
        unseal(a, c, key_header, key_packet)
    except ValueError:
        pass
    else:
        raise AssertionError("C unsealed a key intended for B")
    assert not target_offer(wire, a_id, after, c_id)
    new_key = unseal(a, b, key_header, key_packet)
    assert last_c_key != new_key
    chat(pb, "撤权后 B → A ✅")
    expect_message(pa, "撤权后 B → A ✅")
    chat(pa, "撤权后 A → B 🔒")
    expect_message(pb, "撤权后 A → B 🔒")
    assert not c.state()["chat"]
    report(f"REVOCATION epoch {before_restart}->{after}, group {old_group}->{new_group}; new epoch "
           "held while B was absent; first B key delivery dropped; B confirmation observed after retry; "
           "A/B bidirectional; C HTTP 409")
    old = fixture(b, room, before_restart, 9301, Kind.CHAT, last_c_key, b"old epoch fixture")
    wire.transmit(old[1], new_group)
    until(lambda: logged(a, "kind=CHAT seq=9301 reason=old epoch or room not ready"))
    removed = fixture(c, room, before_restart, 9302, Kind.CHAT, last_c_key, b"removed peer fixture")
    wire.transmit(removed[1], new_group)
    until(lambda: logged(a, "kind=CHAT seq=9302 reason=unauthorized sender"))
    expect(pa.get_by_text("old epoch fixture", exact=True)).to_have_count(0)
    expect(pa.get_by_text("removed peer fixture", exact=True)).to_have_count(0)
    revoked_file = browser_transfer(pa, [(b, pb)], SAMPLES / MEDIA[0], report)
    assert c.request(f"/api/files/{revoked_file}/download", allow_error=True)[0] == 404
    for kind in (Kind.CHAT, Kind.MANIFEST, Kind.BLOCK):
        assert not wire.matching(kind=kind, sender=a_id, epoch=after, destination=old_group)
        header, packet = until(lambda: wire.matching(kind=kind, sender=a_id, epoch=after,
                                                      destination=new_group))[-1]
        try:
            plaintext(header, packet, last_c_key, a)
        except InvalidTag:
            pass
        else:
            raise AssertionError(f"stale key decrypted {kind.name}")
        assert plaintext(header, packet, new_key, a)
    report("REVOCATION no new payload on old group; actual new CHAT, MANIFEST and BLOCK cannot decrypt with C's key; "
           "C RSA cannot open B envelope; stale epoch & unauthorized sender rejected at A's socket")
    pc.screenshot(path=root / "C-removed.png", animations="disabled")
    pa.screenshot(path=root / "A-after-revocation.png", full_page=True, animations="disabled")

    pc.get_by_label("房间邀请", exact=True).fill(invite)
    pc.get_by_role("button", name="申请加入").click()
    expect(pc.get_by_text("等待房主批准加入")).to_be_visible(timeout=WAIT)
    expect(pc.get_by_role("button", name="发送", exact=True)).to_be_disabled()
    expect(pa.get_by_role("button", name="批准 C")).to_be_visible(timeout=WAIT)
    pa.get_by_role("button", name="批准 C").click()
    expect(pc.get_by_role("button", name="发送", exact=True)).to_be_enabled(timeout=WAIT)
    expect(pc.get_by_text("撤权后 A → B 🔒", exact=True)).to_have_count(0)
    expect(pc.get_by_text(MEDIA[0], exact=True)).to_have_count(0)
    report("READMISSION manual approval required; no excluded-period history")
    for index, members in enumerate((((b, pb), (c, pc)), ((c, pc), (b, pb)))):
        if index:
            join(pa, pc, "C", invite)
        check_rapid_exits(a, pa, members, invite)
        join(pa, pb, "B", invite)
        order = " -> ".join(node.name for node, _ in members)
        for sender, recipient, text in ((pa, pb, f"A 恢复后发给 B {order}"),
                                        (pb, pa, f"B 重新加入发给 A {order}")):
            chat(sender, text)
            expect_message(recipient, text)
        report(f"RAPID EXITS {order}, A drained the previous lease and recovered with its original "
               "invite; B rejoined and exchanged chat in both directions")
    assert not any(h.kind.encrypted for h, _ in wire.matching(destination=wire.destination))
    assert not errors, errors
    report("PASS browser JS errors=0")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / ".test-run")
    args = parser.parse_args()
    root = args.output.resolve() / datetime.now().strftime("browser-%Y%m%d-%H%M%S")
    root.mkdir(parents=True)
    report = Report(root)
    nodes = [Node(name, port, root, fault=name != "C") for name, port in zip("ABC", range(18761, 18764))]
    report(f"OUTPUT {root}")
    try:
        with WireTap() as wire, sync_playwright() as playwright:
            for node in nodes:
                node.start()
            until(lambda: all(node.state()["mode"] == "idle" for node in nodes),
                  description="three local Starlette listeners")
            browser = playwright.chromium.launch(executable_path=args.browser or browser_path(),
                                                 args=["--autoplay-policy=no-user-gesture-required"])
            try:
                acceptance(browser, root, nodes, wire, report)
                report("PASS full browser + multicast acceptance")
            finally:
                browser.close()
    finally:
        for node in nodes:
            node.stop()


if __name__ == "__main__":
    main()
