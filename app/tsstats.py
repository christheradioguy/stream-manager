"""MPEG-TS packet analysis: transport errors and continuity counters.

Both are the standard health signals for a transport stream:

* **Transport error** — the transport_error_indicator bit is set, meaning an
  upstream demodulator or muxer marked the packet as containing uncorrectable
  errors. Anything above zero means the signal or link is damaged.
* **Continuity error** — a PID's 4-bit continuity counter did not advance by one.
  Usually packet loss, and the direct cause of macroblocking and audio dropouts.

Analysis runs on the already-aligned packet stream, so it sees whole packets and
never has to hunt for sync itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

TS_PACKET = 188
NULL_PID = 0x1FFF

# Per-PID state is packed into one int to keep the hot loop allocation-free:
# low nibble is the last continuity counter, bit 4 marks "a duplicate is spent".
_CC_MASK = 0x0F
_DUP_FLAG = 0x10


@dataclass
class Counters:
    """Cumulative stream-health counters."""

    packets: int = 0
    transport_errors: int = 0
    continuity_errors: int = 0
    scrambled: int = 0
    nulls: int = 0
    discontinuities: int = 0  # signalled, i.e. expected - not counted as errors
    # Holes in the presentation timeline: frames that should have been here and
    # are not. Survives a re-mux, which the two error counts above do not.
    content_gaps: int = 0
    content_lost: float = 0.0        # seconds missing, summed

    def add(self, other: "Counters") -> None:
        self.packets += other.packets
        self.transport_errors += other.transport_errors
        self.continuity_errors += other.continuity_errors
        self.scrambled += other.scrambled
        self.nulls += other.nulls
        self.discontinuities += other.discontinuities
        self.content_gaps += other.content_gaps
        self.content_lost += other.content_lost

    def as_dict(self) -> dict[str, int]:
        return {
            "packets": self.packets,
            "transport_errors": self.transport_errors,
            "continuity_errors": self.continuity_errors,
            "scrambled": self.scrambled,
            "nulls": self.nulls,
            "discontinuities": self.discontinuities,
            "content_gaps": self.content_gaps,
            "content_lost": round(self.content_lost, 3),
        }


class TSAnalyser:
    """Counts transport and continuity errors over a stream of whole packets."""

    __slots__ = ("counters", "_state", "pid_errors", "enabled", "_timeline")

    def __init__(self, enabled: bool = True):
        self.counters = Counters()
        self.pid_errors: dict[int, int] = {}
        self._state: dict[int, int] = {}
        # Per PID: the last decode timestamp seen and the usual gap between
        # them, for spotting frames that never arrived.
        self._timeline: dict[int, tuple[int, float]] = {}
        self.enabled = enabled

    def feed(self, data: bytes) -> None:
        """Analyse a buffer of whole, sync-aligned TS packets."""
        if not self.enabled:
            return
        counters = self.counters
        state = self._state
        packets = transport_errors = continuity_errors = 0
        scrambled = nulls = discontinuities = 0
        content_gaps = 0
        content_lost = 0.0

        for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
            packets += 1
            byte1 = data[offset + 1]

            if byte1 & 0x80:
                # Flagged corrupt upstream, so its continuity counter cannot be
                # trusted either - count it and leave the PID's state alone.
                transport_errors += 1
                continue

            pid = ((byte1 & 0x1F) << 8) | data[offset + 2]
            if pid == NULL_PID:
                nulls += 1
                continue

            byte3 = data[offset + 3]
            if byte3 & 0xC0:
                scrambled += 1

            adaptation = (byte3 >> 4) & 0x03
            if adaptation == 0:
                continue  # reserved; no payload and no adaptation field

            if adaptation & 0x02:
                # An adaptation field may declare an intentional discontinuity,
                # e.g. after a splice. Honouring it avoids false positives.
                length = data[offset + 4]
                if length and (data[offset + 5] & 0x80):
                    discontinuities += 1
                    if adaptation & 0x01:
                        state[pid] = byte3 & _CC_MASK
                    else:
                        state.pop(pid, None)
                    continue

            if not adaptation & 0x01:
                continue  # no payload, so the counter must not advance

            if byte1 & 0x40 and adaptation & 0x01:
                # A hole in the presentation timeline is the one sign of lost
                # content that survives a re-mux. ffmpeg drops the damaged
                # packets and writes a clean transport layer, but it cannot
                # invent the frames that went with them, so their timestamps are
                # simply missing - and that is visible here.
                lost = self._timeline_gap(data, offset, pid)
                if lost:
                    content_gaps += 1
                    content_lost += lost

            counter = byte3 & _CC_MASK
            previous = state.get(pid)
            if previous is None:
                state[pid] = counter
                continue

            last = previous & _CC_MASK
            if counter == (last + 1) & _CC_MASK:
                state[pid] = counter
            elif counter == last and not previous & _DUP_FLAG:
                # One duplicate packet is legal; a second in a row is not.
                state[pid] = counter | _DUP_FLAG
            else:
                continuity_errors += 1
                self.pid_errors[pid] = self.pid_errors.get(pid, 0) + 1
                state[pid] = counter

        counters.packets += packets
        counters.transport_errors += transport_errors
        counters.continuity_errors += continuity_errors
        counters.scrambled += scrambled
        counters.nulls += nulls
        counters.discontinuities += discontinuities
        counters.content_gaps += content_gaps
        counters.content_lost += content_lost

    def _timeline_gap(self, data: bytes, offset: int, pid: int) -> float:
        """Seconds of content missing before this packet, or 0.

        Decode timestamps are used rather than presentation ones because they
        run in order: anything with B-frames presents out of sequence, and
        differences between presentation stamps would read as holes that are not
        there. What counts as a hole is judged against the gap this stream
        usually has, so it works whatever the frame rate.
        """
        byte3 = data[offset + 3]
        at = offset + 4 + (1 + data[offset + 4] if byte3 & 0x20 else 0)
        if at + 14 > offset + TS_PACKET or data[at:at + 3] != b"\x00\x00\x01":
            return 0.0
        flags = data[at + 7]
        if not flags & 0x80:
            return 0.0
        # Prefer the decode stamp; without one it equals the presentation stamp.
        base = at + 14 if (flags & 0x40 and at + 19 <= offset + TS_PACKET) else at + 9
        b = data[base:base + 5]
        now = ((((b[0] >> 1) & 0x07) << 30) | (b[1] << 22)
               | (((b[2] >> 1) & 0x7F) << 15) | (b[3] << 7) | (b[4] >> 1))

        seen = self._timeline.get(pid)
        if seen is None:
            self._timeline[pid] = (now, 0.0)
            return 0.0
        last, usual = seen
        step = (now - last) % (1 << 33)
        if step == 0 or step > 10 * 90000:
            # A jump this large is a discontinuity or a wrap, not a lost frame;
            # start again rather than reporting hours of missing content.
            self._timeline[pid] = (now, usual)
            return 0.0
        if usual <= 0:
            self._timeline[pid] = (now, float(step))
            return 0.0
        # Settle on the usual spacing slowly, so one long gap does not become
        # the new normal and hide the next one.
        self._timeline[pid] = (now, usual * 0.95 + step * 0.05)
        if step > usual * 1.5:
            return (step - usual) / 90000.0
        return 0.0

    def worst_pids(self, limit: int = 5) -> list[dict[str, int]]:
        """The PIDs losing the most packets, for the GUI."""
        ranked = sorted(self.pid_errors.items(), key=lambda kv: kv[1], reverse=True)
        return [{"pid": pid, "errors": count} for pid, count in ranked[:limit]]


@dataclass
class LedgerEntry:
    """Counters for one (channel, profile) that outlive individual sessions.

    Prometheus counters must not go backwards, but a session is destroyed and
    recreated whenever a channel goes idle. Keeping the totals here instead means
    a restart does not look like a counter reset.
    """

    key: str
    channel_id: str
    channel_name: str
    profile: str = ""
    kind: str = "source"
    bytes_out: int = 0
    connections: int = 0
    restarts: int = 0
    reconnects: int = 0
    dropped_chunks: int = 0
    input_dropped: int = 0
    ts: Counters = field(default_factory=Counters)
    # What the source tool itself complained about, by kind. See classify_log.
    events: dict[str, int] = field(default_factory=dict)


# What a source tool's own output says about the stream, grouped so the counts
# mean something on a dashboard.
#
# This exists because the transport-level counters cannot see most real faults.
# A source that ends in `ffmpeg ... -c copy -f mpegts` rebuilds the transport
# layer from scratch: it drops whatever was damaged, writes a fresh continuity
# sequence and never sets the transport-error bit, so a stream that is visibly
# breaking up arrives looking immaculate. What ffmpeg discarded, it says in its
# log, and that is the only place the evidence survives.
LOG_EVENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Pictures or sound the decoder could not reconstruct: the direct cause of
    # macroblocking and audio dropouts.
    ("decode", (
        "error while decoding",
        "corrupt decoded frame",
        "error decoding the audio block",
        "invalid level prefix",
        "corrupted macroblock",
        "concealing",
        "ac-tex damaged",
        "mmco:",
        "no frame",
        "invalid frame dimensions",
    )),
    # The stream's own timing is inconsistent - usually the provider's fault,
    # and what stalls players that pace themselves from the clock.
    ("timestamp", (
        "non-monotonic dts",
        "invalid timestamps",
        "out of order",
        "timestamp discontinuity",
        "invalid dts",
        "invalid pts",
        "pts < dts",
    )),
    # Trouble reaching or holding the upstream connection.
    ("input", (
        "will reconnect",
        "connection reset",
        "connection timed out",
        "connection refused",
        "server returned",
        "error opening input",
        "end of file",
        "i/o error",
        "failed to open",
    )),
    # The muxer having to intervene, which means something upstream is wrong.
    ("muxer", (
        "packet too large",
        "application provided invalid",
        "timestamps are unset",
        "cur_dts is invalid",
        "max interleave",
    )),
)


LOG_EVENT_KINDS: tuple[str, ...] = tuple(kind for kind, _ in LOG_EVENTS)


def classify_log(line: str) -> Optional[str]:
    """Which kind of trouble a source tool's log line reports, if any.

    Returns None for ordinary chatter - progress lines, banners, stream
    descriptions - so only real complaints are counted.
    """
    lowered = line.lower()
    for kind, needles in LOG_EVENTS:
        for needle in needles:
            if needle in lowered:
                return kind
    return None


class StatsLedger:
    """Process-lifetime totals per session key."""

    def __init__(self) -> None:
        self.entries: dict[str, LedgerEntry] = {}

    def entry(
        self, key: str, channel_id: str, channel_name: str, profile: str, kind: str
    ) -> LedgerEntry:
        found = self.entries.get(key)
        if found is None:
            found = LedgerEntry(
                key=key,
                channel_id=channel_id,
                channel_name=channel_name,
                profile=profile,
                kind=kind,
            )
            self.entries[key] = found
        else:
            found.channel_name = channel_name
        return found

    def forget_channel(self, channel_id: str) -> None:
        self.entries = {
            k: v for k, v in self.entries.items() if v.channel_id != channel_id
        }

    def totals(self) -> Counters:
        total = Counters()
        for entry in self.entries.values():
            total.add(entry.ts)
        return total
