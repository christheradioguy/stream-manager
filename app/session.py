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
import re
import shlex
import signal
import time
from bisect import bisect_left, bisect_right
from collections import deque
from typing import AsyncIterator, Awaitable, Callable, Optional

from .models import Channel, Network, Profile, SessionState, Settings, Source
from .tsstats import NULL_PID, LedgerEntry, TSAnalyser, classify_log

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

# How far the prebuffer may overshoot its byte limit to keep a whole GOP in
# it. Past this the stream's keyframes are simply too far apart to buffer,
# and it is trimmed by bytes like any other.
PREBUFFER_GOP_SLACK = 4

# ffmpeg reports progress as "... bitrate=N speed=1.23x".
_SPEED_RE = re.compile(r"speed=\s*([0-9]+\.?[0-9]*)x")

# MPEG-TS timestamps are 33-bit values ticking at 90 kHz, and wrap.
TS_CLOCK = 1 << 33
TS_HZ = 90000
# Headroom left at a splice so the new run starts fractionally after the old one
# ended rather than exactly on top of it. It has to stay well inside the 100 ms
# a decoder is entitled to expect between clock references, or the splice itself
# becomes a gap in the clock.
TIMELINE_GAP = TS_HZ // 50

# The PES stream ids that have no optional header, and so never carry a
# timestamp to rewrite: stream maps and directories, padding, ECM/EMM, DSM-CC.
# Everything else does, and every one of those has to be shifted together.
#
# Getting this wrong is subtle and severe. AC-3 - the audio on most US
# broadcast streams - travels as private_stream_1, id 0xBD, which sits below
# the 0xC0 where audio ids are usually said to start. Rewriting video but not
# that audio leaves the two on different timelines, a little further apart with
# every restart, which is heard as the sound running steadily ahead of the
# picture.
NO_PES_HEADER = frozenset({0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF})


def _carries_timestamps(stream_id: int) -> bool:
    return stream_id not in NO_PES_HEADER


# Stream types the PMT uses for audio, alongside VIDEO_STREAM_TYPES above.
AUDIO_STREAM_TYPES = frozenset({
    0x03,  # MPEG-1 audio
    0x04,  # MPEG-2 audio
    0x0F,  # AAC (ADTS)
    0x11,  # AAC (LATM)
    0x1C,  # AAC (raw)
    0x81,  # AC-3
    0x87,  # E-AC-3
})


class TSAudioClock:
    """Rebuilds audio timestamps from the stream's own clock.

    A few providers hand out streams whose audio timestamps are simply wrong:
    minutes away from the video's, advancing at the wrong rate, or both. The
    audio itself is fine - every packet present, interleaved with the picture it
    belongs to - and players that ignore timestamps sound perfect on it, which is
    why such a stream can look healthy right up until something paces itself from
    the clock and stalls.

    No ffmpeg filter fixes this. By the time a filter runs the demuxer has
    already decided which audio goes with which picture, using the timestamps
    that are wrong, and no later correction recovers the pairing. This works a
    step earlier, on the transport stream, where the information still exists.

    Two things are needed and both are present in a stream like this:

    * *Where* the audio belongs comes from its position. A muxer emits an audio
      packet alongside the video it accompanies, so the clock reference at that
      point in the stream says when it should play.
    * *How long* each packet lasts comes from its size. Position alone is too
      coarse - audio is emitted in bursts, and stamps derived from it jitter by
      hundreds of milliseconds and run backwards. Constant-rate audio covers time
      in proportion to its bytes, so the first packet is anchored from the clock
      and the rest follow from the data. The result advances by exactly one frame
      each time and cannot go backwards.
    """

    __slots__ = ("_pmt_pids", "_video_pids", "_audio_pids", "_pcr",
                 "_lead", "_anchor", "_carried", "_per_byte", "_rate_from",
                 "_last", "_marked")

    # A decoder expects the picture a little ahead of the clock. Until the real
    # figure is learned from the video, assume the usual.
    DEFAULT_LEAD = (7 * TS_HZ) // 10

    def __init__(self) -> None:
        self._pmt_pids: set[int] = set()
        self._video_pids: frozenset[int] = frozenset()
        self._audio_pids: frozenset[int] = frozenset()
        self._pcr: list[tuple[int, int]] = []      # (position, clock) pairs
        self._lead: Optional[int] = None
        self._anchor: Optional[int] = None
        self._carried = 0                          # audio bytes since the anchor
        self._per_byte: Optional[float] = None
        self._rate_from: Optional[tuple[int, int]] = None   # (bytes, clock)
        self._last: Optional[int] = None                   # last stamp emitted
        self._marked = 0                                   # bytes at that stamp

    def restart(self) -> None:
        """The source reopened; anchor afresh rather than trusting the old one."""
        self._anchor = None
        self._carried = 0
        self._marked = 0
        self._last = None
        self._pcr.clear()

    def process(self, chunk: bytes, base: int) -> bytes:
        """Rewrite the audio timestamps in a buffer of whole, aligned packets."""
        data = bytearray(chunk)
        for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
            byte1 = data[offset + 1]
            pid = ((byte1 & 0x1F) << 8) | data[offset + 2]
            byte3 = data[offset + 3]
            position = base + offset

            if byte3 & 0x20 and data[offset + 4] >= 7 and data[offset + 5] & 0x10:
                a = offset + 6
                clock = (data[a] << 25 | data[a + 1] << 17 | data[a + 2] << 9
                         | data[a + 3] << 1 | data[a + 4] >> 7)
                self._pcr.append((position, clock))
                if len(self._pcr) > 4:
                    del self._pcr[0]

            if pid == 0:
                if byte1 & 0x40:
                    self._read_pat(data, offset)
                continue
            if pid in self._pmt_pids:
                if byte1 & 0x40:
                    self._read_pmt(data, offset)
                continue
            if pid in self._video_pids:
                self._learn_lead(data, offset, position)
                continue
            if pid in self._audio_pids:
                self._stamp(data, offset, position)
        return bytes(data)

    # -- learning ----------------------------------------------------------

    def _clock_at(self, position: int) -> Optional[int]:
        """The clock at a byte position, interpolated between references."""
        if len(self._pcr) < 2:
            return None
        (p1, t1), (p2, t2) = self._pcr[-2], self._pcr[-1]
        if p2 == p1:
            return t2
        per_byte = ((t2 - t1) % TS_CLOCK) / (p2 - p1)
        return int(t2 + (position - p2) * per_byte) % TS_CLOCK

    def _learn_lead(self, data: bytearray, offset: int, position: int) -> None:
        """How far ahead of the clock the video's stamps sit."""
        header = self._pes_header(data, offset)
        if header is None:
            return
        at, flags = header
        if not flags & 0x80:
            return
        clock = self._clock_at(position)
        if clock is None:
            return
        lead = (self._decode(data, at + 9) - clock) % TS_CLOCK
        if lead > 5 * TS_HZ:            # not a sane buffering delay; ignore it
            return
        # Settle towards it rather than following every frame, so one odd
        # stamp cannot drag the audio with it.
        self._lead = lead if self._lead is None else (self._lead * 7 + lead) // 8

    def _stamp(self, data: bytearray, offset: int, position: int) -> None:
        byte3 = data[offset + 3]
        if not byte3 & 0x10 or byte3 & 0xC0:
            return
        payload = offset + 4 + (1 + data[offset + 4] if byte3 & 0x20 else 0)
        if payload >= offset + TS_PACKET:
            return
        header = self._pes_header(data, offset)
        if header is None:
            # A continuation packet: all of it is audio data.
            self._carried += offset + TS_PACKET - payload
            return
        at, flags = header
        body = at + 9 + data[at + 8]
        if flags & 0x80:
            clock = self._clock_at(position)
            if self._anchor is None:
                # Anchor the first packet on the clock where it sits, which is
                # where the muxer put it alongside the picture it belongs with.
                if clock is None:
                    self._carried += max(0, offset + TS_PACKET - body)
                    return
                lead = self.DEFAULT_LEAD if self._lead is None else self._lead
                self._anchor = (clock + lead) % TS_CLOCK
                self._rate_from = (self._carried, clock)
                self._marked = self._carried
            self._learn_rate(clock)
            if self._per_byte is None:
                # The rate needs a second or so of audio to measure. Until then
                # stamp from position: it jitters, but leaving the provider's own
                # values in place would put minutes-wrong stamps in front of
                # correct ones, which is far worse.
                if clock is not None:
                    lead = self.DEFAULT_LEAD if self._lead is None else self._lead
                    self._emit(data, at, flags, offset, (clock + lead) % TS_CLOCK)
                    # Keep the marker with it, or the first step once the rate
                    # is known would span every byte since the anchor at once.
                    self._marked = self._carried
            elif self._last is None:
                self._emit(data, at, flags, offset, self._anchor)
                self._marked = self._carried
            else:
                # Step from the previous stamp by however much audio has gone by
                # since it. Recomputing from the anchor instead would mean every
                # refinement of the rate shifted the whole run at once, by more
                # the longer it had been playing, and that shows up as jitter.
                # Stepping keeps each interval exactly one packet of audio.
                since = self._carried - self._marked
                self._emit(data, at, flags, offset,
                           (self._last + max(int(since * self._per_byte), 1)) % TS_CLOCK)
                self._marked = self._carried
        self._carried += max(0, offset + TS_PACKET - body)

    def _emit(self, data: bytearray, at: int, flags: int, offset: int, value: int) -> None:
        """Write a stamp, never letting it fall behind the one before it.

        Both the correction above and the jittery opening second can produce a
        value below its predecessor, and audio whose timestamps step backwards
        is worse than audio that drifts - a decoder discards it. So the clock is
        held forwards here, by a whole sample period at least, whatever the
        arithmetic upstream produced.
        """
        if self._last is not None:
            ahead = (value - self._last) % TS_CLOCK
            if not 0 < ahead < TS_CLOCK // 2:
                value = (self._last + 1) % TS_CLOCK
        self._last = value
        self._write(data, at + 9, value)
        if flags & 0x40 and at + 19 <= offset + TS_PACKET:
            self._write(data, at + 14, value)

    def _learn_rate(self, clock: Optional[int]) -> None:
        """Ticks per byte of audio, from how far the clock moved across it."""
        if clock is None or self._rate_from is None:
            return
        bytes_since = self._carried - self._rate_from[0]
        moved = (clock - self._rate_from[1]) % TS_CLOCK
        # Wait for a decent span before trusting the figure, then keep refining
        # it: too short a sample and the muxer's burstiness dominates.
        if bytes_since > 16000 and moved and moved < 3600 * TS_HZ:
            self._per_byte = moved / bytes_since

    # -- packet plumbing ---------------------------------------------------

    @staticmethod
    def _pes_header(data: bytearray, offset: int) -> Optional[tuple[int, int]]:
        """Where a PES header starts in this packet, and its second flag byte."""
        byte3 = data[offset + 3]
        if not byte3 & 0x10 or not data[offset + 1] & 0x40:
            return None
        at = offset + 4 + (1 + data[offset + 4] if byte3 & 0x20 else 0)
        if at + 14 > offset + TS_PACKET or data[at:at + 3] != b"\x00\x00\x01":
            return None
        if data[at + 3] in NO_PES_HEADER:
            return None
        return at, data[at + 7]

    @staticmethod
    def _decode(data: bytearray, at: int) -> int:
        return ((((data[at] >> 1) & 0x07) << 30) | (data[at + 1] << 22)
                | (((data[at + 2] >> 1) & 0x7F) << 15) | (data[at + 3] << 7)
                | (data[at + 4] >> 1))

    @staticmethod
    def _write(data: bytearray, at: int, value: int) -> None:
        data[at] = (data[at] & 0xF0) | ((value >> 29) & 0x0E) | 0x01
        data[at + 1] = (value >> 22) & 0xFF
        data[at + 2] = ((value >> 14) & 0xFE) | 0x01
        data[at + 3] = (value >> 7) & 0xFF
        data[at + 4] = ((value << 1) & 0xFE) | 0x01

    # -- tables ------------------------------------------------------------

    def _read_pat(self, data: bytearray, offset: int) -> None:
        start, end = TSStartPoints._section(data, offset, 0x00)
        if not end:
            return
        i = start + 8
        while i + 4 <= end:
            if (data[i] << 8) | data[i + 1]:
                self._pmt_pids.add(((data[i + 2] & 0x1F) << 8) | data[i + 3])
            i += 4

    def _read_pmt(self, data: bytearray, offset: int) -> None:
        start, end = TSStartPoints._section(data, offset, 0x02)
        if not end or start + 12 > end:
            return
        i = start + 12 + (((data[start + 10] & 0x0F) << 8) | data[start + 11])
        video: set[int] = set()
        audio: set[int] = set()
        while i + 5 <= end:
            pid = ((data[i + 1] & 0x1F) << 8) | data[i + 2]
            if data[i] in VIDEO_STREAM_TYPES:
                video.add(pid)
            elif data[i] in AUDIO_STREAM_TYPES:
                audio.add(pid)
            i += 5 + (((data[i + 3] & 0x0F) << 8) | data[i + 4])
        if video:
            self._video_pids = frozenset(video)
        if audio:
            self._audio_pids = frozenset(audio)


class TSRestamper:
    """Holds one continuous timeline across source restarts.

    Reopening a source starts a brand-new encoder run, whose timestamps begin
    again from near zero. Splicing that into a client's stream rewinds its clock
    by however long the previous run lasted, and players do not agree on what to
    do about that: some resync, some freeze, some keep the old clock and play
    audio and video at different offsets from then on. That last one is a sync
    error that gets worse with every reconnect, and live sources reconnect
    often - a rotating playlist window or token is a normal end, not a fault.

    So each run's timestamps are shifted to carry on from where the last one
    stopped. The client sees one timeline that only moves forwards and never
    learns that the source restarted.
    """

    __slots__ = ("_offset", "_last", "_pending", "_cc_out", "_cc_shift")

    def __init__(self) -> None:
        self._offset = 0
        # The furthest stamp handed out, kept per kind. A stream's clock
        # references run behind its presentation stamps by however much the
        # decoder is expected to buffer - a second is normal. Resuming the clock
        # from where the presentation stamps got to would jump it forward by
        # that much at every restart, so each kind has to resume from its own.
        self._last: dict[str, int] = {}
        self._pending = False              # a new run needs an offset
        # Continuity counters restart with the run too. A demuxer reads that as
        # packet loss and resyncs, which is a glitch per reconnect and a burst
        # of continuity errors against a stream that never actually lost
        # anything. Each PID gets a shift that carries its counter on instead.
        self._cc_out: dict[int, int] = {}
        self._cc_shift: dict[int, int] = {}

    def restart(self) -> None:
        """The next run starts its clocks over; carry them on when they arrive."""
        self._pending = True
        self._cc_shift.clear()

    def process(self, chunk: bytes) -> bytes:
        """Put a buffer of whole, aligned packets onto the running timeline.

        The first run keeps its own timestamps - there is nothing in front of it
        to follow - but is still walked, because where it ends is where the next
        run has to start.
        """
        buf = bytearray(chunk)
        self.apply(buf)
        return bytes(buf)

    def apply(self, data: bytearray) -> None:
        """Rewrite the timestamps in a buffer of whole, aligned packets."""
        for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
            byte3 = data[offset + 3]
            pid = ((data[offset + 1] & 0x1F) << 8) | data[offset + 2]
            if pid != NULL_PID:
                byte3 = self._shift_counter(data, offset, byte3, pid)
            payload = offset + 4
            if byte3 & 0x20:
                field = data[offset + 4]
                payload += 1 + field
                # PCR shares the timeline, so it has to move with the rest.
                if field >= 7 and data[offset + 5] & 0x10:
                    self._shift_pcr(data, offset + 6)
            if not byte3 & 0x10 or not data[offset + 1] & 0x40:
                continue
            if byte3 & 0xC0:
                continue  # scrambled: the PES header is ciphertext, not a header
            if payload + 14 > offset + TS_PACKET:
                continue
            if data[payload:payload + 3] != b"\x00\x00\x01":
                continue
            if not _carries_timestamps(data[payload + 3]):
                continue
            flags = data[payload + 7]
            if flags & 0x80:
                self._shift_stamp(data, payload + 9, advance=True)
            if flags & 0x40 and payload + 19 <= offset + TS_PACKET:
                # The decode stamp moves by the same amount, or the two stop
                # meaning the same thing.
                self._shift_stamp(data, payload + 14, advance=False)

    # -- the two encodings -------------------------------------------------

    def _shift_stamp(self, data: bytearray, at: int, advance: bool) -> None:
        raw = (((data[at] >> 1) & 0x07) << 30 | data[at + 1] << 22
               | ((data[at + 2] >> 1) & 0x7F) << 15 | data[at + 3] << 7
               | data[at + 4] >> 1)
        value = self._rebase(raw, "pts")
        if advance:
            self._advance(value, "pts")
        data[at] = (data[at] & 0xF0) | ((value >> 29) & 0x0E) | 0x01
        data[at + 1] = (value >> 22) & 0xFF
        data[at + 2] = ((value >> 14) & 0xFE) | 0x01
        data[at + 3] = (value >> 7) & 0xFF
        data[at + 4] = ((value << 1) & 0xFE) | 0x01

    def _shift_pcr(self, data: bytearray, at: int) -> None:
        base = (data[at] << 25 | data[at + 1] << 17 | data[at + 2] << 9
                | data[at + 3] << 1 | data[at + 4] >> 7)
        value = self._rebase(base, "pcr")
        self._advance(value, "pcr")
        data[at] = (value >> 25) & 0xFF
        data[at + 1] = (value >> 17) & 0xFF
        data[at + 2] = (value >> 9) & 0xFF
        data[at + 3] = (value >> 1) & 0xFF
        data[at + 4] = ((value << 7) & 0x80) | (data[at + 4] & 0x7F)

    def _shift_counter(self, data: bytearray, offset: int, byte3: int, pid: int) -> int:
        """Continue this PID's continuity counter across a restart."""
        counter = byte3 & 0x0F
        shift = self._cc_shift.get(pid)
        if shift is None:
            previous = self._cc_out.get(pid)
            # Only a PID the client has already been watching needs to be
            # carried on; one appearing for the first time starts where it likes.
            # A packet with no payload does not advance the counter, so it has to
            # repeat the last one rather than follow it.
            if previous is None:
                shift = 0
            elif byte3 & 0x10:
                shift = (previous + 1 - counter) & 0x0F
            else:
                shift = (previous - counter) & 0x0F
            self._cc_shift[pid] = shift
        if shift:
            counter = (counter + shift) & 0x0F
            byte3 = (byte3 & 0xF0) | counter
            data[offset + 3] = byte3
        self._cc_out[pid] = counter
        return byte3

    # -- the timeline ------------------------------------------------------

    def _rebase(self, raw: int, kind: str) -> int:
        if self._pending:
            # First stamp of a new run: line it up just past the last one of its
            # own kind that the client saw. On the very first run there is
            # nothing to follow, so the stream keeps its own timestamps.
            self._pending = False
            reference = self._last.get(kind)
            if reference is None:
                self._offset = 0
            else:
                self._offset = (reference + TIMELINE_GAP - raw) % TS_CLOCK
        return (raw + self._offset) % TS_CLOCK

    def _advance(self, value: int, kind: str) -> None:
        """Remember the furthest stamp of each kind handed out, where a restart resumes.

        This has to track what was actually emitted rather than a filtered view
        of it, or the next restart rebases onto a timestamp the client never saw
        and rewinds anyway. Only backward steps are ignored - a stream that
        reorders slightly within a run must not drag the resume point back.
        """
        previous = self._last.get(kind)
        if previous is None or 0 < (value - previous) % TS_CLOCK < TS_CLOCK // 2:
            self._last[kind] = value


# Stream types the PMT uses for video. A consumer has to be started on a video
# keyframe, so we have to know which PID carries the video.
VIDEO_STREAM_TYPES = frozenset({
    0x01,  # MPEG-1 video
    0x02,  # MPEG-2 video
    0x10,  # MPEG-4 part 2
    0x1B,  # H.264
    0x24,  # HEVC
    0x33,  # VVC
    0x42,  # AVS
    0xD1,  # Dirac
    0xEA,  # VC-1
})


class TSStartPoints:
    """Finds the places in a transport stream where a consumer may be started.

    Handing a viewer bytes from the middle of a GOP looks like it works: the
    demuxer syncs, the audio decodes immediately, and the picture appears a
    moment later at the next keyframe. What actually happened is that the player
    started its clock on the first thing it could decode - the audio - and every
    frame after that is presented late by however far into the GOP we happened to
    join. Up to a full keyframe interval, and it never corrects itself.

    So a joining consumer is started at a *random access point* instead: a video
    packet whose adaptation field sets random_access_indicator. Finding those
    needs the video PID, which needs the PMT, which needs the PAT - all of which
    this learns as the stream goes past.

    Streams whose muxer does not flag random access points leave ``marks_raps``
    false, and callers fall back to their previous behaviour rather than waiting
    for a keyframe that will never be announced.

    A caveat worth knowing: random_access_indicator is the muxer's word for "a
    decoder may begin here", and on an open-GOP H.264 stream that means a
    recovery point rather than an IDR. The pictures immediately after it still
    reference frames from before, so a decoder starting there has nothing to
    reference until the recovery completes. Software decoders discard those and
    carry on; some hardware decoders do not, and whether they survive depends on
    exactly where the join landed - which is why such a stream can look fine on
    a computer and fail unpredictably on a set-top box. Nothing here can mend
    that; only re-encoding produces real IDRs.
    """

    __slots__ = ("_pmt_pid", "_video_pids", "marks_raps")

    def __init__(self) -> None:
        self._pmt_pid: Optional[int] = None
        self._video_pids: frozenset[int] = frozenset()
        self.marks_raps = False

    def scan(self, data: bytes) -> tuple[list[int], list[int]]:
        """Offsets of the PATs and the random access points in aligned packets."""
        pats: list[int] = []
        raps: list[int] = []
        for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
            byte1 = data[offset + 1]
            pid = ((byte1 & 0x1F) << 8) | data[offset + 2]
            unit_start = byte1 & 0x40
            if pid == 0:
                if unit_start:
                    pats.append(offset)
                    self._read_pat(data, offset)
                continue
            if pid == self._pmt_pid:
                if unit_start:
                    self._read_pmt(data, offset)
                continue
            if unit_start and pid in self._video_pids:
                byte3 = data[offset + 3]
                # An adaptation field must be present, non-empty, and flag this
                # packet as a point the decoder can be started from.
                if (byte3 & 0x20) and data[offset + 4] and (data[offset + 5] & 0x40):
                    raps.append(offset)
                    self.marks_raps = True
        return pats, raps

    # -- section parsing ---------------------------------------------------
    #
    # Only sections that fit in their first packet are read, which is all a PAT
    # or a single-program PMT ever needs. Anything longer simply leaves the
    # tables unlearned, and the caller degrades to not aligning on keyframes.

    @staticmethod
    def _section(data: bytes, offset: int, table_id: int) -> tuple[int, int]:
        """Start and end offsets of a PSI section, or (0, 0) if unreadable."""
        byte3 = data[offset + 3]
        if not byte3 & 0x10:
            return 0, 0                       # no payload
        start = offset + 4
        if byte3 & 0x20:
            start += 1 + data[offset + 4]     # skip the adaptation field
        if start >= offset + TS_PACKET:
            return 0, 0
        start += 1 + data[start]              # skip the pointer field
        if start + 3 > offset + TS_PACKET or data[start] != table_id:
            return 0, 0
        length = ((data[start + 1] & 0x0F) << 8) | data[start + 2]
        end = min(start + 3 + length - 4, offset + TS_PACKET)  # less the CRC
        return start, end

    def _read_pat(self, data: bytes, offset: int) -> None:
        start, end = self._section(data, offset, 0x00)
        if not end:
            return
        i = start + 8
        while i + 4 <= end:
            program = (data[i] << 8) | data[i + 1]
            if program:                        # 0 is the network PID, not a program
                self._pmt_pid = ((data[i + 2] & 0x1F) << 8) | data[i + 3]
                return
            i += 4

    def _read_pmt(self, data: bytes, offset: int) -> None:
        start, end = self._section(data, offset, 0x02)
        if not end or start + 12 > end:
            return
        i = start + 12 + (((data[start + 10] & 0x0F) << 8) | data[start + 11])
        pids: set[int] = set()
        while i + 5 <= end:
            if data[i] in VIDEO_STREAM_TYPES:
                pids.add(((data[i + 1] & 0x1F) << 8) | data[i + 2])
            i += 5 + (((data[i + 3] & 0x0F) << 8) | data[i + 4])
        if pids:
            self._video_pids = frozenset(pids)



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

    __slots__ = ("queue", "dropped", "closed", "label", "needs_keyframe")

    def __init__(self, maxsize: int, label: str = ""):
        self.queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.closed = False
        self.label = label
        # Set while this consumer is waiting to be started on a keyframe rather
        # than partway through a GOP.
        self.needs_keyframe = False

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

        # Drops are counted here as well as on the subscriber, because a client
        # takes its own counter with it when it disconnects - which hid this
        # entirely until a stream was sampled mid-flight.
        self.dropped_total = 0
        self._subscribers: set[Subscriber] = set()
        self._prebuffer = bytearray()
        # Where in the prebuffer a consumer may be started, and where the
        # program tables that precede those points are.
        self._start_points = TSStartPoints()
        self._prebuffer_raps: list[int] = []
        self._prebuffer_pats: list[int] = []
        # Holds one timeline across restarts, so a reconnect does not rewind
        # every watching client's clock.
        self._restamper = TSRestamper()
        # Rebuilds audio timestamps for sources whose provider gets them wrong.
        # Off unless a source asks for it.
        self._audio_clock: Optional[TSAudioClock] = None
        self._published = 0
        # True while the output is MPEG-TS, so replay can respect packet
        # boundaries. Cleared for profiles that mux to something else.
        self._ts_output = True
        self._logs: deque[str] = deque(maxlen=settings.log_lines)
        self._bitrate_window: deque[tuple[float, int]] = deque()

        # Stream health for this session, plus the process-lifetime ledger that
        # survives the session being torn down and recreated.
        self.analyser = TSAnalyser(enabled=settings.ts_analysis)
        self.ledger: Optional[LedgerEntry] = None
        # What the source tool has complained about this session, and how fast
        # it says it is running. The transport counters cannot see either.
        self.events: dict[str, int] = {}
        self.speed: float = 0.0

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
            # Whatever this attempt produces, its timestamps start over. Rebase
            # them onto the timeline clients are already watching.
            self._restamper.restart()
            if self._audio_clock is not None:
                self._audio_clock.restart()
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
        # Hand over recent data so playback (or an encoder) can start at once,
        # from the last keyframe in it rather than from wherever the backlog
        # happens to begin.
        replay = self._replay_bytes()
        if replay:
            # One chunk: it is already a whole number of packets, and splitting
            # it would only risk the queue dropping half of it.
            sub.put(replay)
        elif self._ts_output and self._start_points.marks_raps:
            # Nothing to replay from. Rather than start it mid-GOP, hold it
            # until the stream reaches its next keyframe.
            sub.needs_keyframe = True
        self._subscribers.add(sub)
        self._cancel_idle_timer()
        self.start()
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)
        self.dropped_total += sub.dropped
        sub.dropped = 0
        if not self._has_consumers():
            self._schedule_idle_stop()

    async def publish(self, chunk: bytes) -> None:
        """Fan one chunk out, waiting for consumers that are briefly behind.

        A source is often much faster than real time - an HLS input downloads its
        whole segment backlog at line speed - while a player consumes at the rate
        the content plays. Throwing away the excess corrupts the stream, so
        instead this waits, which stops the pipe being drained, which makes the
        source process block. The burst is then absorbed upstream where it costs
        nothing, and no bytes are lost.

        The wait is bounded: a consumer still full at the deadline is genuinely
        too slow, and its oldest data is dropped so it cannot stall everyone else.
        """
        if self._ts_output and self._audio_clock is not None:
            # Before the restamper, and before anything else: this repairs what
            # the provider sent, the restamper then places it on our timeline.
            chunk = self._audio_clock.process(chunk, self._published)
        self._published += len(chunk)
        if self._ts_output:
            # Do this first: the prebuffer, the analyser and every consumer must
            # all see the same timeline the client is going to be given.
            chunk = self._restamper.process(chunk)
        self.bytes_out += len(chunk)
        if self._ts_output:
            before = self.analyser.counters
            packets, terr, cerr = before.packets, before.transport_errors, before.continuity_errors
            gaps, lost = before.content_gaps, before.content_lost
            self.analyser.feed(chunk)
            after = self.analyser.counters
            if self.ledger is not None:
                self.ledger.ts.packets += after.packets - packets
                self.ledger.ts.transport_errors += after.transport_errors - terr
                self.ledger.ts.continuity_errors += after.continuity_errors - cerr
                self.ledger.ts.content_gaps += after.content_gaps - gaps
                self.ledger.ts.content_lost += after.content_lost - lost
        if self.ledger is not None:
            self.ledger.bytes_out += len(chunk)
        self._record_bitrate(len(chunk))
        # One pass over the packets locates both the points a consumer can be
        # started from and the program tables in front of them.
        pats: list[int] = []
        raps: list[int] = []
        if self._ts_output:
            pats, raps = self._start_points.scan(chunk)
        self._push_prebuffer(chunk, pats, raps)
        await self._deliver(chunk, self._clean_start(pats, raps))

    @staticmethod
    def _clean_start(pats: list[int], raps: list[int]) -> Optional[int]:
        """Offset in a chunk a consumer may be started at, if there is one.

        The keyframe itself is the point that matters, but a demuxer starting
        there has no program tables yet, so this rewinds to the last PAT in
        front of it when the chunk carries one. That keeps the handover a
        contiguous run of bytes, which the continuity counters depend on.
        """
        if not raps:
            return None
        rap = raps[0]
        earlier = [offset for offset in pats if offset <= rap]
        return earlier[-1] if earlier else rap

    async def _deliver(self, chunk: bytes, start: Optional[int]) -> None:
        subscribers = list(self._subscribers)
        if not subscribers:
            return
        wait = self.settings.backpressure_seconds
        if wait <= 0:
            for sub in subscribers:
                portion = self._for_subscriber(sub, chunk, start)
                if portion:
                    self._put_counting(sub, portion)
            return
        deadline = asyncio.get_running_loop().time() + wait
        await asyncio.gather(
            *(self._deliver_one(sub, chunk, start, deadline) for sub in subscribers)
        )

    @staticmethod
    def _for_subscriber(sub: Subscriber, chunk: bytes, start: Optional[int]) -> bytes:
        """What of this chunk this consumer should get, which may be none of it.

        A consumer waiting to be started on a keyframe gets nothing until one
        arrives: handing it the bytes in between would put it back where it
        began, with audio it can play and video it cannot.
        """
        if not sub.needs_keyframe:
            return chunk
        if start is None:
            return b""
        sub.needs_keyframe = False
        return chunk[start:]

    async def _deliver_one(
        self, sub: Subscriber, chunk: bytes, start: Optional[int], deadline: float
    ) -> None:
        chunk = self._for_subscriber(sub, chunk, start)
        if not chunk:
            # Nothing to hand over, so nothing to wait for. Holding the source
            # up for room in a queue we are not about to write to would stall
            # every other viewer behind a consumer that is only marking time.
            return
        loop = asyncio.get_running_loop()
        while sub.queue.full():
            # Give up waiting on a consumer that has gone away, otherwise a
            # client disconnecting on a full queue would stall the whole source.
            if sub.closed or sub not in self._subscribers:
                return
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.02)
        self._put_counting(sub, chunk)

    def _put_counting(self, sub: Subscriber, chunk: bytes) -> None:
        before = sub.dropped
        sub.put(chunk)
        if sub.dropped != before:
            self.dropped_total += sub.dropped - before
            sub.dropped = before
            if self.ledger is not None:
                self.ledger.dropped_chunks += 1
            # Restarting it on a keyframe was tried here and is not worth it:
            # the hole is already inside the queue, in front of data the
            # consumer is about to read, so the macroblocking happens either
            # way and skipping to the next keyframe only throws more away. A
            # consumer that reaches this point wants a transcode profile, not
            # better handling of the bytes it cannot carry.

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
        """Take down a pipeline and everything it spawned.

        The group is signalled even when the direct child has already exited by
        itself. Source commands fork helpers - streamlink spawning ffmpeg, a
        shell pipeline, a wrapper script - and those helpers routinely outlive
        their parent while still holding the upstream connection open. Reopening
        the source then makes a *second* connection to a provider that only
        expects one, which it answers with a reset.
        """
        grace = self.settings.terminate_grace_seconds
        for proc in procs:
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

        # Whatever is still standing in the group is an orphaned helper.
        await self._reap_group(procs, grace)

    async def _reap_group(
        self, procs: list[asyncio.subprocess.Process], grace: float
    ) -> None:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not any(self._group_alive(p) for p in procs):
                return
            await asyncio.sleep(0.2)

        for proc in procs:
            if self._group_alive(proc):
                log.info(
                    "source %s left helper processes behind; killing group %s",
                    self.key, proc.pid,
                )
                self._log("--- killed leftover child processes ---")
                self._signal_group(proc, signal.SIGKILL)

    @staticmethod
    def _group_alive(proc: asyncio.subprocess.Process) -> bool:
        try:
            os.killpg(proc.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        """Signal the whole process group.

        Spawning with start_new_session=True makes the child a session and group
        leader, so its pid *is* the process group id. Using that directly rather
        than os.getpgid() matters: getpgid fails once the child has been reaped,
        which is precisely when its orphaned helpers still need killing.
        """
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError, ValueError):
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
                        self._note(text)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.debug("stderr reader for %s ended", self.key, exc_info=True)

        self._helper_tasks.append(asyncio.create_task(drain()))

    def _note(self, text: str) -> None:
        """Count what the source tool reports, and note how fast it is running.

        The transport-level counters are blind to most real faults, because a
        source that re-muxes writes a clean transport layer around damaged
        content. Its own log is where the evidence is, so it is tallied here and
        published, which is what makes a bad source visible on a dashboard
        rather than only in a log nobody is reading.
        """
        kind = classify_log(text)
        if kind is not None:
            self.events[kind] = self.events.get(kind, 0) + 1
            if self.ledger is not None:
                self.ledger.events[kind] = self.ledger.events.get(kind, 0) + 1
            return
        # ffmpeg's progress line carries how far ahead of real time it is.
        # Below 1.0 sustained means the source cannot keep up and every viewer
        # will eventually starve, which nothing else here would reveal.
        found = _SPEED_RE.search(text)
        if found:
            try:
                self.speed = float(found.group(1))
            except ValueError:
                pass

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

    def _push_prebuffer(self, chunk: bytes, pats: list[int], raps: list[int]) -> None:
        limit = self.settings.prebuffer_bytes
        if limit <= 0:
            if self._prebuffer:
                self._prebuffer.clear()
                self._prebuffer_raps.clear()
                self._prebuffer_pats.clear()
            return
        base = len(self._prebuffer)
        self._prebuffer += chunk
        if self._ts_output:
            self._prebuffer_raps += [base + offset for offset in raps]
            self._prebuffer_pats += [base + offset for offset in pats]
        self._trim_prebuffer(limit)

    def _trim_prebuffer(self, limit: int) -> None:
        """Discard the oldest backlog, in whole GOPs where the stream allows it.

        Trimming to a byte count leaves the buffer starting partway through a
        GOP, which is exactly the handover that puts a joining viewer's audio
        ahead of its picture. Cutting at a keyframe instead means whatever is
        left can always be replayed as-is.
        """
        if len(self._prebuffer) <= limit:
            return

        if self._ts_output and self._prebuffer_raps:
            # The oldest start point that brings the buffer back under the
            # limit; failing that, the newest one, even though keeping it
            # overshoots - a backlog nobody can start from is worth less than
            # a slightly large one.
            keep = next(
                (i for i, r in enumerate(self._prebuffer_raps)
                 if len(self._prebuffer) - self._start_of(r) <= limit),
                len(self._prebuffer_raps) - 1,
            )
            cut = self._start_of(self._prebuffer_raps[keep])
            # A single GOP larger than several times the limit is not a GOP, it
            # is a stream whose keyframes are too far apart to buffer. Fall
            # through to the byte trim rather than hoarding it.
            if cut or len(self._prebuffer) <= limit * PREBUFFER_GOP_SLACK:
                if cut:
                    self._discard_prebuffer(cut)
                return

        slab = max(len(self._prebuffer) - limit, limit // 4)
        slab = min(slab, len(self._prebuffer))
        if self._ts_output:
            slab -= slab % TS_PACKET
        if slab > 0:
            self._discard_prebuffer(slab)

    def _start_of(self, rap: int) -> int:
        """Where a consumer starting at ``rap`` must actually begin reading."""
        i = bisect_right(self._prebuffer_pats, rap)
        return self._prebuffer_pats[i - 1] if i else rap

    def _discard_prebuffer(self, cut: int) -> None:
        del self._prebuffer[:cut]
        raps = self._prebuffer_raps
        pats = self._prebuffer_pats
        self._prebuffer_raps = [r - cut for r in raps[bisect_left(raps, cut):]]
        self._prebuffer_pats = [p - cut for p in pats[bisect_left(pats, cut):]]

    def _replay_bytes(self) -> bytes:
        """The backlog handed to a joining consumer.

        For MPEG-TS this starts at a video keyframe, with the program tables in
        front of it, so the consumer has a picture from its first frame. Starting
        it anywhere else hands it audio it can play and video it cannot, and the
        gap between the two is where it stays for the rest of the session.
        """
        if not self._prebuffer:
            return b""
        if not self._ts_output:
            return bytes(self._prebuffer)
        if self._prebuffer_raps:
            return bytes(self._prebuffer[self._start_of(self._prebuffer_raps[0]):])
        if self._start_points.marks_raps:
            # This stream does flag its keyframes, there just is not one in the
            # backlog. The caller waits for the next rather than replaying a
            # partial GOP.
            return b""
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
            dropped_chunks=self.dropped_total + sum(s.dropped for s in self._subscribers),
            input_dropped=0,
            ts_packets=c.packets,
            ts_transport_errors=c.transport_errors,
            ts_continuity_errors=c.continuity_errors,
            ts_scrambled=c.scrambled,
            ts_discontinuities=c.discontinuities,
            ts_content_gaps=c.content_gaps,
            ts_content_lost=round(c.content_lost, 3),
            ts_error_pids=self.analyser.worst_pids(),
            events=dict(self.events),
            speed=self.speed,
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
        # Sources differ in whether their provider's audio timestamps can be
        # trusted, and failing over swaps one for another, so this is decided
        # per attempt rather than once per session.
        if source.fix_audio_timing and self._audio_clock is None:
            self._audio_clock = TSAudioClock()
            self._log("--- rebuilding audio timestamps from the stream clock ---")
        elif not source.fix_audio_timing:
            self._audio_clock = None
        elif self._audio_clock is not None:
            self._audio_clock.restart()
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
            await self.publish(data)


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
                await self.publish(data)
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
