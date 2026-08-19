"""Prometheus text exposition.

Per-session counters come from the stats ledger rather than the live session, so
they stay monotonic across a channel going idle and starting again — Prometheus
treats a counter that goes backwards as a reset and the graphs lie about it.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional

from .epg import EpgStore
from .manager import SessionManager
from .store import ConfigStore

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: Iterable[tuple[str, Optional[str]]]) -> str:
    rendered = [f'{k}="{_escape(v)}"' for k, v in pairs if v not in (None, "")]
    return "{" + ",".join(rendered) + "}" if rendered else ""


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def metric(
        self,
        name: str,
        value: float,
        labels: Iterable[tuple[str, Optional[str]]] = (),
        *,
        kind: str = "gauge",
        help_text: str = "",
    ) -> None:
        if name not in self._declared:
            self._declared.add(name)
            if help_text:
                self.lines.append(f"# HELP {name} {help_text}")
            self.lines.append(f"# TYPE {name} {kind}")
        if isinstance(value, bool):
            value = int(value)
        rendered = f"{value:.6g}" if isinstance(value, float) else str(value)
        self.lines.append(f"{name}{_labels(labels)} {rendered}")

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


def render(
    store: ConfigStore, manager: SessionManager, epg: EpgStore, version: str
) -> str:
    w = _Writer()
    now = time.time()

    w.metric(
        "streams_manager_build_info", 1, [("version", version)],
        help_text="Build information; the value is always 1.",
    )

    channels = store.channels
    w.metric(
        "streams_manager_channels", len(channels),
        help_text="Configured channels.",
    )
    w.metric(
        "streams_manager_channels_enabled", sum(1 for c in channels if c.enabled),
        help_text="Channels enabled for streaming.",
    )
    w.metric(
        "streams_manager_sources", sum(len(c.sources) for c in channels),
        help_text="Configured sources across all channels.",
    )
    w.metric("streams_manager_profiles", len(store.profiles), help_text="Transcode profiles.")

    sessions = manager.all()
    for kind in ("source", "transcode"):
        w.metric(
            "streams_manager_sessions",
            sum(1 for s in sessions if s.kind == kind),
            [("kind", kind)],
            help_text="Live sessions by kind.",
        )
    w.metric(
        "streams_manager_clients",
        sum(s.client_count for s in sessions),
        help_text="Connected stream clients.",
    )

    # -- live session gauges ------------------------------------------------
    states = {s.key: s.state() for s in sessions}
    for key, st in states.items():
        labels = [
            ("channel", st.channel_id),
            ("channel_name", st.channel_name),
            ("kind", st.kind),
            ("profile", st.profile or ""),
            ("source", st.source_id or ""),
            ("network", st.network or ""),
        ]
        w.metric(
            "streams_manager_session_up", 1 if st.status == "running" else 0, labels,
            help_text="1 when a session is running and data is flowing.",
        )
        w.metric("streams_manager_session_clients", st.clients, labels,
                 help_text="Clients attached to a session.")
        w.metric("streams_manager_session_bitrate_bps", st.bitrate_bps, labels,
                 help_text="Measured output bitrate over the last five seconds.")
        w.metric("streams_manager_session_uptime_seconds", round(st.uptime_seconds, 3), labels,
                 help_text="Seconds since data started flowing on this session.")

    # -- cumulative counters from the ledger --------------------------------
    for key, entry in manager.ledger.entries.items():
        labels = [
            ("channel", entry.channel_id),
            ("channel_name", entry.channel_name),
            ("kind", entry.kind),
            ("profile", entry.profile),
        ]
        w.metric("streams_manager_bytes_total", entry.bytes_out, labels, kind="counter",
                 help_text="Bytes sent to clients.")
        w.metric("streams_manager_connections_total", entry.connections, labels, kind="counter",
                 help_text="Times a source process was started.")
        w.metric("streams_manager_restarts_total", entry.restarts, labels, kind="counter",
                 help_text="Pipeline restarts, including source failovers.")
        w.metric("streams_manager_ts_packets_total", entry.ts.packets, labels, kind="counter",
                 help_text="MPEG-TS packets relayed.")
        w.metric(
            "streams_manager_ts_transport_errors_total", entry.ts.transport_errors, labels,
            kind="counter",
            help_text="Packets with the transport_error_indicator set (damaged upstream).",
        )
        w.metric(
            "streams_manager_ts_continuity_errors_total", entry.ts.continuity_errors, labels,
            kind="counter",
            help_text="Continuity counter discontinuities, i.e. lost packets.",
        )

    # -- per-session TS detail (resets with the session, hence gauges) -------
    for key, st in states.items():
        labels = [
            ("channel", st.channel_id),
            ("kind", st.kind),
            ("profile", st.profile or ""),
        ]
        w.metric("streams_manager_session_ts_scrambled", st.ts_scrambled, labels,
                 help_text="Scrambled packets seen in the current session.")
        w.metric("streams_manager_session_ts_discontinuities", st.ts_discontinuities, labels,
                 help_text="Signalled (expected) discontinuities in the current session.")
        w.metric("streams_manager_session_dropped_chunks", st.dropped_chunks, labels,
                 help_text="Chunks dropped to clients that could not keep up.")
        w.metric("streams_manager_session_input_dropped", st.input_dropped, labels,
                 help_text="Chunks dropped feeding a transcoder that could not keep up.")

    # -- networks ------------------------------------------------------------
    usage = manager.registry.usage()
    for network in store.networks:
        labels = [("network", network.id), ("network_name", network.name)]
        w.metric("streams_manager_network_streams", usage.get(network.id, 0), labels,
                 help_text="Upstream streams currently held on a network.")
        w.metric("streams_manager_network_max_streams", network.max_streams, labels,
                 help_text="Configured stream cap; 0 means unlimited.")
        w.metric("streams_manager_network_enabled", 1 if network.enabled else 0, labels,
                 help_text="1 when a network is enabled.")

    # -- EPG -----------------------------------------------------------------
    for source in store.epg_sources:
        status = epg.status.get(source.id)
        labels = [("source", source.id), ("source_name", source.name)]
        healthy = bool(status and status.last_refresh and not status.last_error)
        w.metric("streams_manager_epg_up", 1 if healthy else 0, labels,
                 help_text="1 when an EPG source last refreshed without error.")
        w.metric("streams_manager_epg_channels", status.channels if status else 0, labels,
                 help_text="Channels found in an EPG source.")
        w.metric("streams_manager_epg_programmes", status.programmes if status else 0, labels,
                 help_text="Programmes found in an EPG source.")
        w.metric(
            "streams_manager_epg_last_refresh_timestamp_seconds",
            status.last_refresh if status and status.last_refresh else 0, labels,
            help_text="Unix time of the last successful refresh.",
        )
        w.metric(
            "streams_manager_epg_age_seconds",
            round(now - status.last_refresh, 1) if status and status.last_refresh else 0,
            labels,
            help_text="Seconds since an EPG source last refreshed.",
        )

    mapped = len(epg.resolve(channels))
    w.metric("streams_manager_epg_mapped_channels", mapped,
             help_text="Channels with a matched guide.")
    w.metric("streams_manager_epg_cache_bytes", epg.disk_usage(),
             help_text="Disk used by cached XMLTV.")

    return w.render()
