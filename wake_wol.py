#!/usr/bin/env python3
import argparse
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pcapy


DEFAULT_COOLDOWN_SEC = 5 * 60
DEFAULT_PING_TIMEOUT_SEC = 1
DEFAULT_PING_COUNT = 1
DEFAULT_DEVICE_FILE = "devices.txt"
DEFAULT_SNAPLEN = 96
DEFAULT_PROMISC = 0
DEFAULT_READ_TIMEOUT_MS = 250

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("wol-listener-pcapy")

stop_event = threading.Event()


@dataclass
class Device:
    interface: str
    target_ip: str
    target_port: int
    target_mac: str
    cooldown_sec: int = DEFAULT_COOLDOWN_SEC
    ping_timeout_sec: int = DEFAULT_PING_TIMEOUT_SEC
    ping_count: int = DEFAULT_PING_COUNT
    last_wol_time: float = field(default=0.0)

    def key(self) -> Tuple[str, str, int]:
        return (self.interface, self.target_ip, self.target_port)

    def in_cooldown(self) -> bool:
        return (time.time() - self.last_wol_time) < self.cooldown_sec

    def remaining_cooldown(self) -> int:
        remaining = self.cooldown_sec - (time.time() - self.last_wol_time)
        return max(0, int(remaining))


class DeviceRegistry:
    def __init__(self, devices: List[Device]):
        self.devices = devices
        self.lock = threading.Lock()
        self.by_interface: Dict[str, Dict[Tuple[str, int], Device]] = {}
        for dev in devices:
            self.by_interface.setdefault(dev.interface, {})
            self.by_interface[dev.interface][(dev.target_ip, dev.target_port)] = dev

    def get_interfaces(self) -> List[str]:
        return list(self.by_interface.keys())

    def get_interface_devices(self, interface: str) -> List[Device]:
        return list(self.by_interface.get(interface, {}).values())

    def get_device(self, interface: str, dst_ip: str, dst_port: int) -> Optional[Device]:
        return self.by_interface.get(interface, {}).get((dst_ip, dst_port))


def normalize_mac(mac: str) -> str:
    mac_clean = mac.replace(":", "").replace("-", "").lower()
    if len(mac_clean) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    return ":".join(mac_clean[i:i + 2] for i in range(0, 12, 2))


def ping_host(ip: str, count: int, timeout_sec: int) -> bool:
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout_sec), ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.error("Ping failed for %s: %s", ip, exc)
        return False


def send_wol_broadcast(mac: str, udp_port: int = 9) -> None:
    mac_clean = mac.replace(":", "").replace("-", "")
    mac_bytes = bytes.fromhex(mac_clean)
    magic_packet = b"\xff" * 6 + mac_bytes * 16

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic_packet, ("255.255.255.255", udp_port))
    finally:
        sock.close()


def parse_device_line(line: str, line_no: int) -> Optional[Device]:
    line = line.strip()

    if not line or line.startswith("#"):
        return None

    if line.endswith(";"):
        line = line[:-1]

    parts = [p.strip() for p in line.split(";")]

    if len(parts) < 4:
        raise ValueError(
            f"Line {line_no}: expected at least 4 fields "
            f"(interface;ip;port;mac[;cooldown_sec])"
        )

    interface = parts[0]
    target_ip = parts[1]
    target_port = int(parts[2])
    target_mac = normalize_mac(parts[3])

    cooldown_sec = DEFAULT_COOLDOWN_SEC
    if len(parts) >= 5 and parts[4]:
        cooldown_sec = int(parts[4])

    return Device(
        interface=interface,
        target_ip=target_ip,
        target_port=target_port,
        target_mac=target_mac,
        cooldown_sec=cooldown_sec,
    )


def load_devices_from_file(filepath: str) -> List[Device]:
    devices: List[Device] = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            dev = parse_device_line(line, line_no)
            if dev:
                devices.append(dev)
    return devices


def load_devices_from_args(args) -> List[Device]:
    if args.interface and args.target_ip and args.target_port and args.target_mac:
        return [
            Device(
                interface=args.interface,
                target_ip=args.target_ip,
                target_port=args.target_port,
                target_mac=normalize_mac(args.target_mac),
                cooldown_sec=args.cooldown,
            )
        ]
    return []


def build_bpf_for_interface(devices: List[Device]) -> str:
    clauses = []
    for dev in devices:
        clauses.append(
            f"(dst host {dev.target_ip} and dst port {dev.target_port})"
        )

    joined = " or ".join(clauses)
    return (
        f"tcp and ({joined}) and "
        f"(tcp[13] & 0x02 != 0) and (tcp[13] & 0x10 = 0)"
    )


def parse_ipv4_tcp_packet(packet: bytes) -> Optional[Tuple[str, int, str, int]]:
    # Ethernet header = 14 bytes
    if len(packet) < 14 + 20:
        return None

    eth_type = struct.unpack("!H", packet[12:14])[0]
    if eth_type != 0x0800:
        return None  # not IPv4

    ip_offset = 14
    version_ihl = packet[ip_offset]
    version = version_ihl >> 4
    if version != 4:
        return None

    ihl = (version_ihl & 0x0F) * 4
    if len(packet) < 14 + ihl + 20:
        return None

    protocol = packet[ip_offset + 9]
    if protocol != 6:
        return None  # not TCP

    src_ip = socket.inet_ntoa(packet[ip_offset + 12:ip_offset + 16])
    dst_ip = socket.inet_ntoa(packet[ip_offset + 16:ip_offset + 20])

    tcp_offset = ip_offset + ihl
    src_port, dst_port = struct.unpack("!HH", packet[tcp_offset:tcp_offset + 4])

    return src_ip, src_port, dst_ip, dst_port


def handle_match(interface: str, registry: DeviceRegistry, src_ip: str, src_port: int, dst_ip: str, dst_port: int) -> None:
    device = registry.get_device(interface, dst_ip, dst_port)
    if device is None:
        return

    logger.info(
        "Observed SYN on %s: %s:%s -> %s:%s",
        interface, src_ip, src_port, dst_ip, dst_port
    )

    with registry.lock:
        if device.in_cooldown():
            logger.info(
                "Cooldown active for %s:%s on %s, remaining %s sec",
                device.target_ip,
                device.target_port,
                device.interface,
                device.remaining_cooldown(),
            )
            return

        if ping_host(device.target_ip, device.ping_count, device.ping_timeout_sec):
            logger.info("Target %s responded to ping, no WoL needed", device.target_ip)
            return

        logger.warning(
            "Target %s did not respond to ping, sending WoL to %s",
            device.target_ip,
            device.target_mac,
        )

        try:
            send_wol_broadcast(device.target_mac, udp_port=9)
            device.last_wol_time = time.time()
            logger.info(
                "Cooldown started for %s sec on %s:%s",
                device.cooldown_sec,
                device.target_ip,
                device.target_port,
            )
        except Exception as exc:
            logger.error("Failed to send WoL for %s: %s", device.target_ip, exc)


def sniff_worker(interface: str, registry: DeviceRegistry) -> None:
    devices = registry.get_interface_devices(interface)
    bpf_filter = build_bpf_for_interface(devices)

    logger.info("Starting capture on %s with filter: %s", interface, bpf_filter)

    try:
        cap = pcapy.open_live(
            interface,
            DEFAULT_SNAPLEN,
            DEFAULT_PROMISC,
            DEFAULT_READ_TIMEOUT_MS
        )
        cap.setfilter(bpf_filter)
    except Exception as exc:
        logger.error("Failed to open capture on %s: %s", interface, exc)
        return

    while not stop_event.is_set():
        try:
            header, packet = cap.next()
            if not header:
                continue

            parsed = parse_ipv4_tcp_packet(packet)
            if not parsed:
                continue

            src_ip, src_port, dst_ip, dst_port = parsed
            handle_match(interface, registry, src_ip, src_port, dst_ip, dst_port)
        except Exception as exc:
            logger.error("Capture error on %s: %s", interface, exc)
            time.sleep(1)

    logger.info("Capture stopped on %s", interface)


def shutdown_handler(signum, frame):
    logger.info("Received signal %s, shutting down", signum)
    stop_event.set()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Passive WoL listener using pcapy-ng + kernel BPF SYN filter"
    )
    parser.add_argument("-I", dest="interface", help="Interface to sniff on")
    parser.add_argument("-IP", dest="target_ip", help="Target IP to watch")
    parser.add_argument("-P", dest="target_port", type=int, help="Target port to watch")
    parser.add_argument("-M", dest="target_mac", help="Target MAC for WoL")
    parser.add_argument(
        "-C",
        dest="cooldown",
        type=int,
        default=DEFAULT_COOLDOWN_SEC,
        help=f"Cooldown in seconds (default: {DEFAULT_COOLDOWN_SEC})",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="device_file",
        default=DEFAULT_DEVICE_FILE,
        help=f"Device file to load if CLI args are not given (default: {DEFAULT_DEVICE_FILE})",
    )
    return parser.parse_args()


def main() -> int:
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    args = parse_args()
    devices = load_devices_from_args(args)

    if not devices:
        if not os.path.isfile(args.device_file):
            logger.error(
                "No CLI device specified and device file not found: %s",
                args.device_file,
            )
            return 1
        try:
            devices = load_devices_from_file(args.device_file)
        except Exception as exc:
            logger.error("Failed to load device file %s: %s", args.device_file, exc)
            return 1

    if not devices:
        logger.error("No devices configured")
        return 1

    registry = DeviceRegistry(devices)

    logger.info("Loaded %s device(s)", len(devices))
    for dev in devices:
        logger.info(
            "Watching %s -> %s:%s MAC %s cooldown=%ss",
            dev.interface,
            dev.target_ip,
            dev.target_port,
            dev.target_mac,
            dev.cooldown_sec,
        )

    threads = []
    for interface in registry.get_interfaces():
        t = threading.Thread(
            target=sniff_worker,
            args=(interface, registry),
            daemon=True,
            name=f"sniff-{interface}",
        )
        t.start()
        threads.append(t)

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_event.set()

    for t in threads:
        t.join(timeout=2)

    logger.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
