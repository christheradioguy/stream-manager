"""M3U/M3U8 playlist generation."""

from __future__ import annotations

from urllib.parse import quote, urlencode

from .models import Channel, Settings


def _attr(value: str) -> str:
    """Quote an EXTINF attribute value (they are double-quoted, no escaping)."""
    return value.replace('"', "'").replace("\n", " ")


def build_playlist(
    channels: list[Channel],
    settings: Settings,
    base_url: str,
    profile: str | None = None,
    token: str | None = None,
) -> str:
    base = (settings.public_base_url or base_url).rstrip("/")

    # Clients such as TiVimate, IPTVnator and OTT Navigator read the guide URL
    # straight off the header, so pointing them at it saves a manual step.
    header = "#EXTM3U"
    if settings.epg_in_playlist:
        guide = f"{base}/xmltv.xml"
        if token:
            guide += "?" + urlencode({"token": token})
        header += f' url-tvg="{guide}" x-tvg-url="{guide}"'
    lines = [header]

    for ch in channels:
        if not ch.enabled:
            continue

        query: dict[str, str] = {}
        effective_profile = profile if profile is not None else ch.default_profile
        if effective_profile:
            query["profile"] = effective_profile
        if token:
            query["token"] = token
        url = f"{base}/stream/{quote(ch.id, safe='')}"
        if query:
            url += "?" + urlencode(query)

        # tvg-id must match the <channel id> in our XMLTV or clients show no guide.
        attrs = [f'tvg-id="{_attr(ch.guide_id())}"', f'tvg-name="{_attr(ch.name)}"']
        if ch.logo:
            attrs.append(f'tvg-logo="{_attr(ch.logo)}"')
        if ch.channel_number is not None:
            attrs.append(f'tvg-chno="{ch.channel_number}"')

        # M3U allows a single group-title per entry, so a channel that belongs to
        # several groups is emitted once per group, sharing one id and one URL.
        groups = ch.groups or [""]
        if not settings.playlist_multi_group:
            groups = groups[:1]
        for group_title in groups:
            entry = list(attrs)
            if group_title:
                entry.append(f'group-title="{_attr(group_title)}"')
            lines.append(f"#EXTINF:-1 {' '.join(entry)},{ch.name}")
            lines.append(url)

    return "\n".join(lines) + "\n"
