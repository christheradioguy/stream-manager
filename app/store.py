"""JSON-backed configuration store with atomic writes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from .models import Channel, Config, EpgSource, Network, Profile, Settings, sort_channels

log = logging.getLogger(__name__)

# ffmpeg probes a pipe for 5s by default before emitting anything, which shows up
# as dead air at the start of every transcoded channel. 2M/2s is enough to detect
# a normal TS reliably while cutting the delay roughly in half.
FAST_START = "-probesize 2M -analyzeduration 2M -fflags +genpts"

DEFAULT_PROFILES = [
    Profile(
        id="720p",
        name="720p H.264",
        description="Scale to 720p, H.264 veryfast, AAC stereo. Good general-purpose profile.",
        input_args=FAST_START,
        output_args=(
            "-map 0:v:0 -map 0:a:0? "
            "-c:v libx264 -preset veryfast -profile:v high -level 4.0 "
            "-vf scale=-2:720 -b:v 3000k -maxrate 3300k -bufsize 6000k -g 50 "
            "-c:a aac -b:a 128k -ac 2"
        ),
    ),
    Profile(
        id="480p",
        name="480p H.264",
        description="Low-bandwidth 480p for mobile / remote viewing.",
        input_args=FAST_START,
        output_args=(
            "-map 0:v:0 -map 0:a:0? "
            "-c:v libx264 -preset veryfast -vf scale=-2:480 "
            "-b:v 1200k -maxrate 1400k -bufsize 2400k -g 50 "
            "-c:a aac -b:a 96k -ac 2"
        ),
    ),
    Profile(
        id="audio",
        name="Audio only",
        description="Strip video, AAC 128k. Useful for radio or listening in the car.",
        input_args=FAST_START,
        output_args="-vn -c:a aac -b:a 128k -ac 2",
    ),
    Profile(
        id="remux",
        name="Remux (no re-encode)",
        description="Copy both streams into a clean MPEG-TS. Fixes broken timestamps cheaply.",
        input_args="-fflags +genpts",
        output_args="-c copy",
    ),
]


class ConfigStore:
    """Holds the config in memory, persists to a JSON file on every change."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._config = Config()

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            log.info("no config at %s, creating defaults", self.path)
            self._config = Config(profiles=list(DEFAULT_PROFILES))
            self._write()
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            self._config = Config.model_validate(raw)
            log.info(
                "loaded %d channels, %d profiles from %s",
                len(self._config.channels),
                len(self._config.profiles),
                self.path,
            )
        except Exception:
            log.exception("failed to load %s; backing it up and starting fresh", self.path)
            backup = self.path.with_suffix(self.path.suffix + ".broken")
            try:
                self.path.replace(backup)
                log.warning("previous config moved to %s", backup)
            except OSError:
                log.exception("could not move broken config aside")
            self._config = Config(profiles=list(DEFAULT_PROFILES))
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._config.model_dump(mode="json"), indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- reads (cheap, no lock: model objects are replaced, never mutated) --

    @property
    def config(self) -> Config:
        return self._config

    @property
    def settings(self) -> Settings:
        return self._config.settings

    @property
    def channels(self) -> list[Channel]:
        return list(self._config.channels)

    @property
    def profiles(self) -> list[Profile]:
        return list(self._config.profiles)

    def sorted_channels(self) -> list[Channel]:
        """Channels in display order, used by the GUI, playlist and guide alike."""
        return sort_channels(self._config.channels, self._config.settings.channel_sort)

    def channel(self, channel_id: str) -> Optional[Channel]:
        return next((c for c in self._config.channels if c.id == channel_id), None)

    def profile(self, profile_id: str) -> Optional[Profile]:
        return next((p for p in self._config.profiles if p.id == profile_id), None)

    @property
    def networks(self) -> list[Network]:
        return list(self._config.networks)

    def network(self, network_id: str) -> Optional[Network]:
        return next((n for n in self._config.networks if n.id == network_id), None)

    # -- writes ------------------------------------------------------------

    async def add_channel(self, channel: Channel) -> Channel:
        async with self._lock:
            if self.channel(channel.id):
                raise KeyError(f"channel id {channel.id!r} already exists")
            self._config.channels.append(channel)
            self._write()
        return channel

    async def update_channel(self, channel_id: str, channel: Channel) -> Channel:
        async with self._lock:
            idx = next(
                (i for i, c in enumerate(self._config.channels) if c.id == channel_id), None
            )
            if idx is None:
                raise KeyError(f"no channel {channel_id!r}")
            if channel.id != channel_id and self.channel(channel.id):
                raise KeyError(f"channel id {channel.id!r} already exists")
            self._config.channels[idx] = channel
            self._write()
        return channel

    async def delete_channel(self, channel_id: str) -> None:
        async with self._lock:
            before = len(self._config.channels)
            self._config.channels = [c for c in self._config.channels if c.id != channel_id]
            if len(self._config.channels) == before:
                raise KeyError(f"no channel {channel_id!r}")
            self._write()

    async def reorder_channels(self, ids: list[str]) -> list[Channel]:
        async with self._lock:
            by_id = {c.id: c for c in self._config.channels}
            ordered = [by_id[i] for i in ids if i in by_id]
            ordered += [c for c in self._config.channels if c.id not in set(ids)]
            self._config.channels = ordered
            self._write()
        return list(self._config.channels)

    async def add_profile(self, profile: Profile) -> Profile:
        async with self._lock:
            if self.profile(profile.id):
                raise KeyError(f"profile id {profile.id!r} already exists")
            self._config.profiles.append(profile)
            self._write()
        return profile

    async def update_profile(self, profile_id: str, profile: Profile) -> Profile:
        async with self._lock:
            idx = next(
                (i for i, p in enumerate(self._config.profiles) if p.id == profile_id), None
            )
            if idx is None:
                raise KeyError(f"no profile {profile_id!r}")
            if profile.id != profile_id and self.profile(profile.id):
                raise KeyError(f"profile id {profile.id!r} already exists")
            self._config.profiles[idx] = profile
            self._write()
        return profile

    async def delete_profile(self, profile_id: str) -> None:
        async with self._lock:
            before = len(self._config.profiles)
            self._config.profiles = [p for p in self._config.profiles if p.id != profile_id]
            if len(self._config.profiles) == before:
                raise KeyError(f"no profile {profile_id!r}")
            for ch in self._config.channels:
                if ch.default_profile == profile_id:
                    ch.default_profile = None
            self._write()

    async def add_network(self, network: Network) -> Network:
        async with self._lock:
            if self.network(network.id):
                raise KeyError(f"network id {network.id!r} already exists")
            self._config.networks.append(network)
            self._write()
        return network

    async def update_network(self, network_id: str, network: Network) -> Network:
        async with self._lock:
            idx = next(
                (i for i, n in enumerate(self._config.networks) if n.id == network_id), None
            )
            if idx is None:
                raise KeyError(f"no network {network_id!r}")
            if network.id != network_id and self.network(network.id):
                raise KeyError(f"network id {network.id!r} already exists")
            self._config.networks[idx] = network
            if network.id != network_id:
                for channel in self._config.channels:
                    for source in channel.sources:
                        if source.network == network_id:
                            source.network = network.id
            self._write()
        return network

    async def delete_network(self, network_id: str) -> None:
        async with self._lock:
            before = len(self._config.networks)
            self._config.networks = [n for n in self._config.networks if n.id != network_id]
            if len(self._config.networks) == before:
                raise KeyError(f"no network {network_id!r}")
            # Sources left pointing at it become uncapped rather than unusable.
            for channel in self._config.channels:
                for source in channel.sources:
                    if source.network == network_id:
                        source.network = None
            self._write()

    def network_users(self, network_id: str) -> list[str]:
        """Channels with at least one source on this network."""
        return [
            c.id
            for c in self._config.channels
            if any(s.network == network_id for s in c.sources)
        ]

    @property
    def epg_sources(self) -> list[EpgSource]:
        return list(self._config.epg_sources)

    def epg_source(self, source_id: str) -> Optional[EpgSource]:
        return next((s for s in self._config.epg_sources if s.id == source_id), None)

    async def add_epg_source(self, source: EpgSource) -> EpgSource:
        async with self._lock:
            if self.epg_source(source.id):
                raise KeyError(f"EPG source id {source.id!r} already exists")
            self._config.epg_sources.append(source)
            self._write()
        return source

    async def update_epg_source(self, source_id: str, source: EpgSource) -> EpgSource:
        async with self._lock:
            idx = next(
                (i for i, s in enumerate(self._config.epg_sources) if s.id == source_id), None
            )
            if idx is None:
                raise KeyError(f"no EPG source {source_id!r}")
            if source.id != source_id and self.epg_source(source.id):
                raise KeyError(f"EPG source id {source.id!r} already exists")
            self._config.epg_sources[idx] = source
            self._write()
        return source

    async def delete_epg_source(self, source_id: str) -> None:
        async with self._lock:
            before = len(self._config.epg_sources)
            self._config.epg_sources = [
                s for s in self._config.epg_sources if s.id != source_id
            ]
            if len(self._config.epg_sources) == before:
                raise KeyError(f"no EPG source {source_id!r}")
            self._write()

    async def set_epg_mapping(
        self,
        mapping: Optional[dict[str, Optional[str]]] = None,
        auto: Optional[list[str]] = None,
    ) -> None:
        """Pin or release guide mappings.

        Anything in ``mapping`` is pinned, and a null value pins "no guide" -
        otherwise clearing a wrong mapping would just let auto-matching put the
        same wrong guide straight back. Ids in ``auto`` go back to auto-matching.
        """
        async with self._lock:
            by_id = {c.id: c for c in self._config.channels}
            for channel_id, upstream in (mapping or {}).items():
                channel = by_id.get(channel_id)
                if channel is None:
                    raise KeyError(f"no channel {channel_id!r}")
                channel.epg_channel = upstream or None
                channel.epg_auto = False
            for channel_id in auto or []:
                channel = by_id.get(channel_id)
                if channel is None:
                    raise KeyError(f"no channel {channel_id!r}")
                channel.epg_auto = True
                channel.epg_channel = None
            self._write()

    async def update_settings(self, settings: Settings) -> Settings:
        async with self._lock:
            self._config.settings = settings
            self._write()
        return settings
