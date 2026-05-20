import hmac
import hashlib

from skyalert_bridge.openbridge import OpenBridgeEndpoint, OpenBridgeSender, int_to_3, int_to_4


def test_openbridge_packet_rewrites_network_id_and_hmac():
    endpoint = OpenBridgeEndpoint(
        local_ip="127.0.0.1",
        local_port=0,
        target_ip="127.0.0.1",
        target_port=9,
        passphrase="secret",
        network_id=1234,
    )
    sender = OpenBridgeSender(endpoint)
    try:
        dmrd = b"DMRD" + bytes([1]) + int_to_3(1001) + int_to_3(2002) + int_to_4(9999) + bytes([0x21]) + int_to_4(5555) + (b"\x00" * 33)
        assert len(dmrd) == 53
        packet = sender.build_packet(dmrd)
        assert len(packet) == 73
        assert packet[11:15] == int_to_4(1234)
        assert packet[-20:] == hmac.new(b"secret", packet[:53], hashlib.sha1).digest()
    finally:
        sender.close()


def test_openbridge_packet_forces_slot1_before_hmac():
    endpoint = OpenBridgeEndpoint(
        local_ip="127.0.0.1",
        local_port=0,
        target_ip="127.0.0.1",
        target_port=9,
        passphrase="secret",
        network_id=1234,
    )
    sender = OpenBridgeSender(endpoint)
    try:
        # Byte 15 bit 0x80 means slot 2 to HBLink/OpenBridge. The sender must
        # clear it before HMAC signing so receivers accept the frame as slot 1.
        dmrd = b"DMRD" + bytes([1]) + int_to_3(1001) + int_to_3(2002) + int_to_4(9999) + bytes([0xA1]) + int_to_4(5555) + (b"\x00" * 33)
        assert len(dmrd) == 53
        packet = sender.build_packet(dmrd)
        assert packet[15] == 0x21
        assert packet[-20:] == hmac.new(b"secret", packet[:53], hashlib.sha1).digest()
    finally:
        sender.close()


def test_openbridge_packet_can_use_per_group_network_id_override():
    endpoint = OpenBridgeEndpoint(
        local_ip="127.0.0.1",
        local_port=0,
        target_ip="127.0.0.1",
        target_port=9,
        passphrase="secret",
        network_id=1234,
    )
    sender = OpenBridgeSender(endpoint)
    try:
        dmrd = b"DMRD" + bytes([1]) + int_to_3(1001) + int_to_3(2002) + int_to_4(9999) + bytes([0x21]) + int_to_4(5555) + (b"\x00" * 33)
        packet = sender.build_packet(dmrd, network_id=31000183)
        assert packet[11:15] == int_to_4(31000183)
        assert packet[-20:] == hmac.new(b"secret", packet[:53], hashlib.sha1).digest()
    finally:
        sender.close()
