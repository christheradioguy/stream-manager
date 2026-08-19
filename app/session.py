"""Live stream sessions.

A channel's source is opened exactly once, no matter how many people watch it or
which transcode profiles they ask for:

    SourceSession(channel)           one subprocess, one upstream connection
      |- HTTP client                 passthrough viewers
      |- HTTP client
      \\- TranscodeSession(profile)  one ffmpeg, fed from those same bytes
           |- HTTP client
           \\- HTTP client

That structure is the point. Most upstream providers cap concurrent connections,
and the obvious design - one pipeline per (channel, profile) - opens a second
connection the moment somebody switches profile, which the provider then refuses.
Here, switching profile attaches to a source that is already running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import signal
import time
from collections import deque
from typing import AsyncIterator, Awaitable, Callable, Optional

from .models import Channel, Network, Profile, SessionState, Settings, Source
from .tsstats import LedgerEntry, TSAnalyser

log = logging.getLogger(__name__)

READ_CHUNK = 64 * 1024

# A run that lasted this long counts as healthy: the next failure starts its
# backoff from scratch instead of inheriting the previous ladder.
HEALTHY_RUN_SECONDS = 60.0

# A source that streamed for at least this long and then ended is treated as
# reconnecting rather than failing. Live HLS sources routinely end when a token
# or playlist window rotates, and hammering them as though they had crashed is
# what turns a brief rotation into a visible outage.
RECONNECT_MIN_SECONDS = 15.0

TS_PACKET = 188
TS_SYNC = 0x47
# Give up on finding TS sync after this much data and pass bytes through raw,
# so a source that is not actually MPEG-TS still works.
TS_SYNC_SEARCH_LIMIT = 1024 * 1024


class TSAligner:
    """Turns a raw byte stream into whole, sync-aligned MPEG-TS packets.

    Reads off a pipe arrive in arbitrary sizes, and 64 KB is not a multiple of
    188, so relaying them verbatim hands clients a stream that starts partway
    through a packet. Most demuxers resync, but some players choke on it and
    none of them should have to.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._synced = False
        self._searched = 0
        self.passthrough = False  # set if the stream turns out not to be TS

    def feed(self, data: bytes) -> bytes:
        if self.passthrough:
            return data
        self._buf += data

        if not self._synced:
            offset = self._find_sync(self._buf)
            if offset is None:
                self._searched += len(data)
                if self._searched > TS_SYNC_SEARCH_LIMIT:
                    self.passthrough = True
                    out = bytes(self._buf)
                    self._buf.clear()
                    return out
                # Keep only enough tail to still find sync across the boundary.
                excess = len(self._buf) - 64 * TS_PACKET
                if excess > 0:
                    del self._buf[:excess]
                return b""
            if offset:
                del self._buf[:offset]
            self._synced = True

        whole = (len(self._buf) // TS_PACKET) * TS_PACKET
        if not whole:
            return b""
        out = bytes(self._buf[:whole])
        del self._buf[:whole]
        return out

    @staticmethod
    def _find_sync(buf: bytearray, needed: int = 8) -> Optional[int]:
        """Offset of a sync byte backed by `needed` more at 188-byte spacing."""
        if len(buf) < needed * TS_PACKET:
            return None
        for offset in range(TS_PACKET):
            if all(buf[offset + i * TS_PACKET] == TS_SYNC for i in range(needed)):
                return offset
        return None


def ts_pid(packet: memoryview | bytes, offset: int = 0) -> int:
    return ((packet[offset + 1] & 0x1F) << 8) | packet[offset + 2]


def program_name(command: str) -> str:
    """The binary a command line will try to execute, for error messages."""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else command


def describe_exit(returncode: Optional[int]) -> str:
    if returncode is None:
        return "still running"
    if returncode < 0:
        return f"killed by signal {-returncode}"
    return f"exit code {returncode}"


class StreamFailure(RuntimeError):
    """A pipeline attempt ended. Carries enough detail to explain why."""

    def __init__(self, message: str, tail: list[str] | None = None, fatal: bool = False):
        self.tail = tail or []
        self.fatal = fatal  # retrying cannot help (bad arguments, missing binary)
        detail = f"{message}: {tail[-1]}" if tail else message
        super().__init__(detail)


class TooManyClients(RuntimeError):
    pass


class NoCapacity(RuntimeError):
    """No source can be started: none usable, or every network is full."""


class NetworkRegistry:
    """Tracks which sessions currently hold a slot on each network.

    A slot is one live upstream connection. Extra viewers of an already-running
    channel cost nothing here, which is the point: the cap is on streams pulled
    from the provider, not on people watching.
    """

    def __init__(self, get_network: Callable[[str], Optional[Network]]):
        self._get_network = get_network
        self._held: dict[str, str] = {}  # session key -> network id

    def limit(self, network_id: Optional[str]) -> int:
        """0 means unlimited, which is also what an unknown network gets."""
        if not network_id:
            return 0
        network = self._get_network(network_id)
        return network.max_streams if network else 0

    def enabled(self, network_id: Optional[str]) -> bool:
        if not network_id:
            return True
        network = self._get_network(network_id)
        return network.enabled if network else True

    def in_use(self, network_id: str, exclude_key: Optional[str] = None) -> int:
        return sum(
            1 for key, held in self._held.items() if held == network_id and key != exclude_key
        )

    def has_room(self, network_id: Optional[str], key: Optional[str] = None) -> bool:
        if not self.enabled(network_id):
            return False
        if not network_id:
            return True
        limit = self.limit(network_id)
        if limit <= 0:
            return True
        # A session already holding this network's slot keeps it on restart.
        if key is not None and self._held.get(key) == network_id:
            return True
        return self.in_use(network_id, exclude_key=key) < limit

    def acquire(self, network_id: Optional[str], key: str) -> bool:
        if not network_id:
            self._held.pop(key, None)
            return True
        if not self.has_room(network_id, key):
            return False
        self._held[key] = network_id
        return True

    def release(self, key: str) -> None:
        self._held.pop(key, None)

    def usage(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for network_id in self._held.values():
            counts[network_id] = counts.get(network_id, 0) + 1
        return counts


class Subscriber:
    """One consumer of a broadcaster - an HTTP client or a transcoder's input."""

    __slots__ = ("queue", "dropped", "closed", "label")

    def __init__(self, maxsize: int, label: str = ""):
        self.queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.closed = False
        self.label = label

    def put(self, chunk: Optional[bytes]) -> None:
        """Never blocks. A consumer that falls behind loses the oldest data."""
        try:
            self.queue.put_nowait(chunk)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
                self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(chunk)

    def close(self) -> None:
        self.closed = True
        self.put(None)


# ---------------------------------------------------------------------------
# Shared machinery
# ---------------------------------------------------------------------------


class Broadcaster:
    """Owns a subprocess pipeline and fans its output out to subscribers.

    Subclasses implement :meth:`_attempt`, which runs one pipeline until it ends
    and is retried by :meth:`_run` under a backoff.
    """

    kind = "source"

    def __init__(
        self,
        key: str,
        channel: Channel,
        settings: Settings,
        on_idle: Callable[["Broadcaster"], None],
    ):
        self.key = key
        self.channel = channel
        self.settings = settings
        self._on_idle = on_idle

        self.status = "starting"
        self.last_error = ""
        self.last_end = ""         # why the last attempt ended, error or not
        self.restarts = 0
        self.reconnects = 0        # clean re-opens after a good run
        self.failures = 0          # consecutive failed attempts
        self.bytes_out = 0
        self.started_at: Optional[float] = None
        self.flowing_since: Optional[float] = None

        self._subscribers: set[Subscriber] = set()
        self._prebuffer = bytearray()
        # True while the output is MPEG-TS, so replay can respect packet
        # boundaries. Cleared for profiles that mux to something else.
        self._ts_output = True
        self._logs: deque[str] = deque(maxlen=settings.log_lines)
        self._bitrate_window: deque[tuple[float, int]] = deque()

        # Stream health for this session, plus the process-lifetime ledger that
        # survives the session being torn down and recreated.
        self.analyser = TSAnalyser(enabled=settings.ts_analysis)
        self.ledger: Optional[LedgerEntry] = None

        self._procs: list[asyncio.subprocess.Process] = []
        self._runner: Optional[asyncio.Task] = None
        self._idle_timer: Optional[asyncio.Task] = None
        self._helper_tasks: list[asyncio.Task] = []
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._runner is None or self._runner.done():
            self._stopping = False
            self._runner = asyncio.create_task(self._run(), name=f"session:{self.key}")

    async def stop(self, reason: str = "stopped") -> None:
        self._stopping = True
        self.status = "stopping"
        self._cancel_idle_timer()
        if self._runner and not self._runner.done():
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
        await self._cleanup()
        for sub in list(self._subscribers):
            sub.close()
        self._subscribers.clear()
        self.status = "stopped"
        self._on_finished()
        self._log(f"--- session {reason} ---")

    async def _run(self) -> None:
        backoff = self.settings.restart_backoff_seconds
        while not self._stopping:
            self.status = "starting"
            self.flowing_since = None
            attempt_started = time.monotonic()
            fatal = False
            failure: Optional[StreamFailure] = None
            reconnecting = False
            try:
                self.started_at = attempt_started
                await self._attempt()
                failure = StreamFailure("stream ended")
            except asyncio.CancelledError:
                raise
            except StreamFailure as exc:
                fatal = exc.fatal
                failure = exc
            except Exception as exc:  # noqa: BLE001 - surfaced to the GUI
                failure = StreamFailure(str(exc))
            finally:
                ran_for = time.monotonic() - attempt_started
                # Ending after a decent run is a reconnect, not a fault: the
                # source worked, the upstream just closed the window.
                reconnecting = (
                    failure is not None
                    and not fatal
                    and self.flowing_since is not None
                    and ran_for >= RECONNECT_MIN_SECONDS
                )
                if failure is not None:
                    self._record_end(failure, reconnecting)
                await self._cleanup()

            if reconnecting or ran_for >= HEALTHY_RUN_SECONDS:
                # It worked for a while, so this is a fresh problem.
                backoff = self.settings.restart_backoff_seconds
                self._on_healthy_run()

            if self._stopping or not self._has_consumers():
                break
            if fatal:
                self._log("--- not retrying: the command or arguments cannot work ---")
                break

            if reconnecting:
                # Give the upstream a moment to be ready again. Reopening
                # instantly is what produces a burst of connection resets.
                self.status = "restarting"
                self.restarts += 1
                if self.ledger is not None:
                    self.ledger.restarts += 1
                delay = self.settings.reconnect_delay_seconds
                self._log(f"--- reopening in {delay:.0f}s ---")
                await asyncio.sleep(delay)
                continue

            if self._retry_immediately():
                # Another candidate is waiting; failing over should not make the
                # viewer sit through a backoff that exists for flaky networks.
                self.status = "restarting"
                self.restarts += 1
                if self.ledger is not None:
                    self.ledger.restarts += 1
                self._log("--- failing over to the next source ---")
                continue
            if not self.settings.auto_restart:
                self._log("--- auto-restart disabled, giving up ---")
                break
            give_up = self.settings.give_up_after_failures
            if give_up and self.failures >= give_up:
                self._log(f"--- giving up after {self.failures} consecutive failures ---")
                self.status = "error"
                break

            self.status = "restarting"
            self.restarts += 1
            if self.ledger is not None:
                self.ledger.restarts += 1
            self._log(f"--- retry {self.failures} in {backoff:.0f}s ---")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.settings.max_restart_backoff_seconds)

        for sub in list(self._subscribers):
            sub.close()
        if self.status not in ("stopping", "stopped", "error"):
            self.status = "error" if self.last_error else "stopped"
        self._on_finished()

    def _retry_immediately(self) -> bool:
        return False

    def _on_healthy_run(self) -> None:
        """Called once an attempt has been up long enough to count as working."""

    def _on_finished(self) -> None:
        """Called when the session will not try again without being restarted."""

    def _record_end(self, exc: StreamFailure, reconnecting: bool) -> None:
        self.last_end = str(exc)
        if reconnecting:
            # Not an error, so it neither climbs the backoff ladder nor counts
            # towards give-up, and it does not shout in the log every rotation.
            self.reconnects += 1
            self.failures = 0
            self.last_error = ""
            self.status = "restarting"
            if self.ledger is not None:
                self.ledger.reconnects += 1
            log.info("session %s reopening: %s", self.key, exc)
            self._log(f"--- {exc}; reopening ---")
            return
        self.failures += 1
        self.last_error = str(exc)
        self.status = "error"
        log.warning("session %s: %s", self.key, exc)
        self._log(f"--- {exc} ---")

    async def _attempt(self) -> None:
        raise NotImplementedError

    async def _cleanup(self) -> None:
        for task in self._helper_tasks:
            task.cancel()
        self._helper_tasks.clear()
        await self._terminate(self._procs)
        self._procs = []

    # -- subscribers -------------------------------------------------------

    def subscribe(self, label: str = "") -> Subscriber:
        sub = Subscriber(self.settings.client_queue_chunks, label)
        # Hand over recent data so playback (or an encoder) can start at once
        # instead of waiting for the next keyframe.
        replay = self._replay_bytes()
        if replay:
            # One chunk: it is already a whole number of packets, and splitting
            # it would only risk the queue dropping half of it.
            sub.put(replay)
        self._subscribers.add(sub)
        self._cancel_idle_timer()
        self.start()
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)
        if not self._has_consumers():
            self._schedule_idle_stop()

    def publish(self, chunk: bytes) -> None:
        self.bytes_out += len(chunk)
        if self._ts_output:
            before = self.analyser.counters
            packets, terr, cerr = before.packets, before.transport_errors, before.continuity_errors
            self.analyser.feed(chunk)
            after = self.analyser.counters
            if self.ledger is not None:
                self.ledger.ts.packets += after.packets - packets
                self.ledger.ts.transport_errors += after.transport_errors - terr
                self.ledger.ts.continuity_errors += after.continuity_errors - cerr
        if self.ledger is not None:
            self.ledger.bytes_out += len(chunk)
        self._record_bitrate(len(chunk))
        self._push_prebuffer(chunk)
        for sub in list(self._subscribers):
            sub.put(chunk)

    async def attach(
        self, request_disconnected: Callable[[], Awaitable[bool]]
    ) -> AsyncIterator[bytes]:
        """Yield the live stream to one HTTP client until it disconnects."""
        if not self.can_accept():
            raise TooManyClients(
                f"channel {self.channel.id} is at its {self.client_limit} client limit"
            )
        sub = self.subscribe("client")
        log.info("client attached to %s (%d total)", self.key, self.client_count)
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(sub.queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    # Starlette cancels this generator on disconnect, but a client
                    # that vanishes while the source is down would otherwise sit
                    # here forever holding a slot.
                    if await request_disconnected():
                        break
                    continue
                if chunk is None:
                    break
                yield chunk
        finally:
            self.unsubscribe(sub)
            log.info("client detached from %s (%d left)", self.key, self.client_count)

    @property
    def client_count(self) -> int:
        return sum(1 for s in self._subscribers if s.label == "client")

    @property
    def client_limit(self) -> int:
        """0 means unlimited."""
        limit = self.channel.max_clients
        return self.settings.default_max_clients if limit is None else limit

    def can_accept(self) -> bool:
        limit = self.client_limit
        return not limit or self.total_clients() < limit

    def total_clients(self) -> int:
        return self.client_count

    def _has_consumers(self) -> bool:
        return bool(self._subscribers)

    # -- idle handling -----------------------------------------------------

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer and not self._idle_timer.done():
            self._idle_timer.cancel()
        self._idle_timer = None

    def _schedule_idle_stop(self) -> None:
        self._cancel_idle_timer()
        linger = self.settings.linger_seconds

        async def _wait_then_stop() -> None:
            try:
                await asyncio.sleep(linger)
            except asyncio.CancelledError:
                return
            if not self._has_consumers():
                log.info("nothing consuming %s for %.0fs, stopping", self.key, linger)
                await self.stop("idle")
                self._on_idle(self)

        self._idle_timer = asyncio.create_task(_wait_then_stop(), name=f"idle:{self.key}")

    # -- process helpers ---------------------------------------------------

    async def _terminate(self, procs: list[asyncio.subprocess.Process]) -> None:
        grace = self.settings.terminate_grace_seconds
        for proc in procs:
            if proc.returncode is None:
                self._signal_group(proc, signal.SIGTERM)
        for proc in procs:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=grace)
                except asyncio.TimeoutError:
                    log.warning("pid %s ignored SIGTERM after %.0fs, killing", proc.pid, grace)
                    self._signal_group(proc, signal.SIGKILL)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=grace)
                except ProcessLookupError:
                    pass

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Signal the whole process group.

        Source commands routinely fork helpers (streamlink spawns ffmpeg); killing
        only the direct child would leave those holding the upstream connection.
        """
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)

    async def _reap(self, proc: asyncio.subprocess.Process) -> Optional[int]:
        """Get a real exit code after EOF, rather than reporting None."""
        if proc.returncode is not None:
            return proc.returncode
        try:
            return await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            return None

    def _watch_stderr(
        self, proc: asyncio.subprocess.Process, tag: str, tail: deque[str]
    ) -> None:
        async def drain() -> None:
            assert proc.stderr is not None
            try:
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace").rstrip()
                    if text:
                        tail.append(text)
                        self._log(f"[{tag}] {text}")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.debug("stderr reader for %s ended", self.key, exc_info=True)

        self._helper_tasks.append(asyncio.create_task(drain()))

    # -- stats -------------------------------------------------------------

    def logs(self) -> list[str]:
        return list(self._logs)

    def _log(self, line: str) -> None:
        self._logs.append(f"{time.strftime('%H:%M:%S')} {line}")

    def _mark_flowing(self) -> None:
        if self.flowing_since is None:
            self.flowing_since = time.monotonic()
            self.status = "running"
            self.failures = 0
            self.last_error = ""
            self._log("--- first bytes received ---")

    def _push_prebuffer(self, chunk: bytes) -> None:
        limit = self.settings.prebuffer_bytes
        if limit <= 0:
            if self._prebuffer:
                self._prebuffer.clear()
            return
        self._prebuffer += chunk
        excess = len(self._prebuffer) - limit
        if excess > 0:
            # Trim in whole packets, and in slabs, so this is not a memmove of
            # the entire buffer on every 64 KB that arrives.
            slab = max(excess, limit // 4)
            slab = min(slab, len(self._prebuffer))
            if self._ts_output:
                slab -= slab % TS_PACKET
            if slab > 0:
                del self._prebuffer[:slab]

    def _replay_bytes(self) -> bytes:
        """The backlog handed to a joining consumer.

        For MPEG-TS this starts at a PAT, so the demuxer sees the program tables
        immediately rather than resyncing from a random point mid-packet. That is
        what a joining client gets from a real tuner backend, and some players
        misbehave without it.
        """
        if not self._prebuffer:
            return b""
        if not self._ts_output:
            return bytes(self._prebuffer)
        start = self._first_pat_offset(self._prebuffer)
        return bytes(self._prebuffer[start:])

    @staticmethod
    def _first_pat_offset(buf: bytearray) -> int:
        for offset in range(0, len(buf) - TS_PACKET + 1, TS_PACKET):
            if buf[offset] == TS_SYNC and ts_pid(buf, offset) == 0:
                return offset
        return 0

    def _record_bitrate(self, nbytes: int) -> None:
        now = time.monotonic()
        self._bitrate_window.append((now, nbytes))
        cutoff = now - 5.0
        while self._bitrate_window and self._bitrate_window[0][0] < cutoff:
            self._bitrate_window.popleft()

    def _bitrate(self) -> int:
        if len(self._bitrate_window) < 2:
            return 0
        span = self._bitrate_window[-1][0] - self._bitrate_window[0][0]
        if span <= 0:
            return 0
        return int(sum(n for _, n in self._bitrate_window) * 8 / span)

    def state(self) -> SessionState:
        now = time.monotonic()
        c = self.analyser.counters
        return SessionState(
            key=self.key,
            kind=self.kind,  # type: ignore[arg-type]
            channel_id=self.channel.id,
            channel_name=self.channel.name,
            profile=getattr(self, "profile_id", None),
            status=self.status,  # type: ignore[arg-type]
            clients=self.client_count,
            consumers=len(self._subscribers),
            bytes_out=self.bytes_out,
            bitrate_bps=self._bitrate(),
            started_at=self.started_at,
            uptime_seconds=(now - self.flowing_since) if self.flowing_since else 0.0,
            restarts=self.restarts,
            reconnects=self.reconnects,
            last_end=self.last_end,
            dropped_chunks=sum(s.dropped for s in self._subscribers),
            input_dropped=0,
            ts_packets=c.packets,
            ts_transport_errors=c.transport_errors,
            ts_continuity_errors=c.continuity_errors,
            ts_scrambled=c.scrambled,
            ts_discontinuities=c.discontinuities,
            ts_error_pids=self.analyser.worst_pids(),
            last_error=self.last_error,
            pids=[p.pid for p in self._procs if p.returncode is None],
        )


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


class SourceSession(Broadcaster):
    """Runs a channel's command and broadcasts its stdout."""

    kind = "source"

    def __init__(
        self,
        channel: Channel,
        settings: Settings,
        on_idle: Callable[["Broadcaster"], None],
        registry: "NetworkRegistry",
    ):
        super().__init__(channel.id, channel, settings, on_idle)
        self.transcoders: set["TranscodeSession"] = set()
        self.registry = registry
        self.active_source: Optional[Source] = None
        # Sources already tried in this failover cycle, cleared once one works.
        self._tried: set[str] = set()

    # -- source selection --------------------------------------------------

    def select_source(self) -> Optional[Source]:
        """The best source to try next, or None if nothing is available.

        Prefers untried sources so a failure moves down the priority list rather
        than hammering the one that just failed.
        """
        candidates = self.channel.ordered_sources()
        if not candidates:
            return None
        untried = [s for s in candidates if s.id not in self._tried]
        for pool in (untried, candidates):
            for source in pool:
                if self.registry.has_room(source.network, self.key):
                    return source
        return None

    def _untried_available(self) -> bool:
        """Is there a *different* source worth trying right now?

        The active source is excluded explicitly. A good run clears the tried
        set so the highest-priority source gets another go, and without this
        guard that made the source which just ended look untried - so it was
        reopened with no delay at all, hammering an upstream that had only just
        closed the connection.
        """
        current = self.active_source.id if self.active_source else None
        return any(
            s.id not in self._tried
            and s.id != current
            and self.registry.has_room(s.network, self.key)
            for s in self.channel.ordered_sources()
        )

    def _retry_immediately(self) -> bool:
        """Fail over to the next source at once; only back off once all failed."""
        return self._untried_available()

    def _on_healthy_run(self) -> None:
        # A source that held up for a while earns a clean slate, so the next
        # failure starts again from the highest-priority source.
        self._tried.clear()

    def _on_finished(self) -> None:
        self.registry.release(self.key)
        self.active_source = None

    def state(self) -> SessionState:
        st = super().state()
        if self.active_source is not None:
            st.source_id = self.active_source.id
            st.source_name = self.active_source.label()
            st.network = self.active_source.network
        st.failed_sources = sorted(
            self._tried - ({self.active_source.id} if self.active_source else set())
        )
        return st

    def describe_unavailable(self) -> str:
        candidates = self.channel.ordered_sources()
        if not candidates:
            return "no enabled sources configured"

        full: set[str] = set()
        disabled: set[str] = set()
        for source in candidates:
            if not source.network or self.registry.has_room(source.network, self.key):
                continue
            if self.registry.enabled(source.network):
                full.add(source.network)
            else:
                disabled.add(source.network)

        reasons = []
        if full:
            reasons.append(f"at capacity: {', '.join(sorted(full))}")
        if disabled:
            reasons.append(f"disabled: {', '.join(sorted(disabled))}")
        if reasons:
            return f"every source is blocked ({'; '.join(reasons)})"
        return "no source available"

    def _has_consumers(self) -> bool:
        return bool(self._subscribers) or bool(self.transcoders)

    def total_clients(self) -> int:
        """Everyone watching this channel, whichever profile they chose."""
        return self.client_count + sum(t.client_count for t in self.transcoders)

    def add_transcoder(self, transcoder: "TranscodeSession") -> None:
        self.transcoders.add(transcoder)
        self._cancel_idle_timer()
        self.start()

    def remove_transcoder(self, transcoder: "TranscodeSession") -> None:
        self.transcoders.discard(transcoder)
        if not self._has_consumers():
            self._schedule_idle_stop()

    async def _attempt(self) -> None:
        source = self.select_source()
        if source is None:
            self.active_source = None
            # Not streaming, so do not sit on a network slot others could use.
            self.registry.release(self.key)
            raise StreamFailure(self.describe_unavailable())

        if not self.registry.acquire(source.network, self.key):
            # Lost a race for the last slot; a retry will pick another source.
            raise StreamFailure(f"network {source.network!r} filled up before we could start")

        self._tried.add(source.id)
        self.active_source = source
        if self.ledger is not None:
            self.ledger.connections += 1
        cmd = source.command
        where = f" on {source.network}" if source.network else ""
        self._log(f"--- source {source.label()}{where}: {cmd} ---")
        tail: deque[str] = deque(maxlen=6)

        try:
            if source.use_shell:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *source.argv(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
        except FileNotFoundError as exc:
            # Fatal for this source, but another source may still work, so this
            # is only terminal when there is nothing left to fail over to.
            raise StreamFailure(
                f"{source.label()}: command not found: {program_name(cmd)}",
                fatal=not self._untried_available(),
            ) from exc
        except PermissionError as exc:
            raise StreamFailure(
                f"{source.label()}: not executable: {program_name(cmd)}",
                fatal=not self._untried_available(),
            ) from exc

        self._procs = [proc]
        self._watch_stderr(proc, "src", tail)
        assert proc.stdout is not None

        aligner = TSAligner()
        while True:
            timeout = (
                self.settings.stall_timeout_seconds
                if self.flowing_since
                else self.settings.startup_timeout_seconds
            )
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(READ_CHUNK), timeout=timeout)
            except asyncio.TimeoutError as exc:
                what = "produced no data" if not self.flowing_since else "stalled"
                raise StreamFailure(
                    f"{source.label()} {what} for {timeout:.0f}s", list(tail)
                ) from exc
            if not chunk:
                rc = await self._reap(proc)
                raise StreamFailure(
                    f"{source.label()} ended ({describe_exit(rc)})", list(tail)
                )

            # Only publish whole TS packets, so every client starts on a packet
            # boundary no matter when it joins.
            data = aligner.feed(chunk)
            if aligner.passthrough and self._ts_output:
                self._ts_output = False
                self._log("--- output is not MPEG-TS, relaying bytes unaligned ---")
                log.warning("source %s does not look like MPEG-TS", self.key)
            if not data:
                continue
            self._mark_flowing()
            self.publish(data)


# ---------------------------------------------------------------------------
# The transcoder
# ---------------------------------------------------------------------------


class TranscodeSession(Broadcaster):
    """Feeds a source's bytes through ffmpeg and broadcasts the result."""

    kind = "transcode"

    def __init__(
        self,
        source: SourceSession,
        profile: Profile,
        settings: Settings,
        on_idle: Callable[["Broadcaster"], None],
    ):
        super().__init__(f"{source.channel.id}@{profile.id}", source.channel, settings, on_idle)
        self.source = source
        self.profile = profile
        self.profile_id = profile.id
        self.input_dropped = 0
        # Packet alignment only means anything for a TS muxer.
        self._ts_output = profile.container == "mpegts"

    def start(self) -> None:
        # Attaching to the source also starts it if it is not already running.
        self.source.add_transcoder(self)
        super().start()

    async def stop(self, reason: str = "stopped") -> None:
        await super().stop(reason)
        self.source.remove_transcoder(self)

    def can_accept(self) -> bool:
        limit = self.client_limit
        return not limit or self.source.total_clients() < limit

    async def _attempt(self) -> None:
        argv = self.profile.ffmpeg_argv(self.settings.ffmpeg_bin, self.settings.ffmpeg_loglevel)
        self._log(f"--- transcode: {' '.join(argv)} ---")
        tail: deque[str] = deque(maxlen=8)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise StreamFailure(
                f"ffmpeg not found: {self.settings.ffmpeg_bin}", fatal=True
            ) from exc

        self._procs = [proc]
        self._watch_stderr(proc, "ff", tail)

        # Take our own feed off the shared source.
        feed = self.source.subscribe(f"transcode:{self.profile.id}")
        writer = asyncio.create_task(self._feed(proc, feed))
        self._helper_tasks.append(writer)

        aligner = TSAligner() if self._ts_output else None
        try:
            assert proc.stdout is not None
            while True:
                timeout = (
                    self.settings.stall_timeout_seconds
                    if self.flowing_since
                    else self.settings.startup_timeout_seconds
                )
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(READ_CHUNK), timeout=timeout)
                except asyncio.TimeoutError as exc:
                    what = "produced no data" if not self.flowing_since else "stalled"
                    raise StreamFailure(
                        f"transcoder {what} for {timeout:.0f}s", list(tail)
                    ) from exc
                if not chunk:
                    rc = await self._reap(proc)
                    # ffmpeg that dies without ever emitting a frame is almost
                    # always a broken profile, not a flaky network.
                    fatal = self.flowing_since is None and self.failures + 1 >= 3
                    raise StreamFailure(
                        f"transcoder ended ({describe_exit(rc)})", list(tail), fatal=fatal
                    )

                data = aligner.feed(chunk) if aligner else chunk
                if aligner and aligner.passthrough and self._ts_output:
                    self._ts_output = False
                    self._log("--- transcoder output is not MPEG-TS, relaying unaligned ---")
                if not data:
                    continue
                self._mark_flowing()
                self.publish(data)
        finally:
            self.source.unsubscribe(feed)
            self.input_dropped += feed.dropped

    async def _feed(self, proc: asyncio.subprocess.Process, feed: Subscriber) -> None:
        """Pump source bytes into ffmpeg's stdin.

        drain() applies backpressure here only. If ffmpeg cannot keep up, our own
        queue overflows and drops its oldest chunks, which never stalls the source
        or any other viewer of it.
        """
        assert proc.stdin is not None
        try:
            while True:
                chunk = await feed.queue.get()
                if chunk is None:
                    break
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self._log("[ff] stdin closed by ffmpeg")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("feed for %s ended", self.key, exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    def state(self) -> SessionState:
        st = super().state()
        st.input_dropped = self.input_dropped + sum(
            s.dropped for s in self.source._subscribers if s.label == f"transcode:{self.profile.id}"
        )
        return st
