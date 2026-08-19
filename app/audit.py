"""Bulk verification of every channel source.

Walks the configured sources, runs each one for a few seconds and reports what
came out. The equivalent of looping over Tvheadend's services to find the dead
ones, except it honours the same network capacity limits the live streams use -
an audit that opened a connection per source at once would trip every
concurrent-stream cap you have.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .manager import test_source
from .models import Channel, Settings
from .session import NetworkRegistry

log = logging.getLogger(__name__)

# How long to wait for a busy network to free a slot before giving up on a source.
NETWORK_WAIT_SECONDS = 180.0
NETWORK_POLL_SECONDS = 2.0


@dataclass
class SourceResult:
    channel_id: str
    channel_name: str
    channel_number: Optional[int]
    source_id: str
    source_name: str
    network: Optional[str]
    priority: int
    enabled: bool

    status: str = "pending"  # pending | testing | ok | failed | skipped
    error: str = ""
    bytes: int = 0
    bitrate_bps: int = 0
    duration: float = 0.0
    video: str = ""
    audio: str = ""
    stderr: str = ""
    checked_at: Optional[float] = None

    @property
    def key(self) -> str:
        return f"{self.channel_id}/{self.source_id}"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "channel_number": self.channel_number,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "network": self.network,
            "priority": self.priority,
            "enabled": self.enabled,
            "status": self.status,
            "error": self.error,
            "bytes": self.bytes,
            "bitrate_bps": self.bitrate_bps,
            "duration": round(self.duration, 2),
            "video": self.video,
            "audio": self.audio,
            "stderr": self.stderr,
            "checked_at": self.checked_at,
        }


def _summarise(probe: Optional[dict]) -> tuple[str, str]:
    """Turn ffprobe output into one line each for video and audio."""
    if not probe:
        return "", ""
    video = audio = ""
    for stream in probe.get("streams", []):
        if stream.get("type") == "video" and not video:
            fps = stream.get("fps") or ""
            if "/" in str(fps):
                num, _, den = str(fps).partition("/")
                try:
                    fps = f"{int(num) / int(den):g}fps" if int(den) else ""
                except (ValueError, ZeroDivisionError):
                    fps = ""
            video = " ".join(
                p for p in (stream.get("codec"), stream.get("resolution"), fps) if p
            )
        elif stream.get("type") == "audio" and not audio:
            channels = stream.get("channels")
            rate = stream.get("sample_rate")
            audio = " ".join(
                p
                for p in (
                    stream.get("codec"),
                    f"{channels}ch" if channels else "",
                    f"{rate}Hz" if rate else "",
                )
                if p
            )
    return video, audio


class Auditor:
    """Runs one audit at a time and keeps the last result set in memory."""

    def __init__(self, registry: NetworkRegistry):
        self.registry = registry
        self.status = "idle"  # idle | running | done | cancelled | failed
        self.results: list[SourceResult] = []
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.message = ""
        self._task: Optional[asyncio.Task] = None
        self._cancel = False

    # -- state -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self) -> dict:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        done = sum(counts.get(s, 0) for s in ("ok", "failed", "skipped"))
        return {
            "status": self.status,
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": (
                (self.finished_at or time.time()) - self.started_at
                if self.started_at
                else 0.0
            ),
            "total": len(self.results),
            "completed": done,
            "counts": counts,
            "message": self.message,
            "results": [r.as_dict() for r in self.results],
        }

    # -- control -----------------------------------------------------------

    def start(
        self,
        channels: list[Channel],
        settings: Settings,
        duration: float = 8.0,
        concurrency: int = 3,
        channel_id: Optional[str] = None,
        include_disabled: bool = False,
    ) -> None:
        if self.running:
            raise RuntimeError("an audit is already running")

        selected = [c for c in channels if channel_id is None or c.id == channel_id]
        if not include_disabled:
            selected = [c for c in selected if c.enabled]

        self.results = [
            SourceResult(
                channel_id=channel.id,
                channel_name=channel.name,
                channel_number=channel.channel_number,
                source_id=source.id,
                source_name=source.label(),
                network=source.network,
                priority=source.priority,
                enabled=source.enabled,
            )
            for channel in selected
            for source in channel.sources
            if source.enabled or include_disabled
        ]
        by_key = {r.key: r for r in self.results}
        jobs = [
            (by_key[f"{c.id}/{s.id}"], s)
            for c in selected
            for s in c.sources
            if f"{c.id}/{s.id}" in by_key
        ]

        self.status = "running"
        self.message = ""
        self.started_at = time.time()
        self.finished_at = None
        self._cancel = False
        self._task = asyncio.create_task(
            self._run(jobs, settings, duration, concurrency), name="audit"
        )

    async def cancel(self) -> None:
        self._cancel = True
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self.status = "cancelled"
        self.finished_at = time.time()
        for result in self.results:
            if result.status in ("pending", "testing"):
                result.status = "skipped"
                result.error = "audit cancelled"

    # -- execution ---------------------------------------------------------

    async def _run(self, jobs, settings: Settings, duration: float, concurrency: int) -> None:
        limiter = asyncio.Semaphore(max(1, concurrency))
        try:
            await asyncio.gather(
                *(self._one(result, source, settings, duration, limiter) for result, source in jobs)
            )
            self.status = "cancelled" if self._cancel else "done"
        except asyncio.CancelledError:
            self.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - reported in the GUI
            self.status = "failed"
            self.message = str(exc)
            log.exception("audit failed")
        finally:
            self.finished_at = time.time()
            ok = sum(1 for r in self.results if r.status == "ok")
            bad = sum(1 for r in self.results if r.status == "failed")
            log.info("audit finished: %d ok, %d failed, %d total", ok, bad, len(self.results))

    async def _one(
        self,
        result: SourceResult,
        source,
        settings: Settings,
        duration: float,
        limiter: asyncio.Semaphore,
    ) -> None:
        async with limiter:
            if self._cancel:
                result.status = "skipped"
                result.error = "audit cancelled"
                return

            key = f"audit:{result.key}"
            if not await self._acquire(source.network, key):
                result.status = "skipped"
                result.error = (
                    f"network {source.network!r} stayed at capacity for "
                    f"{NETWORK_WAIT_SECONDS:.0f}s"
                )
                return

            result.status = "testing"
            started = time.monotonic()
            try:
                outcome = await test_source(
                    source.command, source.use_shell, None, settings, duration
                )
            except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
                result.status = "failed"
                result.error = str(exc)
                result.checked_at = time.time()
                return
            finally:
                self.registry.release(key)

            result.duration = time.monotonic() - started
            result.bytes = outcome.get("bytes", 0)
            result.stderr = (outcome.get("stderr") or "")[-2000:]
            result.error = outcome.get("error") or ""
            result.checked_at = time.time()
            if result.duration > 0 and result.bytes:
                result.bitrate_bps = int(result.bytes * 8 / min(result.duration, duration))
            result.video, result.audio = _summarise(outcome.get("probe"))
            result.status = "ok" if outcome.get("ok") else "failed"
            if result.status == "ok" and not result.video and not result.audio:
                # Bytes arrived but ffprobe found nothing decodable in them.
                result.status = "failed"
                result.error = result.error or "no playable streams found"

    async def _acquire(self, network: Optional[str], key: str) -> bool:
        """Take a network slot, waiting for live viewers to free one if needed."""
        deadline = time.monotonic() + NETWORK_WAIT_SECONDS
        while True:
            if self.registry.acquire(network, key):
                return True
            if self._cancel or time.monotonic() >= deadline:
                return False
            await asyncio.sleep(NETWORK_POLL_SECONDS)
