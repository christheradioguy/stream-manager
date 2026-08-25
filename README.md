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
- **One continuous timeline** — a source that reopens does not rewind the clock of
  anyone watching. Timestamps and continuity counters are carried across the
  restart, so a routine playlist or token rotation is invisible to the player
  instead of pushing audio and video further apart each time.
- **Keyframe-aligned handover** — clients always start on a 188-byte TS boundary,
  and one joining a running channel starts on a video keyframe with the program
  tables in front of it. Starting mid-GOP is what leaves a player's audio running
  a second or two ahead of its picture for the rest of the session.
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
- **Bulk audit** — verify every source in one pass, from the GUI or the command
  line, without exceeding your providers' connection limits.

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

### Sources that rotate

A live HLS source — especially one behind a proxy, a token or Cloudflare Access —
often ends cleanly every few minutes when its playlist window or token rotates.
The stream did not fail; the upstream simply closed it.

This is handled as a **reopen** rather than a failure: it does not climb the
backoff ladder, does not count towards *give up after N failures*, does not mark
the session in error, and waits **Reopen delay** (3s by default) before
reconnecting so the upstream has a moment to be ready. The session row shows a
**Reopens** count separately from restarts, and `streams_manager_reconnects_total`
tracks it in Prometheus.

Viewers stay connected throughout — they see a short stall, not a disconnect —
and the reopen is made invisible to them. A new run is a new encoder: its
timestamps start again from zero and so do its continuity counters. Spliced into
a live viewer's stream as-is, that rewinds the player's clock by however long the
previous run lasted, and looks like a burst of packet loss on top. Players do not
agree on what to do about either — the common answer is to keep the old clock and
present audio and video at different offsets from then on, so lip sync drifts a
little further with every reopen. Instead each run's timestamps and counters are
carried on from where the last one stopped, and the client sees one timeline that
only ever moves forwards.

The stream's clock references move with them, and resume from where the clock
got to rather than from where the presentation stamps got to. Those two are not
the same place — a clock reference sits behind the picture it times by however
much the decoder is expected to buffer, often the better part of a second — and
resuming the clock from the wrong one leaves a gap in it at every reopen.
Players that decode by presentation stamp never notice; players that pace from
the clock, which is most set-top and Android ones, run slow and fall behind.

Both tracks are moved together, which is less obvious than it sounds. AC-3 —
the audio on most US broadcast sources — travels as `private_stream_1`, stream
id `0xBD`, below the range audio ids are usually said to occupy. Rebasing the
video and not that leaves the two on separate timelines, one run's length
further apart at every reopen, heard as the sound pulling steadily ahead of the
picture. `tools/avdrift.py` measures the gap on any stream if you want to check
one.

Everything the source spawned is killed before reopening, including helpers that
outlive their parent — streamlink's muxer, a shell pipeline's other half, a
wrapper script's background job. A helper left running keeps the upstream
connection open, so the reopen arrives as a *second* connection and a provider
expecting one answers it with a reset.

Better still, let the source tool ride out the rotation itself so no reopen is
needed at all. For streamlink:

```bash
streamlink --stdout --retry-streams 5 --retry-open 10 --retry-max 0 \
           --stream-timeout 120 'https://…' best
```

`--retry-open`/`--retry-streams` make streamlink reconnect internally instead of
exiting, which is invisible to viewers. The ffmpeg equivalent is
`-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 30`.

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

### Sources faster than real time

An HLS input does not arrive at the rate the content plays. `ffmpeg -i <playlist>`
downloads the whole segment backlog at line speed — a playlist holding 80 seconds
of a 12 Mbit/s stream arrives in **bursts of 90 Mbit/s and more**. The player at
the other end still consumes at 12 Mbit/s.

That excess is held back rather than discarded: the source is paused, so its pipe
fills, so the source process blocks, and the burst waits upstream where it costs
nothing. The stream stays byte-exact. Discarding the excess instead is what
produces glitching and macroblocking on exactly this kind of source, because
bytes removed from the middle of a transport stream are lost frames.

The hold is bounded by **Hold back a slow client for** (20s by default). A viewer
still behind at that point is genuinely too slow rather than briefly outrun, and
its oldest data is dropped so it cannot stall everyone else on the same source.
Watch the session's **Dropped** column: it should be zero.

A viewer that does lose data loses picture with it: the bytes after a hole
decode against reference frames that never arrived, so it macroblocks until the
next keyframe, and audio and video come out of the gap a different distance
apart than they went in. Nothing downstream of the drop can undo that. Give a
client that cannot carry the source a transcode profile instead.

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

### Pinned vs auto

Every channel is in one of three states, shown as a badge in the mapping table:

| State | Meaning |
|---|---|
| **auto-matched** | No choice made; the guide is matched by name on every request, and follows whatever your sources currently offer. |
| **pinned** | You set it by hand. Nothing changes it — not adding channels, not editing the channel, not **Auto-match unpinned**. |
| **no guide (pinned)** | You deliberately cleared it. It stays empty; auto-matching will not put a guess back. |

Clearing a mapping pins "no guide" rather than reverting to a guess — otherwise
correcting a bad auto-match would be impossible, since the same wrong guess would
return immediately. **use auto-match** on the row puts a channel back on auto.

**Auto-match unpinned** guesses for channels still on auto and pins the results.
**Re-match all** overwrites *everything*, pinned choices included — it asks first.

The *include* checkbox drops a channel from the guide while leaving it in the
playlist.

You can pin an id before the guide that defines it has been fetched; it is
accepted and flagged rather than rejected.

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
| **Prebuffer** | Recent stream data replayed to a joining client so playback starts immediately. It is trimmed at keyframes rather than at a byte count, so the replay always begins somewhere a decoder can start; a stream whose keyframes are further apart than this may overshoot it. Set it to at least one keyframe interval — at 15 Mbit/s with a 2-second GOP that is about 4 MB. |
| **Client queue** | 64 KB chunks buffered per viewer. A client that can't keep up drops data rather than stalling everyone else on the same source. |
| **Startup timeout** | How long a newly started source may take to produce its first byte. Separate from the stall timeout, because connecting and authenticating is much slower than staying connected. Raise it for slow providers. |
| **Stall timeout** | Restarts a source that stops producing data mid-stream. |
| **Reopen delay** | Pause before reopening a source that ended *after streaming normally* — a live HLS token or playlist window rotating, say. Separate from the failure backoff because this is expected, not a fault. Raise it when reopening produces connection resets. |
| **First / max retry delay** | Backoff ladder for failed attempts. It doubles per consecutive failure and resets once a run survives a minute. |
| **Give up after N failures** | Stop retrying a source that keeps failing. 0 keeps trying forever. A transcode profile that dies without ever emitting a frame is treated as broken and abandoned after three tries regardless. |
| **Terminate grace** | How long a process gets to exit on SIGTERM before it is killed. Raise it if you see `ignored SIGTERM, killing` in the log. |
| **Default max clients** | Viewer cap per channel, counted across all profiles. 0 = unlimited. |

## When a provider's timestamps are broken

Some sources hand out streams whose audio and video timelines disagree — by
seconds, sometimes by hours. ffmpeg spots the mismatch and corrects each stream
separately, the two corrections drift apart, and lip sync gets worse the longer
you watch. The source's log gives it away, thousands of lines of:

```
[vist#0:0/mpeg2video] timestamp discontinuity (stream id=0): 2404404545, new offset= 112612
[aist#0:1/ac3]        timestamp discontinuity (stream id=0): -2404372545, new offset= 2404485157
```

Note the two offsets: one a second or so, the other seven hours. That is the
desync being created, upstream of anything this server does.

There is no known good fix here yet. Rebuilding the timestamps from frame and
sample counts (`setpts=N/FRAME_RATE/TB`, `-af asetpts=N/SR/TB`) repairs a
recorded file completely, and is worth trying on a channel that is already
broken — but do not leave it on a channel that works. On a live source that is
losing frames it converts each lost frame into lost *time*, so the output runs
slower than real time and players fall behind until they stall. It was tried
here and made a bad channel unplayable.

If you hit this, the honest options are a different source URL for that channel,
or living with it. `tools/avdrift.py` will tell you which of your channels are
affected.

## When a provider's audio timestamps are wrong

A few sources hand out audio whose timestamps are minutes away from the video's
and advance at the wrong rate, while the audio itself is complete and sitting
exactly where it belongs between the pictures. ffplay and VLC ignore timestamps
that look absurd and sound perfect on such a stream; anything that paces itself
from the clock — most set-top boxes, and TiVimate and other ExoPlayer clients —
stalls or drifts on it.

`tools/tsclock.py` names it:

```
pid 256 video   vs clock: median    +0.767s   timeline spans 90.97s
pid 257 audio   vs clock: median -4152.843s   timeline spans 75.65s
audio timeline advances at 0.832x the video's - THEY DISAGREE
```

**No ffmpeg filter repairs this.** `asetpts`, `aresample`, `setpts`, wallclock
timestamps — all of them run after the demuxer has already decided which audio
goes with which picture, using the timestamps that are wrong. They can make the
rate come out right while leaving the sound attached to the wrong moment, which
measures beautifully and sounds worse than before.

Tick **Fix audio timing** on the source instead. That works a step earlier, on
the transport stream, where the information still exists: a packet's position
says which picture it arrived beside, and its size says how long it lasts. On a
real broken feed that turned 0.832x into 0.996x with no timestamp arriving late
and every interval exactly one frame of audio.

Leave it off unless a source needs it. It is safe on a healthy stream — the test
suite checks that — but it is a repair, not an improvement.

## Spotting a bad source from metrics

The transport-level error counters — `ts_transport_errors_total` and
`ts_continuity_errors_total` — only see damage done *after* the source command.
A source ending in an ffmpeg re-mux (`ffmpeg -i … -c copy -f mpegts pipe:1`)
rebuilds the transport layer from scratch: it drops whatever was damaged, writes
a fresh continuity sequence and never sets the transport-error bit. A channel
visibly breaking up therefore reports zero errors. Measured on a stream with 86
packets dropped and 105 flagged corrupt: 191 continuity errors before the
re-mux, **zero** after, with the decoder still reporting 147 problems.

What it cannot do is invent the frames that went with the packets it dropped, so
their timestamps are simply absent — and that hole is measurable whatever the
source does. Three metrics see what the error counters cannot:

| Metric | What it means |
|---|---|
| `streams_manager_content_gaps_total` | Holes in the presentation timeline: frames that should have been there and were not. **This is the one that works on a passthrough channel.** |
| `streams_manager_content_lost_seconds_total` | How much content is missing, in seconds. Rate this for a "how bad is it" figure. |
| `streams_manager_source_events_total{event=…}` | Faults the source tool itself reported. `decode` = pictures or sound it could not reconstruct. `timestamp` = the stream's own timing is inconsistent. `input` = trouble reaching or holding the upstream. `muxer` = it had to intervene. Note `decode` only appears where something decodes — a `-c copy` source does not, so use the gap counters for those. |
| `streams_manager_session_speed` | How far ahead of real time the tool is running. Sustained below 1.0 means it cannot keep up and every viewer will eventually starve. |

The gap counters read decode timestamps rather than presentation ones, because
anything with B-frames presents out of order and the differences between
presentation stamps would look like holes that are not there. A jump too large
to be a lost frame is treated as a discontinuity and not counted, so a source
reopening does not register as hours of missing content.

Both are labelled by channel, kind, profile and source, so a query like

```
topk(5, rate(streams_manager_content_lost_seconds_total[15m]))
```

ranks your channels by how much content they are actually losing, passthrough
and transcoded alike — which no other metric here can tell you. Every kind is published even at zero, so `rate()`
works from the moment a channel first runs.

**For `session_speed` on a source**, the command must emit ffmpeg's progress
line. That is on by default; if you have set `-loglevel warning` or quieter, add
`-stats` to get it back. Transcode profiles always emit it.

## Checking lip sync on a channel

Audio and video are two streams of the same programme, so they should arrive in
equal amounts. When they don't, one of them is losing content and the gap is
what a viewer hears as bad lip sync. `tools/avdrift.py` measures that gap, in
slices, so you can see whether it is a fixed offset or one that keeps growing:

```bash
tools/avdrift.py http://localhost:8409/stream/bbc1 300          # through here
tools/avdrift.py --command 'ffmpeg -i URL -c copy -f mpegts pipe:1' 300
tools/avdrift.py /tmp/capture.ts                                # a saved file
tools/avdrift.py http://localhost:8409/stream/bbc1 300 --keep /tmp/c.ts
```

Run it both ways on the same channel. If only the first drifts, this server is
doing it. If both do, the stream arrives that way and the fix belongs in the
source arguments. `--keep` saves the capture and also asks a decoder what it
makes of it — if the decoder disagrees with the timestamp arithmetic, the tool
says so rather than reporting a confident wrong number.

### When it plays on a computer but not on a television

The two follow different clocks. Desktop players decode by presentation stamp;
set-top and Android players pace from the stream's program clock reference. A
fault in the latter is invisible on one and crippling on the other — video that
runs slow and falls further behind, on a stream that looks perfect in ffplay.
`tools/tsclock.py` checks the clock rather than the picture:

```bash
tools/tsclock.py /tmp/capture.ts
tools/tsclock.py direct.ts via-manager.ts     # compare against the source
```

Capture from the source on its own as well and compare the two. The source is
the reference; anything this server adds is its own fault.

## Auditing every source

Checking that all your sources still work, the way you would loop over
Tvheadend's services. Available from the **Audit** tab, the API, or a script:

```bash
tools/audit.py                                # everything
tools/audit.py --failed-only                  # just the broken ones
tools/audit.py --channel bbc1 --duration 15   # one channel, tested for longer
tools/audit.py --json > audit.json            # for scripting
tools/audit.py --results                      # reprint the last run
```

```
      CHANNEL         SOURCE          VIDEO                     RATE  DETAIL
PASS     1 BBC One    HD              h264 1280x720 25fps    7.7Mb/s  aac 1ch 44100Hz
PASS     1 BBC One    SD backup       h264 720x576 25fps     3.2Mb/s
PASS     3 ITV        Main            h264 1280x720 25fps    7.7Mb/s  aac 1ch 44100Hz
FAIL     3 ITV        Dead link                                    -  no data produced (exit code 145)
FAIL     4 Channel 4  Missing binary                               -  command not found: streamlink

3 ok, 2 failed, 0 skipped, in 11s
```

The script is standard library only, so it needs no virtualenv, and it takes
`--url` / `--token` (or `STREAMS_MANAGER_URL` / `STREAMS_MANAGER_TOKEN`). It
exits **0** when everything passed, **1** when anything failed and **2** on a
usage or connection error, so it drops straight into cron:

```cron
0 5 * * *  /opt/streams-manager/tools/audit.py --failed-only || mail -s "Dead TV sources" me@example.com
```

Each source is run for a few seconds and the output is probed, so a pass means
bytes actually arrived **and** ffprobe found a playable stream in them — a source
that emits data but nothing decodable is reported as a failure, not a pass.

**Network limits are honoured.** An audit takes slots from the same pool the live
streams use, so a provider capped at two concurrent streams is still only asked
for two at a time and the rest queue. That is also why an audit can take a while
with tight caps; it is doing that deliberately rather than getting your account
throttled. Live viewers keep priority — the audit waits for a slot rather than
taking one.

Results feed Prometheus as `streams_manager_source_ok{channel,source}`, so a
nightly audit can drive an alert:

```promql
streams_manager_source_ok == 0
```

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
GET    /api/audit                       last audit results and progress
POST   /api/audit[?duration=&concurrency=&channel=&include_disabled=]
POST   /api/audit/stop
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
| Glitching or macroblocking on a high-bitrate source | Check the session's **Dropped** count. Anything above zero means data is being discarded rather than held back — raise **Hold back a slow client for**, and see *Sources faster than real time*. |
| Audio ahead of the picture | A viewer started partway through a GOP. This server starts one on a keyframe instead, but only when the source flags them: a muxer that never sets `random_access_indicator` leaves nothing to align on. Put a **Remux** profile in front of such a source — re-muxing through ffmpeg marks the keyframes. |
| Video macroblocking but the error counters read zero | Expected if the source re-muxes: ffmpeg rebuilds a clean transport layer around the damaged content. Watch `streams_manager_source_events_total{event="decode"}` instead — see *Spotting a bad source from metrics*. |
| Rising continuity errors | Packet loss upstream. Check the network path to the provider; the worst-PID hint in the session row narrows it to video or audio. |
| Rising transport errors | The source itself is marking packets corrupt — a bad tuner, aerial or upstream link, not something this server can fix. |
| A source drops every few minutes and reconnects | Normal for live HLS behind a proxy or token: the window rotates and the source exits cleanly. See **Sources that rotate** below. |
| Lip sync drifts further the longer a channel is left on | Was a source reopening: each new run restarted its timestamps and the viewer's clock was rewound with them, a little more out of sync every time. Fixed as of this version. Check the session's **Reopens** count — if it is climbing, that was it. |
| Plays fine on a computer but slow and stuttering on a set-top or Android client | The two disagree about which clock to follow: desktop players decode by presentation stamp, set-top ones pace from the stream's clock references. A fault in the latter is invisible on one and crippling on the other. Fixed as of this version — a reopen used to leave a gap in the clock. |
| Audio out of sync on a set-top box but fine in VLC or ffplay | The provider's audio timestamps. Run `tools/tsclock.py` on a capture; if it says the two timelines disagree while the audio's own frame count matches the video's span, tick **Fix audio timing** on that source. See *When a provider's audio timestamps are wrong*. |
| Lip sync drifts on one channel while the others are fine | Most likely the provider's own timestamps, not this server. Check the session log for `timestamp discontinuity` lines with wildly different offsets for the video and audio streams. See *When a provider's timestamps are broken* — there is no reliable fix from this end. |
| Audio steadily running ahead of the picture | Same cause, worst on AC-3 sources — most US broadcast channels. AC-3 travels as `private_stream_1`, below where audio stream ids are usually assumed to start, so it was being left behind when the video was put back on the timeline: one run's length further ahead every reopen. Fixed as of this version. `tools/avdrift.py` measures it if you want to confirm. |
| `Connection reset by peer` right after a reopen | Usually a leftover helper still holding the old connection, so the new one looks like a second client. Fixed as of this version; if it persists, raise **Reopen delay** and prefer letting the source tool retry internally. |
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
