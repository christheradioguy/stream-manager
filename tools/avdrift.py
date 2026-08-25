#!/usr/bin/env python3
"""Measure whether audio and video drift apart in an MPEG-TS stream.

Point it at a stream and it reports, in slices, how much audio and how much
video actually arrived. In a healthy stream the two accumulate at the same rate.
If one falls behind the other, that difference is the lip sync error, and the
slice table shows whether it is growing.

  tools/avdrift.py http://localhost:8409/stream/msnbc 120
  tools/avdrift.py --command 'ffmpeg -i http://provider/x.m3u8 -c copy -f mpegts pipe:1' 120

Run it both ways on the same channel: through the manager, and against the
source command on its own. If only the first drifts, the manager is doing it. If
both drift, the stream is arriving that way and the manager is passing it on.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
import urllib.request

TS_PACKET = 188
NULL_PID = 0x1FFF
VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24, 0x33, 0x42, 0xD1, 0xEA}
AUDIO_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x1C, 0x81, 0x87}


def capture_url(url: str, seconds: float) -> bytes:
    out, deadline = [], time.time() + seconds
    req = urllib.request.Request(url, headers={"User-Agent": "avdrift"})
    with urllib.request.urlopen(req, timeout=seconds + 30) as resp:
        while time.time() < deadline:
            chunk = resp.read(65536)
            if not chunk:
                break
            out.append(chunk)
    return b"".join(out)


def capture_command(command: str, seconds: float) -> bytes:
    proc = subprocess.Popen(shlex.split(command), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    out, deadline = [], time.time() + seconds
    try:
        while time.time() < deadline:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            out.append(chunk)
    finally:
        proc.kill()
        proc.wait()
    return b"".join(out)


def sync_offset(data: bytes) -> int:
    for off in range(TS_PACKET):
        if all(data[off + i * TS_PACKET] == 0x47 for i in range(20)):
            return off
    return -1


def read_pat(data: bytes, offset: int) -> set[int]:
    """Every program's PMT PID. A transport stream may carry several."""
    b3 = data[offset + 3]
    if not b3 & 0x10:
        return set()
    s = offset + 4 + (1 + data[offset + 4] if b3 & 0x20 else 0)
    if s >= offset + TS_PACKET:
        return set()
    s += 1 + data[s]
    if s + 8 > offset + TS_PACKET or data[s] != 0x00:
        return set()
    length = ((data[s + 1] & 0x0F) << 8) | data[s + 2]
    end = min(s + 3 + length - 4, offset + TS_PACKET)
    pmts = set()
    i = s + 8
    while i + 4 <= end:
        if (data[i] << 8) | data[i + 1]:          # 0 is the network PID
            pmts.add(((data[i + 2] & 0x1F) << 8) | data[i + 3])
        i += 4
    return pmts


def read_pmt(data: bytes, offset: int) -> dict[int, str]:
    """Elementary PIDs and their kind, from a PMT that fits in one packet."""
    b3 = data[offset + 3]
    if not b3 & 0x10:
        return {}
    s = offset + 4 + (1 + data[offset + 4] if b3 & 0x20 else 0)
    if s >= offset + TS_PACKET:
        return {}
    s += 1 + data[s]
    if s + 12 > offset + TS_PACKET or data[s] != 0x02:
        return {}
    length = ((data[s + 1] & 0x0F) << 8) | data[s + 2]
    end = min(s + 3 + length - 4, offset + TS_PACKET)
    i = s + 12 + (((data[s + 10] & 0x0F) << 8) | data[s + 11])
    found = {}
    while i + 5 <= end:
        kind = ("video" if data[i] in VIDEO_TYPES
                else "audio" if data[i] in AUDIO_TYPES else None)
        if kind:
            found[((data[i + 1] & 0x1F) << 8) | data[i + 2]] = kind
        i += 5 + (((data[i + 3] & 0x0F) << 8) | data[i + 4])
    return found


def pes_pts(data: bytes, offset: int):
    b3 = data[offset + 3]
    if not b3 & 0x10 or not data[offset + 1] & 0x40 or b3 & 0xC0:
        return None
    at = offset + 4 + (1 + data[offset + 4] if b3 & 0x20 else 0)
    if at + 14 > offset + TS_PACKET or data[at:at + 3] != b"\x00\x00\x01":
        return None
    if not data[at + 7] & 0x80:
        return None
    b = data[at + 9:at + 14]
    return ((((b[0] >> 1) & 7) << 30) | (b[1] << 22) | (((b[2] >> 1) & 0x7F) << 15)
            | (b[3] << 7) | (b[4] >> 1)) / 90000.0


def analyse(data: bytes, slices: int = 10) -> tuple[int, dict]:
    off = sync_offset(data)
    if off < 0:
        print("  not an MPEG-TS stream (no sync found)")
        return 1, {}
    if off:
        print(f"  note: stream begins {off} bytes into a packet")
        data = data[off:]

    pmt_pids: set[int] = set()
    kinds: dict[int, str] = {}
    marks: list[tuple[int, int, float]] = []      # byte offset, pid, pts
    counts: dict[int, int] = {}

    for o in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        if data[o] != 0x47:
            continue
        pid = ((data[o + 1] & 0x1F) << 8) | data[o + 2]
        if pid == NULL_PID:
            continue
        counts[pid] = counts.get(pid, 0) + 1
        if pid == 0 and data[o + 1] & 0x40:
            pmt_pids |= read_pat(data, o)
            continue
        if pid in pmt_pids and data[o + 1] & 0x40:
            kinds.update(read_pmt(data, o))
            continue
        if pid not in kinds:
            continue
        pts = pes_pts(data, o)
        if pts is not None:
            marks.append((o, pid, pts))

    if not kinds:
        print("  no PMT found, so audio and video cannot be told apart")
        return 1, {}

    # Per PID, because a transport stream often carries more than one audio
    # track and the spare ones - a second language, a description track - can be
    # sparse or barely there. Measuring one of those against the video and
    # calling the difference drift is exactly the wrong answer, so show the work.
    print(f"  {len(data)/1e6:.1f} MB, {len(pmt_pids)} program(s)")
    print(f"    {'pid':>6}  {'kind':5}  {'packets':>9}  {'PES':>7}  {'timestamps span':>16}")
    best: dict[str, int] = {}
    for pid in sorted(kinds):
        kind = kinds[pid]
        stamps = [t for _, p, t in marks if p == pid]
        span = (max(stamps) - min(stamps)) if len(stamps) > 1 else 0.0
        print(f"    {pid:6d}  {kind:5}  {counts.get(pid, 0):9d}  {len(stamps):7d}  {span:15.1f}s")
        if counts.get(pid, 0) > counts.get(best.get(kind, -1), 0):
            best[kind] = pid
    if "audio" not in best or "video" not in best:
        print("  need both an audio and a video stream to measure drift")
        return 1, {}
    for kind in ("video", "audio"):
        if sum(1 for k in kinds.values() if k == kind) > 1:
            print(f"    measuring {kind} on pid {best[kind]}, the one carrying most data")

    vpid, apid = best["video"], best["audio"]
    marks = [(o, "video" if p == vpid else "audio", t)
             for o, p, t in marks if p in (vpid, apid)]

    rewinds = {"video": 0, "audio": 0}
    last: dict[str, float] = {}
    for _, kind, pts in marks:
        if kind in last and pts < last[kind] - 0.5:
            rewinds[kind] += 1
        last[kind] = pts

    # A stream's own frame spacing sets what counts as a hole rather than a
    # normal gap, so this works whatever the rates are.
    steps: dict[str, list[float]] = {"video": [], "audio": []}
    for kind in ("video", "audio"):
        times = sorted(p for _, k, p in marks if k == kind)
        steps[kind] = [b - a for a, b in zip(times, times[1:])]
    limit = {}
    for kind in ("video", "audio"):
        good = sorted(d for d in steps[kind] if d > 0)
        if not good:
            print(f"  no usable {kind} timestamps")
            return 1, {}
        limit[kind] = 5 * good[len(good) // 2]

    edges = [len(data) * i // slices for i in range(slices + 1)]
    rows, total = [], {"video": 0.0, "audio": 0.0}
    for i in range(slices):
        lo, hi = edges[i], edges[i + 1]
        span = {"video": 0.0, "audio": 0.0}
        for kind in ("video", "audio"):
            # Sorted, because timestamps arrive in decode order: anything with
            # B-frames reorders them, and summing differences as they come
            # inflates the total by more than half on a typical broadcast
            # stream. Sorting first makes this the span minus the holes, which
            # is what it is supposed to be.
            times = sorted(p for o, k, p in marks if k == kind and lo <= o < hi)
            span[kind] = sum(b - a for a, b in zip(times, times[1:])
                             if 0 < b - a < limit[kind])
        for k in span:
            total[k] += span[k]
        rows.append((lo, span, total["audio"] - total["video"]))

    # Report against the first slice: audio and video rarely begin on the same
    # frame, and that fixed head start is not drift.
    base = rows[0][2] if rows else 0.0
    print(f"\n  {'slice':>16}  {'video':>9}  {'audio':>9}  {'a-v':>8}  {'drift so far':>13}")
    for lo, span, cum in rows:
        print(f"  {lo/1e6:12.1f} MB  {span['video']:8.2f}s  {span['audio']:8.2f}s  "
              f"{span['audio']-span['video']:+7.2f}s  {cum-base:+12.2f}s")

    gap = rows[-1][2] - base if rows else 0.0
    seconds = max(total["video"], total["audio"])
    print(f"\n  video content delivered: {total['video']:.2f}s  (pid {vpid})")
    print(f"  audio content delivered: {total['audio']:.2f}s  (pid {apid})")
    print(f"  DRIFT over {seconds:.0f}s of stream: {gap:+.2f}s ", end="")
    if abs(gap) < 0.20:
        print("- audio and video kept pace")
    elif gap > 0:
        print("- video content is going missing, audio ends up this far ahead")
    else:
        print("- audio content is going missing, video ends up this far ahead")
    if seconds > 0 and abs(gap) >= 0.20:
        print(f"  that is {gap / seconds * 3600:+.0f}s per hour at this rate")
    if rewinds["video"] or rewinds["audio"]:
        print(f"  timeline rewinds: video={rewinds['video']} audio={rewinds['audio']}"
              "  (the clock jumps backwards mid-stream)")
    return 0, {"video": total["video"], "audio": total["audio"], "drift": gap}


def decoded_report(path: str, measured: dict) -> None:
    """Ask a decoder what it gets, and check that against what was measured.

    The timestamp arithmetic above can be fooled - by a stream carrying several
    programs, by a sparse second audio track, by a codec whose framing it
    misreads. A decoder is fooled by none of those, so if the two disagree it is
    the arithmetic that is wrong, and saying so is more use than a confident
    wrong number.
    """
    def probe(selector: str, *fields: str) -> dict[str, str]:
        """One stream's fields, by name. csv output orders them its own way."""
        out = subprocess.run(
            ["ffprobe", "-hide_banner", "-v", "error", "-count_frames",
             "-select_streams", selector, "-show_entries",
             "stream=" + ",".join(fields), "-of",
             "default=noprint_wrappers=1", path],
            capture_output=True, text=True).stdout
        found: dict[str, str] = {}
        for line in out.splitlines():
            key, _, value = line.partition("=")
            if key and key not in found:      # first stream only
                found[key] = value
        return found

    vid = probe("V:0", "codec_name", "nb_read_frames", "avg_frame_rate")
    aud = probe("a:0", "codec_name", "nb_read_frames", "sample_rate")
    decode = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-i", path,
                             "-f", "null", "-"], capture_output=True, text=True)
    errors = [l for l in decode.stderr.splitlines() if l.strip()]

    print("\n  what a decoder makes of it:")
    print(f"    video: {vid.get('codec_name', '?')} "
          f"{vid.get('avg_frame_rate', '?')} fps, {vid.get('nb_read_frames', '?')} frames")
    print(f"    audio: {aud.get('codec_name', '?')} "
          f"{aud.get('sample_rate', '?')} Hz, {aud.get('nb_read_frames', '?')} frames")
    print(f"    decoder complaints: {len(errors)}")
    for line in errors[:5]:
        print(f"      {line}")

    decoded: dict[str, float] = {}
    try:
        num, _, den = vid["avg_frame_rate"].partition("/")
        fps = float(num) / float(den or 1)
        if fps > 0:
            decoded["video"] = int(vid["nb_read_frames"]) / fps
    except (KeyError, ValueError, ZeroDivisionError):
        pass
    try:
        per_frame = 1536 if aud.get("codec_name") in ("ac3", "eac3") else 1024
        decoded["audio"] = int(aud["nb_read_frames"]) * per_frame / float(aud["sample_rate"])
    except (KeyError, ValueError, ZeroDivisionError):
        pass
    for kind, seconds in decoded.items():
        mine = measured.get(kind)
        note = ""
        if mine and abs(mine - seconds) > max(2.0, 0.1 * seconds):
            note = f"   <-- DISAGREES with the {mine:.1f}s measured above"
        print(f"    decoded {kind}: {seconds:.1f}s{note}")
    if len(decoded) == 2:
        gap = decoded["audio"] - decoded["video"]
        print(f"    decoder's own audio-video difference: {gap:+.1f}s")
    if any(measured.get(k) and abs(measured[k] - v) > max(2.0, 0.1 * v)
           for k, v in decoded.items()):
        print("\n  The timestamp measurement and the decoder disagree, so trust the\n"
              "  decoder and treat the drift figure above as unreliable for this\n"
              "  stream - most likely the wrong track was measured.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="stream URL, or a .ts file, or - with --command")
    ap.add_argument("seconds", nargs="?", type=float, default=120.0)
    ap.add_argument("--command", action="store_true",
                    help="treat the target as a source command to run, not a URL")
    ap.add_argument("--slices", type=int, default=10)
    ap.add_argument("--keep", metavar="FILE",
                    help="write the capture here and ask a decoder about it too")
    args = ap.parse_args()

    print(f"capturing {args.seconds:.0f}s ...", flush=True)
    if args.command:
        data = capture_command(args.target, args.seconds)
    elif args.target.startswith(("http://", "https://")):
        data = capture_url(args.target, args.seconds)
    else:
        data = open(args.target, "rb").read()
    if len(data) < 100 * TS_PACKET:
        print(f"  only got {len(data)} bytes - nothing to measure")
        return 1
    rc, measured = analyse(data, args.slices)
    if args.keep:
        with open(args.keep, "wb") as fh:
            fh.write(data)
        print(f"\n  capture written to {args.keep}")
        decoded_report(args.keep, measured)
    return rc


if __name__ == "__main__":
    sys.exit(main())
