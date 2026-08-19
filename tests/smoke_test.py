#!/usr/bin/env python3
"""End-to-end smoke test.

Starts a real server against a throwaway config, drives it over HTTP with a
synthetic ffmpeg source, and checks the parts that are easy to get wrong:
MPEG-TS passthrough, transcoding on request, session fan-out, and teardown.

    python tests/smoke_test.py

Requires ffmpeg/ffprobe on PATH.
"""

from __future__ import annotations

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
    total = 0
    deadline = time.time() + seconds
    try:
        with urllib.request.urlopen(url, timeout=seconds + 15) as resp:
            while time.time() < deadline:
                chunk = resp.read(16 * 1024)
                if not chunk:
                    break
                total += len(chunk)
    except Exception as exc:  # noqa: BLE001 - reported by the caller
        print(f"    (hold ended: {exc})")
    return total


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
        # That replay must also be packet-aligned and lead with a PAT.
        joiner = read_stream(f"{base}/stream/testcard", 188 * 400)
        check("mid-stream joiner starts on a packet boundary", is_aligned(joiner),
              f"sync offset={sync_offset(joiner)}")
        check("mid-stream joiner starts at a PAT", starts_at_pat(joiner),
              f"first packet pid={((joiner[1] & 0x1F) << 8 | joiner[2]) if len(joiner) > 2 else '-'}")

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
