#!/usr/bin/env python3
"""Check the clock a strict player follows.

Desktop players decode by presentation timestamp and largely ignore the
program clock reference. Set-top and Android players - TiviMate, and anything
else built on ExoPlayer - pace playback from it instead. So a clock that stalls,
jitters, or sits in the wrong place relative to the presentation stamps plays
perfectly on a computer and badly on a television, which makes it easy to blame
the television.

  tools/tsclock.py /tmp/capture.ts
  tools/tsclock.py direct.ts via-manager.ts        # compare the two

What it looks for:

  * intervals over 100 ms - the longest a decoder should ever wait for the clock
  * the clock going backwards, which no player handles gracefully
  * presentation stamps behind the clock, which arrive too late to show
  * presentation stamps far ahead of it, which make a player sit and wait

Capture through this server and from the source on its own, and compare. The
source is the reference: anything this server adds is its own fault.
"""
import sys
P = 188
HZ = 90000.0

def walk(path):
    data = open(path, "rb").read()
    pcrs, ptss, counts = [], [], {}
    for o in range(0, len(data) - P + 1, P):
        if data[o] != 0x47:
            continue
        pid = ((data[o+1] & 0x1F) << 8) | data[o+2]
        if pid == 0x1FFF:
            continue
        counts[pid] = counts.get(pid, 0) + 1
        b3 = data[o+3]
        if b3 & 0x20 and data[o+4] >= 7 and data[o+5] & 0x10:
            a = o + 6
            base = (data[a] << 25 | data[a+1] << 17 | data[a+2] << 9
                    | data[a+3] << 1 | data[a+4] >> 7)
            ext = ((data[a+4] & 0x01) << 8) | data[a+5]
            pcrs.append((o, pid, base / HZ + ext / 27000000.0))
        if b3 & 0x10 and data[o+1] & 0x40 and not b3 & 0xC0:
            s = o + 4 + (1 + data[o+4] if b3 & 0x20 else 0)
            if s + 14 <= o + P and data[s:s+3] == b"\x00\x00\x01" and data[s+7] & 0x80:
                b = data[s+9:s+14]
                pts = ((((b[0] >> 1) & 7) << 30) | (b[1] << 22)
                       | (((b[2] >> 1) & 0x7F) << 15) | (b[3] << 7) | (b[4] >> 1)) / HZ
                ptss.append((o, pid, pts))
    return data, pcrs, ptss, counts

for path in sys.argv[1:]:
    data, pcrs, ptss, counts = walk(path)
    print(f"\n== {path.split('/')[-1]}  {len(data)/1e6:.1f} MB")
    if not pcrs:
        print("   NO PCR AT ALL - a player with no clock to follow")
        continue
    pcr_pid = max({p for _, p, _ in pcrs},
                  key=lambda q: sum(1 for _, p, _ in pcrs if p == q))
    series = [(o, t) for o, p, t in pcrs if p == pcr_pid]
    print(f"   PCR on pid {pcr_pid}: {len(series)} samples")

    back = [(a, b) for (_, a), (_, b) in zip(series, series[1:]) if b < a]
    gaps = [b - a for (_, a), (_, b) in zip(series, series[1:]) if b >= a]
    if gaps:
        big = [g for g in gaps if g > 0.1]
        print(f"   interval: median {sorted(gaps)[len(gaps)//2]*1000:.0f} ms, "
              f"max {max(gaps)*1000:.0f} ms, {len(big)} over the 100 ms limit")
    print(f"   PCR goes backwards: {len(back)} time(s)"
          + (f"  first {back[0][0]:.3f} -> {back[0][1]:.3f}" if back else ""))

    span = series[-1][1] - series[0][1]
    print(f"   PCR spans {span:.1f}s across the capture")

    # Where the timestamps sit relative to the clock. A frame whose PTS is
    # behind the clock is already late; one absurdly ahead makes a player that
    # honours the clock sit and wait, which looks like slow playback.
    j = 0
    deltas = []
    for o, pid, pts in ptss:
        while j + 1 < len(series) and series[j+1][0] <= o:
            j += 1
        deltas.append(pts - series[j][1])
    if deltas:
        deltas.sort()
        late = sum(1 for d in deltas if d < 0)
        far = sum(1 for d in deltas if d > 3.0)
        print(f"   PTS minus PCR: min {deltas[0]:+.3f}s  median "
              f"{deltas[len(deltas)//2]:+.3f}s  max {deltas[-1]:+.3f}s")
        print(f"     {late} of {len(deltas)} stamps arrive already late (PTS behind PCR)")
        print(f"     {far} sit more than 3s ahead of the clock")
        if late > len(deltas) * 0.02 or far > len(deltas) * 0.02:
            print("     ^ a player that paces from PCR will struggle with this")
