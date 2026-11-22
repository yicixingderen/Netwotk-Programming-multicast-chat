import asyncio
import logging
import os
import socket
import sys

MAX_DATAGRAM = 1200
log = logging.getLogger("multicast.socket")


def multicast_options(sock, interface, ttl):
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)


def open_sender(interface, ttl):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        multicast_options(sock, interface, ttl)
        return sock
    except BaseException:
        sock.close()
        raise


def membership(group, interface):
    return socket.inet_aton(group) + socket.inet_aton(interface)


def open_multicast(group, port, interface, ttl):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        sock.bind(("", port))
        multicast_options(sock, interface, ttl)
        if sys.platform.startswith("linux"):
            # IP_MULTICAST_ALL: deliver only groups subscribed on this socket.
            sock.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", 49), 0)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership(group, interface))
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


class Multicast:
    def __init__(self, config, destination, channel, on_packet):
        self.config = config
        self.destination = destination
        self.channel = channel
        self.on_packet = on_packet
        self.socket = None
        self.reader = None
        self.received = 0

    def start(self):
        self.socket = open_multicast(*self.destination, self.config.interface, self.config.ttl)
        self.reader = asyncio.create_task(self.read(self.socket))
        log.info("JOIN pid=%d channel=%s dest=%s:%s interface=%s ttl=%d", os.getpid(), self.channel,
                 *self.destination, self.config.interface, self.config.ttl)

    async def read(self, sock):
        loop = asyncio.get_running_loop()
        while self.socket is sock:
            try:
                packet, address = await loop.sock_recvfrom(sock, 65535)
            except OSError as exc:
                if self.socket is not sock:
                    break
                log.warning("UDP receive channel=%s error=%s", self.channel, exc)
                await asyncio.sleep(0.1)
                continue
            if self.socket is sock and len(packet) <= MAX_DATAGRAM:
                self.received += 1
                self.on_packet(packet, address, self)

    def send(self, packet):
        if len(packet) > MAX_DATAGRAM:
            raise ValueError("UDP payload exceeds 1200 bytes")
        try:
            self.socket.sendto(packet, self.destination)
        except BlockingIOError:
            log.warning("UDP send buffer full channel=%s dest=%s:%s", self.channel, *self.destination)

    def close(self):
        sock, self.socket = self.socket, None
        if sock is not None:
            self.reader.cancel()
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP,
                                membership(self.destination[0], self.config.interface))
            except OSError as exc:
                log.warning("UDP drop failed channel=%s error=%s", self.channel, exc)
            finally:
                sock.close()
            log.info("DROP pid=%d channel=%s dest=%s:%s received=%d", os.getpid(), self.channel,
                     *self.destination, self.received)
