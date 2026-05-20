from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .config import AnalogBridgeUSRPConfig, Config, GroupConfig
from .dmr_packetizer import dmrd_packets_from_ambe72
from .encoder import build_encoder
from .openbridge import OpenBridgeEndpoint, OpenBridgeSender
from .process_manager import ProcessSupervisor
from .managed_openbridge import ManagedOpenBridgeSupervisor
from .usrp import USRPClient

logger = logging.getLogger(__name__)


class BaseTransmitter:
    def transmit(self, group: GroupConfig, wav_path: Path, text: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class DryRunTransmitter(BaseTransmitter):
    def transmit(self, group: GroupConfig, wav_path: Path, text: str) -> None:
        logger.info("DRY-RUN TG %s group %s wav=%s text=%s", group.talkgroup, group.name, wav_path, text)


class DirectOpenBridgeTransmitter(BaseTransmitter):
    def __init__(self, cfg: Config):
        ob = cfg.output.direct_openbridge
        endpoint = OpenBridgeEndpoint(
            local_ip=ob.local_ip,
            local_port=ob.local_port,
            target_ip=ob.target_ip,
            target_port=ob.target_port,
            passphrase=ob.passphrase,
            network_id=ob.network_id,
        )
        self.cfg = cfg
        self.sender = OpenBridgeSender(endpoint)
        self.encoder = build_encoder(cfg.encoder, ob.silence_ambe72_hex)
        self.silence_frame = bytes.fromhex(ob.silence_ambe72_hex)
        if len(self.silence_frame) != 9:
            raise ValueError("output.direct_openbridge.silence_ambe72_hex must decode to 9 bytes")
        self.delay = ob.frame_interval_ms / 1000.0

    def close(self) -> None:
        self.sender.close()

    def transmit(self, group: GroupConfig, wav_path: Path, text: str) -> None:  # noqa: ARG002
        ob = self.cfg.output.direct_openbridge
        frames = list(self.encoder.encode(wav_path))
        if not frames:
            raise RuntimeError("AMBE encoder produced no frames")
        count = 0
        for packet in dmrd_packets_from_ambe72(
            frames,
            rf_src_id=ob.source_id,
            dst_talkgroup=group.talkgroup,
            peer_id=ob.peer_id,
            slot=ob.slot,
            silence_frame=self.silence_frame,
        ):
            self.sender.send_dmrd(packet)
            count += 1
            time.sleep(self.delay)
        logger.info("Sent %s OpenBridge DMRD packets to TG %s (%s)", count, group.talkgroup, group.name)


class GroupUSRPTransmitter(BaseTransmitter):
    """Send each group to its own USRP endpoint when configured.

    A single Analog_Bridge USRP endpoint has one active TX talkgroup at a time.
    This class locks per endpoint, so groups with separate ports can transmit at
    the same time while groups sharing a port remain serialized.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._groups_by_name = {item.name: item for item in cfg.groups}
        self._clients: dict[tuple, USRPClient] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()
        self._locks.clear()

    def _effective_group(self, group: GroupConfig) -> GroupConfig:
        return self._groups_by_name.get(group.name, group)

    def _usrp_for_group(self, group: GroupConfig) -> AnalogBridgeUSRPConfig:
        effective = self._effective_group(group)
        if effective.bridge is not None:
            return effective.bridge.usrp
        return self.cfg.output.analog_bridge_usrp

    def _key(self, cfg: AnalogBridgeUSRPConfig) -> tuple:
        return (
            cfg.address,
            cfg.tx_port,
            cfg.local_rx_port,
            cfg.sample_rate,
            cfg.frame_ms,
            cfg.slot,
            cfg.color_code,
            cfg.subscriber_id,
            cfg.repeater_id,
            cfg.callsign,
        )

    def _client_and_lock(self, cfg: AnalogBridgeUSRPConfig) -> tuple[USRPClient, threading.Lock]:
        key = self._key(cfg)
        with self._guard:
            if key not in self._clients:
                self._clients[key] = USRPClient(cfg)
                self._locks[key] = threading.Lock()
            return self._clients[key], self._locks[key]

    def transmit(self, group: GroupConfig, wav_path: Path, text: str) -> None:  # noqa: ARG002
        effective = self._effective_group(group)
        cfg = self._usrp_for_group(effective)
        client, lock = self._client_and_lock(cfg)
        with lock:
            client.prepare_for_group(effective.talkgroup)
            packets, bytes_sent = client.stream_wav(wav_path, effective.talkgroup)
            logger.info(
                "Streamed USRP audio to %s:%s for TG %s (%s); packets=%s bytes=%s",
                cfg.address,
                cfg.tx_port,
                effective.talkgroup,
                effective.name,
                packets,
                bytes_sent,
            )


class ManagedDVSwitchTransmitter(GroupUSRPTransmitter):
    """Start optional per-group bridge children, then send group-isolated USRP."""

    def __init__(self, cfg: Config):
        self.supervisor = ProcessSupervisor(cfg)
        self.supervisor.start()
        super().__init__(cfg)

    def close(self) -> None:
        try:
            super().close()
        finally:
            self.supervisor.stop()


class ManagedOpenBridgeTransmitter(GroupUSRPTransmitter):
    """Manage local DVSwitch helpers and forward MMDVM DMRD to OpenBridge."""

    def __init__(self, cfg: Config):
        self.supervisor = ManagedOpenBridgeSupervisor(cfg)
        try:
            self.supervisor.start()
        except Exception:
            self.supervisor.stop()
            raise
        super().__init__(self.supervisor.cfg)

    def transmit(self, group: GroupConfig, wav_path: Path, text: str) -> None:
        effective = self._effective_group(group)
        forwarder = self.supervisor.forwarder_for(effective.name)
        before_forwarded = forwarder.forwarded_packets if forwarder else 0
        before_dropped = forwarder.dropped_packets if forwarder else 0
        before_ob_packets = forwarder.openbridge_packets if forwarder else 0
        before_ob_bytes = forwarder.openbridge_bytes if forwarder else 0
        super().transmit(effective, wav_path, text)
        # MMDVM_Bridge may emit terminator/hang frames after the last USRP packet.
        time.sleep(1.5)
        after_forwarded = forwarder.forwarded_packets if forwarder else 0
        after_dropped = forwarder.dropped_packets if forwarder else 0
        after_ob_packets = forwarder.openbridge_packets if forwarder else 0
        after_ob_bytes = forwarder.openbridge_bytes if forwarder else 0
        logger.info(
            "Managed OpenBridge TX summary for %s TG %s: DMRD forwarded=%s dropped=%s OpenBridge packets=%s bytes=%s",
            effective.name,
            effective.talkgroup,
            after_forwarded - before_forwarded,
            after_dropped - before_dropped,
            after_ob_packets - before_ob_packets,
            after_ob_bytes - before_ob_bytes,
        )
        if after_forwarded == before_forwarded:
            logger.warning(
                "No DMRD voice/data frames were forwarded for %s after USRP audio. "
                "Check Analog_Bridge, MMDVM_Bridge, and md380-emu logs.",
                effective.name,
            )

    def close(self) -> None:
        try:
            super().close()
        finally:
            self.supervisor.stop()


# Backward-compatible alias for existing imports/docs.
AnalogBridgeUSRPTransmitter = GroupUSRPTransmitter


def build_transmitter(cfg: Config) -> BaseTransmitter:
    if cfg.app.dry_run:
        return DryRunTransmitter()
    mode = cfg.output.mode.lower()
    if mode == "dry_run":
        return DryRunTransmitter()
    if mode == "direct_openbridge":
        return DirectOpenBridgeTransmitter(cfg)
    if mode == "analog_bridge_usrp":
        return GroupUSRPTransmitter(cfg)
    if mode == "managed_dvswitch":
        return ManagedDVSwitchTransmitter(cfg)
    if mode == "managed_openbridge":
        return ManagedOpenBridgeTransmitter(cfg)
    raise ValueError(f"Unsupported output.mode: {cfg.output.mode}")
