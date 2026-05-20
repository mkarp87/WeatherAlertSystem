import hashlib
import socket
import time

from skyalert_bridge.config import GroupConfig
from skyalert_bridge.managed_openbridge import HBPForwarder, dmrd_for_test


class FakeOpenBridge:
    def __init__(self):
        self.frames = []

    def send_dmrd(self, dmrd: bytes, network_id: int | None = None) -> int:
        self.frames.append((dmrd, network_id))
        return len(dmrd)


def recv(sock):
    sock.settimeout(2)
    return sock.recvfrom(2048)[0]


def test_hbp_forwarder_login_and_forward_dmrd():
    group = GroupConfig(name="ARC125", county_codes=("ARC125",), talkgroup=28515)
    fake = FakeOpenBridge()
    forwarder = HBPForwarder(group, "127.0.0.1", 0, "secret", fake)  # type: ignore[arg-type]
    forwarder.start()
    peer_id = (31000182).to_bytes(4, "big")
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        target = ("127.0.0.1", forwarder.actual_port)
        client.sendto(b"RPTL" + peer_id, target)
        ack = recv(client)
        assert ack.startswith(b"RPTACK")
        salt = ack[6:10]
        digest = bytes.fromhex(hashlib.sha256(salt + b"secret").hexdigest())
        client.sendto(b"RPTK" + peer_id + digest, target)
        assert recv(client) == b"RPTACK" + peer_id
        client.sendto(b"RPTC" + peer_id + b"0" * 300, target)
        assert recv(client) == b"RPTACK" + peer_id
        frame = dmrd_for_test(31000182, 1234567, 28515, slot=1)
        client.sendto(frame, target)
        deadline = time.monotonic() + 2
        while not fake.frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(fake.frames) == 1
        forwarded, network_id = fake.frames[0]
        assert forwarded[5:11] == frame[5:11]
        assert forwarded[16:20] != frame[16:20]
        assert network_id is None
    finally:
        client.close()
        forwarder.stop()


def test_hbp_forwarder_rewrites_stream_id_per_group():
    group = GroupConfig(name="ARC119", county_codes=("ARC119",), talkgroup=28514)
    fake = FakeOpenBridge()
    forwarder = HBPForwarder(group, "127.0.0.1", 0, "secret", fake)  # type: ignore[arg-type]
    forwarder.start()
    peer_id = (31000183).to_bytes(4, "big")
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        target = ("127.0.0.1", forwarder.actual_port)
        client.sendto(b"RPTL" + peer_id, target)
        ack = recv(client)
        salt = ack[6:10]
        digest = bytes.fromhex(hashlib.sha256(salt + b"secret").hexdigest())
        client.sendto(b"RPTK" + peer_id + digest, target)
        assert recv(client) == b"RPTACK" + peer_id
        client.sendto(b"RPTC" + peer_id + b"0" * 300, target)
        assert recv(client) == b"RPTACK" + peer_id
        frame = dmrd_for_test(31000183, 310002, 28514, slot=1, stream_id=1234)
        client.sendto(frame, target)
        deadline = time.monotonic() + 2
        while not fake.frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(fake.frames) == 1
        forwarded, network_id = fake.frames[0]
        assert forwarded[5:8] == (310002).to_bytes(3, "big")
        assert forwarded[8:11] == (28514).to_bytes(3, "big")
        assert forwarded[16:20] != (1234).to_bytes(4, "big")
        assert network_id is None
    finally:
        client.close()
        forwarder.stop()
