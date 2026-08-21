#!/usr/bin/env python3
"""End-to-end smoke test.

Starts a real server against a throwaway config, drives it over HTTP with a
synthetic ffmpeg source, and checks the parts that are easy to get wrong:
MPEG-TS passthrough, transcoding on request, session fan-out, and teardown.

    python tests/smoke_test.py

Requires ffmpeg/ffprobe on PATH.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable

# A self-contained MPEG-TS source: colour bars + a tone, paced in real time.
TEST_SOURCE = (
    "ffmpeg -hide_banner -loglevel error -re "
    "-f lavfi -i testsrc2=size=640x360:rate=25 "
    "-f lavfi -i sine=frequency=440:sample_rate=48000 "
    "-c:v libx264 -preset ultrafast -tune zerolatency -b:v 1500k -g 25 "
    "-c:a aac -b:a 96k -shortest -t 120 -f mpegts pipe:1"
)

# A tiny XMLTV generator standing in for the user's guide-assembly script.
# Times are computed at run time so the "future" programmes stay in range.
XMLTV_SCRIPT = r"""#!/bin/sh
NOW=$(date -u +%s)
fmt() { date -u -d "@$1" +%Y%m%d%H%M%S; }
cat <<XML
<?xml version="1.0" encoding="UTF-8"?>
<tv generator-info-name="test">
  <channel id="news24.example">
    <display-name>News 24</display-name>
    <icon src="http://upstream/news.png" />
  </channel>
  <channel id="movies.example">
    <display-name>Movie Channel</display-name>
  </channel>
  <channel id="sports.example">
    <display-name>Sports Extra</display-name>
  </channel>
  <programme channel="news24.example" start="$(fmt $((NOW - 600))) +0000" stop="$(fmt $((NOW + 3000))) +0000">
    <title>Evening News</title>
    <desc>Headlines &amp; weather</desc>
    <category>News</category>
  </programme>
  <programme channel="news24.example" start="$(fmt $((NOW - 864000))) +0000" stop="$(fmt $((NOW - 860400))) +0000">
    <title>Ancient History</title>
  </programme>
  <programme channel="movies.example" start="$(fmt $((NOW - 300))) +0000" stop="$(fmt $((NOW + 7000))) +0000">
    <title>A Fine Film</title>
  </programme>
  <programme channel="sports.example" start="$(fmt $((NOW - 300))) +0000" stop="$(fmt $((NOW + 5000))) +0000">
    <title>The Big Match</title>
  </programme>
</tv>
XML
"""

PASSED: list[str] = []
FAILED: list[str] = []


# Emits MPEG-TS with deliberate faults: every 50th packet skips a continuity
# counter, and every 97th sets the transport_error_indicator.
CORRUPT_TS_SOURCE = r'''
import os, sys, time
PID = 0x0100
cc = 0
n = 0
buf = bytearray()
while n < 20000:
    n += 1
    pkt = bytearray(188)
    pkt[0] = 0x47
    tei = 0x80 if n % 97 == 0 else 0
    pkt[1] = ((PID >> 8) & 0x1F) | tei
    pkt[2] = PID & 0xFF
    if n % 50 == 0:
        cc = (cc + 2) & 0x0F        # skip one -> continuity error
    else:
        cc = (cc + 1) & 0x0F
    pkt[3] = 0x10 | cc              # payload only
    buf += pkt
    if len(buf) >= 188 * 100:
        sys.stdout.buffer.write(buf)
        sys.stdout.buffer.flush()
        buf.clear()
        time.sleep(0.01)
'''


def metrics_parse(body: str) -> dict[str, list[tuple[str, float]]]:
    """Parse Prometheus exposition into {name: [(labels, value), ...]}."""
    out: dict[str, list[tuple[str, float]]] = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})?\s+(-?[\d.eE+]+)$", line)
        if not match:
            raise AssertionError(f"unparseable metric line: {line!r}")
        name, labels, value = match.groups()
        out.setdefault(name, []).append((labels or "", float(value)))
    return out


def run_analyser_checks() -> None:
    """Unit-level checks on the TS analyser, where faults can be exact."""
    sys.path.insert(0, str(ROOT))
    from app.tsstats import TSAnalyser

    def pkt(pid, cc, tei=0, afc=0x01, disc=False):
        b = bytearray(188)
        b[0] = 0x47
        b[1] = ((pid >> 8) & 0x1F) | (0x80 if tei else 0)
        b[2] = pid & 0xFF
        b[3] = (afc << 4) | (cc & 0x0F)
        if afc & 0x02:
            b[4] = 1
            b[5] = 0x80 if disc else 0
        return bytes(b)

    a = TSAnalyser()
    a.feed(b"".join(pkt(256, c) for c in range(6)))
    check("analyser: clean sequence has no errors",
          a.counters.continuity_errors == 0 and a.counters.packets == 6)

    a.feed(pkt(256, 7))  # skipped 6
    check("analyser: a skipped counter is one error", a.counters.continuity_errors == 1)

    a.feed(pkt(256, 7))
    check("analyser: one duplicate packet is legal", a.counters.continuity_errors == 1)
    a.feed(pkt(256, 7))
    check("analyser: a second duplicate is an error", a.counters.continuity_errors == 2)

    a.feed(pkt(256, 8, tei=1))
    check("analyser: transport_error_indicator is counted",
          a.counters.transport_errors == 1)

    a.feed(pkt(0x1FFF, 0))
    check("analyser: null packets are ignored, not errors",
          a.counters.nulls == 1 and a.counters.continuity_errors == 2)

    b = TSAnalyser()
    b.feed(b"".join(pkt(100, c) for c in range(3)))
    b.feed(pkt(100, 9, afc=0x03, disc=True))
    b.feed(pkt(100, 10))
    check("analyser: a signalled discontinuity is not an error",
          b.counters.discontinuities == 1 and b.counters.continuity_errors == 0)

    c = TSAnalyser()
    c.feed(b"".join(pkt(200, c2, afc=0x02) for c2 in (0, 0, 0)))
    check("analyser: packets without payload do not advance the counter",
          c.counters.continuity_errors == 0)

    d = TSAnalyser()
    d.feed(b"".join(pkt(300, 0) for _ in range(2)) + b"".join(pkt(400, 0) for _ in range(2)))
    check("analyser: PIDs are tracked independently", d.counters.continuity_errors == 0)

    off = TSAnalyser(enabled=False)
    off.feed(b"".join(pkt(256, 0) for _ in range(4)))
    check("analyser: can be disabled", off.counters.packets == 0)


def ts_continuity_errors(data: bytes) -> int:
    """Continuity errors in a captured stream, via the server's own analyser."""
    sys.path.insert(0, str(ROOT))
    from app.tsstats import TSAnalyser

    analyser = TSAnalyser()
    analyser.feed(data)
    return analyser.counters.continuity_errors


def dup_status_409(status: int, body) -> bool:
    return status == 409 and "already running" in str(body)


def is_valid_xml(text: str) -> bool:
    try:
        ET.fromstring(text)
        return True
    except ET.ParseError:
        return False


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(name)
    mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  [{mark}] {name}{('  — ' + detail) if detail else ''}")
    return ok


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, base: str):
        self.base = base

    def request(self, method: str, path: str, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw.decode("utf-8", "replace")

    def text(self, path: str, timeout=30) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.base + path, timeout=timeout) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")


def read_stream(url: str, want_bytes: int, timeout: float = 45.0) -> bytes:
    """Pull up to want_bytes from a never-ending response."""
    buf = bytearray()
    deadline = time.time() + timeout
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            while len(buf) < want_bytes and time.time() < deadline:
                chunk = resp.read(16 * 1024)
                if not chunk:
                    break
                buf += chunk
    except Exception as exc:  # noqa: BLE001 - reported by the caller
        print(f"    (stream read ended: {exc})")
    return bytes(buf)


class CountingSource:
    """A source command that records every time it is executed.

    Lets the test assert how many upstream connections were opened, which is the
    thing that actually matters to a provider with a concurrent-stream limit.
    """

    def __init__(self, directory: Path):
        self.tally = directory / "connections.log"
        self.script = directory / "counted-source.sh"
        self.script.write_text(
            "#!/bin/sh\n"
            f'echo connect >> "{self.tally}"\n'
            "exec ffmpeg -hide_banner -loglevel error -re "
            "-f lavfi -i testsrc2=size=320x180:rate=15 "
            "-c:v libx264 -preset ultrafast -b:v 400k -g 15 -f mpegts pipe:1\n"
        )
        self.script.chmod(0o755)
        self.command = f"sh {self.script}"

    def start(self) -> None:
        self.tally.write_text("")

    def stop(self) -> None:
        pass

    @property
    def count(self) -> int:
        try:
            return len([ln for ln in self.tally.read_text().splitlines() if ln.strip()])
        except OSError:
            return 0


def stream_status(url: str, timeout: float = 15.0) -> tuple[int, str]:
    """Open a stream, note the status, and hang up. Never drains the body."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.read(4096)  # prove the body is actually flowing
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read()).get("detail", "")
        except Exception:  # noqa: BLE001
            return exc.code, ""
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def hold_stream(url: str, seconds: float) -> int:
    """Stay attached to a stream for a while and return the bytes received."""
    return len(hold_capture(url, seconds))


def hold_capture(url: str, seconds: float) -> bytes:
    """Stay attached to a stream for a while and keep what it sent."""
    out: list[bytes] = []
    deadline = time.time() + seconds
    try:
        with urllib.request.urlopen(url, timeout=seconds + 15) as resp:
            while time.time() < deadline:
                chunk = resp.read(16 * 1024)
                if not chunk:
                    break
                out.append(chunk)
    except Exception as exc:  # noqa: BLE001 - reported by the caller
        print(f"    (hold ended: {exc})")
    return b"".join(out)


TS_PACKET = 188


def sync_offset(data: bytes, needed: int = 20) -> int | None:
    """Byte offset where 188-spaced sync bytes begin. 0 means properly aligned."""
    if len(data) < needed * TS_PACKET:
        return None
    for offset in range(TS_PACKET):
        if all(data[offset + i * TS_PACKET] == 0x47 for i in range(needed)):
            return offset
    return None


def is_mpegts(data: bytes) -> bool:
    return sync_offset(data) is not None


def is_aligned(data: bytes) -> bool:
    """Every client must start on a packet boundary, not partway through one."""
    return sync_offset(data) == 0


def starts_at_pat(data: bytes) -> bool:
    """The first packet should be a PAT so the demuxer gets program tables at once."""
    if len(data) < TS_PACKET or data[0] != 0x47:
        return False
    return (((data[1] & 0x1F) << 8) | data[2]) == 0


def timeline_rewinds(data: bytes) -> list[tuple[float, float]]:
    """Points where the timestamps a client is given jump backwards.

    Reopening a source starts a new encoder run whose timestamps begin again
    from zero. Splicing that into a live viewer's stream rewinds its clock,
    which players resolve in their own contradictory ways - the common one being
    to carry the old clock forward and drift audio away from video, a little
    further on every reconnect.
    """
    video = busiest_pid(data)
    rewinds: list[tuple[float, float]] = []
    last = None
    for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        if data[offset] != 0x47:
            continue
        if (((data[offset + 1] & 0x1F) << 8) | data[offset + 2]) != video:
            continue
        pts = packet_pts(data, offset)
        if pts is None:
            continue
        if last is not None and pts < last - 0.5:
            rewinds.append((round(last, 3), round(pts, 3)))
        last = pts
    return rewinds


def track_separation(data: bytes) -> float | None:
    """How far the two busiest streams' timelines move apart over a capture.

    Both carry the same programme, so the gap between their timestamps should
    stay put. A gap that grows means one of them is being put on a different
    timeline from the other.
    """
    counts: dict[int, int] = {}
    for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        if data[offset] != 0x47:
            continue
        pid = ((data[offset + 1] & 0x1F) << 8) | data[offset + 2]
        if pid != 0x1FFF:
            counts[pid] = counts.get(pid, 0) + 1
    stamps: dict[int, list[tuple[int, float]]] = {}
    for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        if data[offset] != 0x47:
            continue
        pid = ((data[offset + 1] & 0x1F) << 8) | data[offset + 2]
        pts = packet_pts(data, offset)
        if pts is not None:
            stamps.setdefault(pid, []).append((offset, pts))
    ranked = [p for p in sorted(counts, key=counts.__getitem__, reverse=True)
              if len(stamps.get(p, ())) > 5][:2]
    if len(ranked) < 2:
        return None
    first, second = stamps[ranked[0]], stamps[ranked[1]]
    gaps, j = [], 0
    for offset, pts in first:
        while j + 1 < len(second) and second[j + 1][0] <= offset:
            j += 1
        gaps.append(second[j][1] - pts)
    return max(gaps) - min(gaps) if gaps else None


def packet_pts(data: bytes, offset: int) -> float | None:
    """The PTS in a TS packet that starts a PES, if it carries one."""
    byte3 = data[offset + 3]
    if not byte3 & 0x10 or not data[offset + 1] & 0x40:
        return None
    at = offset + 4 + (1 + data[offset + 4] if byte3 & 0x20 else 0)
    if at + 14 > offset + TS_PACKET or data[at:at + 3] != b"\x00\x00\x01":
        return None
    if not data[at + 7] & 0x80:
        return None
    b = data[at + 9:at + 14]
    ticks = ((((b[0] >> 1) & 0x07) << 30) | (b[1] << 22)
             | (((b[2] >> 1) & 0x7F) << 15) | (b[3] << 7) | (b[4] >> 1))
    return ticks / 90000.0


def busiest_pid(data: bytes) -> int:
    """The PID carrying most of the bytes, which in practice is the video."""
    counts: dict[int, int] = {}
    for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        pid = ((data[offset + 1] & 0x1F) << 8) | data[offset + 2]
        if pid != 0x1FFF:
            counts[pid] = counts.get(pid, 0) + 1
    return max(counts, key=counts.__getitem__) if counts else -1


def starts_on_keyframe(data: bytes) -> bool:
    """The first video packet must be one a decoder can start on.

    Handing a viewer the middle of a GOP gives it audio it can decode and video
    it cannot. Players that start their clock on the first thing they decode
    then run the sound ahead of the picture by however far into the GOP the
    handover happened, for the rest of the session.
    """
    video = busiest_pid(data)
    for offset in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        if data[offset] != 0x47:
            return False
        if (((data[offset + 1] & 0x1F) << 8) | data[offset + 2]) != video:
            continue
        return bool(
            data[offset + 1] & 0x40                 # starts a payload unit
            and data[offset + 3] & 0x20             # has an adaptation field
            and data[offset + 4]                    # which is not empty
            and data[offset + 5] & 0x40             # and flags random access
        )
    return False


def main() -> int:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"{tool} is required for this test")
            return 2

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    api = Client(base)
    tmpdir = Path(tempfile.mkdtemp(prefix="sm-test-"))
    config = tmpdir / "config.json"

    env = {
        **os.environ,
        "STREAMS_MANAGER_CONFIG": str(config),
        "STREAMS_MANAGER_LOGLEVEL": "WARNING",
    }
    server = subprocess.Popen(
        [PY, "run.py", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    server_output: list[str] = []

    def pump_output() -> None:
        assert server.stdout
        for line in server.stdout:
            server_output.append(line.rstrip())

    threading.Thread(target=pump_output, daemon=True).start()

    try:
        # ---- boot ------------------------------------------------------
        print("\nStarting server…")
        up = False
        for _ in range(100):
            if server.poll() is not None:
                break
            try:
                status, _ = api.request("GET", "/healthz", timeout=2)
                if status == 200:
                    up = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        if not check("server starts", up):
            print("\n".join(server_output[-30:]))
            return 1

        print("\nConfiguration")
        status, state = api.request("GET", "/api/state")
        check("GET /api/state", status == 200 and "settings" in state)
        check(
            "default profiles seeded",
            len(state["profiles"]) >= 3,
            f"{[p['id'] for p in state['profiles']]}",
        )

        # Keep the linger short so teardown is testable.
        settings = dict(state["settings"])
        settings["linger_seconds"] = 2
        settings["prebuffer_bytes"] = 512 * 1024
        status, _ = api.request("PUT", "/api/settings", settings)
        check("PUT /api/settings", status == 200)

        # ---- channel CRUD ----------------------------------------------
        print("\nChannel CRUD")
        channel = {
            "id": "testcard",
            "name": "Test Card",
            "command": TEST_SOURCE,
            "group": "Test",
            "channel_number": 101,
        }
        status, created = api.request("POST", "/api/channels", channel)
        check("POST /api/channels", status == 201 and created["id"] == "testcard")

        status, dup = api.request("POST", "/api/channels", channel)
        check("duplicate id rejected", status == 409, str(dup)[:60])

        status, bad = api.request(
            "POST", "/api/channels", {"id": "bad id!", "name": "x", "command": "true"}
        )
        check("invalid id rejected", status == 422)

        status, bad = api.request(
            "POST", "/api/channels", {"id": "unclosed", "name": "x", "command": "echo 'oops"}
        )
        check("unparseable command rejected", status == 422)

        # ---- playlist ---------------------------------------------------
        print("\nPlaylist")
        status, body = api.text("/playlist.m3u8")
        check("GET /playlist.m3u8", status == 200 and body.startswith("#EXTM3U"))
        check(
            "playlist lists the channel URL",
            f"{base}/stream/testcard" in body,
            body.strip().splitlines()[-1] if body.strip() else "",
        )
        check("playlist carries metadata", 'tvg-chno="101"' in body and 'group-title="Test"' in body)

        status, body = api.text("/playlist.m3u8?profile=480p")
        check("playlist bakes in ?profile", "/stream/testcard?profile=480p" in body)

        status, body = api.text("/playlist.m3u8?profile=nope")
        check("unknown profile rejected", status == 400)

        # ---- channel ordering ------------------------------------------------
        print("\nChannel ordering")
        # Deliberately created out of order, with a gap and two unnumbered.
        for cid, name, num in [("ord-c", "Charlie", 30), ("ord-a", "Alpha", 10),
                               ("ord-z", "Zulu", None), ("ord-b", "Bravo", 20),
                               ("ord-d", "Delta", None)]:
            body = {"id": cid, "name": name, "sources": [{"id": "s1", "command": TEST_SOURCE}]}
            if num is not None:
                body["channel_number"] = num
            api.request("POST", "/api/channels", body)

        def listed(path="/playlist.m3u8"):
            _, text = api.text(path)
            return [i for i in re.findall(r'tvg-id="(ord-[a-z])"', text)]

        def api_order():
            _, chans = api.request("GET", "/api/channels")
            return [c["id"] for c in chans if c["id"].startswith("ord-")]

        check("playlist is in channel-number order",
              listed()[:3] == ["ord-a", "ord-b", "ord-c"], str(listed()))
        check("unnumbered channels come last",
              set(listed()[3:]) == {"ord-z", "ord-d"}, str(listed()))
        check("the API (and so the GUI) uses the same order",
              api_order() == listed(), f"api={api_order()} playlist={listed()}")

        settings_now = api.request("GET", "/api/settings")[1]
        api.request("PUT", "/api/settings", {**settings_now, "channel_sort": "name"})
        check("name order sorts alphabetically",
              listed() == ["ord-a", "ord-b", "ord-c", "ord-d", "ord-z"], str(listed()))

        api.request("PUT", "/api/settings", {**settings_now, "channel_sort": "manual"})
        check("manual order keeps the configured order",
              listed() == ["ord-c", "ord-a", "ord-z", "ord-b", "ord-d"], str(listed()))
        api.request("PUT", "/api/settings", {**settings_now, "channel_sort": "number"})

        # Renumbering must move a channel without touching anything else.
        ch = next(c for c in api.request("GET", "/api/channels")[1] if c["id"] == "ord-c")
        api.request("PUT", "/api/channels/ord-c", {**ch, "channel_number": 5})
        check("changing a number reorders immediately",
              listed()[0] == "ord-c", str(listed()))

        for cid in ("ord-a", "ord-b", "ord-c", "ord-d", "ord-z"):
            api.request("DELETE", f"/api/channels/{cid}")

        # ---- multiple groups ------------------------------------------------
        print("\nMultiple groups")
        api.request("POST", "/api/channels", {
            "id": "multi", "name": "Multi", "groups": ["News", "UK", "News", " "],
            "sources": [{"id": "s1", "command": TEST_SOURCE}],
        })
        status, saved = api.request("GET", "/api/channels")
        multi = next(c for c in saved if c["id"] == "multi")
        check("groups are de-duplicated and trimmed", multi["groups"] == ["News", "UK"],
              str(multi["groups"]))

        status, body = api.text("/playlist.m3u8")
        entries = [ln for ln in body.splitlines() if 'tvg-id="multi"' in ln]
        check("channel is listed once per group", len(entries) == 2, f"{len(entries)} entries")
        check("each entry carries a different group-title",
              sorted(re.findall(r'group-title="([^"]+)"', "\n".join(entries))) == ["News", "UK"],
              str(re.findall(r'group-title="([^"]+)"', "\n".join(entries))))
        check("both entries share one id and one URL",
              body.count("/stream/multi\n") == 2 and len(set(entries)) == 2)

        status, body = api.text("/playlist.m3u8?group=UK")
        check("?group= matches any of a channel's groups",
              'tvg-id="multi"' in body and 'group-title="UK"' in body)
        status, body = api.text("/playlist.m3u8?group=News")
        check("?group= matches the other group too", 'tvg-id="multi"' in body)

        settings_now = api.request("GET", "/api/settings")[1]
        api.request("PUT", "/api/settings", {**settings_now, "playlist_multi_group": False})
        status, body = api.text("/playlist.m3u8")
        entries = [ln for ln in body.splitlines() if 'tvg-id="multi"' in ln]
        check("multi_group off lists the channel once", len(entries) == 1,
              f"{len(entries)} entries")
        api.request("PUT", "/api/settings", settings_now)

        # A pre-multi-group config used a single `group` string.
        status, legacy = api.request("POST", "/api/channels", {
            "id": "legacygrp", "name": "Legacy", "group": "Sport",
            "sources": [{"id": "s1", "command": TEST_SOURCE}],
        })
        check("legacy single `group` migrates to a list",
              status == 201 and legacy["groups"] == ["Sport"], str(legacy.get("groups")))
        api.request("DELETE", "/api/channels/multi")
        api.request("DELETE", "/api/channels/legacygrp")

        # ---- passthrough streaming --------------------------------------
        print("\nStreaming (passthrough)")
        data = read_stream(f"{base}/stream/testcard", 188 * 400)
        check("stream returns data", len(data) > 0, f"{len(data)} bytes")
        check("output is valid MPEG-TS", is_mpegts(data))
        check("first client starts on a packet boundary", is_aligned(data),
              f"sync offset={sync_offset(data)}")

        # A client joining an already-running session gets the prebuffer replayed.
        # That replay must be packet-aligned, lead with a PAT, and open on a
        # keyframe - starting it mid-GOP is what leaves a player's audio running
        # ahead of its picture for the rest of the session.
        joiner = read_stream(f"{base}/stream/testcard", 188 * 400)
        check("mid-stream joiner starts on a packet boundary", is_aligned(joiner),
              f"sync offset={sync_offset(joiner)}")
        check("mid-stream joiner starts at a PAT", starts_at_pat(joiner),
              f"first packet pid={((joiner[1] & 0x1F) << 8 | joiner[2]) if len(joiner) > 2 else '-'}")
        check("mid-stream joiner's first video packet is a keyframe",
              starts_on_keyframe(joiner), f"video pid={busiest_pid(joiner)}")

        # And again with nothing to replay: the joiner has to be held until the
        # stream reaches its next keyframe rather than started wherever it is.
        status, before = api.request("GET", "/api/settings")
        api.request("PUT", "/api/settings", {**before, "prebuffer_bytes": 0})
        bare = read_stream(f"{base}/stream/testcard", 188 * 400)
        check("joiner with no prebuffer still starts on a keyframe",
              starts_on_keyframe(bare), f"video pid={busiest_pid(bare)}")
        api.request("PUT", "/api/settings", before)

        status, sessions = api.request("GET", "/api/sessions")
        running = [s for s in sessions if s["channel_id"] == "testcard"]
        check("session registered", len(running) == 1, f"status={running[0]['status'] if running else '-'}")
        if running:
            check("session reports throughput", running[0]["bytes_out"] > 0,
                  f"{running[0]['bytes_out']} bytes")

        status, log = api.request("GET", f"/api/sessions/{running[0]['key']}/log") if running else (0, {})
        check("session log available", status == 200 and len(log.get("lines", [])) > 0)

        # ---- fan-out -----------------------------------------------------
        print("\nFan-out (shared upstream)")
        results: dict[int, int] = {}

        def puller(idx: int) -> None:
            # Hold the connection open for a fixed window rather than a byte
            # count: the prebuffer replay would otherwise satisfy a small read
            # instantly and the session would be gone before the check below.
            results[idx] = hold_stream(f"{base}/stream/testcard", seconds=8)

        threads = [threading.Thread(target=puller, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        time.sleep(3)
        status, sessions = api.request("GET", "/api/sessions")
        mine = [s for s in sessions if s["channel_id"] == "testcard" and s["profile"] is None]
        check("3 viewers share 1 session", len(mine) == 1,
              f"sessions={len(mine)} clients={mine[0]['clients'] if mine else 0}")
        if mine:
            check("session counts all clients", mine[0]["clients"] >= 2,
                  f"clients={mine[0]['clients']}")
            check("only one source process", len(mine[0]["pids"]) == 1, f"pids={mine[0]['pids']}")
        pids_before = mine[0]["pids"] if mine else []
        for t in threads:
            t.join(timeout=60)
        check("all viewers received data",
              len(results) == 3 and all(v > 0 for v in results.values()),
              ", ".join(f"{v}B" for v in results.values()))

        # ---- profile switching --------------------------------------------
        # The failure this guards against: watching passthrough, then switching
        # to a profile, used to open a SECOND upstream connection while the old
        # session lingered. Providers that cap concurrent connections refuse it.
        print("\nProfile switch (must not double-connect)")
        connections = CountingSource(tmpdir)
        connections.start()
        api.request("POST", "/api/channels", {
            "id": "counted", "name": "Counted", "command": connections.command,
        })

        hold_a = threading.Thread(target=lambda: hold_stream(f"{base}/stream/counted", 6))
        hold_a.start()
        time.sleep(3)
        check("passthrough opened 1 upstream connection", connections.count == 1,
              f"count={connections.count}")

        # Switch profile while the first session is still within its linger window.
        hold_b = threading.Thread(
            target=lambda: hold_stream(f"{base}/stream/counted?profile=remux", 8))
        hold_b.start()
        time.sleep(4)
        check("switching profile reuses the same upstream", connections.count == 1,
              f"count={connections.count}")

        status, sessions = api.request("GET", "/api/sessions")
        counted = {s["key"]: s for s in sessions if s["channel_id"] == "counted"}
        check("source and transcoder are separate sessions",
              set(counted) == {"counted", "counted@remux"}, str(sorted(counted)))
        if "counted" in counted:
            check("source reports its transcoder as a consumer",
                  counted["counted"]["consumers"] > counted["counted"]["clients"],
                  f"consumers={counted['counted']['consumers']} clients={counted['counted']['clients']}")
            check("source runs exactly 1 process", len(counted["counted"]["pids"]) == 1,
                  f"pids={counted['counted']['pids']}")
        if "counted@remux" in counted:
            check("transcoder runs its own ffmpeg",
                  len(counted["counted@remux"]["pids"]) == 1,
                  f"pids={counted['counted@remux']['pids']}")
        for t in (hold_a, hold_b):
            t.join(timeout=30)
        time.sleep(6)
        check("upstream never reconnected", connections.count == 1, f"count={connections.count}")
        connections.stop()
        api.request("DELETE", "/api/channels/counted")

        # ---- networks: capacity limits -------------------------------------
        print("\nNetworks (capacity limits)")
        status, _ = api.request("POST", "/api/networks", {
            "id": "provider", "name": "Provider", "max_streams": 1,
        })
        check("POST /api/networks", status == 201)

        for n in (1, 2):
            api.request("POST", "/api/channels", {
                "id": f"net{n}", "name": f"Net {n}",
                "sources": [{"id": "s1", "command": TEST_SOURCE, "network": "provider"}],
            })

        holder = threading.Thread(target=lambda: hold_stream(f"{base}/stream/net1", 14))
        holder.start()
        time.sleep(4)
        status, usage = api.request("GET", "/api/networks")
        used = next((n for n in usage if n["id"] == "provider"), {})
        check("network reports 1 stream in use", used.get("in_use") == 1, f"in_use={used.get('in_use')}")

        code, detail = stream_status(f"{base}/stream/net2")
        check("second stream refused with 503", code == 503, f"got {code}: {detail[:70]}")

        # A second viewer of the ALREADY running channel must still be allowed:
        # it needs no extra upstream stream.
        extra = hold_stream(f"{base}/stream/net1", 3)
        check("extra viewer of a running channel is allowed", extra > 0, f"{extra} bytes")

        holder.join(timeout=40)
        time.sleep(6)  # linger 2s + release
        code, detail = stream_status(f"{base}/stream/net2", timeout=25)
        check("slot frees up once the first stream ends", code == 200, f"got {code} {detail[:50]}")
        time.sleep(5)

        status, _ = api.request("PUT", "/api/networks/provider", {
            "id": "provider", "name": "Provider", "max_streams": 0,
        })
        check("PUT /api/networks (unlimited)", status == 200)
        api.request("DELETE", "/api/channels/net1")
        api.request("DELETE", "/api/channels/net2")

        # ---- multiple sources with priority failover -------------------------
        print("\nSource failover (priority order)")
        good = CountingSource(tmpdir)
        good.start()
        api.request("POST", "/api/channels", {
            "id": "fo", "name": "Failover",
            "sources": [
                {"id": "dead", "name": "Dead", "command": "false", "priority": 100},
                {"id": "alsodead", "name": "Also dead", "command": "sh -c 'exit 3'",
                 "priority": 50},
                {"id": "good", "name": "Good", "command": good.command, "priority": 10},
            ],
        })
        data = read_stream(f"{base}/stream/fo", 188 * 200, timeout=45)
        check("failover reaches a working source", len(data) > 0, f"{len(data)} bytes")
        check("failover output is valid MPEG-TS", is_mpegts(data))

        status, sessions = api.request("GET", "/api/sessions")
        fo = next((s for s in sessions if s["channel_id"] == "fo"), None)
        if fo:
            check("session reports the source in use", fo["source_id"] == "good",
                  f"source_id={fo['source_id']} name={fo['source_name']}")
            check("session lists the sources it failed over from",
                  set(fo["failed_sources"]) == {"dead", "alsodead"},
                  f"failed={fo['failed_sources']}")
        check("only one upstream connection was made", good.count == 1, f"count={good.count}")

        # Priority must decide the order, not list order.
        api.request("PUT", "/api/channels/fo", {
            "id": "fo", "name": "Failover",
            "sources": [
                {"id": "good", "name": "Good", "command": good.command, "priority": 1},
                {"id": "better", "name": "Better", "command": "false", "priority": 99},
            ],
        })
        time.sleep(1)
        good.start()  # reset the counter
        data = read_stream(f"{base}/stream/fo", 188 * 200, timeout=45)
        status, sessions = api.request("GET", "/api/sessions")
        fo = next((s for s in sessions if s["channel_id"] == "fo"), None)
        check("highest priority is tried first regardless of list order",
              bool(fo) and fo["failed_sources"] == ["better"] and fo["source_id"] == "good",
              f"tried={fo['failed_sources'] if fo else '-'} using={fo['source_id'] if fo else '-'}")

        # A channel whose every source is on a full network is refused outright.
        api.request("POST", "/api/networks", {"id": "tiny", "name": "Tiny", "max_streams": 0})
        api.request("PUT", "/api/networks/tiny",
                    {"id": "tiny", "name": "Tiny", "max_streams": 1, "enabled": False})
        api.request("POST", "/api/channels", {
            "id": "blocked", "name": "Blocked",
            "sources": [{"id": "s1", "command": TEST_SOURCE, "network": "tiny"}],
        })
        code, detail = stream_status(f"{base}/stream/blocked")
        check("disabled network blocks its sources", code == 503, f"got {code}: {detail[:70]}")

        status, bad = api.request("POST", "/api/channels", {"id": "nosrc", "name": "No sources",
                                                           "sources": []})
        check("channel with no sources rejected", status == 422)

        api.request("DELETE", "/api/channels/fo")
        api.request("DELETE", "/api/channels/blocked")
        api.request("DELETE", "/api/networks/tiny")

        # ---- EPG -----------------------------------------------------------
        print("\nEPG (XMLTV ingest and mapping)")
        guide = tmpdir / "guide.sh"
        guide.write_text(XMLTV_SCRIPT)
        guide.chmod(0o755)

        # Two channels: one whose tvg-id matches upstream exactly, one that has
        # to be matched on name alone.
        api.request("POST", "/api/channels", {
            "id": "news", "name": "News 24", "tvg_id": "news24.example",
            "channel_number": 7, "logo": "http://logo/news.png",
            "sources": [{"id": "s1", "command": TEST_SOURCE}],
        })
        api.request("POST", "/api/channels", {
            "id": "movies", "name": "Movie Channel HD",
            "sources": [{"id": "s1", "command": TEST_SOURCE}],
        })

        status, created = api.request("POST", "/api/epg/sources", {
            "id": "main", "name": "Main guide", "kind": "command",
            "command": f"sh {guide}", "refresh_hours": 12,
        })
        check("POST /api/epg/sources", status == 201)

        status, st = api.request("POST", "/api/epg/sources/main/refresh", timeout=60)
        check("EPG source refresh succeeds", status == 200 and not st.get("last_error"),
              st.get("last_error", ""))
        check("EPG indexes channels and programmes",
              st.get("channels") == 3 and st.get("programmes") == 4,
              f"channels={st.get('channels')} programmes={st.get('programmes')}")

        status, ep = api.request("GET", "/api/epg")
        by_channel = {m["channel_id"]: m for m in ep["mapping"]}
        check("exact tvg-id auto-matches",
              by_channel["news"]["matched"] == "news24.example",
              f"matched={by_channel['news']['matched']}")
        check("fuzzy name auto-matches (HD suffix ignored)",
              by_channel["movies"]["matched"] == "movies.example",
              f"matched={by_channel['movies']['matched']}")

        # Serve the guide and verify what a client would actually receive.
        status, xml = api.text("/xmltv.xml", timeout=30)
        check("GET /xmltv.xml", status == 200 and xml.startswith("<?xml"))
        check("guide is well-formed XML", is_valid_xml(xml), xml[:80])

        root = ET.fromstring(xml)
        ids = sorted(c.get("id") for c in root.findall("channel"))
        check("channel ids are rewritten to the playlist tvg-id",
              ids == ["movies", "news24.example"], str(ids))
        prog_channels = sorted({p.get("channel") for p in root.findall("programme")})
        check("programme channel attributes are remapped too",
              prog_channels == ["movies", "news24.example"], str(prog_channels))
        check("unmapped upstream channels are left out",
              not any(p.get("channel") == "sports.example" for p in root.findall("programme")))
        names = [c.findtext("display-name") for c in root.findall("channel")]
        check("display names come from our config", "Movie Channel HD" in names, str(names))
        check("logo is carried into the guide",
              any(i.get("src") == "http://logo/news.png" for i in root.iter("icon")))
        check("programme detail survives the rewrite",
              any(p.findtext("title") == "Evening News" for p in root.findall("programme")))

        # Playlist and guide must agree on ids, or clients show an empty EPG.
        status, pl = api.text("/playlist.m3u8")
        check("playlist advertises the guide URL", 'url-tvg="' in pl and "/xmltv.xml" in pl,
              pl.splitlines()[0][:90])
        playlist_ids = set(re.findall(r'tvg-id="([^"]+)"', pl))
        check("every guide channel id appears in the playlist",
              set(ids).issubset(playlist_ids), f"guide={ids} playlist={sorted(playlist_ids)}")

        # Manual mapping overrides the auto-match.
        status, _ = api.request("PUT", "/api/epg/mapping",
                                {"mapping": {"movies": "sports.example"}})
        check("PUT /api/epg/mapping", status == 200)
        status, xml = api.text("/xmltv.xml", timeout=30)
        root = ET.fromstring(xml)
        titles = {p.get("channel"): p.findtext("title") for p in root.findall("programme")}
        check("manual mapping overrides the auto-match",
              titles.get("movies") == "The Big Match", f"got {titles.get('movies')!r}")

        # Excluding a channel drops it from the guide but not the playlist.
        ch = next(c for c in api.request("GET", "/api/channels")[1] if c["id"] == "movies")
        api.request("PUT", "/api/channels/movies", {**ch, "epg_enabled": False})
        status, xml = api.text("/xmltv.xml", timeout=30)
        root = ET.fromstring(xml)
        check("epg_enabled=false removes a channel from the guide",
              "movies" not in [c.get("id") for c in root.findall("channel")])
        status, pl = api.text("/playlist.m3u8")
        check("...but it stays in the playlist", "/stream/movies" in pl)

        # Past-programme trimming.
        settings_now = api.request("GET", "/api/settings")[1]
        api.request("PUT", "/api/settings", {**settings_now, "epg_past_hours": 0.001})
        status, xml = api.text("/xmltv.xml", timeout=30)
        root = ET.fromstring(xml)
        check("old programmes are trimmed",
              not any(p.findtext("title") == "Ancient History" for p in root.findall("programme")))
        api.request("PUT", "/api/settings", settings_now)

        # ---- mappings must survive everything ------------------------------
        # The reported failure: pin a mapping, then add or edit channels, and the
        # pin quietly reverts to a wrong auto-match.
        print("\nEPG mapping stability")
        api.request("PUT", "/api/epg/mapping", {"mapping": {"news": "sports.example"}})
        status, ep = api.request("GET", "/api/epg")
        news = next(m for m in ep["mapping"] if m["channel_id"] == "news")
        check("a pinned mapping is recorded as pinned",
              news["assigned"] == "sports.example" and news["auto_mode"] is False,
              f"assigned={news['assigned']} auto={news['auto_mode']}")

        # Adding an unrelated channel must not disturb it.
        api.request("POST", "/api/channels", {
            "id": "newcomer", "name": "News 24",   # deliberately collides by name
            "sources": [{"id": "s1", "command": TEST_SOURCE}],
        })
        status, ep = api.request("GET", "/api/epg")
        news = next(m for m in ep["mapping"] if m["channel_id"] == "news")
        check("adding a channel leaves pinned mappings alone",
              news["matched"] == "sports.example", f"matched={news['matched']}")

        # Saving the channel from the GUI form must not wipe it. The GUI sends the
        # whole record, so this mimics a form that carries the fields it does not show.
        status, chans = api.request("GET", "/api/channels")
        ch = next(c for c in chans if c["id"] == "news")
        api.request("PUT", "/api/channels/news", {**ch, "name": "News 24 HD"})
        status, ep = api.request("GET", "/api/epg")
        news = next(m for m in ep["mapping"] if m["channel_id"] == "news")
        check("editing a channel keeps its pinned mapping",
              news["assigned"] == "sports.example" and news["auto_mode"] is False,
              f"assigned={news['assigned']} auto={news['auto_mode']}")

        # Auto-match must never touch a pinned channel.
        status, r = api.request("POST", "/api/epg/automap", timeout=30)
        status, ep = api.request("GET", "/api/epg")
        news = next(m for m in ep["mapping"] if m["channel_id"] == "news")
        check("auto-match skips pinned channels",
              news["assigned"] == "sports.example", f"assigned={news['assigned']}")

        # Clearing a mapping means "no guide", not "guess again".
        api.request("PUT", "/api/epg/mapping", {"mapping": {"news": None}})
        status, ep = api.request("GET", "/api/epg")
        news = next(m for m in ep["mapping"] if m["channel_id"] == "news")
        check("clearing a mapping pins 'no guide' rather than reverting to auto",
              news["matched"] is None and news["auto_mode"] is False,
              f"matched={news['matched']} auto={news['auto_mode']}")
        status, xml = api.text("/xmltv.xml", timeout=30)
        check("a channel pinned to no guide is absent from the XMLTV",
              "news24.example" not in [c.get("id") for c in ET.fromstring(xml).findall("channel")])

        # And it stays cleared across an auto-match run.
        api.request("POST", "/api/epg/automap", timeout=30)
        status, ep = api.request("GET", "/api/epg")
        news = next(m for m in ep["mapping"] if m["channel_id"] == "news")
        check("'no guide' survives an auto-match run", news["matched"] is None,
              f"matched={news['matched']}")

        # Releasing it puts the channel back on auto-matching.
        api.request("PUT", "/api/epg/mapping", {"auto": ["news"]})
        status, ep = api.request("GET", "/api/epg")
        news = next(m for m in ep["mapping"] if m["channel_id"] == "news")
        check("releasing a channel restores auto-matching",
              news["auto_mode"] is True and news["matched"] == "news24.example",
              f"auto={news['auto_mode']} matched={news['matched']}")

        # Pinning an id the guide has not seen yet is allowed but reported.
        status, r = api.request("PUT", "/api/epg/mapping",
                                {"mapping": {"news": "not.in.guide"}})
        check("pinning an unknown id is accepted and flagged",
              r.get("unknown") == ["not.in.guide"], str(r))
        api.request("PUT", "/api/epg/mapping", {"auto": ["news"]})
        api.request("DELETE", "/api/channels/newcomer")

        status, bad = api.request("POST", "/api/epg/sources", {
            "id": "broken", "name": "Broken", "kind": "command", "command": "false",
        })
        time.sleep(3)
        status, ep = api.request("GET", "/api/epg")
        broken = next((s for s in ep["sources"] if s["id"] == "broken"), {})
        check("a failing EPG source reports its error", bool(broken.get("last_error")),
              broken.get("last_error", "")[:60])
        status, xml = api.text("/xmltv.xml", timeout=30)
        check("a broken source does not break the guide", is_valid_xml(xml))

        status, _ = api.request("DELETE", "/api/epg/sources/broken")
        check("DELETE /api/epg/sources", status == 200)
        api.request("DELETE", "/api/channels/news")
        api.request("DELETE", "/api/channels/movies")
        api.request("DELETE", "/api/epg/sources/main")

        # ---- TS error analysis ---------------------------------------------
        print("\nMPEG-TS error counters")
        run_analyser_checks()

        api.request("POST", "/api/channels", {
            "id": "clean", "name": "Clean", "sources": [{"id": "s1", "command": TEST_SOURCE}],
        })
        data = read_stream(f"{base}/stream/clean", 188 * 600)
        check("clean stream produces data", len(data) > 0, f"{len(data)} bytes")
        status, sessions = api.request("GET", "/api/sessions")
        clean = next((s for s in sessions if s["channel_id"] == "clean"), None)
        if clean:
            check("packets are counted", clean["ts_packets"] > 100,
                  f"packets={clean['ts_packets']}")
            check("a locally generated stream has no errors",
                  clean["ts_continuity_errors"] == 0 and clean["ts_transport_errors"] == 0,
                  f"cont={clean['ts_continuity_errors']} tei={clean['ts_transport_errors']}")

        # A source that emits deliberately corrupt TS must be counted, not hidden.
        corrupt = tmpdir / "corrupt.py"
        corrupt.write_text(CORRUPT_TS_SOURCE)
        api.request("POST", "/api/channels", {
            "id": "dirty", "name": "Dirty",
            "sources": [{"id": "s1", "command": f"{PY} {corrupt}"}],
        })
        read_stream(f"{base}/stream/dirty", 188 * 400, timeout=25)
        status, sessions = api.request("GET", "/api/sessions")
        dirty = next((s for s in sessions if s["channel_id"] == "dirty"), None)
        if dirty:
            check("continuity errors are detected", dirty["ts_continuity_errors"] > 0,
                  f"cont={dirty['ts_continuity_errors']}")
            check("transport errors are detected", dirty["ts_transport_errors"] > 0,
                  f"tei={dirty['ts_transport_errors']}")
            check("the worst PID is identified", bool(dirty["ts_error_pids"]),
                  str(dirty["ts_error_pids"][:2]))

        # ---- Prometheus ------------------------------------------------------
        print("\nPrometheus metrics")
        status, body = api.text("/metrics")
        check("GET /metrics", status == 200 and body.startswith("# HELP"), body[:60])
        check("exposition format is parseable", metrics_parse(body) is not None)
        names = metrics_parse(body)
        for name in ("streams_manager_build_info", "streams_manager_channels",
                     "streams_manager_clients", "streams_manager_bytes_total",
                     "streams_manager_ts_continuity_errors_total",
                     "streams_manager_ts_transport_errors_total",
                     "streams_manager_network_max_streams",
                     "streams_manager_epg_mapped_channels"):
            check(f"exposes {name}", name in names)
        check("every metric declares a TYPE",
              all(f"# TYPE {n} " in body for n in names), "")
        check("counters carry TS errors from the ledger",
              any(v > 0 for k, v in names["streams_manager_ts_continuity_errors_total"]),
              str(names["streams_manager_ts_continuity_errors_total"][:2]))
        # A channel name is operator-supplied text that lands in a label value,
        # so quotes and backslashes must not be able to break the format.
        api.request("POST", "/api/channels", {
            "id": "quoted", "name": 'He said "hi" \\ bye',
            "sources": [{"id": "s1", "command": TEST_SOURCE}],
        })
        hold_stream(f"{base}/stream/quoted", 3)
        status, body = api.text("/metrics")
        quoted_lines = [ln for ln in body.splitlines() if "quoted" in ln and not ln.startswith("#")]
        check("quotes and backslashes in labels are escaped",
              any(r'He said \"hi\" \\ bye' in ln for ln in quoted_lines),
              quoted_lines[0][:100] if quoted_lines else "no lines")
        check("escaped output still parses", metrics_parse(body) is not None)
        api.request("DELETE", "/api/channels/quoted")

        # Ledger counters must survive the session being torn down.
        before = sum(v for _, v in names["streams_manager_ts_packets_total"])
        api.request("POST", "/api/sessions/dirty/stop")
        time.sleep(2)
        status, body = api.text("/metrics")
        after_names = metrics_parse(body)
        after = sum(v for _, v in after_names["streams_manager_ts_packets_total"])
        check("counters do not reset when a session stops", after >= before,
              f"{before} -> {after}")

        api.request("DELETE", "/api/channels/clean")
        api.request("DELETE", "/api/channels/dirty")

        # ---- source audit ----------------------------------------------------
        print("\nSource audit")
        api.request("POST", "/api/networks", {
            "id": "auditnet", "name": "Audit net", "max_streams": 1,
        })
        api.request("POST", "/api/channels", {
            "id": "aud1", "name": "Audit One", "channel_number": 1,
            "sources": [
                {"id": "good", "name": "Good", "priority": 10, "network": "auditnet",
                 "command": TEST_SOURCE},
                {"id": "dead", "name": "Dead", "priority": 5, "command": "false"},
            ],
        })
        api.request("POST", "/api/channels", {
            "id": "aud2", "name": "Audit Two", "channel_number": 2,
            "sources": [{"id": "good", "name": "Good", "network": "auditnet",
                         "command": TEST_SOURCE}],
        })

        # Other channels from earlier sections are still configured, so assert on
        # the ones this section owns rather than on the whole run.
        mine = {"aud1/good", "aud1/dead", "aud2/good"}
        started = time.time()
        status, run = api.request("POST", "/api/audit?duration=4&concurrency=4")
        check("POST /api/audit starts a run", status == 200 and run["total"] >= 3,
              f"total={run.get('total')}")
        check("audit enumerates every source up front",
              mine.issubset({r["key"] for r in run["results"]}),
              str(sorted(r["key"] for r in run["results"])))

        status, dup = api.request("POST", "/api/audit")
        check("a second concurrent audit is refused", dup_status_409(status, dup), str(status))

        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(2)
            status, run = api.request("GET", "/api/audit")
            if not run["running"]:
                break
        elapsed = time.time() - started
        check("audit completes", run["status"] == "done", run["status"])

        by_key = {r["key"]: r for r in run["results"]}
        check("a working source passes", by_key["aud1/good"]["status"] == "ok",
              by_key["aud1/good"]["error"])
        check("audit reports the video format",
              "640x360" in by_key["aud1/good"]["video"], by_key["aud1/good"]["video"])
        check("audit reports the audio format",
              "aac" in by_key["aud1/good"]["audio"], by_key["aud1/good"]["audio"])
        check("audit measures a bitrate", by_key["aud1/good"]["bitrate_bps"] > 0,
              f"{by_key['aud1/good']['bitrate_bps']}")
        check("a dead source fails with a reason",
              by_key["aud1/dead"]["status"] == "failed" and by_key["aud1/dead"]["error"],
              by_key["aud1/dead"]["error"])
        statuses = [by_key[k]["status"] for k in sorted(mine)]
        check("counts summarise the run",
              statuses.count("ok") == 2 and statuses.count("failed") == 1,
              f"{statuses} overall={run['counts']}")

        # Two sources share a network capped at one, so despite concurrency=4 they
        # must have run one after the other rather than both at once.
        check("audit honours network capacity", elapsed >= 8,
              f"3 sources, 4s each, cap 1 on two of them -> {elapsed:.0f}s")

        status, one = api.request("POST", "/api/audit?duration=3&channel=aud2")
        check("a single channel can be audited", one["total"] == 1, f"total={one['total']}")
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(1)
            status, one = api.request("GET", "/api/audit")
            if not one["running"]:
                break
        check("single-channel audit finishes", one["status"] == "done", one["status"])

        status, bad = api.request("POST", "/api/audit?channel=nosuch")
        check("auditing an unknown channel is a 404", status == 404, str(status))

        # Stopping mid-run must not leave sources stuck as pending.
        api.request("POST", "/api/audit?duration=30&concurrency=1")
        time.sleep(2)
        status, stopped = api.request("POST", "/api/audit/stop")
        check("POST /api/audit/stop cancels the run", stopped["status"] == "cancelled",
              stopped["status"])
        check("cancelled sources are marked skipped, not left pending",
              not any(r["status"] in ("pending", "testing") for r in stopped["results"]),
              str([r["status"] for r in stopped["results"]]))
        time.sleep(1)
        status, net = api.request("GET", "/api/networks")
        auditnet = next(n for n in net if n["id"] == "auditnet")
        check("a cancelled audit releases its network slots", auditnet["in_use"] == 0,
              f"in_use={auditnet['in_use']}")

        status, body = api.text("/metrics")
        check("audit results reach Prometheus",
              "streams_manager_source_ok" in body and "streams_manager_audit_sources" in body)

        api.request("DELETE", "/api/channels/aud1")
        api.request("DELETE", "/api/channels/aud2")
        api.request("DELETE", "/api/networks/auditnet")

        # ---- AC-3 audio across a reopen -----------------------------------
        # AC-3 travels as private_stream_1, id 0xBD, below the range audio ids
        # are usually assumed to start at. A reopen that rebases the video's
        # timestamps but not that audio's leaves the two on separate timelines,
        # further apart every time, which is heard as sound running ahead of
        # picture. Most US broadcast sources are AC-3, so this is the common
        # case, not an exotic one.
        print("\nAC-3 audio across a reopen")
        ac3 = tmpdir / "ac3.sh"
        ac3.write_text(
            "#!/bin/sh\n"
            "exec ffmpeg -hide_banner -loglevel error -re "
            "-f lavfi -i testsrc2=size=320x180:rate=15 "
            "-f lavfi -i sine=frequency=440:sample_rate=48000 -t 12 "
            "-c:v mpeg2video -b:v 800k -g 15 -c:a ac3 -b:a 128k -ac 2 "
            "-f mpegts pipe:1\n"
        )
        ac3.chmod(0o755)
        settings_now = api.request("GET", "/api/settings")[1]
        api.request("PUT", "/api/settings", {**settings_now, "reconnect_delay_seconds": 1})
        api.request("POST", "/api/channels", {
            "id": "ac3", "name": "AC-3", "enabled": True,
            "sources": [{"id": "s1", "name": "AC-3", "command": f"sh {ac3}"}],
        })
        heard = hold_capture(f"{base}/stream/ac3", 30)   # spans two reopens
        check("AC-3 channel delivers data", len(heard) > 100_000, f"{len(heard)} bytes")
        apart = track_separation(heard)
        # Splicing two encoder runs shifts how the tracks interleave by a
        # fraction of a second either way, so a little movement is normal. The
        # fault this guards against moves them apart by a whole run every
        # reopen, so there is a wide gap between the two.
        check("AC-3 stays on the same timeline as the video across a reopen",
              apart is not None and apart < 2.0,
              f"tracks drift {apart if apart is None else round(apart, 2)}s apart")
        api.request("PUT", "/api/settings", settings_now)
        api.request("DELETE", "/api/channels/ac3")

        # ---- rotating source (streams, ends cleanly, must reopen politely) ----
        # Reproduces a live HLS source whose token or playlist window rotates:
        # it streams fine, exits 0, and must be reopened after a settle pause
        # rather than instantly - reopening at once is what produced a burst of
        # connection resets against the upstream.
        print("\nRotating source (reopen, not failure)")
        rotator = tmpdir / "rotator.sh"
        attempts = tmpdir / "rotator.log"
        rotator.write_text(
            "#!/bin/sh\n"
            f'date +%s.%N >> "{attempts}"\n'
            "exec ffmpeg -hide_banner -loglevel error -re "
            "-f lavfi -i testsrc2=size=320x180:rate=15 "
            "-c:v libx264 -preset ultrafast -g 15 -t 18 -f mpegts pipe:1\n"
        )
        rotator.chmod(0o755)
        attempts.write_text("")

        settings_now = api.request("GET", "/api/settings")[1]
        api.request("PUT", "/api/settings", {**settings_now, "reconnect_delay_seconds": 5})
        api.request("POST", "/api/channels", {
            "id": "rot", "name": "Rotator",
            "sources": [{"id": "s1", "name": "Rotating", "command": f"sh {rotator}"}],
        })

        # Hold a viewer across at least one rotation.
        seen = hold_capture(f"{base}/stream/rot", 34)
        held = len(seen)
        check("viewer keeps receiving data across a rotation", held > 100_000,
              f"{held} bytes")

        # The viewer must not be able to tell that the source restarted. A new
        # run's timestamps and continuity counters both begin again, and handing
        # either of those to a player mid-stream is what turns a routine reopen
        # into drifting lip sync and phantom packet loss.
        rewinds = timeline_rewinds(seen)
        check("a reopen does not rewind the viewer's clock", not rewinds,
              f"{len(rewinds)} rewind(s), first {rewinds[0] if rewinds else '-'}")
        check("a reopen does not look like packet loss to the viewer",
              ts_continuity_errors(seen) == 0,
              f"continuity errors={ts_continuity_errors(seen)}")

        status, sessions = api.request("GET", "/api/sessions")
        rot = next((s for s in sessions if s["channel_id"] == "rot"), None)
        if rot:
            check("a clean end after a good run counts as a reopen, not a failure",
                  rot["reconnects"] >= 1, f"reconnects={rot['reconnects']}")
            check("a reopen does not leave the session in error",
                  rot["status"] in ("running", "restarting") and not rot["last_error"],
                  f"status={rot['status']} err={rot['last_error']!r}")
            check("the reason for the last end is still reported",
                  bool(rot["last_end"]), rot["last_end"])

        stamps = [float(x) for x in attempts.read_text().split()]
        gaps = [round(b - a, 1) for a, b in zip(stamps, stamps[1:])]
        # The source runs 18s, so consecutive starts should be ~18s + the 5s
        # settle pause. Without the pause they were about 18s apart.
        check("reopen waits the settle delay instead of retrying instantly",
              bool(gaps) and all(g >= 22 for g in gaps), f"start gaps: {gaps}")

        status, body = api.text("/metrics")
        check("reopens are exposed to Prometheus",
              "streams_manager_reconnects_total" in body)

        api.request("PUT", "/api/settings", settings_now)
        api.request("DELETE", "/api/channels/rot")

        # ---- orphaned helper processes ---------------------------------------
        # The case that matters: the source forks a helper and then exits *by
        # itself*, with no client attached. The helper would otherwise keep the
        # upstream connection open, so the next attempt becomes a second
        # connection and the provider resets it.
        print("\nOrphaned helper processes")
        heartbeat = tmpdir / "helper-heartbeat"
        orphan = tmpdir / "orphan.sh"
        orphan.write_text(
            "#!/bin/sh\n"
            "# A helper that outlives its parent, as streamlink's muxer would.\n"
            f'( while true; do echo tick >> "{heartbeat}"; sleep 0.3; done ) &\n'
            "exec ffmpeg -hide_banner -loglevel error -re "
            "-f lavfi -i testsrc2=size=320x180:rate=15 "
            "-c:v libx264 -preset ultrafast -g 15 -t 5 -f mpegts pipe:1\n"
        )
        orphan.chmod(0o755)
        heartbeat.write_text("")

        api.request("PUT", "/api/settings", {**settings_now, "linger_seconds": 2})
        api.request("POST", "/api/channels", {
            "id": "orph", "name": "Orphan maker",
            "sources": [{"id": "s1", "command": f"sh {orphan}"}],
        })
        # Leave before the source's own 5s runtime is up, so it exits on its own
        # with nobody watching - the path that used to skip cleanup entirely.
        hold_stream(f"{base}/stream/orph", 3)
        check("the helper ran while the source was up",
              len(heartbeat.read_text().splitlines()) > 0,
              f"{len(heartbeat.read_text().splitlines())} ticks")

        time.sleep(20)  # source self-exits, linger passes, session is released
        settled = len(heartbeat.read_text().splitlines())
        time.sleep(4)
        after = len(heartbeat.read_text().splitlines())
        check("a helper orphaned by a self-exiting source is reaped",
              after == settled, f"{settled} -> {after} ticks (still running = leaked)")

        leaked = subprocess.run(
            ["pgrep", "-f", str(heartbeat)], capture_output=True, text=True
        ).stdout.strip()
        check("no helper processes survive", not leaked, f"pids {leaked}")
        if leaked:
            for pid in leaked.split():
                with contextlib.suppress(Exception):
                    os.kill(int(pid), 9)

        api.request("DELETE", "/api/channels/orph")
        api.request("PUT", "/api/settings", settings_now)

        # ---- faster-than-real-time source ------------------------------------
        # An HLS input downloads its segment backlog at line speed, so the source
        # runs far ahead of a player consuming at the content's own rate. The
        # excess must be held back by making the source block, not discarded -
        # discarding bytes mid-transport-stream is what produces glitching.
        print("\nBursty source (backpressure, not dropping)")
        # No -re, so ffmpeg produces as fast as the CPU allows, like a backlog download.
        BURST = (
            "ffmpeg -hide_banner -loglevel error "
            "-f lavfi -i testsrc2=size=1280x720:rate=25 "
            "-c:v libx264 -preset ultrafast -b:v 8000k -g 25 -t 90 -f mpegts pipe:1"
        )
        api.request("POST", "/api/channels", {
            "id": "burst", "name": "Bursty",
            "sources": [{"id": "s1", "command": BURST}],
        })

        def slow_reader(url: str, seconds: float, path: Path) -> int:
            """Consume at roughly a real player's rate, well under the burst."""
            total = 0
            deadline = time.time() + seconds
            with open(path, "wb") as out:
                try:
                    with urllib.request.urlopen(url, timeout=seconds + 20) as resp:
                        while time.time() < deadline:
                            chunk = resp.read(64 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            total += len(chunk)
                            time.sleep(0.05)
                except Exception as exc:  # noqa: BLE001
                    print(f"    (reader ended: {exc})")
            return total

        capture = tmpdir / "burst.ts"
        got = slow_reader(f"{base}/stream/burst", 25, capture)
        check("slow client receives data from a bursty source", got > 1_000_000,
              f"{got / 1e6:.1f} MB")

        status, sessions = api.request("GET", "/api/sessions")
        burst = next((s for s in sessions if s["channel_id"] == "burst"), None)
        if burst:
            check("no data is dropped when the source outruns the client",
                  burst["dropped_chunks"] == 0, f"dropped={burst['dropped_chunks']}")

        data = capture.read_bytes()
        analyser_errors = ts_continuity_errors(data)
        source_errors = burst["ts_continuity_errors"] if burst else 0
        check("the client's stream has no continuity errors the source did not",
              analyser_errors <= source_errors,
              f"client {analyser_errors} vs source {source_errors} "
              f"in {len(data) / 1e6:.1f} MB")
        check("captured output stays packet-aligned", is_aligned(data),
              f"sync offset={sync_offset(data)}")

        # Drop counts must survive the client detaching, or this whole class of
        # fault stays invisible in the GUI.
        api.request("POST", "/api/sessions/burst/stop")
        time.sleep(1)
        api.request("DELETE", "/api/channels/burst")

        # With backpressure disabled the same run must lose data - otherwise the
        # check above proves nothing.
        api.request("PUT", "/api/settings", {**settings_now, "backpressure_seconds": 0})
        api.request("POST", "/api/channels", {
            "id": "burst2", "name": "Bursty 2",
            "sources": [{"id": "s1", "command": BURST}],
        })
        capture2 = tmpdir / "burst2.ts"
        slow_reader(f"{base}/stream/burst2", 20, capture2)
        status, sessions = api.request("GET", "/api/sessions")
        burst2 = next((s for s in sessions if s["channel_id"] == "burst2"), None)
        dropped2 = burst2["dropped_chunks"] if burst2 else 0
        check("without backpressure the same source does lose data", dropped2 > 0,
              f"dropped={dropped2} (confirms the test discriminates)")
        check("drop counts survive the client disconnecting", dropped2 > 0,
              f"counted {dropped2} after the reader finished")
        api.request("PUT", "/api/settings", settings_now)
        api.request("DELETE", "/api/channels/burst2")

        # A client that stops reading entirely must not hold up anyone else.
        print("\nStalled client isolation")
        api.request("PUT", "/api/settings", {
            **settings_now, "backpressure_seconds": 3, "client_queue_chunks": 8,
        })
        api.request("POST", "/api/channels", {
            "id": "stall", "name": "Stall test",
            "sources": [{"id": "s1", "command": TEST_SOURCE}],
        })
        frozen = urllib.request.urlopen(f"{base}/stream/stall", timeout=60)
        frozen.read(65536)          # attach, then never read again
        time.sleep(12)              # long enough for its small queue to fill
        healthy = hold_stream(f"{base}/stream/stall", 8)
        check("a healthy client keeps streaming past a frozen one", healthy > 500_000,
              f"{healthy / 1e6:.1f} MB")
        status, sessions = api.request("GET", "/api/sessions")
        st = next((s for s in sessions if s["channel_id"] == "stall"), None)
        check("the frozen client is the one that loses data",
              bool(st) and st["dropped_chunks"] > 0, f"dropped={st['dropped_chunks'] if st else 0}")
        with contextlib.suppress(Exception):
            frozen.close()
        api.request("PUT", "/api/settings", settings_now)
        api.request("DELETE", "/api/channels/stall")

        # ---- teardown ----------------------------------------------------
        print("\nIdle teardown")
        time.sleep(5)  # linger is 2s
        status, sessions = api.request("GET", "/api/sessions")
        check("session released after linger", not any(s["channel_id"] == "testcard" for s in sessions),
              f"{len(sessions)} session(s) left")
        alive = [p for p in pids_before if Path(f"/proc/{p}").exists()]
        check("source process reaped", not alive, f"still alive: {alive}")

        # ---- transcoding --------------------------------------------------
        print("\nTranscoding")
        data = read_stream(f"{base}/stream/testcard?profile=480p", 188 * 1200, timeout=60)
        check("transcoded stream returns data", len(data) > 0, f"{len(data)} bytes")
        check("transcoded output is MPEG-TS", is_mpegts(data))
        check("transcoded output is packet-aligned", is_aligned(data),
              f"sync offset={sync_offset(data)}")

        probe_file = tmpdir / "out.ts"
        probe_file.write_bytes(data)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=height,codec_name", "-of", "json", str(probe_file)],
            capture_output=True, text=True, timeout=30,
        )
        try:
            info = json.loads(probe.stdout)["streams"][0]
        except Exception:  # noqa: BLE001
            info = {}
        check("transcode applied (height 480)", info.get("height") == 480, f"probed {info}")

        status, sessions = api.request("GET", "/api/sessions")
        keyed = [s for s in sessions if s["profile"] == "480p"]
        check("transcode session is separate", len(keyed) == 1,
              f"key={keyed[0]['key'] if keyed else '-'}")
        if keyed:
            check("transcode session owns only its ffmpeg", len(keyed[0]["pids"]) == 1,
                  f"pids={keyed[0]['pids']}")
            check("transcode is fed by a shared source session",
                  any(s["kind"] == "source" and s["channel_id"] == "testcard" for s in sessions))
            status, _ = api.request("POST", f"/api/sessions/{keyed[0]['key']}/stop")
            check("POST /api/sessions/{key}/stop", status == 200)

        status, body = api.text("/stream/testcard?profile=nope")
        check("unknown profile on stream rejected", status == 400)

        status, body = api.text("/stream/nosuch")
        check("unknown channel rejected", status == 404)

        # ---- source test endpoint -------------------------------------------
        print("\nSource test endpoint")
        status, result = api.request(
            "POST", "/api/test", {"command": TEST_SOURCE, "duration": 5}, timeout=60
        )
        check("POST /api/test succeeds", status == 200 and result.get("ok"),
              result.get("error", "")[:60])
        check("test probes the stream", bool(result.get("probe")),
              json.dumps(result.get("probe", {}))[:90])

        status, result = api.request(
            "POST", "/api/test", {"command": "false", "duration": 3}, timeout=30
        )
        check("failing command reported", status == 200 and not result.get("ok"),
              result.get("error", ""))

        status, result = api.request(
            "POST", "/api/test",
            {"command": "definitely-not-a-real-binary-xyz", "duration": 3}, timeout=30,
        )
        check("missing binary reported", status == 200 and "not found" in result.get("error", ""),
              result.get("error", ""))

        # ---- error handling in a live session --------------------------------
        print("\nDead source handling")
        api.request("POST", "/api/channels", {
            "id": "broken", "name": "Broken", "command": "false",
        })
        data = read_stream(f"{base}/stream/broken", 1024, timeout=8)
        check("dead source yields no data without hanging", len(data) == 0)
        status, sessions = api.request("GET", "/api/sessions")
        broken = [s for s in sessions if s["channel_id"] == "broken"]
        check("dead source surfaces an error",
              not broken or broken[0]["status"] in ("error", "restarting", "stopped"),
              broken[0]["last_error"] if broken else "session already released")

        # ---- deletion ---------------------------------------------------------
        print("\nDeletion")
        status, _ = api.request("DELETE", "/api/channels/testcard")
        check("DELETE /api/channels", status == 200)
        status, body = api.text("/playlist.m3u8")
        check("deleted channel leaves the playlist", "/stream/testcard" not in body)
        status, _ = api.request("DELETE", "/api/channels/testcard")
        check("deleting twice is a 404", status == 404)

        # ---- persistence -------------------------------------------------------
        print("\nPersistence")
        saved = json.loads(config.read_text())
        check("config written to disk", config.exists() and saved["settings"]["linger_seconds"] == 2)
        check("channels persisted", [c["id"] for c in saved["channels"]] == ["broken"],
              str([c["id"] for c in saved["channels"]]))

    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("Failed: " + ", ".join(FAILED))
        print("\n--- last server output ---")
        print("\n".join(server_output[-40:]))
        return 1

    # Nothing should be left running.
    leaked = subprocess.run(
        ["pgrep", "-f", "testsrc2=size=640x360"], capture_output=True, text=True
    ).stdout.strip()
    if leaked:
        print(f"WARNING: leaked ffmpeg processes: {leaked}")
        return 1
    print("All good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
