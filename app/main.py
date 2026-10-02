"""FastAPI application: admin API, GUI, playlist and stream endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .audit import Auditor
from .epg import EpgStore, SourceStatus
from .manager import SessionManager, test_source
from .metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from .metrics import render as render_metrics
from .models import (
    Channel,
    EpgMappingRequest,
    EpgSource,
    Network,
    Profile,
    ReorderRequest,
    Settings,
    TestRequest,
)
from .playlist import build_playlist
from .session import NoCapacity, TooManyClients
from .store import ConfigStore

log = logging.getLogger(__name__)

CONFIG_PATH = Path(
    os.environ.get("STREAMS_MANAGER_CONFIG", Path.home() / ".config/streams-manager/config.json")
).expanduser()
AUTH_TOKEN = os.environ.get("STREAMS_MANAGER_TOKEN", "").strip()
PROTECT_STREAMS = os.environ.get("STREAMS_MANAGER_PROTECT_STREAMS", "").lower() in (
    "1",
    "true",
    "yes",
)
METRICS_PUBLIC = os.environ.get("STREAMS_MANAGER_METRICS_PUBLIC", "").lower() in (
    "1",
    "true",
    "yes",
)
STATIC_DIR = Path(__file__).parent / "static"

store = ConfigStore(CONFIG_PATH)
manager = SessionManager(get_network=lambda nid: store.network(nid))
epg = EpgStore(CONFIG_PATH.parent / "epg")
auditor = Auditor(manager.registry)

EPG_TICK_SECONDS = 300


async def _epg_scheduler() -> None:
    """Refresh each EPG source when its own interval says it is due."""
    while True:
        try:
            await asyncio.sleep(EPG_TICK_SECONDS)
            for source in store.epg_sources:
                if source.enabled and epg.due(source):
                    await epg.refresh(source, store.settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must outlive one bad source
            log.exception("EPG scheduler tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=os.environ.get("STREAMS_MANAGER_LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    store.load()
    if AUTH_TOKEN:
        log.info("admin API requires a token")
    else:
        log.warning(
            "STREAMS_MANAGER_TOKEN is not set - anyone who can reach this port can run "
            "arbitrary commands through it. Bind to localhost or set a token."
        )

    sources = store.epg_sources
    epg.load_cached(sources)
    epg.prune({s.id for s in sources})
    # Fetch anything stale in the background so startup is not blocked on a
    # slow guide script.
    stale = [s for s in sources if s.enabled and epg.due(s)]
    if stale:
        log.info("refreshing %d stale EPG source(s) in the background", len(stale))
        for source in stale:
            asyncio.create_task(epg.refresh(source, store.settings))
    scheduler = asyncio.create_task(_epg_scheduler(), name="epg-scheduler")

    yield

    scheduler.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler
    if auditor.running:
        await auditor.cancel()
    await manager.shutdown()


app = FastAPI(title="Streams Manager", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _token_from(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.query_params.get("token", "")


async def require_admin(request: Request) -> None:
    if not AUTH_TOKEN:
        return
    if _token_from(request) != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing token")


async def _stop_channel_after_edit(channel_id: str) -> None:
    try:
        await manager.stop_channel(channel_id, "channel edited")
    except Exception:  # noqa: BLE001 - surfaced in logs without breaking the save
        log.exception("failed to restart channel %s after edit", channel_id)


async def _stop_profile_after_edit(profile_id: str) -> None:
    try:
        await manager.stop_profile(profile_id, "profile edited")
    except Exception:  # noqa: BLE001 - surfaced in logs without breaking the save
        log.exception("failed to restart profile %s after edit", profile_id)


async def require_stream_access(request: Request) -> None:
    if not AUTH_TOKEN or not PROTECT_STREAMS:
        return
    if _token_from(request) != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing token")


admin = [Depends(require_admin)]


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/auth", include_in_schema=False)
async def auth_check(request: Request) -> dict:
    """Lets the GUI find out whether it needs a token before asking for one."""
    required = bool(AUTH_TOKEN)
    ok = not required or _token_from(request) == AUTH_TOKEN
    return {"required": required, "ok": ok}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _network_usage() -> list[dict]:
    usage = manager.registry.usage()
    return [
        {
            **n.model_dump(),
            "in_use": usage.get(n.id, 0),
            "channels": len(store.network_users(n.id)),
        }
        for n in store.networks
    ]


@app.get("/api/state", dependencies=admin)
async def get_state(request: Request) -> dict:
    return {
        "settings": store.settings.model_dump(),
        "channels": [c.model_dump() for c in store.sorted_channels()],
        "profiles": [p.model_dump() for p in store.profiles],
        "networks": _network_usage(),
        "sessions": [s.state().model_dump() for s in manager.all()],
        "base_url": store.settings.public_base_url or _base_url(request),
        "protect_streams": bool(AUTH_TOKEN and PROTECT_STREAMS),
        "config_path": str(CONFIG_PATH),
    }


@app.get("/api/sessions", dependencies=admin)
async def get_sessions() -> list[dict]:
    return [s.state().model_dump() for s in manager.all()]


@app.post("/api/sessions/{key}/stop", dependencies=admin)
async def stop_session(key: str) -> dict:
    if not await manager.stop(key):
        raise HTTPException(status_code=404, detail=f"no session {key!r}")
    return {"stopped": key}


@app.get("/api/sessions/{key}/log", dependencies=admin)
async def session_log(key: str) -> dict:
    session = manager.get(key)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no session {key!r}")
    return {"key": key, "lines": session.logs()}


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


@app.get("/api/channels", dependencies=admin)
async def list_channels() -> list[dict]:
    return [c.model_dump() for c in store.sorted_channels()]


@app.post("/api/channels", dependencies=admin, status_code=201)
async def create_channel(channel: Channel) -> dict:
    if channel.default_profile and not store.profile(channel.default_profile):
        raise HTTPException(status_code=400, detail=f"no profile {channel.default_profile!r}")
    try:
        await store.add_channel(channel)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return channel.model_dump()


@app.put("/api/channels/{channel_id}", dependencies=admin)
async def update_channel(
    channel_id: str, channel: Channel, background_tasks: BackgroundTasks
) -> dict:
    if channel.default_profile and not store.profile(channel.default_profile):
        raise HTTPException(status_code=400, detail=f"no profile {channel.default_profile!r}")
    try:
        await store.update_channel(channel_id, channel)
    except KeyError as exc:
        code = 409 if "already exists" in str(exc) else 404
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    # Persist first, then restart any live session in the background so the GUI
    # is not held open waiting for a stubborn source process to exit.
    background_tasks.add_task(_stop_channel_after_edit, channel_id)
    return channel.model_dump()


@app.delete("/api/channels/{channel_id}", dependencies=admin)
async def delete_channel(channel_id: str) -> dict:
    try:
        await store.delete_channel(channel_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await manager.stop_channel(channel_id, "channel deleted")
    return {"deleted": channel_id}


@app.post("/api/channels/reorder", dependencies=admin)
async def reorder_channels(body: ReorderRequest) -> list[dict]:
    return [c.model_dump() for c in await store.reorder_channels(body.ids)]


@app.post("/api/channels/{channel_id}/test", dependencies=admin)
async def test_channel(channel_id: str, body: TestRequest) -> dict:
    channel = store.channel(channel_id)
    if channel is None and not body.command:
        raise HTTPException(status_code=404, detail=f"no channel {channel_id!r}")

    source = None
    if channel is not None:
        source = channel.source(body.source) if body.source else channel.ordered_sources()[0]
        if source is None:
            raise HTTPException(
                status_code=404, detail=f"no source {body.source!r} on channel {channel_id!r}"
            )

    command = body.command or (source.command if source else None)
    if not command:
        raise HTTPException(status_code=400, detail="nothing to test")
    use_shell = body.use_shell if body.use_shell is not None else bool(source and source.use_shell)

    profile = None
    if body.profile:
        profile = store.profile(body.profile)
        if profile is None:
            raise HTTPException(status_code=400, detail=f"no profile {body.profile!r}")

    return await test_source(command, use_shell, profile, store.settings, body.duration)


@app.post("/api/test", dependencies=admin)
async def test_ad_hoc(body: TestRequest) -> dict:
    """Test a command that has not been saved as a channel yet."""
    if not body.command:
        raise HTTPException(status_code=400, detail="command is required")
    profile = None
    if body.profile:
        profile = store.profile(body.profile)
        if profile is None:
            raise HTTPException(status_code=400, detail=f"no profile {body.profile!r}")
    return await test_source(
        body.command, bool(body.use_shell), profile, store.settings, body.duration
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@app.get("/api/profiles", dependencies=admin)
async def list_profiles() -> list[dict]:
    return [p.model_dump() for p in store.profiles]


@app.post("/api/profiles", dependencies=admin, status_code=201)
async def create_profile(profile: Profile) -> dict:
    try:
        await store.add_profile(profile)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return profile.model_dump()


@app.put("/api/profiles/{profile_id}", dependencies=admin)
async def update_profile(
    profile_id: str, profile: Profile, background_tasks: BackgroundTasks
) -> dict:
    try:
        await store.update_profile(profile_id, profile)
    except KeyError as exc:
        code = 409 if "already exists" in str(exc) else 404
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    background_tasks.add_task(_stop_profile_after_edit, profile_id)
    return profile.model_dump()


@app.delete("/api/profiles/{profile_id}", dependencies=admin)
async def delete_profile(profile_id: str) -> dict:
    try:
        await store.delete_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await manager.stop_profile(profile_id, "profile deleted")
    return {"deleted": profile_id}


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------


@app.get("/api/networks", dependencies=admin)
async def list_networks() -> list[dict]:
    return _network_usage()


@app.post("/api/networks", dependencies=admin, status_code=201)
async def create_network(network: Network) -> dict:
    try:
        await store.add_network(network)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return network.model_dump()


@app.put("/api/networks/{network_id}", dependencies=admin)
async def update_network(network_id: str, network: Network) -> dict:
    try:
        await store.update_network(network_id, network)
    except KeyError as exc:
        code = 409 if "already exists" in str(exc) else 404
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    # Lowering the cap does not evict anyone; it applies to the next stream.
    return network.model_dump()


@app.delete("/api/networks/{network_id}", dependencies=admin)
async def delete_network(network_id: str) -> dict:
    users = store.network_users(network_id)
    try:
        await store.delete_network(network_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": network_id, "sources_uncapped_in": users}


# ---------------------------------------------------------------------------
# EPG
# ---------------------------------------------------------------------------


def _epg_state() -> dict:
    sources = store.epg_sources
    resolved = epg.resolve(store.channels)
    return {
        "sources": [
            {
                **s.model_dump(),
                **epg.status.get(s.id, SourceStatus(id=s.id)).as_dict(),
                "due": epg.due(s),
            }
            for s in sources
        ],
        "channels": sorted(
            (
                {
                    "id": c.id,
                    "label": c.label,
                    "icon": c.icon,
                    "programmes": c.programmes,
                    "source": c.source_id,
                }
                for c in epg.channels.values()
            ),
            key=lambda c: c["label"].lower(),
        ),
        "mapping": [
            {
                "channel_id": c.id,
                "channel_name": c.name,
                "guide_id": c.guide_id(),
                "epg_enabled": c.epg_enabled,
                "auto_mode": c.epg_auto,
                "assigned": c.epg_channel,
                "matched": resolved.get(c.id),
                "auto": epg.suggest(c) if c.epg_auto else None,
            }
            for c in store.sorted_channels()
        ],
        "matched": len(resolved),
        "total_channels": len(store.channels),
        "disk_bytes": epg.disk_usage(),
    }


@app.get("/api/epg", dependencies=admin)
async def get_epg_state() -> dict:
    return _epg_state()


@app.post("/api/epg/sources", dependencies=admin, status_code=201)
async def create_epg_source(source: EpgSource) -> dict:
    try:
        await store.add_epg_source(source)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    asyncio.create_task(epg.refresh(source, store.settings))
    return source.model_dump()


@app.put("/api/epg/sources/{source_id}", dependencies=admin)
async def update_epg_source(source_id: str, source: EpgSource) -> dict:
    try:
        await store.update_epg_source(source_id, source)
    except KeyError as exc:
        code = 409 if "already exists" in str(exc) else 404
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    if source.id != source_id:
        epg.forget(source_id)
    return source.model_dump()


@app.delete("/api/epg/sources/{source_id}", dependencies=admin)
async def delete_epg_source(source_id: str) -> dict:
    try:
        await store.delete_epg_source(source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    epg.forget(source_id)
    return {"deleted": source_id}


@app.post("/api/epg/sources/{source_id}/refresh", dependencies=admin)
async def refresh_epg_source(source_id: str) -> dict:
    source = store.epg_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"no EPG source {source_id!r}")
    status = await epg.refresh(source, store.settings)
    return status.as_dict()


@app.post("/api/epg/refresh", dependencies=admin)
async def refresh_all_epg() -> list[dict]:
    results = []
    for source in store.epg_sources:
        if source.enabled:
            results.append((await epg.refresh(source, store.settings)).as_dict())
    return results


@app.post("/api/epg/automap", dependencies=admin)
async def automap_epg(overwrite: bool = Query(default=False)) -> dict:
    """Assign every channel its best-guess XMLTV id.

    Without `overwrite`, channels that already have an explicit mapping keep it.
    """
    mapping: dict[str, Optional[str]] = {}
    for channel in store.channels:
        # A pinned mapping is a deliberate choice; only `overwrite` may undo it.
        if not channel.epg_auto and not overwrite:
            continue
        suggestion = epg.suggest(channel)
        if suggestion:
            mapping[channel.id] = suggestion
    if mapping:
        await store.set_epg_mapping(mapping)
    return {"mapped": len(mapping), "mapping": mapping}


@app.put("/api/epg/mapping", dependencies=admin)
async def set_epg_mapping(body: EpgMappingRequest) -> dict:
    unknown = [c for c in body.mapping.values() if c and c not in epg.channels]
    try:
        await store.set_epg_mapping(body.mapping, body.auto)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "pinned": len(body.mapping),
        "released": len(body.auto),
        # Not an error: a mapping may be set before the guide that defines it is
        # fetched. Reported so the GUI can point it out.
        "unknown": unknown,
    }


@app.api_route("/xmltv.xml", methods=["GET", "HEAD"])
@app.api_route("/epg.xml", methods=["GET", "HEAD"], include_in_schema=False)
async def xmltv(request: Request) -> Response:
    """The merged guide, with channel ids rewritten to match the playlist."""
    if AUTH_TOKEN and PROTECT_STREAMS and _token_from(request) != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing token")

    headers = {
        "Content-Disposition": 'inline; filename="xmltv.xml"',
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/xml", headers=headers)

    body = epg.generate(store.sorted_channels(), store.settings, store.epg_sources)
    return StreamingResponse(body, media_type="application/xml", headers=headers)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@app.get("/api/audit", dependencies=admin)
async def get_audit() -> dict:
    return auditor.state()


@app.post("/api/audit", dependencies=admin)
async def start_audit(
    duration: float = Query(default=8.0, ge=2, le=60, description="Seconds to run each source."),
    concurrency: int = Query(default=3, ge=1, le=16),
    channel: Optional[str] = Query(default=None, description="Audit one channel only."),
    include_disabled: bool = Query(default=False),
) -> dict:
    if channel and store.channel(channel) is None:
        raise HTTPException(status_code=404, detail=f"no channel {channel!r}")
    try:
        auditor.start(
            store.sorted_channels(),
            store.settings,
            duration=duration,
            concurrency=concurrency,
            channel_id=channel,
            include_disabled=include_disabled,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return auditor.state()


@app.post("/api/audit/stop", dependencies=admin)
async def stop_audit() -> dict:
    await auditor.cancel()
    return auditor.state()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@app.get("/api/settings", dependencies=admin)
async def get_settings() -> dict:
    return store.settings.model_dump()


@app.put("/api/settings", dependencies=admin)
async def put_settings(settings: Settings) -> dict:
    return (await store.update_settings(settings)).model_dump()


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------


@app.api_route("/playlist.m3u8", methods=["GET", "HEAD"], response_class=PlainTextResponse)
@app.api_route(
    "/playlist.m3u",
    methods=["GET", "HEAD"],
    response_class=PlainTextResponse,
    include_in_schema=False,
)
async def playlist(
    request: Request,
    profile: Optional[str] = Query(
        default=None,
        description="Bake a transcode profile into every URL. Omit to use each "
        "channel's own default.",
    ),
    group: Optional[str] = Query(
        default=None, description="Only channels belonging to this group."
    ),
) -> Response:
    if profile and not store.profile(profile):
        raise HTTPException(status_code=400, detail=f"no profile {profile!r}")

    channels = store.sorted_channels()
    if group:
        channels = [c for c in channels if group in c.groups]

    token = _token_from(request) if (AUTH_TOKEN and PROTECT_STREAMS) else None
    body = build_playlist(channels, store.settings, _base_url(request), profile, token)
    return PlainTextResponse(
        body,
        media_type="audio/x-mpegurl",
        headers={
            "Content-Disposition": 'inline; filename="playlist.m3u8"',
            "Cache-Control": "no-cache",
        },
    )


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@app.api_route(
    "/stream/{channel_id}",
    methods=["GET", "HEAD"],
    dependencies=[Depends(require_stream_access)],
)
async def stream(
    request: Request,
    channel_id: str,
    profile: Optional[str] = Query(
        default=None, description="Transcode profile id. Omit for the source as-is."
    ),
) -> Response:
    channel = store.channel(channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"no channel {channel_id!r}")
    if not channel.enabled:
        raise HTTPException(status_code=503, detail=f"channel {channel_id!r} is disabled")

    profile_id = profile if profile is not None else channel.default_profile
    profile_obj = None
    if profile_id:
        profile_obj = store.profile(profile_id)
        if profile_obj is None:
            raise HTTPException(status_code=400, detail=f"no profile {profile_id!r}")

    try:
        session = await manager.get_output(channel, profile_obj, store.settings)
    except NoCapacity as exc:
        # Same shape as a tuner backend refusing a subscription: the client is
        # told now rather than handed a stream that will never carry data.
        log.info("refused %s: %s", channel_id, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    container = profile_obj.container if profile_obj else "mpegts"
    media_type = {
        "mpegts": "video/mp2t",
        "mp4": "video/mp4",
        "matroska": "video/x-matroska",
        "webm": "video/webm",
        "mp3": "audio/mpeg",
        "adts": "audio/aac",
        "flv": "video/x-flv",
        "ogg": "audio/ogg",
    }.get(container, "application/octet-stream")

    if request.method == "HEAD":
        return Response(status_code=200, media_type=media_type)

    # attach() is an async generator, so its limit check would not run until the
    # first chunk is pulled - by which point the response status is already sent.
    if not session.can_accept():
        raise HTTPException(
            status_code=503,
            detail=f"channel {channel_id!r} is at its {session.client_limit} client limit",
        )

    async def guarded():
        try:
            async for chunk in session.attach(request.is_disconnected):
                yield chunk
        except TooManyClients:
            log.warning("rejected client for %s: raced past the client limit", session.key)
            return

    return StreamingResponse(
        guarded(),
        media_type=media_type,
        headers={
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.api_route("/metrics", methods=["GET", "HEAD"], response_class=PlainTextResponse)
async def metrics(request: Request) -> Response:
    """Prometheus scrape endpoint.

    Protected by the admin token when one is set, which Prometheus supplies via
    `authorization: credentials:`. Set STREAMS_MANAGER_METRICS_PUBLIC=1 to leave
    it open for a scraper that cannot send a header.
    """
    if AUTH_TOKEN and not METRICS_PUBLIC and _token_from(request) != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing token")
    body = render_metrics(store, manager, epg, app.version, auditor)
    return PlainTextResponse(body, media_type=METRICS_CONTENT_TYPE)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "channels": len(store.channels),
            "sessions": len(manager.all()),
            "clients": sum(s.client_count for s in manager.all()),
        }
    )
