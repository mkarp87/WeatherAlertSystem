import hashlib
import hmac
import socket
import time
from pathlib import Path

from skyalert_bridge.config import load_config
from skyalert_bridge.managed_openbridge import HBPForwarder, dmrd_for_test
from skyalert_bridge.openbridge import OpenBridgeEndpoint, OpenBridgeSender


def _recv(sock, timeout=2.0):
    sock.settimeout(timeout)
    return sock.recvfrom(2048)[0]


def _login_and_send(group, forwarder, frames):
    peer_id_int = group.bridge.usrp.repeater_id
    peer_id = peer_id_int.to_bytes(4, "big")
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2.0)
    target = ("127.0.0.1", forwarder.actual_port)
    try:
        client.sendto(b"RPTL" + peer_id, target)
        ack = _recv(client)
        salt = ack[6:10]
        digest = bytes.fromhex(hashlib.sha256(salt + b"test-password").hexdigest())
        client.sendto(b"RPTK" + peer_id + digest, target)
        assert _recv(client) == b"RPTACK" + peer_id
        client.sendto(b"RPTC" + peer_id + b"0" * 300, target)
        assert _recv(client) == b"RPTACK" + peer_id
        for seq in range(1, frames + 1):
            client.sendto(
                dmrd_for_test(
                    peer_id_int,
                    group.bridge.usrp.subscriber_id,
                    group.talkgroup,
                    seq=seq,
                    slot=group.bridge.usrp.slot,
                    stream_id=0xABC000 + group.talkgroup,
                ),
                target,
            )
    finally:
        client.close()


def test_openbridge_loopback_receives_multiple_group_talkgroups(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
app:
  log_level: WARNING
  state_file: ./state/state.json
  audio_dir: ./state/audio
openbridge:
  target_ip: 127.0.0.1
  target_port: 9
  passphrase: loopback-secret
  network_id: 31000182
station:
  callsign: NC4ES
  source_id: 1234567
  repeater_id: 31000182
  slot: 1
  color_code: 1
internal_ports:
  start: 47000
  step: 10
groups:
  - name: ARC125
    enabled: true
    county_codes: [ARC125]
    talkgroup: 28515
  - name: ARC119
    enabled: true
    county_codes: [ARC119]
    talkgroup: 119
""",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(2.0)
    sender = OpenBridgeSender(
        OpenBridgeEndpoint(
            local_ip="127.0.0.1",
            local_port=0,
            target_ip="127.0.0.1",
            target_port=listener.getsockname()[1],
            passphrase=cfg.output.direct_openbridge.passphrase,
            network_id=cfg.output.direct_openbridge.network_id,
        )
    )
    forwarders = []
    try:
        for group in cfg.groups:
            fwd = HBPForwarder(group, "127.0.0.1", 0, "test-password", sender)
            fwd.start()
            forwarders.append(fwd)
        for group, forwarder in zip(cfg.groups, forwarders):
            _login_and_send(group, forwarder, frames=2)
        packets = []
        deadline = time.monotonic() + 2.0
        while len(packets) < 4 and time.monotonic() < deadline:
            try:
                packets.append(_recv(listener, timeout=deadline - time.monotonic()))
            except socket.timeout:
                break
        assert len(packets) == 4
        tgs = [int.from_bytes(packet[8:11], "big") for packet in packets]
        assert tgs == [28515, 28515, 119, 119]
        network_ids = [int.from_bytes(packet[11:15], "big") for packet in packets]
        assert network_ids == [31000182, 31000182, 31000182, 31000182]
        for packet in packets:
            assert len(packet) == 73
            assert packet[:4] == b"DMRD"
            assert packet[53:] == hmac.new(b"loopback-secret", packet[:53], hashlib.sha1).digest()
    finally:
        for forwarder in forwarders:
            forwarder.stop()
        sender.close()
        listener.close()
