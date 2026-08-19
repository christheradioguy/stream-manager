# Streams Manager

A stream source manager with a web GUI, built as a lighter replacement for
Tvheadend when what you actually need is "run these commands, hand out an m3u8".

Each channel is a **command that writes MPEG-TS to stdout** — `streamlink`,
`ffmpeg`, `yt-dlp`, a shell pipeline, anything. The server reads that pipe once
and fans the bytes out to every viewer, optionally transcoding on the way if the
client asks for a profile.

```
                      ┌── /stream/bbc1              → viewer 1 ─┐
streamlink ──pipe──►  │                                         ├ ONE upstream
                      ├── /stream/bbc1              → viewer 2 ─┤  connection
                      │                                         │  for all of
                      └── ffmpeg ──┬─ ?profile=480p → viewer 3 ─┤  them
                                   └─ ?profile=480p → viewer 4 ─┘
```

The source is opened once **per channel**, not per profile. Switching profile,
or watching the same channel at two different qualities, never opens a second
connection to your provider — which matters, because most of them cap
concurrent streams and refuse the second one.

## Features

- **GUI configuration** — add, edit, reorder and delete stream sources in the browser.
- **Pipe input** — the full source command is supplied per source, with an
  optional shell mode for pipelines.
- **Multiple sources per channel** — tried in priority order, failing over to the
  next one instantly when a source dies.
- **Networks** — capacity pools that cap how many streams a provider, tuner or
  link will serve at once. Over the cap, requests are refused rather than queued.
- **On-demand transcoding** — `?profile=<id>` runs the output through an ffmpeg
  profile you define in the GUI. No profile means untouched passthrough.
- **m3u8 output** — `/playlist.m3u8` for Jellyfin, Kodi, VLC, Emby, or any IPTV client,
  with channels in as many groups as you like.
- **XMLTV guide** — merges any number of EPG sources, maps them onto your channels
  and serves `/xmltv.xml` with ids rewritten to match the playlist.
- **One upstream connection per channel** — however many viewers, whatever mix
  of profiles. Transcoders are consumers of the shared source, not extra tuners.
- **Packet-aligned output** — clients always start on a 188-byte TS boundary, and
  one joining a running channel starts at a PAT, so the demuxer gets program
  tables immediately instead of resyncing from a random offset.
- **Nothing runs until someone watches** — sources start on the first request and
  stop after a configurable idle period.
- **Self-healing** — dead sources restart with exponential backoff; stalled ones
  are detected and restarted; a failing transcode profile never takes the
  upstream connection down with it.
- **Stream health** — transport and continuity errors counted per session and per
  PID, so a flaky provider is visible rather than guessed at.
- **Prometheus metrics** — `/metrics` exposes sessions, throughput, TS errors,
  network capacity and EPG freshness.
- **Live status** — per-session state, client count, bitrate, restarts and a
  tail of the command's stderr, all in the GUI.
- **Source tester** — run a command for a few seconds and see ffprobe's verdict
  before you save it.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py            # http://127.0.0.1:8409
```

`ffmpeg` and `ffprobe` must be on `PATH` (paths are configurable in Settings).
Whatever your source commands use — `streamlink`, `yt-dlp` — needs to be
installed too.

To expose it on the LAN:

```bash
.venv/bin/python run.py --host 0.0.0.0 --port 8409
```

### As a service

Edit `streams-manager.service` (paths, user, token) and install it:

```bash
sudo useradd --system --home /var/lib/streams-manager --create-home streams
sudo cp -r . /opt/streams-manager
sudo cp streams-manager.service /etc/systemd/system/
sudo systemctl enable --now streams-manager
```

## Adding a channel

Click **+ Add channel**. The only thing that really matters is the command — it
must write MPEG-TS to stdout and keep writing until killed.

```bash
# Streamlink (best all-round choice for most sites)
streamlink --stdout 'https://example.com/live' best

# An existing HLS/DASH/RTSP feed, remuxed without re-encoding
ffmpeg -i 'http://example.com/index.m3u8' -c copy -f mpegts pipe:1

# RTSP camera
ffmpeg -rtsp_transport tcp -i 'rtsp://camera.lan/stream1' -c copy -f mpegts pipe:1

# A DVB tuner, if you still have one
ffmpeg -f mpegts -i /dev/dvb/adapter0/dvr0 -c copy -f mpegts pipe:1

# Shell mode (tick "Run through a shell") for pipelines
yt-dlp -q -o - 'https://…' | ffmpeg -i pipe:0 -c copy -f mpegts pipe:1
```

Use the per-source **Test** button to run it for 8 seconds and see what ffprobe
finds before committing.

## Multiple sources and failover

A channel can have any number of sources. They are tried in **priority order,
highest first** (ties fall back to list order), and a source that fails hands
over to the next one immediately — no backoff, because the backoff exists for
flaky networks, not for a source you already know is dead.

| | |
|---|---|
| A source dies mid-stream | The next source is tried at once; viewers stay connected. |
| All sources have failed | The cycle restarts under the normal backoff ladder. |
| A source runs healthily for a minute, then fails | The list is retried from the top, so a recovered primary wins its place back. |
| A source's binary is missing | Marked fatal for that source only — the channel still fails over. |

The channel row shows which source is live and which ones it failed over from,
and the session log records every switch.

```jsonc
{
  "id": "bbc1",
  "name": "BBC One",
  "sources": [
    { "id": "hd",     "name": "HD",     "priority": 100, "network": "provider-a",
      "command": "streamlink --stdout 'https://a.example/bbc1' best" },
    { "id": "sd",     "name": "SD",     "priority": 50,  "network": "provider-b",
      "command": "streamlink --stdout 'https://b.example/bbc1' 720p" },
    { "id": "backup", "name": "Backup", "priority": 10,
      "command": "ffmpeg -i 'http://c.example/bbc1.m3u8' -c copy -f mpegts pipe:1" }
  ]
}
```

## Networks

A network is a capacity pool, matching Tvheadend's concept. Assign each source to
one and set **max streams** to the number of simultaneous connections that
upstream allows.

- A slot is **one upstream connection, not one viewer.** Ten people watching the
  same channel — at any mix of profiles — hold one slot.
- When every source for a channel sits on a full network, the request is refused
  with **503** and a message naming the network. It is not queued, because the
  provider would refuse it anyway.
- A source with no network is uncapped.
- If a channel's sources are on different networks, it automatically uses
  whichever has room, still preferring higher priority.
- Lowering a limit never evicts current streams; it applies to the next one.
- Disabling a network blocks every source on it — useful for taking a provider
  out of rotation without editing channels.

The Networks tab shows live usage per pool, so you can see at a glance whether
you are at your provider's ceiling.

Note that `-re` is only for file/lavfi inputs. Live sources are already paced by
the sender, and adding `-re` will make the stream drift behind.

## Channel order

Channels are listed in **channel-number order** by default — in the GUI, the
playlist and the guide alike, all from one sorter so they can never disagree.
Channels without a number come last, keeping their configured order, and equal
numbers stay in configured order too.

**Channel order** in Settings offers:

| Mode | Order |
|---|---|
| `number` (default) | Ascending channel number, unnumbered last |
| `name` | Alphabetical by name |
| `manual` | Exactly as configured, via `POST /api/channels/reorder` |

Editing a channel's number moves it immediately; there is nothing to re-save.
Note that most clients apply their own sorting on top — TiVimate sorts by
`tvg-chno` when it is present, which is emitted for every numbered channel.

## Groups

A channel can belong to any number of groups. M3U carries one `group-title` per
entry, so a channel in several groups is emitted **once per group** — same
`tvg-id`, same stream URL, one line each:

```
#EXTINF:-1 tvg-id="bbc1" tvg-name="BBC One" group-title="UK",BBC One
http://host:8409/stream/bbc1
#EXTINF:-1 tvg-id="bbc1" tvg-name="BBC One" group-title="Favourites",BBC One
http://host:8409/stream/bbc1
```

Clients that key on `tvg-id` — TiVimate, OTT Navigator, IPTVnator — show the one
channel under each group, and the guide still matches because the id never
changes. If a client instead shows them as duplicate channels, turn off
**List a channel once per group** in Settings and only the first group is used.

Enter them comma-separated in the channel editor; groups already in use are
offered as suggestions. `/playlist.m3u8?group=UK` filters to channels in that
group, matching any of a channel's groups.

## Transcode profiles

A profile is the middle of an ffmpeg command:

```
ffmpeg -hide_banner -nostdin -loglevel <level> <input args> -i pipe:0 <output args> -f <container> pipe:1
```

Four ship by default: `720p`, `480p`, `audio` (radio / audio-only) and `remux`
(stream copy with fixed timestamps). Add your own in the **Profiles** tab —
hardware encoding is just a matter of the right arguments:

```
input args:  -hwaccel vaapi -hwaccel_output_format vaapi -probesize 2M
output args: -vf scale_vaapi=w=-2:h=720 -c:v h264_vaapi -b:v 3000k -c:a aac -b:a 128k
```

A profile is requested per client, not per channel:

| URL | Result |
|---|---|
| `/stream/bbc1` | source output, untouched |
| `/stream/bbc1?profile=480p` | transcoded to the `480p` profile |
| `/playlist.m3u8` | each channel uses its own default profile (usually none) |
| `/playlist.m3u8?profile=480p` | every channel transcoded to `480p` |

Each profile in use gets one ffmpeg, shared by everyone who asked for it. All of
them are fed from the channel's single source, so the same channel watched at
source and at 480p simultaneously still opens exactly one upstream connection.

The **Active sessions** table in Settings shows this directly: a `source` row per
channel, with its transcoders nested underneath.

### When the encoder can't keep up

Transcoding is CPU-bound, and a source arrives in real time whether or not
ffmpeg is finished with the last second. If it falls behind, its input queue
overflows and drops the oldest chunks — the session shows **encoder behind: N
dropped**. That never stalls the source or any other viewer, but it does mean
visible corruption in that profile's output. Use a faster preset, a lower
resolution, or hardware encoding.

## EPG

The **EPG** tab ingests XMLTV, maps it onto your channels, and serves the result
at `/xmltv.xml`.

### Sources

Three kinds, any number of each:

| Kind | Use it for |
|---|---|
| **Command** | A script that assembles several guides and writes XMLTV to stdout — the same thing you already pipe into Tvheadend. Keep using it as-is. |
| **URL** | An XMLTV file over HTTP. `.gz` is decompressed automatically. |
| **File** | A local path something else already writes. |

Each source has its own refresh interval and is fetched to a cache file on disk.
Command output may be gzipped; it is detected either way. A source that fails
keeps its last good cache and reports the error in the GUI — one broken feed
never takes the guide down.

The cache lives next to the config, in `epg/` — a fortnight of listings stays on
disk rather than in memory, and guide requests stream straight off it.

### Mapping

This is the part that makes clients work. Each channel needs a guide id that
appears in **both** the playlist and the XMLTV, or clients show an empty EPG.
That id is the channel's **tvg-id** (defaulting to its channel id), and the guide
is rewritten to use it — so upstream ids never have to match yours.

Matching to an upstream XMLTV channel is tried in descending confidence:

1. Your tvg-id equals the upstream id.
2. Your channel id equals the upstream id.
3. Normalised name comparison — case, punctuation and noise words like
   `HD`, `UHD`, `SD`, `TV` are ignored, so `Movie Channel HD` finds
   `movies.example` named *Movie Channel*.

**Auto-match unmapped** applies the guesses; **Re-match all** redoes everything
including manual choices. Anything auto-matched shows as such, so you can see
what was guessed versus what you pinned. Setting a mapping by hand always wins,
and the *include* checkbox drops a channel from the guide while leaving it in the
playlist.

One upstream channel can feed several of yours — useful when you carry the same
channel from two providers and want the guide on both.

### TiVimate and friends

Add the playlist URL and you are done: the guide URL is advertised in the
playlist header as `url-tvg` / `x-tvg-url`, which TiVimate, OTT Navigator and
IPTVnator read automatically. If a client wants it separately, it is:

```
http://host:8409/xmltv.xml
```

Jellyfin and Kodi want it entered by hand — Jellyfin under Live TV → TV Guide
Data Providers → XMLTV, Kodi in the PVR IPTV Simple Client settings.

Guide size is trimmed by **Guide history kept** (default 12 hours) and **Guide
days ahead** (default 14), so clients are not handed months of listings.

## Client setup

Point your client at the playlist URL shown at the top of the GUI:

- **Jellyfin** — Dashboard → Live TV → Tuner Devices → add **M3U Tuner**, paste the URL.
- **Kodi** — install *PVR IPTV Simple Client*, set the M3U playlist URL.
- **VLC** — Open Network Stream, paste the URL.
- **ffmpeg** — `ffplay 'http://host:8409/stream/bbc1?profile=480p'`

If you are behind a reverse proxy or NAT, set **Public base URL** in Settings so
generated playlist URLs point somewhere clients can reach. Disable proxy
buffering for `/stream/` — in nginx, `proxy_buffering off;` — or playback will
lag badly.

## Settings worth knowing

| Setting | What it does |
|---|---|
| **Linger after last client** | Keeps a source alive briefly after the last viewer leaves, so channel zapping doesn't restart it. Raise it if your sources are slow to start. |
| **Prebuffer** | Recent stream data replayed to a joining client so playback starts immediately instead of waiting for the next keyframe. |
| **Client queue** | 64 KB chunks buffered per viewer. A client that can't keep up drops data rather than stalling everyone else on the same source. |
| **Startup timeout** | How long a newly started source may take to produce its first byte. Separate from the stall timeout, because connecting and authenticating is much slower than staying connected. Raise it for slow providers. |
| **Stall timeout** | Restarts a source that stops producing data mid-stream. |
| **First / max retry delay** | Backoff ladder for failed attempts. It doubles per consecutive failure and resets once a run survives a minute. |
| **Give up after N failures** | Stop retrying a source that keeps failing. 0 keeps trying forever. A transcode profile that dies without ever emitting a frame is treated as broken and abandoned after three tries regardless. |
| **Terminate grace** | How long a process gets to exit on SIGTERM before it is killed. Raise it if you see `ignored SIGTERM, killing` in the log. |
| **Default max clients** | Viewer cap per channel, counted across all profiles. 0 = unlimited. |

## Stream health

Every packet relayed is checked for the two standard transport-stream faults:

| Counter | Meaning |
|---|---|
| **Transport error** | The `transport_error_indicator` bit is set — an upstream demodulator or muxer marked the packet as containing uncorrectable errors. Anything above zero means the signal or link is damaged. |
| **Continuity error** | A PID's 4-bit continuity counter did not advance by one. This is packet loss, and the direct cause of macroblocking and audio dropouts. |

The counting follows the spec rather than approximating it: null packets are
ignored, packets carrying no payload correctly do not advance the counter, one
duplicate packet is legal (a second in a row is not), and a discontinuity the
adaptation field explicitly signals is recorded separately instead of being
counted as a fault. PIDs are tracked independently, and the GUI names the PID
losing the most packets — usually enough to tell a bad video feed from a bad
audio one.

Channel and session rows show errors both as a raw count and **per million
packets**, which is the figure that stays comparable between a channel up for a
minute and one up for a week. Under 10/M is generally unnoticeable; over 100/M is
visible on screen.

Analysis is a per-packet Python loop. It is cheap, but **Count MPEG-TS transport
and continuity errors** in Settings turns it off if a very busy server needs the
CPU back.

## Prometheus

`/metrics` serves the standard text exposition format.

```yaml
scrape_configs:
  - job_name: streams-manager
    static_configs:
      - targets: ["tv.lan:8409"]
    # Only when STREAMS_MANAGER_TOKEN is set:
    authorization:
      credentials: your-token-here
```

The endpoint requires the admin token when one is set. Set
`STREAMS_MANAGER_METRICS_PUBLIC=1` to leave it open for a scraper that cannot
send a header.

| Metric | Type | Labels |
|---|---|---|
| `streams_manager_channels`, `_channels_enabled`, `_sources`, `_profiles` | gauge | |
| `streams_manager_sessions` | gauge | `kind` |
| `streams_manager_clients` | gauge | |
| `streams_manager_session_up` | gauge | `channel`, `channel_name`, `kind`, `profile`, `source`, `network` |
| `streams_manager_session_clients`, `_bitrate_bps`, `_uptime_seconds` | gauge | as above |
| `streams_manager_bytes_total` | counter | `channel`, `kind`, `profile` |
| `streams_manager_connections_total`, `_restarts_total` | counter | as above |
| `streams_manager_ts_packets_total` | counter | as above |
| `streams_manager_ts_transport_errors_total` | counter | as above |
| `streams_manager_ts_continuity_errors_total` | counter | as above |
| `streams_manager_network_streams`, `_max_streams`, `_enabled` | gauge | `network` |
| `streams_manager_epg_up`, `_channels`, `_programmes`, `_age_seconds` | gauge | `source` |
| `streams_manager_epg_mapped_channels`, `_cache_bytes` | gauge | |

Counters come from a ledger that outlives individual sessions, so a channel going
idle and starting again does not look like a counter reset.

Useful queries:

```promql
# Packet loss per channel, errors per million
1e6 * rate(streams_manager_ts_continuity_errors_total[5m])
    / rate(streams_manager_ts_packets_total[5m])

# Networks at capacity
streams_manager_network_streams >= streams_manager_network_max_streams > 0

# A guide that has stopped refreshing
streams_manager_epg_age_seconds > 86400
```

## API

Everything the GUI does is available directly.

```
GET    /playlist.m3u8[?profile=&group=]
GET    /stream/{id}[?profile=]
GET    /xmltv.xml                       the merged guide (also /epg.xml)
GET    /metrics                         Prometheus exposition
GET    /healthz

GET    /api/state                       everything at once
GET    /api/channels                    POST to create
PUT    /api/channels/{id}               DELETE to remove
POST   /api/channels/reorder            {"ids": [...]}
POST   /api/channels/{id}/test          {"duration": 8, "profile": "480p"}
POST   /api/test                        test an unsaved command
GET    /api/profiles                    POST / PUT / DELETE as above
GET    /api/networks                    POST / PUT / DELETE as above; GET reports live usage
GET    /api/epg                         sources, discovered channels, mapping status
POST   /api/epg/sources                 PUT / DELETE /api/epg/sources/{id}
POST   /api/epg/sources/{id}/refresh    fetch one source now
POST   /api/epg/refresh                 fetch every enabled source now
POST   /api/epg/automap[?overwrite=]    apply best-guess channel matches
PUT    /api/epg/mapping                 {"mapping": {"channel-id": "xmltv-id"}}
GET    /api/settings                    PUT to update
GET    /api/sessions                    live session state
POST   /api/sessions/{key}/stop
GET    /api/sessions/{key}/log          stderr tail
```

Interactive docs at `/docs`.

## Security

**This service runs commands you give it.** Anyone who can reach the admin API
can run arbitrary commands as the service user — that is the whole feature, not
a bug, but it means the port is as sensitive as a shell.

- It binds to `127.0.0.1` by default. Keep it there, or put it behind a VPN or
  authenticated reverse proxy.
- Set `STREAMS_MANAGER_TOKEN` to require a bearer token on the admin API and
  GUI. The server logs a warning at startup when it is unset.
- Set `STREAMS_MANAGER_PROTECT_STREAMS=1` to require that token on
  `/stream` and `/playlist.m3u8` too — playlist URLs then carry `?token=`. Leave
  it off if your players can't send one.
- Run it as an unprivileged user. The supplied systemd unit does, with the usual
  hardening knobs turned on.

## Configuration file

Everything lives in one JSON file, default
`~/.config/streams-manager/config.json`, override with `STREAMS_MANAGER_CONFIG`.
Older shapes are migrated on load and rewritten on the next change, so existing
configs keep working: a top-level `command`/`use_shell` becomes a single source,
and a single `group` string becomes a one-entry `groups` list.
It is written atomically on every change and is safe to edit by hand while the
server is stopped. A file that fails to parse is moved aside to `.broken` rather
than silently discarded.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `STREAMS_MANAGER_CONFIG` | `~/.config/streams-manager/config.json` | Config file location |
| `STREAMS_MANAGER_HOST` | `127.0.0.1` | Bind address |
| `STREAMS_MANAGER_PORT` | `8409` | Bind port |
| `STREAMS_MANAGER_TOKEN` | *(unset)* | Bearer token for the admin API and GUI |
| `STREAMS_MANAGER_PROTECT_STREAMS` | *(unset)* | Also require the token on streams and the playlist |
| `STREAMS_MANAGER_METRICS_PUBLIC` | *(unset)* | Serve `/metrics` without the token |
| `STREAMS_MANAGER_LOGLEVEL` | `INFO` | Server log level |

## Tests

```bash
.venv/bin/python tests/smoke_test.py
```

Starts a real server against a throwaway config and drives it over HTTP with a
synthetic ffmpeg source: playlist generation, MPEG-TS passthrough, transcoding
(verified with ffprobe), session fan-out and process accounting, idle teardown
and reaping, dead-source handling, and persistence. Takes about three minutes.

It also counts upstream connections with an instrumented source command and
asserts that switching profile mid-watch never opens a second one — the
regression that motivated the shared-source design.

## Troubleshooting

Every session keeps a tail of its command's stderr: click **Log** on the channel
or session row. Failures are reported with the exit code and the last thing the
command said, e.g.

```
source ended (exit code 145): Error opening input files: Connection refused
transcoder ended (exit code 8): Error opening output files: Encoder not found
```

| Symptom | Likely cause |
|---|---|
| Fails a few times then works | Something else still held the upstream connection. Should no longer happen for profile switches; if it persists, raise **Linger** so reconnects reuse the live source, or check for another client of the same provider. |
| `produced no data for Ns` | The command connected but never emitted anything — wrong URL, expired auth, or missing `-f mpegts pipe:1`. Use **Test source**. |
| `ignored SIGTERM, killing` | The command traps or ignores SIGTERM. Harmless, but raise **Terminate grace** to let it exit cleanly. |
| `encoder behind: N dropped` | The transcode can't run in real time on this CPU. Faster preset, lower resolution, or hardware encoding. |
| Rising continuity errors | Packet loss upstream. Check the network path to the provider; the worst-PID hint in the session row narrows it to video or audio. |
| Rising transport errors | The source itself is marking packets corrupt — a bad tuner, aerial or upstream link, not something this server can fix. |
| Stream plays then dies after ~30s | Source stopped producing. Check the log; if the source is just slow, raise **Stall timeout**. |
| `503 every source is blocked` | The channel's networks are at capacity or disabled. Check the Networks tab; raise the cap, or give the channel a source on another network. |
| Client shows no guide | The tvg-id in the playlist must match a `<channel id>` in the XMLTV. Check the EPG tab's mapping table — anything showing **no guide** has no match. |
| Guide is empty after a refresh | Look at the source's error in the EPG tab. A source that fails keeps its previous cache, so the guide reflects the last good fetch. |
| `ffplay` hangs when you close its window | Not this server — reproducible playing the origin URL directly, and with a local test card. It is ffplay's SDL audio shutdown on PipeWire-via-ALSA. Use `SDL_AUDIODRIVER=pipewire ffplay …`, or `-an`. `mpv` and `vlc` are unaffected. |

## What this deliberately does not do

Tvheadend is a DVR. This is not. There is no EPG/XMLTV, no recording, no
scheduling, no conditional access, and no tuner hardware management — if you need
those, you need Tvheadend. This handles the part where you have a list of stream
sources and want them available as a playlist.
