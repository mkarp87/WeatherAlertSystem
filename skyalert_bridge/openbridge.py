from __future__ import annotations

import hmac
import hashlib
import socket
import threading
from dataclasses import dataclass


DMRD = b"DMRD"
OPENBRIDGE_SLOT_BYTE = 15
OPENBRIDGE_TS2_FLAG = 0x80


def int_to_3(value: int) -> bytes:
    if value < 0 or value > 0xFFFFFF:
        raise ValueError(f"24-bit DMR value out of range: {value}")
    return value.to_bytes(3, "big")


def int_to_4(value: int) -> bytes:
    if value < 0 or value > 0xFFFFFFFF:
        raise ValueError(f"32-bit value out of range: {value}")
    return value.to_bytes(4, "big")


@dataclass(frozen=True)
class OpenBridgeEndpoint:
    local_ip: str
    local_port: int
    target_ip: str
    target_port: int
    passphrase: str
    network_id: int


class OpenBridgeSender:
    """Minimal OpenBridge UDP sender.

    DMRD payloads are 53 bytes. OpenBridge replaces bytes 11:15 with NETWORK_ID,
    forces the DMRD slot flag to time slot 1, and appends HMAC-SHA1 over the
    final 53-byte DMRD frame. Most OpenBridge receivers reject group traffic
    when byte 15 has the TS2 flag set.
    """

    def __init__(self, endpoint: OpenBridgeEndpoint):
        self.endpoint = endpoint
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((endpoint.local_ip, endpoint.local_port))
        self._lock = threading.Lock()
        self.sent_packets = 0
        self.sent_bytes = 0

    def counters(self) -> tuple[int, int]:
        with self._lock:
            return self.sent_packets, self.sent_bytes

    def close(self) -> None:
        self.sock.close()

    @staticmethod
    def force_slot1(dmrd: bytes) -> bytes:
        """Return a DMRD frame with the OpenBridge slot flag cleared.

        HBLink-style OpenBridge receivers derive the slot from bit 0x80 of
        DMRD byte 15. Clearing that bit makes the packet slot 1 while preserving
        the lower frame-type / voice-sequence bits.
        """
        if len(dmrd) != 53:
            raise ValueError(f"DMRD frame must be 53 bytes before forcing slot, got {len(dmrd)}")
        if not (dmrd[OPENBRIDGE_SLOT_BYTE] & OPENBRIDGE_TS2_FLAG):
            return dmrd
        fixed = bytearray(dmrd)
        fixed[OPENBRIDGE_SLOT_BYTE] &= ~OPENBRIDGE_TS2_FLAG
        return bytes(fixed)

    def build_packet(self, dmrd: bytes, network_id: int | None = None) -> bytes:
        if not dmrd.startswith(DMRD):
            raise ValueError("OpenBridge can only send DMRD frames")
        if len(dmrd) == 55:
            # Some HomeBrew code carries two RSSI bytes; OpenBridge HMAC covers 53 bytes.
            dmrd = dmrd[:53]
        if len(dmrd) != 53:
            raise ValueError(f"DMRD frame must be 53 bytes for OpenBridge, got {len(dmrd)}")
        dmrd = self.force_slot1(dmrd)
        effective_network_id = self.endpoint.network_id if network_id is None else int(network_id)
        network_id_bytes = int_to_4(effective_network_id)
        data = b"".join([dmrd[:11], network_id_bytes, dmrd[15:]])
        digest = hmac.new(self.endpoint.passphrase.encode("utf-8"), data, hashlib.sha1).digest()
        return data + digest

    def send_dmrd(self, dmrd: bytes, network_id: int | None = None) -> int:
        packet = self.build_packet(dmrd, network_id=network_id)
        sent = self.sock.sendto(packet, (self.endpoint.target_ip, self.endpoint.target_port))
        with self._lock:
            self.sent_packets += 1
            self.sent_bytes += sent
        return sent
