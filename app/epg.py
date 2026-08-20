"""XMLTV ingest, channel matching and guide generation.

Each EPG source is fetched to a cache file on disk and indexed for its channel
list. Guide requests stream straight off those files, rewriting each programme's
``channel`` attribute to the id the playlist advertises, so a client only ever
sees ids it can match.

Keeping the raw XMLTV on disk rather than in memory matters: a fortnight of
listings for a few hundred channels is tens of megabytes, and it would otherwise
be resident for the life of the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import logging
import os
import re
import shlex
import shutil
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional
from xml.sax.saxutils import escape, quoteattr

from .models import Channel, EpgSource, Settings

log = logging.getLogger(__name__)

GZIP_MAGIC = b"\x1f\x8b"
FETCH_TIMEOUT = 120
MAX_FETCH_BYTES = 512 * 1024 * 1024

# XMLTV times look like "20260818120000 +0100"; the offset is optional.
XMLTV_TIME = re.compile(r"^(\d{14})(?:\s*([+-]\d{4}))?")


def parse_xmltv_time(value: str) -> Optional[float]:
    """Unix timestamp for an XMLTV timestamp, or None if unparseable."""
    if not value:
        return None
    match = XMLTV_TIME.match(value.strip())
    if not match:
        return None
    stamp, offset = match.groups()
    try:
        parts = (
            int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]),
            int(stamp[8:10]), int(stamp[10:12]), int(stamp[12:14]),
        )
    except ValueError:
        return None
    import calendar
    import datetime as _dt

    try:
        naive = _dt.datetime(*parts)
    except ValueError:
        return None
    seconds = calendar.timegm(naive.timetuple())
    if offset:
        sign = 1 if offset[0] == "+" else -1
        seconds -= sign * (int(offset[1:3]) * 3600 + int(offset[3:5]) * 60)
    return float(seconds)


def normalise(text: str) -> str:
    """Squash a channel name to something comparable across providers."""
    text = text.lower()
    text = re.sub(r"\b(hd|uhd|4k|fhd|sd|tv|channel|dt)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


@dataclass
class EpgChannel:
    """A channel discovered in an upstream XMLTV file."""

    id: str
    display_names: list[str] = field(default_factory=list)
    icon: str = ""
    source_id: str = ""
    programmes: int = 0

    @property
    def label(self) -> str:
        return self.display_names[0] if self.display_names else self.id


@dataclass
class SourceStatus:
    id: str
    last_refresh: Optional[float] = None
    last_error: str = ""
    channels: int = 0
    programmes: int = 0
    bytes: int = 0
    refreshing: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "last_refresh": self.last_refresh,
            "age_seconds": (time.time() - self.last_refresh) if self.last_refresh else None,
            "last_error": self.last_error,
            "channels": self.channels,
            "programmes": self.programmes,
            "bytes": self.bytes,
            "refreshing": self.refreshing,
        }


class EpgStore:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.status: dict[str, SourceStatus] = {}
        self.channels: dict[str, EpgChannel] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- paths -------------------------------------------------------------

    def cache_path(self, source_id: str) -> Path:
        return self.cache_dir / f"{source_id}.xml"

    def _status(self, source_id: str) -> SourceStatus:
        return self.status.setdefault(source_id, SourceStatus(id=source_id))

    def _lock(self, source_id: str) -> asyncio.Lock:
        return self._locks.setdefault(source_id, asyncio.Lock())

    # -- lifecycle ---------------------------------------------------------

    def load_cached(self, sources: list[EpgSource]) -> None:
        """Rebuild the channel index from cache files left by a previous run."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.channels = {}
        for source in sources:
            path = self.cache_path(source.id)
            if not path.exists():
                continue
            status = self._status(source.id)
            try:
                channels, programmes = self._index(path, source.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read cached EPG for %s: %s", source.id, exc)
                continue
            self._merge_channels(channels)
            status.last_refresh = path.stat().st_mtime
            status.channels = len(channels)
            status.programmes = programmes
            status.bytes = path.stat().st_size
            log.info(
                "EPG %s: %d channels, %d programmes from cache",
                source.id, len(channels), programmes,
            )

    def forget(self, source_id: str) -> None:
        self.status.pop(source_id, None)
        self._locks.pop(source_id, None)
        with contextlib.suppress(OSError):
            self.cache_path(source_id).unlink()
        self.channels = {c.id: c for c in self.channels.values() if c.source_id != source_id}

    def due(self, source: EpgSource) -> bool:
        status = self.status.get(source.id)
        if status is None or status.last_refresh is None:
            return True
        return (time.time() - status.last_refresh) >= source.refresh_hours * 3600

    # -- refresh -----------------------------------------------------------

    async def refresh(self, source: EpgSource, settings: Settings) -> SourceStatus:
        status = self._status(source.id)
        async with self._lock(source.id):
            status.refreshing = True
            tmp = self.cache_path(source.id).with_suffix(".tmp")
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                raw = await self._fetch(source)
                if not raw:
                    raise RuntimeError("source produced no data")
                if raw[:2] == GZIP_MAGIC:
                    raw = await asyncio.to_thread(gzip.decompress, raw)
                tmp.write_bytes(raw)

                channels, programmes = await asyncio.to_thread(self._index, tmp, source.id)
                if not channels and not programmes:
                    raise RuntimeError("no <channel> or <programme> elements found")

                os.replace(tmp, self.cache_path(source.id))
                # Drop this source's old entries before merging the new ones.
                self.channels = {
                    c.id: c for c in self.channels.values() if c.source_id != source.id
                }
                self._merge_channels(channels)
                status.last_refresh = time.time()
                status.last_error = ""
                status.channels = len(channels)
                status.programmes = programmes
                status.bytes = len(raw)
                log.info(
                    "EPG %s refreshed: %d channels, %d programmes, %.1f MB",
                    source.id, len(channels), programmes, len(raw) / 1e6,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced in the GUI
                status.last_error = str(exc)
                log.warning("EPG %s refresh failed: %s", source.id, exc)
                with contextlib.suppress(OSError):
                    tmp.unlink()
            finally:
                status.refreshing = False
        return status

    async def _fetch(self, source: EpgSource) -> bytes:
        if source.kind == "command":
            return await self._run_command(source)
        if source.kind == "file":
            path = Path(source.path).expanduser()
            if not path.exists():
                raise FileNotFoundError(f"no such file: {path}")
            return await asyncio.to_thread(path.read_bytes)
        return await asyncio.to_thread(self._download, source.url)

    @staticmethod
    def _download(url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "streams-manager/1.0", "Accept-Encoding": "gzip"},
        )
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as resp:
                data = resp.read(MAX_FETCH_BYTES + 1)
                if len(data) > MAX_FETCH_BYTES:
                    raise RuntimeError("guide exceeds 512 MB")
                if resp.headers.get("Content-Encoding") == "gzip" and data[:2] == GZIP_MAGIC:
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"could not fetch: {exc.reason}") from exc

    @staticmethod
    async def _run_command(source: EpgSource) -> bytes:
        if source.use_shell:
            proc = await asyncio.create_subprocess_shell(
                source.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(source.command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=FETCH_TIMEOUT)
        except asyncio.TimeoutError as exc:
            # proc.pid is the group id (start_new_session=True), and unlike
            # os.getpgid() it still works once the child has been reaped.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, 9)
            raise RuntimeError(f"command timed out after {FETCH_TIMEOUT}s") from exc

        if proc.returncode:
            detail = stderr.decode("utf-8", "replace").strip().splitlines()
            tail = detail[-1] if detail else ""
            raise RuntimeError(f"command exited {proc.returncode}{': ' + tail if tail else ''}")
        return stdout

    # -- indexing ----------------------------------------------------------

    @staticmethod
    def _index(path: Path, source_id: str) -> tuple[dict[str, EpgChannel], int]:
        """Read a cached file for its channel list and programme counts."""
        channels: dict[str, EpgChannel] = {}
        programmes = 0
        for _, elem in ET.iterparse(str(path), events=("end",)):
            if elem.tag == "channel":
                cid = (elem.get("id") or "").strip()
                if cid:
                    entry = channels.setdefault(cid, EpgChannel(id=cid, source_id=source_id))
                    entry.display_names = [
                        (d.text or "").strip() for d in elem.findall("display-name") if d.text
                    ]
                    icon = elem.find("icon")
                    if icon is not None:
                        entry.icon = icon.get("src", "")
                elem.clear()
            elif elem.tag == "programme":
                cid = (elem.get("channel") or "").strip()
                if cid:
                    entry = channels.setdefault(cid, EpgChannel(id=cid, source_id=source_id))
                    entry.programmes += 1
                    programmes += 1
                elem.clear()
        return channels, programmes

    def _merge_channels(self, channels: dict[str, EpgChannel]) -> None:
        for cid, entry in channels.items():
            existing = self.channels.get(cid)
            if existing is None:
                self.channels[cid] = entry
            else:
                # Same id from two sources: keep the richer metadata, sum counts.
                existing.programmes += entry.programmes
                if not existing.display_names:
                    existing.display_names = entry.display_names
                if not existing.icon:
                    existing.icon = entry.icon

    # -- matching ----------------------------------------------------------

    def suggest(self, channel: Channel) -> Optional[str]:
        """Best guess at the upstream XMLTV id for one of our channels.

        Tried in descending confidence: the configured tvg-id, our channel id,
        then a normalised name comparison.
        """
        for exact in (channel.tvg_id, channel.id):
            if exact and exact in self.channels:
                return exact

        wanted = {normalise(channel.name), normalise(channel.id)}
        if channel.tvg_id:
            wanted.add(normalise(channel.tvg_id))
        wanted.discard("")

        for cid, entry in self.channels.items():
            if normalise(cid) in wanted:
                return cid
            for name in entry.display_names:
                if normalise(name) in wanted:
                    return cid
        return None

    def resolve(self, channels: list[Channel]) -> dict[str, str]:
        """channel.id -> upstream XMLTV id, for channels that have a match."""
        resolved: dict[str, str] = {}
        for channel in channels:
            if not channel.enabled or not channel.epg_enabled:
                continue
            upstream = self.suggest(channel) if channel.epg_auto else channel.epg_channel
            if upstream and upstream in self.channels:
                resolved[channel.id] = upstream
        return resolved

    # -- generation --------------------------------------------------------

    def generate(
        self, channels: list[Channel], settings: Settings, sources: list[EpgSource]
    ) -> Iterator[bytes]:
        """Stream a merged XMLTV document for the mapped channels."""
        resolved = self.resolve(channels)
        by_id = {c.id: c for c in channels}

        # One upstream channel may feed several of ours (e.g. an HD and SD entry).
        remap: dict[str, list[str]] = {}
        for channel_id, upstream in resolved.items():
            remap.setdefault(upstream, []).append(by_id[channel_id].guide_id())

        now = time.time()
        oldest = now - settings.epg_past_hours * 3600 if settings.epg_past_hours else None
        newest = now + settings.epg_future_days * 86400 if settings.epg_future_days else None

        yield b'<?xml version="1.0" encoding="UTF-8"?>\n'
        yield b'<!DOCTYPE tv SYSTEM "xmltv.dtd">\n'
        yield b'<tv generator-info-name="streams-manager">\n'

        # Channel elements come from our own config, so the names and icons match
        # the playlist exactly rather than whatever the upstream guide called them.
        for channel_id, _ in resolved.items():
            channel = by_id[channel_id]
            yield (
                f"  <channel id={quoteattr(channel.guide_id())}>\n"
                f"    <display-name>{escape(channel.name)}</display-name>\n"
            ).encode()
            if channel.channel_number is not None:
                yield f"    <display-name>{channel.channel_number}</display-name>\n".encode()
            if channel.logo:
                yield f"    <icon src={quoteattr(channel.logo)} />\n".encode()
            yield b"  </channel>\n"

        for source in sources:
            path = self.cache_path(source.id)
            if not source.enabled or not path.exists():
                continue
            try:
                yield from self._programmes(path, remap, oldest, newest)
            except ET.ParseError as exc:
                log.warning("EPG %s is malformed, skipping: %s", source.id, exc)

        yield b"</tv>\n"

    @staticmethod
    def _programmes(
        path: Path,
        remap: dict[str, list[str]],
        oldest: Optional[float],
        newest: Optional[float],
    ) -> Iterator[bytes]:
        for _, elem in ET.iterparse(str(path), events=("end",)):
            if elem.tag != "programme":
                if elem.tag == "channel":
                    elem.clear()
                continue
            targets = remap.get((elem.get("channel") or "").strip())
            if not targets:
                elem.clear()
                continue

            if oldest is not None:
                stop = parse_xmltv_time(elem.get("stop", ""))
                if stop is not None and stop < oldest:
                    elem.clear()
                    continue
            if newest is not None:
                start = parse_xmltv_time(elem.get("start", ""))
                if start is not None and start > newest:
                    elem.clear()
                    continue

            for target in targets:
                elem.set("channel", target)
                yield ET.tostring(elem, encoding="unicode").encode("utf-8")
                yield b"\n"
            elem.clear()

    # -- housekeeping ------------------------------------------------------

    def prune(self, keep: set[str]) -> None:
        """Delete cache files for sources that no longer exist."""
        if not self.cache_dir.exists():
            return
        for path in self.cache_dir.glob("*.xml"):
            if path.stem not in keep:
                with contextlib.suppress(OSError):
                    path.unlink()
                    log.info("removed stale EPG cache %s", path.name)

    def disk_usage(self) -> int:
        if not self.cache_dir.exists():
            return 0
        return sum(p.stat().st_size for p in self.cache_dir.glob("*.xml"))

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            shutil.rmtree(self.cache_dir)
        self.channels = {}
        self.status = {}
