"""Owns the set of live sessions and the one-off source test helper."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import signal
import tempfile
import time
from typing import Callable, Optional

from .models import Channel, Network, Profile, Settings
from .session import (
    Broadcaster,
    NetworkRegistry,
    NoCapacity,
    SourceSession,
    TranscodeSession,
    program_name,
)

log = logging.getLogger(__name__)


def session_key(channel_id: str, profile_id: Optional[str]) -> str:
    return f"{channel_id}@{profile_id}" if profile_id else channel_id


class SessionManager:
    """Tracks one source per channel, plus a transcoder per requested profile.

    A channel's source is shared by every viewer of that channel regardless of
    profile, so switching profile never opens a second upstream connection.
    """

    def __init__(self, get_network: Callable[[str], Optional[Network]]) -> None:
        self._sessions: dict[str, Broadcaster] = {}
        self._lock = asyncio.Lock()
        self.registry = NetworkRegistry(get_network)

    async def get_output(
        self, channel: Channel, profile: Optional[Profile], settings: Settings
    ) -> Broadcaster:
        """The broadcaster an HTTP client should attach to for this request.

        Raises NoCapacity when a new upstream stream would be needed but every
        candidate source is on a network that is already full.
        """
        async with self._lock:
            source = self._live_source(channel, settings)
            if profile is None:
                return source

            key = session_key(channel.id, profile.id)
            existing = self._sessions.get(key)
            if isinstance(existing, TranscodeSession) and existing.status != "stopped":
                return existing

            transcoder = TranscodeSession(source, profile, settings, self._forget)
            self._sessions[key] = transcoder
            return transcoder

    def _live_source(self, channel: Channel, settings: Settings) -> SourceSession:
        existing = self._sessions.get(channel.id)
        if isinstance(existing, SourceSession) and existing.status != "stopped":
            # Already streaming: extra viewers cost no extra upstream stream, so
            # the network cap does not apply to them.
            return existing

        candidate = SourceSession(channel, settings, self._forget, self.registry)
        if candidate.select_source() is None:
            # Not registered yet, so there is nothing to clean up - just refuse.
            raise NoCapacity(f"{channel.name}: {candidate.describe_unavailable()}")
        self._sessions[channel.id] = candidate
        return candidate

    def _forget(self, session: Broadcaster) -> None:
        if self._sessions.get(session.key) is session:
            self._sessions.pop(session.key, None)
            log.info("session %s released", session.key)

    def get(self, key: str) -> Optional[Broadcaster]:
        return self._sessions.get(key)

    def all(self) -> list[Broadcaster]:
        return list(self._sessions.values())

    def for_channel(self, channel_id: str) -> list[Broadcaster]:
        return [s for s in self._sessions.values() if s.channel.id == channel_id]

    async def stop(self, key: str) -> bool:
        session = self._sessions.pop(key, None)
        if session is None:
            return False
        await self._stop_with_dependents(session, "stopped by user")
        return True

    async def stop_channel(self, channel_id: str, reason: str = "channel changed") -> None:
        for session in self.for_channel(channel_id):
            self._sessions.pop(session.key, None)
            await session.stop(reason)

    async def stop_profile(self, profile_id: str, reason: str = "profile changed") -> None:
        for session in list(self._sessions.values()):
            if isinstance(session, TranscodeSession) and session.profile.id == profile_id:
                self._sessions.pop(session.key, None)
                await session.stop(reason)

    async def _stop_with_dependents(self, session: Broadcaster, reason: str) -> None:
        """Stopping a source has to take its transcoders down with it."""
        if isinstance(session, SourceSession):
            for transcoder in list(session.transcoders):
                self._sessions.pop(transcoder.key, None)
                await transcoder.stop(reason)
        await session.stop(reason)

    async def shutdown(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        # Transcoders first: they detach from their source on the way out.
        for session in sorted(sessions, key=lambda s: s.kind != "transcode"):
            with contextlib.suppress(Exception):
                await session.stop("server shutting down")


# ---------------------------------------------------------------------------
# One-off source test
# ---------------------------------------------------------------------------


async def test_source(
    command: str,
    use_shell: bool,
    profile: Optional[Profile],
    settings: Settings,
    duration: float,
) -> dict:
    """Run a source for a few seconds and report what came out.

    Captures to a temp file so ffprobe can inspect a real container rather than
    guessing from a stdin fragment.
    """
    started = time.monotonic()
    result: dict = {
        "ok": False,
        "bytes": 0,
        "duration": duration,
        "stderr": "",
        "error": "",
        "probe": None,
    }

    tmp = tempfile.NamedTemporaryFile(prefix="sm-test-", suffix=".ts", delete=False)
    tmp_path = tmp.name
    tmp.close()

    source: Optional[asyncio.subprocess.Process] = None
    transcoder: Optional[asyncio.subprocess.Process] = None
    read_fd: Optional[int] = None
    write_fd: Optional[int] = None
    stderr_chunks: list[bytes] = []

    try:
        if profile is not None:
            read_fd, write_fd = os.pipe()

        stdout_target = write_fd if write_fd is not None else asyncio.subprocess.PIPE
        try:
            if use_shell:
                source = await asyncio.create_subprocess_shell(
                    command,
                    stdout=stdout_target,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            else:
                source = await asyncio.create_subprocess_exec(
                    *shlex.split(command),
                    stdout=stdout_target,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
        except FileNotFoundError:
            if read_fd is not None:
                os.close(read_fd)
            result["error"] = f"command not found: {program_name(command)}"
            return result
        except PermissionError:
            if read_fd is not None:
                os.close(read_fd)
            result["error"] = f"not executable: {program_name(command)}"
            return result
        finally:
            if write_fd is not None:
                os.close(write_fd)
                write_fd = None

        if profile is not None:
            assert read_fd is not None
            try:
                transcoder = await asyncio.create_subprocess_exec(
                    *profile.ffmpeg_argv(settings.ffmpeg_bin, "warning"),
                    stdin=read_fd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except FileNotFoundError:
                result["error"] = f"ffmpeg not found: {settings.ffmpeg_bin}"
                return result
            finally:
                os.close(read_fd)
                read_fd = None

        final = transcoder or source
        assert final is not None and final.stdout is not None

        async def collect_stderr(proc: asyncio.subprocess.Process, tag: str) -> None:
            if not proc.stderr:
                return
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_chunks.append(b"[" + tag.encode() + b"] " + line)

        stderr_tasks = [
            asyncio.create_task(collect_stderr(p, tag))
            for p, tag in ((source, "src"), (transcoder, "ff"))
            if p is not None
        ]

        total = 0
        with open(tmp_path, "wb") as out:
            deadline = started + duration
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    chunk = await asyncio.wait_for(
                        final.stdout.read(64 * 1024), timeout=max(remaining, 0.1)
                    )
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)

        result["bytes"] = total
        for task in stderr_tasks:
            task.cancel()
        result["stderr"] = b"".join(stderr_chunks[-100:]).decode("utf-8", "replace").strip()

        if total == 0:
            rc = final.returncode
            result["error"] = (
                f"no data produced (exit code {rc})"
                if rc is not None
                else "no data produced within the test window"
            )
            return result

        result["probe"] = await _ffprobe(tmp_path, settings.ffprobe_bin)
        result["ok"] = True
        return result
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        for proc in (transcoder, source):
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=2)
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


async def _ffprobe(path: str, ffprobe_bin: str) -> Optional[dict]:
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None

    try:
        data = json.loads(stdout or b"{}")
    except json.JSONDecodeError:
        return None

    streams = []
    for s in data.get("streams", []):
        entry = {
            "index": s.get("index"),
            "type": s.get("codec_type"),
            "codec": s.get("codec_name"),
        }
        if s.get("codec_type") == "video":
            entry["resolution"] = f"{s.get('width')}x{s.get('height')}"
            entry["fps"] = s.get("avg_frame_rate")
        elif s.get("codec_type") == "audio":
            entry["channels"] = s.get("channels")
            entry["sample_rate"] = s.get("sample_rate")
        streams.append(entry)

    fmt = data.get("format", {})
    return {
        "format": fmt.get("format_long_name") or fmt.get("format_name"),
        "bitrate": fmt.get("bit_rate"),
        "streams": streams,
    }
