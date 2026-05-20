from __future__ import annotations

import logging
import socket
import struct
import time
from pathlib import Path

from .audio import read_pcm_chunks
from .config import AnalogBridgeUSRPConfig

logger = logging.getLogger(__name__)

USRP_TYPE_VOICE = 0
USRP_TYPE_DTMF = 1
USRP_TYPE_TEXT = 2
USRP_TYPE_PING = 3
USRP_TYPE_TLV = 4
USRP_TYPE_VOICE_ADPCM = 5
USRP_TYPE_VOICE_ULAW = 6

TLV_TAG_REMOTE_CMD = 5
TLV_TAG_SET_INFO = 8


class USRPClient:
    def __init__(self, cfg: AnalogBridgeUSRPConfig):
        self.cfg = cfg
        self.seq = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if cfg.local_rx_port is not None:
            self.sock.bind(("", cfg.local_rx_port))

    def close(self) -> None:
        self.sock.close()

    def _wire_type(self, packet_type: int) -> int:
        # pyUC and Analog_Bridge use the high-order byte for non-voice packet type.
        if packet_type == USRP_TYPE_VOICE:
            return 0
        return packet_type << 24

    def send_packet(self, packet_type: int, payload: bytes, *, keyup: int = 0, talkgroup: int = 0) -> int:
        header = b"USRP" + struct.pack(
            ">iiiiiii",
            self.seq,
            0,                  # memory
            int(keyup),
            int(talkgroup),
            self._wire_type(packet_type),
            0,                  # mpxid
            0,                  # reserved
        )
        sent = self.sock.sendto(header + payload, (self.cfg.address, self.cfg.tx_port))
        self.seq = (self.seq + 1) & 0xFFFF
        return sent

    def send_text(self, text: str) -> None:
        self.send_packet(USRP_TYPE_TEXT, text.encode("ascii", errors="ignore"))

    def send_dtmf(self, text: str) -> None:
        self.send_packet(USRP_TYPE_DTMF, text.encode("ascii", errors="ignore"))

    def send_remote_command(self, command: str) -> None:
        payload = command.encode("ascii", errors="ignore")
        if len(payload) > 255:
            raise ValueError("Remote command too long for one TLV packet")
        tlv = struct.pack("BB", TLV_TAG_REMOTE_CMD, len(payload)) + payload
        self.send_packet(USRP_TYPE_TLV, tlv)

    def register(self) -> None:
        self.send_text("REG:DVSWITCH")

    def unregister(self) -> None:
        self.send_text("REG:UNREG")

    def request_info(self) -> None:
        self.send_text("INFO:")

    def set_talkgroup(self, talkgroup: int, slot: int | None = None) -> None:
        if slot is not None:
            self.send_remote_command(f"txTs={slot}")
        self.send_remote_command(f"tgs={talkgroup}")
        self.send_remote_command(f"txTg={talkgroup}")
        self.send_dtmf(str(talkgroup))

    def set_metadata(self, talkgroup: int) -> None:
        # SET_INFO TLV: source ID (3), repeater ID (4), dest TG (3), TS (1), CC (1), callsign\0
        call = self.cfg.callsign.encode("ascii", errors="ignore") + b"\x00"
        dmr_id = self.cfg.subscriber_id
        repeater_id = self.cfg.repeater_id
        tlv_len = 3 + 4 + 3 + 1 + 1 + len(call)
        payload = bytearray([TLV_TAG_SET_INFO, tlv_len])
        payload.extend(dmr_id.to_bytes(3, "big", signed=False))
        payload.extend(repeater_id.to_bytes(4, "big", signed=False))
        payload.extend(int(talkgroup).to_bytes(3, "big", signed=False))
        payload.append(int(self.cfg.slot) & 0xFF)
        payload.append(int(self.cfg.color_code) & 0xFF)
        payload.extend(call)
        self.send_packet(USRP_TYPE_TLV, bytes(payload))

    def prepare_for_group(self, talkgroup: int) -> None:
        if self.cfg.register:
            self.register()
            time.sleep(0.1)
        for command in self.cfg.pre_tx_commands:
            if command:
                self.send_remote_command(command)
                time.sleep(0.05)
        self.send_remote_command("ambeMode=DMR")
        self.set_talkgroup(talkgroup, self.cfg.slot)
        self.set_metadata(talkgroup)
        time.sleep(0.2)

    def stream_wav(self, wav_path: Path, talkgroup: int) -> tuple[int, int]:
        frame_samples = int(self.cfg.sample_rate * self.cfg.frame_ms / 1000)
        delay = self.cfg.frame_ms / 1000.0
        first = True
        packets = 0
        bytes_sent = 0
        for chunk in read_pcm_chunks(wav_path, frame_samples):
            bytes_sent += self.send_packet(USRP_TYPE_VOICE, chunk, keyup=1, talkgroup=talkgroup)
            packets += 1
            if first:
                logger.debug("Started USRP TX for TG %s", talkgroup)
                first = False
            time.sleep(delay)
        # One explicit unkey packet with silence.
        bytes_sent += self.send_packet(USRP_TYPE_VOICE, b"\x00\x00" * frame_samples, keyup=0, talkgroup=talkgroup)
        packets += 1
        return packets, bytes_sent
