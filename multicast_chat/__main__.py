import argparse
import ipaddress
import logging
import sys
from pathlib import Path

import uvicorn

from .room import name_text
from .web import create_app


def options():
    parser = argparse.ArgumentParser(description="IPv4 multicast chat with a local web interface")
    parser.add_argument("--name", required=True)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--http-port", type=int, default=8761, help="local HTTP port (0 selects an available port)")
    parser.add_argument("--group", default="239.255.42.99", help="IPv4 rendezvous group")
    parser.add_argument("--udp-port", type=int, default=50420, help="shared UDP port for rendezvous and room groups")
    parser.add_argument("--interface", default="127.0.0.1")
    parser.add_argument("--ttl", type=int, default=1)
    parser.add_argument("--file-limit-mib", type=int, default=32)
    parser.add_argument("--rate-kib", type=int, default=320, help="maximum outgoing file payload KiB/s")
    parser.add_argument("--fault-demo", action="store_true", help="simulate multicast loss/reorder/duplication")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    try:
        name_text(args.name)
        if not ipaddress.IPv4Address(args.group).is_multicast:
            raise ValueError("group must be an IPv4 multicast address")
        interface = ipaddress.IPv4Address(args.interface)
        if interface.is_multicast or interface.is_unspecified:
            raise ValueError("interface must be a local unicast IPv4 address")
        if not 0 <= args.http_port <= 65535 or not 1 <= args.udp_port <= 65535:
            raise ValueError("HTTP port must be 0–65535 and UDP port must be 1–65535")
        if not 1 <= args.ttl <= 255:
            raise ValueError("TTL must be between 1 and 255")
        if not 1 <= args.file_limit_mib <= 1024 or not 16 <= args.rate_kib <= 1024:
            raise ValueError("file limit must be 1–1024 MiB and rate 16–1024 KiB/s")
    except ValueError as exc:
        parser.error(str(exc))
    args.file_limit = args.file_limit_mib * 1024 * 1024
    return args


def main():
    args = options()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(args), host="127.0.0.1", port=args.http_port,
                access_log=False, timeout_graceful_shutdown=2, ws="none")


if __name__ == "__main__":
    main()
