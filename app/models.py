"""Data models for channels, transcode profiles and settings."""

from __future__ import annotations

import re
import shlex
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validate_slug(value: str) -> str:
    value = value.strip()
    if not SLUG_RE.match(value):
        raise ValueError(
            "id must be 1-64 chars of letters, digits, dot, dash or underscore "
            "and start with a letter or digit"
        )
    return value


class Network(BaseModel):
    """A capacity pool that sources are assigned to.

    Mirrors Tvheadend's networks: whatever the sources on it represent - a
    provider account, a tuner, an upstream link - ``max_streams`` caps how many
    can be pulled at once. Requests beyond the cap are refused rather than
    queued, because the upstream would refuse them anyway.
    """

    id: str
    name: str
    description: str = ""
    max_streams: int = Field(default=0, ge=0, le=1000)  # 0 = unlimited
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _validate_slug(v)

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class Source(BaseModel):
    """One way of obtaining a channel.

    ``command`` is the full command line that writes an MPEG-TS stream to stdout,
    e.g. ``streamlink --stdout 'https://example/live' best`` or
    ``ffmpeg -i 'http://example/x.m3u8' -c copy -f mpegts pipe:1``.
    """

    id: str = ""
    name: str = ""
    command: str
    use_shell: bool = False
    enabled: bool = True

    # Which capacity pool this source draws from. None = uncapped.
    network: Optional[str] = None

    # Higher is tried first, matching Tvheadend. Ties break on list order.
    priority: int = Field(default=0, ge=-1000, le=1000)

    # Rebuild this source's audio timestamps from the stream's own clock.
    #
    # A few providers hand out streams whose audio timestamps are simply wrong -
    # minutes away from the video's and advancing at the wrong rate - while the
    # audio itself is complete and correctly interleaved with the picture it
    # belongs to. Tolerant players ignore the timestamps and sound fine; players
    # that pace themselves from the clock, which is most set-top and Android
    # ones, stall or drift on them. No ffmpeg filter repairs it, because by the
    # time a filter runs the demuxer has already paired the two streams using
    # the very timestamps that are wrong.
    #
    # Leave it off unless a source needs it - tools/tsclock.py says which do.
    fix_audio_timing: bool = False

    @field_validator("command")
    @classmethod
    def _parseable(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("command must not be empty")
        # Only enforce lexical validity; shell mode is checked by the shell itself.
        try:
            if not shlex.split(v):
                raise ValueError("command is empty")
        except ValueError as exc:
            raise ValueError(f"cannot parse command: {exc}") from exc
        return v

    def argv(self) -> list[str]:
        return shlex.split(self.command)

    def label(self) -> str:
        return self.name or self.id


class Channel(BaseModel):
    """A channel, served by one or more sources tried in priority order."""

    id: str
    name: str
    sources: list[Source] = Field(default_factory=list)
    enabled: bool = True

    # Playlist metadata
    groups: list[str] = Field(default_factory=list)
    logo: str = ""
    tvg_id: str = ""
    channel_number: Optional[int] = None

    # Guide mapping. While epg_auto is true the guide is matched by name on every
    # request; once a mapping is set by hand epg_auto goes false and epg_channel
    # is authoritative - including when it is None, which then means "no guide"
    # rather than "guess again".
    epg_auto: bool = True
    epg_channel: Optional[str] = None
    # Set to opt a channel out of the guide even when a match exists.
    epg_enabled: bool = True

    # Per-channel overrides (fall back to global settings when None)
    default_profile: Optional[str] = None
    max_clients: Optional[int] = None

    def guide_id(self) -> str:
        """The id clients see in both the playlist and the XMLTV."""
        return self.tvg_id or self.id

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_source(cls, data: Any) -> Any:
        """Accept the old single-command shape.

        Configs written before channels gained multiple sources carry `command`
        and `use_shell` at the top level. Fold them into one source so existing
        files keep working untouched.
        """
        if not isinstance(data, dict):
            return data
        if data.get("command") and not data.get("sources"):
            data = dict(data)
            data["sources"] = [
                {
                    "id": "src1",
                    "name": "Primary",
                    "command": data.pop("command"),
                    "use_shell": data.pop("use_shell", False),
                }
            ]
        # A mapping stored before epg_auto existed was set by hand, so keep it
        # pinned rather than letting auto-matching take it back.
        if data.get("epg_channel") and "epg_auto" not in data:
            data = dict(data)
            data["epg_auto"] = False
        # Channels written before a channel could be in several groups carry a
        # single `group` string.
        if "group" in data and not data.get("groups"):
            data = dict(data)
            single = (data.pop("group") or "").strip()
            data["groups"] = [single] if single else []
        return data

    @field_validator("groups")
    @classmethod
    def _clean_groups(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for group in v:
            group = group.strip()
            if group and group not in seen:
                seen.append(group)
        return seen

    @model_validator(mode="after")
    def _check_sources(self) -> "Channel":
        if not self.sources:
            raise ValueError("a channel needs at least one source")
        seen: set[str] = set()
        for index, source in enumerate(self.sources, start=1):
            if not source.id:
                source.id = f"src{index}"
            if source.id in seen:
                raise ValueError(f"duplicate source id {source.id!r}")
            seen.add(source.id)
        return self

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _validate_slug(v)

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    def ordered_sources(self) -> list[Source]:
        """Enabled sources, best first: highest priority, then listed order."""
        numbered = [(i, s) for i, s in enumerate(self.sources) if s.enabled]
        numbered.sort(key=lambda pair: (-pair[1].priority, pair[0]))
        return [s for _, s in numbered]

    def source(self, source_id: str) -> Optional[Source]:
        return next((s for s in self.sources if s.id == source_id), None)


def sort_channels(channels: list[Channel], mode: str = "number") -> list[Channel]:
    """Order channels for display and for the playlist.

    ``number`` puts numbered channels in ascending order and any without a number
    after them, keeping the configured order as the tiebreak so equal numbers stay
    stable. ``manual`` leaves the configured order alone.
    """
    if mode == "manual":
        return list(channels)
    if mode == "name":
        return sorted(channels, key=lambda c: (c.name.lower(), c.id))
    return [
        channel
        for _, channel in sorted(
            enumerate(channels),
            key=lambda pair: (
                pair[1].channel_number is None,
                pair[1].channel_number if pair[1].channel_number is not None else 0,
                pair[0],
            ),
        )
    ]


class Profile(BaseModel):
    """An ffmpeg transcode profile applied to a channel's output on request.

    The effective command is::

        ffmpeg <global> <input_args> -i pipe:0 <output_args> -f <container> pipe:1
    """

    id: str
    name: str
    description: str = ""
    input_args: str = ""
    output_args: str = "-c:v libx264 -preset veryfast -c:a aac"
    container: str = "mpegts"

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _validate_slug(v)

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("input_args", "output_args")
    @classmethod
    def _parseable(cls, v: str) -> str:
        try:
            shlex.split(v)
        except ValueError as exc:
            raise ValueError(f"cannot parse arguments: {exc}") from exc
        return v

    @field_validator("container")
    @classmethod
    def _check_container(cls, v: str) -> str:
        v = v.strip() or "mpegts"
        if not re.match(r"^[A-Za-z0-9_,-]+$", v):
            raise ValueError("invalid container/muxer name")
        return v

    def ffmpeg_argv(self, ffmpeg_bin: str, loglevel: str = "warning") -> list[str]:
        return [
            ffmpeg_bin,
            "-hide_banner",
            "-nostdin",
            # Progress goes to stderr whatever the log level, because how far
            # ahead of real time the encoder is running is a health signal worth
            # having: sustained below 1.0 it cannot keep up and viewers starve.
            # It is read for the metrics, not printed for a human.
            "-stats",
            "-loglevel",
            loglevel,
            *shlex.split(self.input_args),
            "-i",
            "pipe:0",
            *shlex.split(self.output_args),
            "-f",
            self.container,
            "pipe:1",
        ]


class EpgSource(BaseModel):
    """Where a chunk of XMLTV comes from.

    ``command`` suits a script that assembles several upstream guides and writes
    the result to stdout, which is how most people already feed Tvheadend.
    Gzipped output is detected and decompressed either way.
    """

    id: str
    name: str
    kind: Literal["command", "url", "file"] = "command"
    command: str = ""
    url: str = ""
    path: str = ""
    use_shell: bool = False
    enabled: bool = True
    refresh_hours: float = Field(default=12.0, ge=0.25, le=168)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        return _validate_slug(v)

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @model_validator(mode="after")
    def _check_target(self) -> "EpgSource":
        if self.kind == "command":
            if not self.command.strip():
                raise ValueError("a command EPG source needs a command")
            try:
                shlex.split(self.command)
            except ValueError as exc:
                raise ValueError(f"cannot parse command: {exc}") from exc
        elif self.kind == "url":
            if not self.url.strip().lower().startswith(("http://", "https://")):
                raise ValueError("url must start with http:// or https://")
        elif self.kind == "file" and not self.path.strip():
            raise ValueError("a file EPG source needs a path")
        return self

    def target(self) -> str:
        return {"command": self.command, "url": self.url, "file": self.path}[self.kind]


class Settings(BaseModel):
    """Global runtime settings, editable from the GUI."""

    # How long a source keeps running after the last client disconnects.
    # Prevents a restart storm when a player reconnects (channel zapping).
    linger_seconds: float = Field(default=15.0, ge=0, le=600)

    # Bytes of recent stream data replayed to a newly attached client so
    # playback starts without waiting for the next keyframe.
    prebuffer_bytes: int = Field(default=2 * 1024 * 1024, ge=0, le=64 * 1024 * 1024)

    # Per-client outbound queue. A client that cannot keep up drops data
    # rather than stalling every other viewer of the same source.
    client_queue_chunks: int = Field(default=256, ge=8, le=8192)

    # How long to wait for a consumer whose queue is full before dropping its
    # oldest data. Waiting stops the source pipe being read, so the source
    # process blocks and a burst is absorbed upstream instead of being thrown
    # away - which is what a faster-than-real-time input needs. 0 restores the
    # old drop-immediately behaviour.
    backpressure_seconds: float = Field(default=20.0, ge=0, le=300)

    # Restart the source if it stops producing data mid-stream.
    stall_timeout_seconds: float = Field(default=30.0, ge=5, le=600)

    # Time allowed for a freshly spawned source to produce its first byte.
    # Separate from the stall timeout because connecting, authenticating and
    # resolving a stream is far slower than keeping one running.
    startup_timeout_seconds: float = Field(default=60.0, ge=5, le=600)

    # Automatically restart a source that exits while clients are attached.
    auto_restart: bool = True
    restart_backoff_seconds: float = Field(default=2.0, ge=0.5, le=60)

    # Pause before reopening a source that ended *after streaming normally*,
    # e.g. an HLS token or playlist window rotating. Separate from the failure
    # backoff because this is an expected event, and reopening the instant the
    # upstream closed is what turns a rotation into a burst of connection
    # resets. Raise it for a proxy that needs a moment to re-authenticate.
    reconnect_delay_seconds: float = Field(default=3.0, ge=0, le=120)
    max_restart_backoff_seconds: float = Field(default=30.0, ge=1, le=300)

    # Stop retrying after this many consecutive failures. 0 = never stop.
    give_up_after_failures: int = Field(default=0, ge=0, le=100)

    # How long a process gets to exit on SIGTERM before it is killed.
    terminate_grace_seconds: float = Field(default=5.0, ge=1, le=60)

    # 0 = unlimited.
    default_max_clients: int = Field(default=0, ge=0, le=1000)

    # Base URL used when generating playlist entries. Empty = derive from request.
    public_base_url: str = ""

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    ffmpeg_loglevel: str = "warning"

    # Lines of stderr kept per source for the GUI log view.
    log_lines: int = Field(default=200, ge=10, le=5000)

    # Count transport and continuity errors on every packet. Cheap, but it is a
    # per-packet Python loop, so it can be turned off on a very busy server.
    ts_analysis: bool = True

    # How channels are ordered in the GUI, the playlist and the guide.
    channel_sort: Literal["number", "name", "manual"] = "number"


    # Guide trimming, so clients are not handed weeks of history.
    epg_past_hours: float = Field(default=12.0, ge=0, le=720)
    epg_future_days: float = Field(default=14.0, ge=0, le=90)  # 0 = no limit
    # Advertise the guide URL in the playlist so clients discover it themselves.
    epg_in_playlist: bool = True

    # M3U carries one group-title per entry, so a channel in several groups is
    # listed once per group. Clients that key on tvg-id (TiVimate, OTT Navigator)
    # show it in each group; turn this off for a client that instead shows one
    # duplicate channel per entry, and only the first group is used.
    playlist_multi_group: bool = True

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")


class Config(BaseModel):
    """The whole persisted configuration."""

    version: int = 2
    settings: Settings = Field(default_factory=Settings)
    channels: list[Channel] = Field(default_factory=list)
    profiles: list[Profile] = Field(default_factory=list)
    networks: list[Network] = Field(default_factory=list)
    epg_sources: list[EpgSource] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ReorderRequest(BaseModel):
    ids: list[str]


class EpgMappingRequest(BaseModel):
    """Pin guide mappings, and/or release channels back to auto-matching.

    A null value in ``mapping`` pins "no guide" for that channel, which is
    different from listing it in ``auto``.
    """

    mapping: dict[str, Optional[str]] = Field(default_factory=dict)
    auto: list[str] = Field(default_factory=list)


class TestRequest(BaseModel):
    command: Optional[str] = None
    use_shell: Optional[bool] = None
    profile: Optional[str] = None
    # Which of the channel's sources to test. Defaults to the highest priority.
    source: Optional[str] = None
    duration: float = Field(default=8.0, ge=1, le=30)


class SessionState(BaseModel):
    key: str
    kind: Literal["source", "transcode"] = "source"
    channel_id: str
    channel_name: str
    profile: Optional[str]
    status: Literal["starting", "running", "restarting", "stopping", "stopped", "error"]
    clients: int
    # Subscribers of any kind: viewers plus attached transcoders.
    consumers: int = 0
    bytes_out: int
    bitrate_bps: int
    started_at: Optional[float]
    # Seconds since data started flowing, not since the process was spawned.
    uptime_seconds: float
    restarts: int
    # Clean re-opens after a good run, as distinct from failures.
    reconnects: int = 0
    last_end: str = ""
    dropped_chunks: int
    # Chunks dropped on the way *into* a transcoder, i.e. the encoder is too slow.
    input_dropped: int = 0
    last_error: str = ""
    pids: list[int] = Field(default_factory=list)
    # MPEG-TS health for this session.
    ts_packets: int = 0
    ts_transport_errors: int = 0
    ts_continuity_errors: int = 0
    ts_scrambled: int = 0
    ts_discontinuities: int = 0
    # Holes in the presentation timeline - content that never arrived. Unlike
    # the two error counts, this survives a source that re-muxes.
    ts_content_gaps: int = 0
    ts_content_lost: float = 0.0
    ts_error_pids: list[dict[str, int]] = Field(default_factory=list)

    # What the source tool itself reported this session, by kind, and how far
    # ahead of real time it says it is running. Below 1.0 sustained means it
    # cannot keep up. See tsstats.classify_log.
    events: dict[str, int] = Field(default_factory=dict)
    speed: float = 0.0

    # Which of the channel's sources is currently in use, and the pool it draws on.
    source_id: Optional[str] = None
    source_name: str = ""
    network: Optional[str] = None
    # Sources tried and failed during the current failover cycle.
    failed_sources: list[str] = Field(default_factory=list)

    model_config: dict[str, Any] = {"extra": "forbid"}
