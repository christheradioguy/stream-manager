"""Record a channel to disk by attaching to its live session as one more consumer.

A recording is a subscriber exactly like an HTTP viewer or a transcoder's
input, so it shares the channel's single upstream connection, starts on a
keyframe, and keeps the source alive while it runs. The only difference is that
the bytes go to a file instead of a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from .manager import SessionManager
from .models import Channel, Profile, RecordingState, Settings
from .session import Broadcaster

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Container -> file extension for the muxers a profile may name. Anything
# else is written with the container name as its extension.
_EXTENSIONS = {
    "mpegts": "ts",
    "matroska": "mkv",
    "mp4": "mp4",
    "webm": "webm",
    "mp3": "mp3",
    "adts": "aac",
    "flv": "flv",
    "ogg": "ogg",
}


class AlreadyRecording(RuntimeError):
    pass


def safe_name(value: str) -> str:
    """A channel name reduced to something every filesystem accepts."""
    return _UNSAFE.sub("_", value.strip()).strip("._") or "channel"


def build_path(
    channel: Channel,
    profile: Optional[Profile],
    directory: str,
    template: str,
    started: float,
) -> Path:
    """Render the output path and pick a name that does not already exist."""
    ext = _EXTENSIONS.get(profile.container, profile.container) if profile else "ts"
    fields = {
        "channel": channel.id,
        "name": safe_name(channel.name),
        "profile": profile.id if profile else "source",
        "start": time.strftime("%Y%m%d-%H%M%S", time.localtime(started)),
        "ext": ext,
    }
    name = template.format(**fields)
    folder = Path(directory).expanduser()
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for n in range(1, 10000):
        candidate = folder / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not find a free name for {name!r} in {folder}")


class Recording:
    """One file being written from one live session."""

    def __init__(
        self,
        channel: Channel,
        profile: Optional[Profile],
        session: Broadcaster,
        path: Path,
        file: BinaryIO,
        max_seconds: Optional[float],
        on_done: Callable[["Recording"], None],
    ) -> None:
        self.channel = channel
        self.profile = profile
        self.session = session
        self.path = path
        self._file = file
        self.max_seconds = max_seconds
        self._on_done = on_done

        self.status = "recording"
        self.started_at: Optional[float] = None
        self._started_mono: Optional[float] = None
        self._ended_mono: Optional[float] = None
        self.bytes_written = 0
        self.dropped = 0
        self.end_reason = ""
        self.error = ""

        self._task: Optional[asyncio.Task] = None
        self._sub = None
        self._stop_reason: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.status == "recording"

    def start(self) -> None:
        self.started_at = time.time()
        self._started_mono = time.monotonic()
        self._sub = self.session.subscribe("record")
        self._task = asyncio.create_task(self._run(), name=f"record:{self.channel.id}")
        log.info("recording %s -> %s", self.session.key, self.path)

    async def stop(self, reason: str = "stopped by user") -> None:
        if self._task is None or self._task.done():
            return
        self._stop_reason = reason
        # Closing the subscriber wakes the writer with a sentinel so it drains
        # what it already has rather than being cancelled mid-write.
        if self._sub is not None:
            self._sub.close()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        assert self._sub is not None
        sub = self._sub
        deadline = (
            self._started_mono + self.max_seconds
            if self.max_seconds and self._started_mono is not None
            else None
        )
        reason = "session ended"
        try:
            while True:
                timeout = None
                if deadline is not None:
                    timeout = max(deadline - time.monotonic(), 0.0)
                try:
                    chunk = await asyncio.wait_for(sub.queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    reason = f"reached {self.max_seconds:g}s limit"
                    break
                if chunk is None:
                    reason = self._stop_reason or "session ended"
                    break
                # Writes block, so hand them to a thread: a slow disk then
                # fills our own queue and drops our data, not everyone else's.
                await asyncio.to_thread(self._file.write, chunk)
                self.bytes_written += len(chunk)
                if deadline is not None and time.monotonic() >= deadline:
                    reason = f"reached {self.max_seconds:g}s limit"
                    break
            self.status = "stopped"
        except asyncio.CancelledError:
            self.status = "stopped"
            reason = self._stop_reason or "cancelled"
            raise
        except OSError as exc:
            self.status = "error"
            self.error = f"write failed: {exc}"
            reason = "write error"
            log.error("recording %s failed: %s", self.path, exc)
        finally:
            self.end_reason = reason
            self._ended_mono = time.monotonic()
            self.dropped += sub.dropped
            self.session.unsubscribe(sub)
            with contextlib.suppress(OSError):
                self._file.close()
            log.info(
                "recording %s finished (%s): %d bytes", self.path, reason, self.bytes_written
            )
            self._on_done(self)

    def state(self) -> RecordingState:
        if self._started_mono is None:
            elapsed = 0.0
        else:
            end = self._ended_mono if self._ended_mono is not None else time.monotonic()
            elapsed = end - self._started_mono
        return RecordingState(
            channel_id=self.channel.id,
            channel_name=self.channel.name,
            profile=self.profile.id if self.profile else None,
            session_key=self.session.key,
            path=str(self.path),
            status=self.status,  # type: ignore[arg-type]
            started_at=self.started_at,
            elapsed_seconds=round(elapsed, 1),
            bytes_written=self.bytes_written,
            dropped_chunks=self.dropped + (self._sub.dropped if self.active and self._sub else 0),
            max_seconds=self.max_seconds,
            end_reason=self.end_reason,
            error=self.error,
        )


class Recorder:
    """At most one recording per channel, plus a short history of finished ones."""

    def __init__(self, manager: SessionManager, history: int = 50) -> None:
        self._manager = manager
        self._active: dict[str, Recording] = {}
        self._recent: deque[Recording] = deque(maxlen=history)

    async def start(
        self,
        channel: Channel,
        profile: Optional[Profile],
        settings: Settings,
        directory: Optional[str] = None,
        filename: Optional[str] = None,
        max_seconds: Optional[float] = None,
    ) -> Recording:
        """Begin recording. Raises NoCapacity if the channel cannot be started."""
        existing = self._active.get(channel.id)
        if existing is not None and existing.active:
            raise AlreadyRecording(
                f"channel {channel.id!r} is already recording to {existing.path}"
            )
        # Open the file before touching the session: a bad directory then fails
        # the request without leaving a session registered that nothing consumes.
        path = build_path(
            channel,
            profile,
            directory or settings.record_dir,
            filename or settings.record_filename,
            time.time(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        file = open(path, "wb")  # noqa: SIM115 - closed when the recording ends
        try:
            session = await self._manager.get_output(channel, profile, settings)
        except Exception:
            file.close()
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        recording = Recording(channel, profile, session, path, file, max_seconds, self._finished)
        recording.start()
        self._active[channel.id] = recording
        return recording

    async def stop(self, channel_id: str, reason: str = "stopped by user") -> Optional[Recording]:
        recording = self._active.get(channel_id)
        if recording is None or not recording.active:
            return None
        await recording.stop(reason)
        return recording

    def get(self, channel_id: str) -> Optional[Recording]:
        """The current or most recent recording for a channel."""
        return self._active.get(channel_id)

    def active(self) -> list[Recording]:
        return [r for r in self._active.values() if r.active]

    def recent(self) -> list[Recording]:
        return list(self._recent)

    def _finished(self, recording: Recording) -> None:
        self._recent.appendleft(recording)

    async def shutdown(self) -> None:
        for recording in self.active():
            with contextlib.suppress(Exception):
                await recording.stop("server shutting down")
