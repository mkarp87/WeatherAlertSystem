from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import time
from pathlib import Path

from .announcer import apply_tail_message, build_announcement
from .audio import ensure_pcm_wav, normalize_pcm16_wav
from .config import Config, GroupConfig
from .nws import NWSClient, filter_alerts, sort_alerts
from .state import StateStore
from .transmitters import BaseTransmitter, build_transmitter
from .tts import TTS

logger = logging.getLogger(__name__)


class SkyAlertBridgeApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.nws = NWSClient(cfg.nws, cfg.app.user_agent)
        self.state = StateStore.load(cfg.app.state_file)
        self.tts = TTS(cfg.tts, cfg.app.audio_dir)
        self.transmitter: BaseTransmitter = build_transmitter(cfg)
        self._groups_by_name = {group.name: group for group in cfg.groups}

    def close(self) -> None:
        self.transmitter.close()

    def _synthesize_text(self, group: GroupConfig, text: str, *, basename: str | None = None) -> Path:
        text = apply_tail_message(text, self.cfg.announcements)
        wav = self.tts.synthesize(text, basename=basename or group.name)
        wav = ensure_pcm_wav(wav, self.cfg.audio)
        if self.cfg.audio.normalize:
            wav = normalize_pcm16_wav(wav)
        return wav

    def _process_group(self, group: GroupConfig) -> tuple[GroupConfig, Path, str] | None:
        logger.info("Polling NWS alerts for group %s zones=%s", group.name, ",".join(group.county_codes))
        alerts = self.nws.get_group_alerts(group)
        alerts = filter_alerts(alerts, self.cfg.nws.include_events, self.cfg.nws.exclude_events, group)
        alerts = sort_alerts(alerts, self.cfg.nws.severity_rank)[: self.cfg.nws.max_alerts_per_group]
        changes = self.state.changes_for_group(group.name, alerts)
        if not changes.has_work:
            logger.info("No alert changes for %s; unchanged alerts are not repeated", group.name)
            return None
        text = build_announcement(group, changes, self.cfg.announcements)
        if not text:
            logger.info("Changes for %s did not require an announcement", group.name)
            return None
        wav = self._synthesize_text(group, text)
        return group, wav, text

    def _transmit_work(self, work: list[tuple[GroupConfig, Path, str]]) -> int:
        if not work:
            return 0
        concurrent_modes = {"direct_openbridge", "analog_bridge_usrp", "managed_dvswitch", "managed_openbridge"}
        if self.cfg.audio_scheduler.mode == "serial" or self.cfg.output.mode.lower() not in concurrent_modes:
            max_workers = 1
        else:
            max_workers = min(len(work), self.cfg.audio_scheduler.max_concurrent_groups)
        failures = 0
        if len(work) > 1 and max_workers > 1:
            logger.info(
                "Transmitting %s group audio streams in parallel; max_concurrent_groups=%s",
                len(work),
                max_workers,
            )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self.transmitter.transmit, group, wav, text) for group, wav, text in work]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    failures += 1
                    logger.exception("Announcement transmit failed")
        return failures

    def run_once(self) -> int:
        work: list[tuple[GroupConfig, Path, str]] = []
        for group in self.cfg.groups:
            if not group.enabled:
                continue
            try:
                item = self._process_group(group)
            except Exception:
                logger.exception("Failed while processing group %s", group.name)
                continue
            if item:
                work.append(item)

        if not work:
            self.state.save()
            return 0

        failures = self._transmit_work(work)
        if failures == 0:
            self.state.save()
        else:
            logger.warning("State not saved because %s transmissions failed; alerts will retry", failures)
            # changes_for_group mutates the in-memory state before transmit. Reload so a
            # long-running process retries the same alert on the next poll after failure.
            self.state = StateStore.load(self.cfg.app.state_file)
        return failures

    def _mark_queue_failed(self, running_path: Path, exc: Exception) -> None:
        failed_dir = self.cfg.app.control_dir / "failed"
        failed_path = failed_dir / f"{running_path.stem}.failed.json"
        try:
            failed_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "error": str(exc),
                "failed_unix": time.time(),
                "original_file": running_path.name,
            }
            failed_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            running_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Could not write queued request failure file for %s", running_path)

    def process_queued_requests(self) -> int:
        """Process local service-control requests without restarting helpers.

        The scheduler claims at most one pending request per group per cycle.
        Requests for different groups are synthesized first, then transmitted in
        parallel when audio_scheduler.mode=parallel_by_group. Additional queued
        requests for the same group stay in the queue for the next cycle so one
        group cannot overlap its own helper chain.
        """
        control_dir = self.cfg.app.control_dir
        control_dir.mkdir(parents=True, exist_ok=True)
        failures = 0
        status_markers = {"failed", "running", "done"}
        selected: list[tuple[Path, Path]] = []
        selected_groups: set[str] = set()

        for request_path in sorted(control_dir.glob("*.json")):
            if any(part in status_markers for part in request_path.stem.split(".")):
                logger.warning("Ignoring stale queue status file: %s", request_path.name)
                continue
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
                group_name = str(payload.get("group") or "")
            except Exception as exc:
                running_path = request_path.with_name(f"{request_path.stem}.running")
                try:
                    request_path.replace(running_path)
                except FileNotFoundError:
                    continue
                failures += 1
                logger.exception("Queued request could not be read: %s", running_path)
                self._mark_queue_failed(running_path, exc)
                continue
            if group_name in selected_groups:
                continue
            running_path = request_path.with_name(f"{request_path.stem}.running")
            try:
                request_path.replace(running_path)
            except FileNotFoundError:
                continue
            selected.append((running_path, running_path))
            selected_groups.add(group_name)

        if not selected:
            return failures

        work: list[tuple[GroupConfig, Path, str]] = []
        completed_paths: list[Path] = []
        for running_path, _ in selected:
            try:
                payload = json.loads(running_path.read_text(encoding="utf-8"))
                group_name = str(payload.get("group") or "")
                text = str(payload.get("text") or "")
                if not group_name or not text:
                    raise ValueError("request requires non-empty group and text")
                group = self._groups_by_name.get(group_name)
                if group is None:
                    raise ValueError(f"unknown group {group_name!r}")
                if not group.enabled:
                    raise ValueError(f"group {group_name!r} is disabled")
                logger.info("Processing queued audio request %s for group %s", running_path.name, group.name)
                wav = self._synthesize_text(group, text, basename=f"queued-{group.name}")
                work.append((group, wav, text))
                completed_paths.append(running_path)
            except Exception as exc:
                failures += 1
                logger.exception("Queued request failed before transmit: %s", running_path)
                self._mark_queue_failed(running_path, exc)

        if work:
            transmit_failures = self._transmit_work(work)
            if transmit_failures:
                failures += transmit_failures
                exc = RuntimeError(f"queued request transmit failed with {transmit_failures} failure(s)")
                for running_path in completed_paths:
                    self._mark_queue_failed(running_path, exc)
            else:
                for running_path in completed_paths:
                    running_path.unlink(missing_ok=True)
        return failures

    def run_forever(self) -> None:
        logger.info("Weather Alert System service running; managed helper chains stay up until the process stops")
        next_poll = 0.0
        while True:
            now = time.monotonic()
            if now >= next_poll:
                self.run_once()
                next_poll = time.monotonic() + self.cfg.app.poll_interval_seconds
            self.process_queued_requests()
            time.sleep(1.0)
