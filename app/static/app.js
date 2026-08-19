"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  settings: {},
  channels: [],
  profiles: [],
  networks: [],
  sessions: [],
  editingNetwork: null,
  editingEpg: null,
  epg: null,
  audit: null,
  draftSources: [],   // sources being edited in the channel modal
  baseUrl: "",
  protectStreams: false,
  configPath: "",
  editingChannel: null,   // channel id when editing, null when adding
  editingProfile: null,
  logKey: null,
  token: localStorage.getItem("sm-token") || "",
};

/* ------------------------------------------------------------------ utils */

function toast(msg, kind = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = kind;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, kind === "err" ? 6000 : 2500);
}

function fmtBits(bps) {
  if (!bps) return "—";
  if (bps >= 1e9) return (bps / 1e9).toFixed(2) + " Gb/s";
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " Mb/s";
  if (bps >= 1e3) return Math.round(bps / 1e3) + " kb/s";
  return bps + " b/s";
}

function fmtBytes(n) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + " " + u[i];
}

function fmtDuration(s) {
  if (!s || s < 1) return "—";
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function slugify(s) {
  return String(s).toLowerCase().trim()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

/* -------------------------------------------------------------------- api */

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (state.token) opts.headers["Authorization"] = "Bearer " + state.token;
  if (opts.body !== undefined && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    $("#token-modal").hidden = false;
    throw new Error("authentication required");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (Array.isArray(body.detail)) {
        detail = body.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
      } else if (body.detail) {
        detail = body.detail;
      }
    } catch (_) { /* not json */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ------------------------------------------------------------------ views */

$$("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("nav button").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + btn.dataset.view));
    // The EPG payload is far heavier than the 2s state poll, so it is only
    // fetched when its tab is actually being looked at.
    if (btn.dataset.view === "epg") refreshEpg();
    if (btn.dataset.view === "audit") refreshAudit();
  });
});

function epgVisible() {
  return $("#view-epg").classList.contains("active");
}

function auditVisible() {
  return $("#view-audit").classList.contains("active");
}

/* ------------------------------------------------------------- rendering  */

function sessionsFor(channelId) {
  return state.sessions.filter((s) => s.channel_id === channelId);
}

function allGroups() {
  const seen = new Set();
  for (const c of state.channels) for (const g of c.groups || []) seen.add(g);
  return [...seen].sort((a, b) => a.localeCompare(b));
}

function networkName(id) {
  if (!id) return "";
  return (state.networks.find((n) => n.id === id) || {}).name || id;
}

function renderSourceCell(ch, live) {
  const sources = ch.sources || [];
  // Show the one in use when streaming, otherwise the one that would be tried first.
  const activeId = live?.source_id;
  const ordered = sources
    .map((s, i) => ({ s, i }))
    .filter((x) => x.s.enabled)
    .sort((a, b) => (b.s.priority - a.s.priority) || (a.i - b.i))
    .map((x) => x.s);
  const shown = sources.find((s) => s.id === activeId) || ordered[0] || sources[0];
  if (!shown) return '<span class="sub">no sources</span>';

  const extra = sources.length > 1
    ? `<span class="sub">${sources.length} sources` +
      (activeId ? ` · using ${esc(shown.name || shown.id)}` : "") +
      (live?.failed_sources?.length ? ` · failed: ${live.failed_sources.map(esc).join(", ")}` : "") +
      `</span>`
    : "";
  const net = shown.network
    ? `<span class="sub">network: ${esc(networkName(shown.network))}</span>` : "";
  return `<span class="cmd" title="${esc(shown.command)}">${esc(shown.command)}</span>
          ${shown.use_shell ? '<span class="sub">via shell</span>' : ""}${net}${extra}`;
}

function tsErrorCell(sessions) {
  const cc = sessions.reduce((n, s) => n + (s.ts_continuity_errors || 0), 0);
  const te = sessions.reduce((n, s) => n + (s.ts_transport_errors || 0), 0);
  const packets = sessions.reduce((n, s) => n + (s.ts_packets || 0), 0);
  if (!packets) return '<span class="sub">—</span>';
  if (!cc && !te) return '<span class="badge running">clean</span>';
  // Errors per million packets is the number that stays comparable between a
  // channel that has been up for a minute and one up for a day.
  const ppm = Math.round(((cc + te) / packets) * 1e6);
  const bad = ppm > 100;
  const bits = [];
  if (cc) bits.push(`${cc.toLocaleString()} cont`);
  if (te) bits.push(`${te.toLocaleString()} tei`);
  return `<span class="badge ${bad ? "error" : "starting"}">${bits.join(" · ")}</span>` +
         `<div class="sub">${ppm}/M packets</div>`;
}

function renderChannels() {
  const tbody = $("#channel-rows");
  tbody.innerHTML = state.channels.map((ch) => {
    const sess = sessionsFor(ch.id);
    // The source session is the channel's real state; transcoders hang off it.
    const live = sess.find((s) => s.kind === "source") || sess[0];
    const clients = sess.reduce((n, s) => n + s.clients, 0);
    const bitrate = sess.reduce((n, s) => n + s.bitrate_bps, 0);
    const active = sess.filter((s) => s.kind === "transcode").map((s) => s.profile);
    const status = !ch.enabled
      ? '<span class="badge off">disabled</span>'
      : live
        ? `<span class="badge ${live.status}">${live.status}</span>` +
          (active.length ? `<div class="sub">+ ${active.map(esc).join(", ")}</div>` : "")
        : '<span class="badge idle">idle</span>';
    const err = live && live.last_error && live.status === "error"
      ? `<div class="sub" title="${esc(live.last_error)}">${esc(live.last_error.slice(0, 60))}</div>`
      : "";
    return `<tr class="${ch.enabled ? "" : "disabled"}" data-id="${esc(ch.id)}">
      <td class="num sub">${ch.channel_number ?? ""}</td>
      <td><div class="name">${esc(ch.name)}</div><div class="sub">${esc(ch.id)}</div></td>
      <td>${renderSourceCell(ch, live)}
          ${ch.default_profile ? `<span class="sub">default profile: ${esc(ch.default_profile)}</span>` : ""}</td>
      <td class="sub">${(ch.groups || []).map(esc).join(", ")}</td>
      <td>${status}${err}</td>
      <td class="num">${clients || "—"}</td>
      <td class="num">${fmtBits(bitrate)}</td>
      <td class="num">${tsErrorCell(sess)}</td>
      <td class="num">${live ? fmtDuration(live.uptime_seconds) : "—"}</td>
      <td class="actions">
        ${live ? `<button class="link small" data-act="log" data-key="${esc(live.key)}">Log</button>` : ""}
        <button class="link small" data-act="copy">URL</button>
        <button class="small" data-act="edit">Edit</button>
        <button class="small danger" data-act="delete">Delete</button>
      </td>
    </tr>`;
  }).join("");
  $("#channel-empty").hidden = state.channels.length > 0;
}

function renderNetworks() {
  $("#network-rows").innerHTML = state.networks.map((n) => {
    const limit = n.max_streams;
    const pct = limit > 0 ? Math.min(100, Math.round((n.in_use / limit) * 100)) : 0;
    const full = limit > 0 && n.in_use >= limit;
    const bar = limit > 0
      ? `<div style="background:var(--bg);border:1px solid var(--border);border-radius:4px;height:8px;width:110px;overflow:hidden">
           <div style="height:100%;width:${pct}%;background:${full ? "var(--err)" : "var(--ok)"}"></div>
         </div>`
      : '<span class="sub">unlimited</span>';
    return `<tr data-id="${esc(n.id)}" class="${n.enabled ? "" : "disabled"}">
      <td><div class="name">${esc(n.name)}</div>
          <div class="sub" style="font-family:inherit">${esc(n.description || "")}</div></td>
      <td class="sub">${esc(n.id)}</td>
      <td class="num">${n.in_use}</td>
      <td class="num">${limit > 0 ? limit : "∞"}</td>
      <td>${bar}${full ? '<div class="sub" style="color:var(--err)">at capacity</div>' : ""}
          ${n.enabled ? "" : '<span class="badge off">disabled</span>'}</td>
      <td class="num">${n.channels || "—"}</td>
      <td class="actions">
        <button class="small" data-act="edit">Edit</button>
        <button class="small danger" data-act="delete">Delete</button>
      </td>
    </tr>`;
  }).join("");
  $("#network-empty").hidden = state.networks.length > 0;
}

function renderProfiles() {
  $("#profile-rows").innerHTML = state.profiles.map((p) => `
    <tr data-id="${esc(p.id)}">
      <td><div class="name">${esc(p.name)}</div>
          <div class="sub" style="font-family:inherit">${esc(p.description || "")}</div></td>
      <td class="sub">${esc(p.id)}</td>
      <td><span class="cmd" title="${esc(p.input_args + " | " + p.output_args)}">${esc(p.output_args)}</span></td>
      <td class="sub">${esc(p.container)}</td>
      <td class="actions">
        <button class="small" data-act="edit">Edit</button>
        <button class="small danger" data-act="delete">Delete</button>
      </td>
    </tr>`).join("");
  $("#profile-empty").hidden = state.profiles.length > 0;
}

function renderSessions() {
  // Sources first, each followed by the transcoders feeding off it.
  const ordered = [];
  for (const s of state.sessions.filter((s) => s.kind === "source")) {
    ordered.push(s);
    ordered.push(...state.sessions.filter(
      (t) => t.kind === "transcode" && t.channel_id === s.channel_id));
  }
  for (const s of state.sessions) if (!ordered.includes(s)) ordered.push(s);

  $("#session-rows").innerHTML = ordered.map((s) => {
    const transcoders = s.kind === "source" ? s.consumers - s.clients : 0;
    const slow = s.input_dropped > 0
      ? `<div class="sub" style="color:var(--warn)" title="The encoder cannot keep up with the source, so input is being dropped. Use a faster preset or hardware encoding.">encoder behind: ${s.input_dropped} dropped</div>`
      : "";
    return `<tr data-key="${esc(s.key)}">
      <td>${s.kind === "transcode" ? '<span class="sub">└ </span>' : ""}<span class="name">${esc(s.channel_name)}</span>
          <div class="sub">${esc(s.key)}</div></td>
      <td>${s.kind === "source"
            ? `<span class="badge">source</span>` +
              (s.source_name ? `<div class="sub">${esc(s.source_name)}</div>` : "") +
              (s.network ? `<div class="sub">on ${esc(networkName(s.network))}</div>` : "") +
              (transcoders ? `<div class="sub">+${transcoders} transcoder(s)</div>` : "") +
              (s.failed_sources?.length
                ? `<div class="sub" style="color:var(--warn)">failed over from ${s.failed_sources.map(esc).join(", ")}</div>`
                : "")
            : `<span class="badge">${esc(s.profile)}</span>`}</td>
      <td><span class="badge ${s.status}">${s.status}</span>
          ${s.last_error ? `<div class="sub" title="${esc(s.last_error)}">${esc(s.last_error.slice(0, 60))}</div>` : ""}</td>
      <td class="num">${s.clients}</td>
      <td class="num">${fmtBits(s.bitrate_bps)}</td>
      <td class="num">${fmtBytes(s.bytes_out)}${slow}</td>
      <td class="num">${tsErrorCell([s])}${(s.ts_error_pids || []).length ? `<div class="sub" title="PIDs losing the most packets">worst pid ${s.ts_error_pids[0].pid} (${s.ts_error_pids[0].errors})</div>` : ""}</td>
      <td class="num">${s.restarts || "—"}</td>
      <td class="num">${s.reconnects || "—"}${s.last_end && !s.last_error
          ? `<div class="sub" title="${esc(s.last_end)}">last: ${esc(s.last_end.slice(0, 40))}</div>` : ""}</td>
      <td class="num">${s.dropped_chunks || "—"}</td>
      <td class="sub">${s.pids.join(", ") || "—"}</td>
      <td class="actions">
        <button class="link small" data-act="log">Log</button>
        <button class="small danger" data-act="stop">Stop</button>
      </td>
    </tr>`;
  }).join("");
  $("#session-empty").hidden = state.sessions.length > 0;
}

function renderStats() {
  $("#stat-channels").textContent = state.channels.filter((c) => c.enabled).length;
  $("#stat-sessions").textContent = state.sessions.length;
  $("#stat-clients").textContent = state.sessions.reduce((n, s) => n + s.clients, 0);
  $("#stat-bitrate").textContent = fmtBits(state.sessions.reduce((n, s) => n + s.bitrate_bps, 0));
}

function profileOptions(selectEl, { includeNone, noneLabel, value }) {
  const opts = [];
  if (includeNone) opts.push(`<option value="">${esc(noneLabel)}</option>`);
  for (const p of state.profiles) {
    opts.push(`<option value="${esc(p.id)}">${esc(p.name)} (${esc(p.id)})</option>`);
  }
  selectEl.innerHTML = opts.join("");
  selectEl.value = value || "";
}

function renderPlaylistUrl() {
  const base = state.baseUrl.replace(/\/$/, "");
  const params = new URLSearchParams();
  const prof = $("#playlist-profile").value;
  if (prof) params.set("profile", prof);
  if (state.protectStreams && state.token) params.set("token", state.token);
  const qs = params.toString();
  const url = `${base}/playlist.m3u8${qs ? "?" + qs : ""}`;
  $("#playlist-url").value = url;
  $("#open-playlist").href = url;
}

/* -------------------------------------------------------------- refreshing */

let refreshTimer = null;

async function refresh() {
  try {
    const s = await api("/api/state");
    state.settings = s.settings;
    state.channels = s.channels;
    state.profiles = s.profiles;
    state.networks = s.networks || [];
    state.sessions = s.sessions;
    state.baseUrl = s.base_url;
    state.protectStreams = s.protect_streams;
    state.configPath = s.config_path;

    renderChannels();
    renderNetworks();
    renderProfiles();
    renderSessions();
    renderStats();

    const keep = $("#playlist-profile").value;
    profileOptions($("#playlist-profile"), {
      includeNone: true, noneLabel: "Source (per channel default)", value: keep,
    });
    renderPlaylistUrl();
    $("#config-path").textContent = "Config: " + state.configPath;
    if (!settingsDirty) fillSettings();
  } catch (err) {
    if (err.message !== "authentication required") console.error(err);
  }
}

let epgTimer = null;
let auditTimer = null;

function startPolling() {
  clearInterval(refreshTimer);
  clearInterval(epgTimer);
  clearInterval(auditTimer);
  refreshTimer = setInterval(refresh, 2000);
  // Slow poll, and only while the tab is on screen, so a mid-refresh source
  // updates its status without hammering the guide index.
  epgTimer = setInterval(() => {
    if (epgVisible() && $("#epg-modal").hidden) refreshEpg();
  }, 15000);
  // The audit table needs a faster tick while a run is in progress.
  auditTimer = setInterval(() => {
    if (auditVisible() || state.audit?.running) refreshAudit();
  }, 2000);
  refresh();
}

/* ------------------------------------------------------------- channel CRUD */

/* ---- sources editor ---- */

function blankSource(index) {
  return {
    id: "", name: index === 0 ? "Primary" : `Backup ${index}`,
    command: "", use_shell: false, enabled: true,
    network: null, priority: Math.max(0, 10 - index * 10),
  };
}

function renderSources() {
  const nets = state.networks;
  // Show them in the order they will actually be tried.
  const order = state.draftSources
    .map((s, i) => ({ s, i }))
    .filter((x) => x.s.enabled)
    .sort((a, b) => (b.s.priority - a.s.priority) || (a.i - b.i))
    .map((x) => x.i);

  $("#c-sources").innerHTML = state.draftSources.map((s, i) => {
    const rank = order.indexOf(i);
    const badge = !s.enabled
      ? '<span class="badge off">disabled</span>'
      : rank === 0
        ? '<span class="badge running">tried 1st</span>'
        : `<span class="badge">tried ${rank + 1}${["st","nd","rd"][rank] || "th"}</span>`;
    const netOpts = ['<option value="">No network (uncapped)</option>']
      .concat(nets.map((n) => `<option value="${esc(n.id)}"${s.network === n.id ? " selected" : ""}>` +
        `${esc(n.name)}${n.max_streams > 0 ? ` (max ${n.max_streams})` : " (unlimited)"}</option>`))
      .join("");
    return `<div class="source-card" data-i="${i}">
      <div class="row" style="margin-bottom:8px">
        ${badge}
        <input type="text" data-f="name" value="${esc(s.name)}" placeholder="Source name"
               style="flex:1;min-width:120px">
        <label style="margin:0" title="Higher is tried first">Priority</label>
        <input type="number" data-f="priority" value="${s.priority}" step="1" min="-1000" max="1000"
               style="width:80px">
        <button type="button" class="small" data-act="test-source">Test</button>
        <button type="button" class="small danger" data-act="remove-source"
                ${state.draftSources.length < 2 ? "disabled title='A channel needs at least one source'" : ""}>Remove</button>
      </div>
      <textarea data-f="command" placeholder="streamlink --stdout 'https://…' best"
                style="min-height:56px">${esc(s.command)}</textarea>
      <div class="row" style="margin-top:8px">
        <select data-f="network" style="width:auto;max-width:260px">${netOpts}</select>
        <label class="check" style="margin:0"><input type="checkbox" data-f="use_shell"
          ${s.use_shell ? "checked" : ""}> <span>Shell</span></label>
        <label class="check" style="margin:0"><input type="checkbox" data-f="enabled"
          ${s.enabled ? "checked" : ""}> <span>Enabled</span></label>
      </div>
      <div class="source-result"></div>
    </div>`;
  }).join("");
}

// Keep the draft in sync as fields change, and re-render only when the ordering
// or labelling could have changed.
$("#c-sources").addEventListener("input", (ev) => {
  const card = ev.target.closest(".source-card");
  if (!card) return;
  const src = state.draftSources[Number(card.dataset.i)];
  const field = ev.target.dataset.f;
  if (!src || !field) return;
  if (field === "priority") src.priority = Number(ev.target.value || 0);
  else if (ev.target.type === "checkbox") src[field] = ev.target.checked;
  else src[field] = ev.target.value;
  if (field === "priority" || field === "enabled") renderSources();
});

$("#c-sources").addEventListener("change", (ev) => {
  const card = ev.target.closest(".source-card");
  if (!card || ev.target.dataset.f !== "network") return;
  state.draftSources[Number(card.dataset.i)].network = ev.target.value || null;
});

$("#c-sources").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const card = btn.closest(".source-card");
  const idx = Number(card.dataset.i);
  if (btn.dataset.act === "remove-source") {
    if (state.draftSources.length < 2) return;
    state.draftSources.splice(idx, 1);
    renderSources();
    return;
  }
  // test-source
  const src = state.draftSources[idx];
  if (!src.command.trim()) { toast("Enter a command for this source first", "err"); return; }
  const out = card.querySelector(".source-result");
  btn.disabled = true; btn.textContent = "Testing…";
  out.innerHTML = '<div class="hint" style="margin-top:8px">Running for 8 seconds…</div>';
  try {
    const r = await api("/api/test", {
      method: "POST",
      body: { command: src.command, use_shell: src.use_shell,
              profile: $("#test-profile").value || null, duration: 8 },
    });
    out.innerHTML = renderTestResult(r);
  } catch (err) {
    out.innerHTML = `<div class="hint" style="margin-top:8px;color:#ffb3b5">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "Test";
  }
});

$("#add-source").addEventListener("click", () => {
  state.draftSources.push(blankSource(state.draftSources.length));
  renderSources();
});

function openChannelModal(channel) {
  state.editingChannel = channel ? channel.id : null;
  $("#channel-modal-title").textContent = channel ? "Edit channel" : "Add channel";
  $("#c-name").value = channel?.name ?? "";
  $("#c-id").value = channel?.id ?? "";
  state.draftSources = channel?.sources?.length
    ? channel.sources.map((s) => ({ ...s }))
    : [blankSource(0)];
  renderSources();
  $("#c-group").value = (channel?.groups ?? []).join(", ");
  // Offer the groups already in use so they stay consistent across channels.
  $("#group-list").innerHTML = allGroups()
    .map((g) => `<option value="${esc(g)}"></option>`).join("");
  $("#c-number").value = channel?.channel_number ?? "";
  $("#c-tvgid").value = channel?.tvg_id ?? "";
  $("#c-logo").value = channel?.logo ?? "";
  $("#c-maxclients").value = channel?.max_clients ?? "";
  $("#c-enabled").checked = channel?.enabled ?? true;
  profileOptions($("#c-profile"), {
    includeNone: true, noneLabel: "None (pass through)", value: channel?.default_profile,
  });
  profileOptions($("#test-profile"), {
    includeNone: true, noneLabel: "Test without transcode", value: "",
  });
  $("#test-result").innerHTML = "";
  $("#channel-modal").hidden = false;
  $("#c-name").focus();
}

function channelFromForm() {
  const num = $("#c-number").value.trim();
  const max = $("#c-maxclients").value.trim();
  // Start from the channel as the server has it. PUT replaces the whole record,
  // so anything this form does not show - the EPG mapping in particular - has to
  // be carried over or saving the channel would silently wipe it.
  const existing = state.editingChannel
    ? state.channels.find((c) => c.id === state.editingChannel) || {}
    : {};
  return {
    ...existing,
    id: $("#c-id").value.trim(),
    name: $("#c-name").value.trim(),
    sources: state.draftSources.map((s, i) => ({
      id: s.id || `src${i + 1}`,
      name: s.name.trim(),
      command: s.command.trim(),
      use_shell: !!s.use_shell,
      enabled: !!s.enabled,
      network: s.network || null,
      priority: Number(s.priority) || 0,
    })),
    enabled: $("#c-enabled").checked,
    groups: $("#c-group").value.split(",").map((g) => g.trim()).filter(Boolean),
    logo: $("#c-logo").value.trim(),
    tvg_id: $("#c-tvgid").value.trim(),
    channel_number: num === "" ? null : Number(num),
    default_profile: $("#c-profile").value || null,
    max_clients: max === "" ? null : Number(max),
  };
}

$("#add-channel").addEventListener("click", () => openChannelModal(null));
$("#channel-cancel").addEventListener("click", () => { $("#channel-modal").hidden = true; });

// Auto-fill the ID from the name while adding, until the user edits it directly.
$("#c-name").addEventListener("input", () => {
  if (state.editingChannel !== null) return;
  if ($("#c-id").dataset.touched === "1") return;
  $("#c-id").value = slugify($("#c-name").value);
});
$("#c-id").addEventListener("input", () => { $("#c-id").dataset.touched = "1"; });

$("#channel-save").addEventListener("click", async () => {
  const form = $("#channel-form");
  if (!form.reportValidity()) return;
  if (!state.draftSources.some((s) => s.command.trim())) {
    toast("At least one source needs a command", "err");
    return;
  }
  const blank = state.draftSources.findIndex((s) => !s.command.trim());
  if (blank !== -1) {
    toast(`Source ${blank + 1} has no command — fill it in or remove it`, "err");
    return;
  }
  if (!state.draftSources.some((s) => s.enabled)) {
    toast("At least one source must be enabled", "err");
    return;
  }
  const channel = channelFromForm();
  try {
    if (state.editingChannel !== null) {
      await api(`/api/channels/${encodeURIComponent(state.editingChannel)}`,
        { method: "PUT", body: channel });
      toast(`Saved ${channel.name}`);
    } else {
      await api("/api/channels", { method: "POST", body: channel });
      toast(`Added ${channel.name}`);
    }
    $("#channel-modal").hidden = true;
    $("#c-id").dataset.touched = "";
    refresh();
  } catch (err) {
    toast(err.message, "err");
  }
});

function renderTestResult(r) {
  const rate = r.bytes && r.duration ? fmtBits((r.bytes * 8) / r.duration) : "—";
  let probeHtml = "";
  if (r.probe) {
    const rows = [`<dt>Container</dt><dd>${esc(r.probe.format || "?")}</dd>`];
    for (const s of r.probe.streams || []) {
      const extra = s.type === "video"
        ? `${s.resolution || ""} ${s.fps && s.fps !== "0/0" ? s.fps + " fps" : ""}`
        : s.type === "audio" ? `${s.channels || "?"}ch ${s.sample_rate || ""}Hz` : "";
      rows.push(`<dt>${esc(s.type || "stream")} #${s.index}</dt><dd>${esc(s.codec || "?")} ${esc(extra)}</dd>`);
    }
    probeHtml = `<dl class="probe-grid">${rows.join("")}</dl>`;
  }
  return `<div class="panel" style="margin:10px 0 0">
    <h2>${r.ok ? '<span class="badge running">stream ok</span>' : '<span class="badge error">no stream</span>'}
      <div class="spacer"></div>
      <span class="sub">${fmtBytes(r.bytes)} in ${r.duration}s · ${rate}</span>
    </h2>
    <div class="panel-body">
      ${r.error ? `<p style="margin-top:0;color:#ffb3b5">${esc(r.error)}</p>` : ""}
      ${probeHtml}
      ${r.stderr ? `<label style="margin-top:12px">Command output</label><pre class="log">${esc(r.stderr)}</pre>` : ""}
    </div></div>`;
}

$("#channel-rows").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  const channel = state.channels.find((c) => c.id === id);
  const act = btn.dataset.act;

  if (act === "edit") {
    openChannelModal(channel);
  } else if (act === "delete") {
    if (!confirm(`Delete channel "${channel.name}"? Any viewers will be disconnected.`)) return;
    try {
      await api(`/api/channels/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast(`Deleted ${channel.name}`);
      refresh();
    } catch (err) { toast(err.message, "err"); }
  } else if (act === "copy") {
    const params = new URLSearchParams();
    if (channel.default_profile) params.set("profile", channel.default_profile);
    if (state.protectStreams && state.token) params.set("token", state.token);
    const qs = params.toString();
    copy(`${state.baseUrl.replace(/\/$/, "")}/stream/${encodeURIComponent(id)}${qs ? "?" + qs : ""}`);
  } else if (act === "log") {
    openLog(btn.dataset.key);
  }
});

/* ------------------------------------------------------------- profile CRUD */

function openProfileModal(profile) {
  state.editingProfile = profile ? profile.id : null;
  $("#profile-modal-title").textContent = profile ? "Edit profile" : "Add profile";
  $("#p-name").value = profile?.name ?? "";
  $("#p-id").value = profile?.id ?? "";
  $("#p-desc").value = profile?.description ?? "";
  $("#p-input").value = profile?.input_args ?? "";
  $("#p-output").value = profile?.output_args ?? "";
  $("#p-container").value = profile?.container ?? "mpegts";
  $("#profile-modal").hidden = false;
  $("#p-name").focus();
}

$("#add-profile").addEventListener("click", () => openProfileModal(null));
$("#profile-cancel").addEventListener("click", () => { $("#profile-modal").hidden = true; });

$("#p-name").addEventListener("input", () => {
  if (state.editingProfile !== null) return;
  if ($("#p-id").dataset.touched === "1") return;
  $("#p-id").value = slugify($("#p-name").value);
});
$("#p-id").addEventListener("input", () => { $("#p-id").dataset.touched = "1"; });

$("#profile-save").addEventListener("click", async () => {
  if (!$("#profile-form").reportValidity()) return;
  const profile = {
    id: $("#p-id").value.trim(),
    name: $("#p-name").value.trim(),
    description: $("#p-desc").value.trim(),
    input_args: $("#p-input").value.trim(),
    output_args: $("#p-output").value.trim(),
    container: $("#p-container").value.trim() || "mpegts",
  };
  try {
    if (state.editingProfile !== null) {
      await api(`/api/profiles/${encodeURIComponent(state.editingProfile)}`,
        { method: "PUT", body: profile });
    } else {
      await api("/api/profiles", { method: "POST", body: profile });
    }
    toast(`Saved ${profile.name}`);
    $("#profile-modal").hidden = true;
    $("#p-id").dataset.touched = "";
    refresh();
  } catch (err) { toast(err.message, "err"); }
});

$("#profile-rows").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  const profile = state.profiles.find((p) => p.id === id);
  if (btn.dataset.act === "edit") {
    openProfileModal(profile);
  } else if (btn.dataset.act === "delete") {
    const users = state.channels.filter((c) => c.default_profile === id).map((c) => c.name);
    const warn = users.length ? `\n\n${users.length} channel(s) use it as their default: ${users.join(", ")}` : "";
    if (!confirm(`Delete profile "${profile.name}"?${warn}`)) return;
    try {
      await api(`/api/profiles/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast(`Deleted ${profile.name}`);
      refresh();
    } catch (err) { toast(err.message, "err"); }
  }
});

/* ------------------------------------------------------------- network CRUD */

function openNetworkModal(network) {
  state.editingNetwork = network ? network.id : null;
  $("#network-modal-title").textContent = network ? "Edit network" : "Add network";
  $("#n-name").value = network?.name ?? "";
  $("#n-id").value = network?.id ?? "";
  $("#n-desc").value = network?.description ?? "";
  $("#n-max").value = network?.max_streams ?? 1;
  $("#n-enabled").checked = network?.enabled ?? true;
  $("#network-modal").hidden = false;
  $("#n-name").focus();
}

$("#add-network").addEventListener("click", () => openNetworkModal(null));
$("#network-cancel").addEventListener("click", () => { $("#network-modal").hidden = true; });

$("#n-name").addEventListener("input", () => {
  if (state.editingNetwork !== null) return;
  if ($("#n-id").dataset.touched === "1") return;
  $("#n-id").value = slugify($("#n-name").value);
});
$("#n-id").addEventListener("input", () => { $("#n-id").dataset.touched = "1"; });

$("#network-save").addEventListener("click", async () => {
  if (!$("#network-form").reportValidity()) return;
  const network = {
    id: $("#n-id").value.trim(),
    name: $("#n-name").value.trim(),
    description: $("#n-desc").value.trim(),
    max_streams: Number($("#n-max").value) || 0,
    enabled: $("#n-enabled").checked,
  };
  try {
    if (state.editingNetwork !== null) {
      await api(`/api/networks/${encodeURIComponent(state.editingNetwork)}`,
        { method: "PUT", body: network });
    } else {
      await api("/api/networks", { method: "POST", body: network });
    }
    toast(`Saved ${network.name}`);
    $("#network-modal").hidden = true;
    $("#n-id").dataset.touched = "";
    refresh();
  } catch (err) { toast(err.message, "err"); }
});

$("#network-rows").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  const network = state.networks.find((n) => n.id === id);
  if (btn.dataset.act === "edit") {
    openNetworkModal(network);
  } else if (btn.dataset.act === "delete") {
    const warn = network.channels
      ? `\n\n${network.channels} channel(s) have sources on it. Those sources become uncapped.`
      : "";
    if (!confirm(`Delete network "${network.name}"?${warn}`)) return;
    try {
      await api(`/api/networks/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast(`Deleted ${network.name}`);
      refresh();
    } catch (err) { toast(err.message, "err"); }
  }
});

/* -------------------------------------------------------------------- EPG */

function fmtAge(seconds) {
  if (seconds == null) return "never";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function renderEpg() {
  const e = state.epg;
  if (!e) return;

  $("#epg-rows").innerHTML = (e.sources || []).map((s) => {
    const target = s.kind === "command" ? s.command : s.kind === "url" ? s.url : s.path;
    const status = s.refreshing
      ? '<span class="badge starting">refreshing</span>'
      : s.last_error
        ? `<span class="badge error">failed</span>`
        : s.last_refresh
          ? `<span class="badge running">ok</span>`
          : '<span class="badge idle">never run</span>';
    return `<tr data-id="${esc(s.id)}" class="${s.enabled ? "" : "disabled"}">
      <td><div class="name">${esc(s.name)}</div><div class="sub">${esc(s.id)}</div></td>
      <td><span class="badge">${esc(s.kind)}</span></td>
      <td><span class="cmd" title="${esc(target)}">${esc(target)}</span>
          ${s.last_error ? `<div class="sub" style="color:var(--err)" title="${esc(s.last_error)}">${esc(s.last_error.slice(0, 70))}</div>` : ""}</td>
      <td>${status}<div class="sub">${fmtAge(s.age_seconds)} · every ${s.refresh_hours}h</div></td>
      <td class="num">${s.channels || "—"}</td>
      <td class="num">${s.programmes ? s.programmes.toLocaleString() : "—"}</td>
      <td class="num">${fmtBytes(s.bytes)}</td>
      <td class="actions">
        <button class="link small" data-act="refresh">Refresh</button>
        <button class="small" data-act="edit">Edit</button>
        <button class="small danger" data-act="delete">Delete</button>
      </td>
    </tr>`;
  }).join("");
  $("#epg-empty").hidden = (e.sources || []).length > 0;

  // Type-ahead list of every upstream channel, shared by all mapping rows.
  $("#epg-channel-list").innerHTML = (e.channels || [])
    .map((c) => `<option value="${esc(c.id)}">${esc(c.label)} — ${c.programmes} programmes</option>`)
    .join("");

  const byId = Object.fromEntries((e.channels || []).map((c) => [c.id, c]));
  // Never rebuild the table out from under someone who is typing in it: the
  // edit commits on change/blur, and replacing the DOM first would discard it.
  if ($("#epg-map-rows").contains(document.activeElement)) return;
  $("#epg-map-rows").innerHTML = (e.mapping || []).map((m) => {
    const up = m.matched ? byId[m.matched] : null;
    let status;
    if (!m.epg_enabled) status = '<span class="badge off">excluded</span>';
    else if (!m.auto_mode && !m.assigned) status = '<span class="badge off">no guide (pinned)</span>';
    else if (!m.matched) status = '<span class="badge error">no guide</span>';
    else if (!m.auto_mode) status = '<span class="badge running">pinned</span>';
    else status = '<span class="badge starting">auto-matched</span>';
    return `<tr data-id="${esc(m.channel_id)}">
      <td><div class="name">${esc(m.channel_name)}</div><div class="sub">${esc(m.channel_id)}</div></td>
      <td class="sub">${esc(m.guide_id)}</td>
      <td>
        <input type="text" list="epg-channel-list" data-f="epg"
               value="${esc(m.auto_mode ? "" : (m.assigned || ""))}"
               placeholder="${esc(m.auto ? "auto: " + m.auto : "type to search, or leave blank for no guide")}"
               style="min-width:230px">
        ${up ? `<div class="sub">${esc(up.label)}</div>` : ""}
        ${!m.auto_mode ? '<button type="button" class="link small" data-act="auto">use auto-match</button>' : ""}
      </td>
      <td>${status}</td>
      <td class="num">${up ? up.programmes.toLocaleString() : "—"}</td>
      <td class="actions">
        <label class="check" style="justify-content:flex-end">
          <input type="checkbox" data-f="enabled" ${m.epg_enabled ? "checked" : ""}>
          <span class="sub">include</span>
        </label>
      </td>
    </tr>`;
  }).join("");
  $("#epg-map-empty").hidden = (e.mapping || []).length > 0;

  const total = e.total_channels || 0;
  $("#epg-map-summary").textContent = `${e.matched || 0} of ${total} channels have a guide`;
  $("#epg-summary").textContent =
    `${(e.channels || []).length} XMLTV channels · ${fmtBytes(e.disk_bytes)} cached`;

  const base = state.baseUrl.replace(/\/$/, "");
  const qs = state.protectStreams && state.token
    ? "?" + new URLSearchParams({ token: state.token }).toString() : "";
  $("#epg-url").value = `${base}/xmltv.xml${qs}`;
  $("#open-epg").href = `${base}/xmltv.xml${qs}`;
}

async function refreshEpg() {
  try {
    state.epg = await api("/api/epg");
    renderEpg();
  } catch (err) {
    if (err.message !== "authentication required") console.error(err);
  }
}

$("#copy-epg").addEventListener("click", () => copy($("#epg-url").value));

// Committing a mapping on change (not every keystroke) keeps the datalist usable.
$("#epg-map-rows").addEventListener("change", async (ev) => {
  const row = ev.target.closest("tr");
  if (!row) return;
  const channelId = row.dataset.id;
  try {
    if (ev.target.dataset.f === "epg") {
      const value = ev.target.value.trim();
      const r = await api("/api/epg/mapping", {
        method: "PUT", body: { mapping: { [channelId]: value || null } },
      });
      if (r.unknown?.length) toast(`No EPG channel called "${r.unknown[0]}" (yet)`, "err");
      else toast(value ? "Mapping pinned" : "Pinned to no guide");
    } else if (ev.target.dataset.f === "enabled") {
      const ch = state.channels.find((c) => c.id === channelId);
      await api(`/api/channels/${encodeURIComponent(channelId)}`, {
        method: "PUT", body: { ...ch, epg_enabled: ev.target.checked },
      });
      toast(ev.target.checked ? "Included in guide" : "Excluded from guide");
    }
    await Promise.all([refresh(), refreshEpg()]);
  } catch (err) { toast(err.message, "err"); }
});

$("#epg-map-rows").addEventListener("click", async (ev) => {
  const btn = ev.target.closest('button[data-act="auto"]');
  if (!btn) return;
  const channelId = btn.closest("tr").dataset.id;
  try {
    await api("/api/epg/mapping", { method: "PUT", body: { auto: [channelId] } });
    toast("Back to auto-matching");
    await Promise.all([refresh(), refreshEpg()]);
  } catch (err) { toast(err.message, "err"); }
});

$("#automap").addEventListener("click", () => runAutomap(false));
$("#automap-all").addEventListener("click", () => {
  if (!confirm("Re-match every channel?\n\nThis OVERWRITES every mapping you pinned by hand.")) return;
  runAutomap(true);
});

async function runAutomap(overwrite) {
  try {
    const r = await api(`/api/epg/automap?overwrite=${overwrite}`, { method: "POST" });
    toast(r.mapped ? `Matched ${r.mapped} channel(s)` : "Nothing new to match");
    await Promise.all([refresh(), refreshEpg()]);
  } catch (err) { toast(err.message, "err"); }
}

$("#refresh-all-epg").addEventListener("click", async () => {
  const btn = $("#refresh-all-epg");
  btn.disabled = true; btn.textContent = "Refreshing…";
  try {
    const r = await api("/api/epg/refresh", { method: "POST" });
    const failed = r.filter((s) => s.last_error);
    toast(failed.length ? `${failed.length} source(s) failed` : `Refreshed ${r.length} source(s)`,
          failed.length ? "err" : "ok");
    await refreshEpg();
  } catch (err) { toast(err.message, "err"); }
  finally { btn.disabled = false; btn.textContent = "Refresh all"; }
});

$("#epg-rows").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  const source = (state.epg.sources || []).find((s) => s.id === id);
  const act = btn.dataset.act;

  if (act === "edit") {
    openEpgModal(source);
  } else if (act === "delete") {
    if (!confirm(`Delete EPG source "${source.name}"? Its cached guide is removed too.`)) return;
    try {
      await api(`/api/epg/sources/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast(`Deleted ${source.name}`);
      refreshEpg();
    } catch (err) { toast(err.message, "err"); }
  } else if (act === "refresh") {
    btn.disabled = true; btn.textContent = "…";
    try {
      const s = await api(`/api/epg/sources/${encodeURIComponent(id)}/refresh`, { method: "POST" });
      toast(s.last_error ? s.last_error : `${s.channels} channels, ${s.programmes} programmes`,
            s.last_error ? "err" : "ok");
      await Promise.all([refreshEpg(), refresh()]);
    } catch (err) { toast(err.message, "err"); }
    finally { btn.disabled = false; btn.textContent = "Refresh"; }
  }
});

function epgKindChanged() {
  const kind = $("#e-kind").value;
  $$("#epg-form [data-kind]").forEach((el) => { el.hidden = el.dataset.kind !== kind; });
}
$("#e-kind").addEventListener("change", epgKindChanged);

function openEpgModal(source) {
  state.editingEpg = source ? source.id : null;
  $("#epg-modal-title").textContent = source ? "Edit EPG source" : "Add EPG source";
  $("#e-name").value = source?.name ?? "";
  $("#e-id").value = source?.id ?? "";
  $("#e-kind").value = source?.kind ?? "command";
  $("#e-command").value = source?.command ?? "";
  $("#e-url").value = source?.url ?? "";
  $("#e-path").value = source?.path ?? "";
  $("#e-shell").checked = source?.use_shell ?? false;
  $("#e-refresh").value = source?.refresh_hours ?? 12;
  $("#e-enabled").checked = source?.enabled ?? true;
  $("#epg-test-result").innerHTML = "";
  epgKindChanged();
  $("#epg-modal").hidden = false;
  $("#e-name").focus();
}

$("#add-epg").addEventListener("click", () => openEpgModal(null));
$("#epg-cancel").addEventListener("click", () => { $("#epg-modal").hidden = true; });

$("#e-name").addEventListener("input", () => {
  if (state.editingEpg !== null) return;
  if ($("#e-id").dataset.touched === "1") return;
  $("#e-id").value = slugify($("#e-name").value);
});
$("#e-id").addEventListener("input", () => { $("#e-id").dataset.touched = "1"; });

$("#epg-save").addEventListener("click", async () => {
  if (!$("#epg-form").reportValidity()) return;
  const body = {
    id: $("#e-id").value.trim(),
    name: $("#e-name").value.trim(),
    kind: $("#e-kind").value,
    command: $("#e-command").value.trim(),
    url: $("#e-url").value.trim(),
    path: $("#e-path").value.trim(),
    use_shell: $("#e-shell").checked,
    refresh_hours: Number($("#e-refresh").value) || 12,
    enabled: $("#e-enabled").checked,
  };
  const btn = $("#epg-save");
  btn.disabled = true; btn.textContent = "Saving…";
  try {
    if (state.editingEpg !== null) {
      await api(`/api/epg/sources/${encodeURIComponent(state.editingEpg)}`,
        { method: "PUT", body });
      const s = await api(`/api/epg/sources/${encodeURIComponent(body.id)}/refresh`,
        { method: "POST" });
      toast(s.last_error || `${s.channels} channels, ${s.programmes} programmes`,
            s.last_error ? "err" : "ok");
    } else {
      await api("/api/epg/sources", { method: "POST", body });
      toast("Added — fetching the guide in the background");
    }
    $("#epg-modal").hidden = true;
    $("#e-id").dataset.touched = "";
    await refreshEpg();
  } catch (err) { toast(err.message, "err"); }
  finally { btn.disabled = false; btn.textContent = "Save & refresh"; }
});

/* ------------------------------------------------------------------ audit */

const AUDIT_BADGE = {
  ok: '<span class="badge running">pass</span>',
  failed: '<span class="badge error">fail</span>',
  skipped: '<span class="badge off">skip</span>',
  testing: '<span class="badge starting">testing</span>',
  pending: '<span class="badge idle">queued</span>',
};

function renderAudit() {
  const a = state.audit;
  if (!a) return;
  const running = a.running;
  $("#audit-start").disabled = running;
  $("#audit-start").textContent = running ? "Running…" : "Run audit";
  $("#audit-stop").hidden = !running;

  const c = a.counts || {};
  if (a.total) {
    const pct = Math.round((a.completed / a.total) * 100);
    $("#audit-progress").innerHTML =
      `<div class="row"><b>${a.completed}/${a.total}</b> checked` +
      `<span style="color:var(--ok)">${c.ok || 0} pass</span>` +
      `<span style="color:var(--err)">${c.failed || 0} fail</span>` +
      (c.skipped ? `<span style="color:var(--warn)">${c.skipped} skipped</span>` : "") +
      `<span class="sub">${Math.round(a.elapsed)}s</span></div>` +
      `<div style="background:var(--bg);border:1px solid var(--border);border-radius:4px;height:6px;margin-top:8px;overflow:hidden">` +
      `<div style="height:100%;width:${pct}%;background:${running ? "var(--accent)" : "var(--ok)"}"></div></div>` +
      (a.message ? `<div class="sub" style="color:var(--err)">${esc(a.message)}</div>` : "");
  }

  $("#audit-rows").innerHTML = (a.results || []).map((r) => `
    <tr data-channel="${esc(r.channel_id)}">
      <td>${AUDIT_BADGE[r.status] || esc(r.status)}</td>
      <td><div class="name">${esc(r.channel_name)}</div>
          <div class="sub">${r.channel_number != null ? r.channel_number + " · " : ""}${esc(r.channel_id)}</div></td>
      <td>${esc(r.source_name || r.source_id)}
          ${r.priority ? `<div class="sub">priority ${r.priority}</div>` : ""}</td>
      <td class="sub">${esc(r.network || "")}</td>
      <td class="sub">${esc(r.video || "")}</td>
      <td class="sub">${esc(r.audio || "")}</td>
      <td class="num">${fmtBits(r.bitrate_bps)}</td>
      <td>${r.error
            ? `<span class="sub" style="color:var(--err)" title="${esc(r.stderr || r.error)}">${esc(r.error.slice(0, 70))}</span>`
            : `<span class="sub">${fmtBytes(r.bytes)} in ${r.duration}s</span>`}</td>
      <td class="actions">
        <button class="link small" data-act="retest">Re-test</button>
      </td>
    </tr>`).join("");
  $("#audit-empty").hidden = (a.results || []).length > 0;
}

async function refreshAudit() {
  try {
    state.audit = await api("/api/audit");
    renderAudit();
  } catch (err) {
    if (err.message !== "authentication required") console.error(err);
  }
}

async function startAudit(channel) {
  const params = new URLSearchParams({
    duration: $("#audit-duration").value || 8,
    concurrency: $("#audit-concurrency").value || 3,
  });
  if (channel) params.set("channel", channel);
  try {
    state.audit = await api("/api/audit?" + params.toString(), { method: "POST" });
    renderAudit();
  } catch (err) { toast(err.message, "err"); }
}

$("#audit-start").addEventListener("click", () => startAudit(null));
$("#audit-stop").addEventListener("click", async () => {
  try {
    state.audit = await api("/api/audit/stop", { method: "POST" });
    renderAudit();
    toast("Audit stopped");
  } catch (err) { toast(err.message, "err"); }
});
$("#audit-rows").addEventListener("click", (ev) => {
  const btn = ev.target.closest('button[data-act="retest"]');
  if (btn) startAudit(btn.closest("tr").dataset.channel);
});

/* ---------------------------------------------------------------- sessions */

$("#session-rows").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const key = btn.closest("tr").dataset.key;
  if (btn.dataset.act === "stop") {
    try {
      await api(`/api/sessions/${encodeURIComponent(key)}/stop`, { method: "POST" });
      toast("Session stopped");
      refresh();
    } catch (err) { toast(err.message, "err"); }
  } else if (btn.dataset.act === "log") {
    openLog(key);
  }
});

async function openLog(key) {
  state.logKey = key;
  $("#log-modal-title").textContent = "Session log — " + key;
  $("#log-modal").hidden = false;
  await pollLog();
}

async function pollLog() {
  if (!state.logKey || $("#log-modal").hidden) return;
  try {
    const r = await api(`/api/sessions/${encodeURIComponent(state.logKey)}/log`);
    const body = $("#log-body");
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    body.textContent = r.lines.join("\n") || "(no output yet)";
    if (atBottom) body.scrollTop = body.scrollHeight;
  } catch (err) {
    $("#log-body").textContent = "Session ended: " + err.message;
  }
}

setInterval(() => { if ($("#log-follow").checked) pollLog(); }, 1500);
$("#log-close").addEventListener("click", () => {
  $("#log-modal").hidden = true;
  state.logKey = null;
});

/* ---------------------------------------------------------------- settings */

let settingsDirty = false;

function fillSettings() {
  const s = state.settings;
  $("#s-linger").value = s.linger_seconds;
  $("#s-prebuffer").value = (s.prebuffer_bytes / (1024 * 1024)).toFixed(1).replace(/\.0$/, "");
  $("#s-queue").value = s.client_queue_chunks;
  $("#s-startup").value = s.startup_timeout_seconds;
  $("#s-stall").value = s.stall_timeout_seconds;
  $("#s-reconnect").value = s.reconnect_delay_seconds;
  $("#s-backoff0").value = s.restart_backoff_seconds;
  $("#s-backoff").value = s.max_restart_backoff_seconds;
  $("#s-giveup").value = s.give_up_after_failures;
  $("#s-grace").value = s.terminate_grace_seconds;
  $("#s-maxclients").value = s.default_max_clients;
  $("#s-ffmpeg").value = s.ffmpeg_bin;
  $("#s-ffprobe").value = s.ffprobe_bin;
  $("#s-loglevel").value = s.ffmpeg_loglevel;
  $("#s-loglines").value = s.log_lines;
  $("#s-baseurl").value = s.public_base_url;
  $("#s-autorestart").checked = s.auto_restart;
  $("#s-epgpast").value = s.epg_past_hours;
  $("#s-epgfuture").value = s.epg_future_days;
  $("#s-epgplaylist").checked = s.epg_in_playlist;
  $("#s-multigroup").checked = s.playlist_multi_group;
  $("#s-tsanalysis").checked = s.ts_analysis;
  $("#s-sort").value = s.channel_sort;
}

$("#settings-form").addEventListener("input", () => { settingsDirty = true; });

$("#settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const body = {
    linger_seconds: Number($("#s-linger").value),
    prebuffer_bytes: Math.round(Number($("#s-prebuffer").value) * 1024 * 1024),
    client_queue_chunks: Number($("#s-queue").value),
    startup_timeout_seconds: Number($("#s-startup").value),
    stall_timeout_seconds: Number($("#s-stall").value),
    reconnect_delay_seconds: Number($("#s-reconnect").value),
    restart_backoff_seconds: Number($("#s-backoff0").value),
    max_restart_backoff_seconds: Number($("#s-backoff").value),
    give_up_after_failures: Number($("#s-giveup").value),
    terminate_grace_seconds: Number($("#s-grace").value),
    default_max_clients: Number($("#s-maxclients").value),
    ffmpeg_bin: $("#s-ffmpeg").value.trim(),
    ffprobe_bin: $("#s-ffprobe").value.trim(),
    ffmpeg_loglevel: $("#s-loglevel").value,
    log_lines: Number($("#s-loglines").value),
    public_base_url: $("#s-baseurl").value.trim(),
    auto_restart: $("#s-autorestart").checked,
    epg_past_hours: Number($("#s-epgpast").value),
    epg_future_days: Number($("#s-epgfuture").value),
    epg_in_playlist: $("#s-epgplaylist").checked,
    playlist_multi_group: $("#s-multigroup").checked,
    ts_analysis: $("#s-tsanalysis").checked,
    channel_sort: $("#s-sort").value,
  };
  try {
    await api("/api/settings", { method: "PUT", body });
    settingsDirty = false;
    toast("Settings saved — they apply to sessions started from now on");
    refresh();
  } catch (err) { toast(err.message, "err"); }
});

/* ---------------------------------------------------------------- playlist */

function copy(text) {
  const done = () => toast("Copied to clipboard");
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallback());
  } else {
    fallback();
  }
  function fallback() {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); }
    catch (_) { toast("Copy failed — select the URL manually", "err"); }
    document.body.removeChild(ta);
  }
}

$("#copy-playlist").addEventListener("click", () => copy($("#playlist-url").value));
$("#playlist-profile").addEventListener("change", renderPlaylistUrl);

/* ------------------------------------------------------------------- token */

$("#token-save").addEventListener("click", () => {
  state.token = $("#token-input").value.trim();
  localStorage.setItem("sm-token", state.token);
  $("#token-modal").hidden = true;
  startPolling();
});

$("#token-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") $("#token-save").click();
});

/* --------------------------------------------------------------- shortcuts */

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  for (const id of ["#channel-modal", "#profile-modal", "#network-modal", "#epg-modal", "#log-modal"]) {
    if (!$(id).hidden) { $(id).hidden = true; return; }
  }
});

$$(".modal-bg").forEach((bg) => {
  bg.addEventListener("mousedown", (ev) => {
    if (ev.target === bg && bg.id !== "token-modal") bg.hidden = true;
  });
});

/* ------------------------------------------------------------------- start */

(async function init() {
  try {
    const auth = await fetch("/api/auth" + (state.token ? "?token=" + encodeURIComponent(state.token) : ""))
      .then((r) => r.json());
    if (auth.required && !auth.ok) {
      $("#token-modal").hidden = false;
      return;
    }
  } catch (_) { /* fall through and let refresh() report it */ }
  startPolling();
})();
