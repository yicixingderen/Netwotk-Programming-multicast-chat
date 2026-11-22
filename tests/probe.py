"""Run three independent Winsock processes with a single group send."""
import os
import select
import socket
import subprocess
import sys

from multicast_chat.network import membership, open_multicast, open_sender

GROUP = "239.255.42.99"
PORT = 50420
PAYLOAD = b"multicast-probe-once"


def child(role):
    if role == "A":
        with open_sender("127.0.0.1", 1) as sock:
            sock.sendto(PAYLOAD, (GROUP, PORT))
        print(f"A pid={os.getpid()} TX dest={GROUP}:{PORT} send-only payload={PAYLOAD!r}")
        return
    with open_multicast(GROUP, PORT, "127.0.0.1", 1) as sock:
        sock.settimeout(4)
        print("READY", flush=True)
        packet, source = sock.recvfrom(1200)
        assert packet == PAYLOAD
        print(f"{role} pid={os.getpid()} RX source={source} joined={GROUP}:{PORT} payload={packet!r}")


def membership_probe():
    port = 50422
    data = "239.255.43.99"
    interface = "127.0.0.1"
    with (open_multicast(GROUP, port, interface, 1) as rendezvous,
          open_multicast(data, port, interface, 1) as member,
          open_multicast(data, port, interface, 1) as leaving,
          open_sender(interface, 1) as sender):
        for sock in (member, leaving):
            sock.settimeout(2)
        sender.sendto(b"first", (data, port))
        assert [sock.recv(1200) for sock in (member, leaving)] == [b"first"] * 2
        assert not select.select([rendezvous], [], [], 0.25)[0]
        leaving.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership(data, interface))
        sender.sendto(b"second", (data, port))
        assert member.recv(1200) == b"second"
        assert not select.select([rendezvous, leaving], [], [], 0.25)[0]
    print(f"PASS same UDP port {port}: control-only socket and departed socket received no private "
          "datagram; a still-subscribed socket received it")


def main():
    nodes = [subprocess.Popen([sys.executable, "-m", "tests.probe", role],
                              stdout=subprocess.PIPE, text=True) for role in ("B", "C")]
    try:
        for node in nodes:
            assert node.stdout.readline().strip() == "READY"
        subprocess.run([sys.executable, "-m", "tests.probe", "A"], check=True)
        for node in nodes:
            output, _ = node.communicate(timeout=6)
            assert node.returncode == 0
            print(output.strip())
    finally:
        for node in nodes:
            if node.poll() is None:
                node.kill()
                node.wait()
    membership_probe()


if __name__ == "__main__":
    if len(sys.argv) == 2:
        child(sys.argv[1])
    else:
        main()
