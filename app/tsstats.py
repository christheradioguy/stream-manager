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

    def add(self, other: "Counters") -> None:
        self.packets += other.packets
        self.transport_errors += other.transport_errors
        self.continuity_errors += other.continuity_errors
        self.scrambled += other.scrambled
        self.nulls += other.nulls
        self.discontinuities += other.discontinuities

    def as_dict(self) -> dict[str, int]:
        return {
            "packets": self.packets,
            "transport_errors": self.transport_errors,
            "continuity_errors": self.continuity_errors,
            "scrambled": self.scrambled,
            "nulls": self.nulls,
            "discontinuities": self.discontinuities,
        }


class TSAnalyser:
    """Counts transport and continuity errors over a stream of whole packets."""

    __slots__ = ("counters", "_state", "pid_errors", "enabled")

    def __init__(self, enabled: bool = True):
        self.counters = Counters()
        self.pid_errors: dict[int, int] = {}
        self._state: dict[int, int] = {}
        self.enabled = enabled

    def feed(self, data: bytes) -> None:
        """Analyse a buffer of whole, sync-aligned TS packets."""
        if not self.enabled:
            return
        counters = self.counters
        state = self._state
        packets = transport_errors = continuity_errors = 0
        scrambled = nulls = discontinuities = 0

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
    dropped_chunks: int = 0
    input_dropped: int = 0
    ts: Counters = field(default_factory=Counters)


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
