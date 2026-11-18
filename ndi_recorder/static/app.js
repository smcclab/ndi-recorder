"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
// Rough SpeedHQ file bitrate written by the NDI recorder for one 1080p30 Maki Live (HX2 transcoded).
const EST_CAMERA_BPS = 107e6 / 8;
const EST_PROXY_BPS = 14e6 / 8;

let state = null;
let busy = false;
let lostConnection = false;
const cards = new Map();

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {} };
  if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}
const q = (name) => "source=" + encodeURIComponent(name);

function fmtBytes(b) {
  if (b == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
  return `${b.toFixed(b < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
function fmtDur(s) {
  if (s == null || !isFinite(s)) return "–";
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return [h, m, sec].map((n) => String(n).padStart(2, "0")).join(":");
}
function fmtHours(s) {
  if (s == null || !isFinite(s)) return "–";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

function banner(msg, kind = "error") {
  const b = $("#banner");
  if (!msg) { b.hidden = true; return; }
  b.textContent = msg;
  b.className = "banner " + kind;
  b.hidden = false;
}

// ---------- rendering ----------

function render() {
  const s = state;
  const sess = s.session;
  const recording = !!(sess && sess.active);
  document.body.classList.toggle("recording", recording);

  if (!s.recorder) banner("NDI recorder not found. Install NDI Tools from ndi.video/tools — it provides Application.Mac.NDI.Recording.");

  // transport
  const btn = $("#rec-btn");
  btn.textContent = recording ? "■ Stop" : "● Record all";
  const visible = s.cameras.filter((c) => !c.hidden);
  const toRecord = visible.filter((c) => c.selected && c.online);
  btn.disabled = busy || (!recording && (!s.folder.writable || !toRecord.length || !s.recorder));
  btn.title = !s.folder.writable ? "Choose a writable folder first" : !toRecord.length ? "No online cameras selected" : "";
  $("#take-name").disabled = recording;
  $("#timer").textContent = sess ? fmtDur(sess.elapsed) : "00:00:00";

  renderStorage(s, toRecord.length, recording);
  renderSession(sess);
  renderCameras(s, sess);

  // settings
  $("#opt-previews").checked = s.settings.previews;
  $("#opt-proxy").checked = s.settings.proxy;
  $("#opt-proxy").disabled = recording;
  const ips = $("#opt-extra-ips");
  if (document.activeElement !== ips) ips.value = s.settings.extra_ips;
  $("#recorder-path").textContent = "Recorder: " + (s.recorder || "not found");
  const hidden = s.cameras.filter((c) => c.hidden);
  const hl = $("#hidden-list");
  hl.replaceChildren();
  if (hidden.length) {
    hl.append("Hidden sources: ");
    for (const c of hidden) {
      const b = document.createElement("button");
      b.className = "ghost small";
      b.textContent = `Show ${c.label || c.name}`;
      b.onclick = () => camSettings(c.name, { hidden: false });
      hl.append(b, " ");
    }
  }
}

function renderStorage(s, nCams, recording) {
  const f = s.folder;
  const panel = $("#storage");
  panel.classList.toggle("unset", !f.path || !f.exists);
  $("#folder-path").textContent = f.path || "No folder chosen — pick a folder on your external drive";
  $("#folder-meta").textContent = f.exists
    ? `${f.volume} · ${f.fs}${f.internal ? " · internal disk" : " · external"}`
    : "";
  const w = $("#folder-warnings");
  w.replaceChildren(...(f.warnings || []).map((t) => Object.assign(document.createElement("li"), { textContent: t })));
  $("#choose-btn").disabled = recording;
  $("#browse-btn").disabled = recording;

  if (f.exists && f.total) {
    $("#space-bar").style.width = `${(100 * (f.total - f.free)) / f.total}%`;
    const sess = s.session;
    let rate, how;
    if (recording && sess.bytes_per_sec > 0) {
      rate = sess.bytes_per_sec;
      how = "at the current measured rate";
    } else {
      rate = nCams * (EST_CAMERA_BPS + (s.settings.proxy ? EST_PROXY_BPS : 0));
      how = `for ${nCams} camera${nCams === 1 ? "" : "s"} (≈${fmtBytes(rate * 3600)}/hour, estimated)`;
    }
    const left = rate > 0 ? Math.max(0, f.free - 5e9) / rate : null;
    $("#space-text").innerHTML =
      `<b>${fmtBytes(f.free)}</b> free of ${fmtBytes(f.total)} · ` +
      (rate > 0 ? `room for about <b>${fmtHours(left)}</b> ${how}` : "select cameras to estimate recording time");
  } else {
    $("#space-bar").style.width = "0";
    $("#space-text").textContent = "";
  }
}

function renderSession(sess) {
  const el = $("#session");
  if (!sess) { el.hidden = true; return; }
  el.hidden = false;
  $("#session-dir").textContent = sess.dir;
  const total = sess.cameras.reduce((a, c) => a + c.bytes, 0);
  let txt = `<b>${fmtBytes(total)}</b> written`;
  if (sess.active) {
    txt += ` · ${fmtBytes(sess.bytes_per_sec)}/s · <b>${fmtHours(sess.seconds_left)}</b> of space left`;
  } else {
    txt += ` · finished (${sess.stop_reason})`;
  }
  $("#session-stats").innerHTML = txt;
}

function renderCameras(s, sess) {
  const grid = $("#cameras");
  const visible = s.cameras.filter((c) => !c.hidden);
  $("#empty").hidden = visible.length > 0;
  const recByName = new Map((sess ? sess.cameras : []).map((c) => [c.source, c]));
  const seen = new Set();

  for (const cam of visible) {
    seen.add(cam.name);
    let card = cards.get(cam.name);
    if (!card) {
      card = makeCard(cam.name);
      cards.set(cam.name, card);
      grid.append(card.el);
    }
    updateCard(card, cam, sess && sess.active ? recByName.get(cam.name) : null, sess && !sess.active ? recByName.get(cam.name) : null);
  }
  for (const [name, card] of cards) {
    if (!seen.has(name)) { card.el.remove(); cards.delete(name); }
  }
}

function makeCard(name) {
  const el = $("#cam-tpl").content.firstElementChild.cloneNode(true);
  const card = { el, name, img: $("img", el) };
  card.img.hidden = true;
  card.img.onload = () => { card.img.hidden = false; card.loading = false; };
  card.img.onerror = () => { card.img.hidden = true; card.loading = false; };

  const label = $(".cam-label", el);
  label.addEventListener("change", () => camSettings(name, { label: label.value }));
  label.addEventListener("keydown", (e) => { if (e.key === "Enter") label.blur(); });
  $(".cam-selected", el).addEventListener("change", (e) => camSettings(name, { selected: e.target.checked }));
  $(".hide-btn", el).addEventListener("click", () => camSettings(name, { hidden: true }));

  // press-and-hold zoom
  const speed = $(".zoom-speed", el);
  for (const [cls, dir] of [[".zoom-in", 1], [".zoom-out", -1]]) {
    const b = $(cls, el);
    const go = (e) => {
      e.preventDefault();
      b.setPointerCapture?.(e.pointerId);
      b.classList.add("active");
      zoom(name, dir * parseFloat(speed.value));
    };
    const stop = () => {
      if (!b.classList.contains("active")) return;
      b.classList.remove("active");
      zoom(name, 0);
    };
    b.addEventListener("pointerdown", go);
    b.addEventListener("pointerup", stop);
    b.addEventListener("pointercancel", stop);
    b.addEventListener("lostpointercapture", stop);
  }

  const presets = $(".presets", el);
  const storeMode = $(".store-mode", el);
  storeMode.addEventListener("change", () => presets.classList.toggle("storing", storeMode.checked));
  for (const b of presets.querySelectorAll("button")) {
    b.addEventListener("click", async () => {
      const store = storeMode.checked;
      try {
        await api(`/api/camera/preset?${q(name)}`, { body: { n: +b.dataset.n, store } });
        if (store) { storeMode.checked = false; presets.classList.remove("storing"); }
      } catch (err) { banner(err.message); }
    });
  }
  return card;
}

function updateCard(card, cam, rec, finished) {
  const el = card.el;
  el.classList.toggle("offline", !cam.online);
  el.classList.toggle("recording", !!rec && rec.state === "recording");
  el.classList.toggle("error", !!rec && ["error", "stalled"].includes(rec.state));

  const label = $(".cam-label", el);
  if (document.activeElement !== label) label.value = cam.label;
  const sel = $(".cam-selected", el);
  sel.checked = cam.selected;
  sel.disabled = !!(state.session && state.session.active);
  $(".cam-name", el).textContent = cam.name;

  const pills = [];
  pills.push(cam.online ? `<span class="pill ok">online</span>` : `<span class="pill bad">offline</span>`);
  if (cam.host) pills.push(`<span class="pill">${cam.host}</span>`);
  if (cam.resolution) pills.push(`<span class="pill">preview ${cam.resolution}${cam.frame_rate ? " @ " + cam.frame_rate : ""}</span>`);
  pills.push(cam.ptz ? `<span class="pill ok">PTZ</span>` : `<span class="pill">no PTZ</span>`);
  $(".cam-meta", el).innerHTML = pills.join("");

  for (const b of el.querySelectorAll(".zoom button, .presets button")) b.disabled = !cam.ptz;

  const web = $(".cam-web", el);
  web.hidden = !cam.web;
  if (cam.web) web.href = cam.web;

  // preview
  if (state.settings.previews && cam.preview_age != null && !card.loading) {
    card.loading = true;
    card.img.src = `/api/camera/preview.jpg?${q(cam.name)}&t=${Date.now()}`;
  } else if (!state.settings.previews || cam.preview_age == null) {
    card.img.hidden = true;
  }

  // recording status
  const r = rec || finished;
  $(".rec-badge", el).hidden = !(rec && rec.state === "recording");
  const vu = $(".vu > div", el);
  vu.style.height = rec && rec.vu_db != null ? `${Math.max(0, Math.min(100, ((rec.vu_db + 60) / 60) * 100))}%` : "0";
  const box = $(".cam-rec", el);
  if (!r) { box.innerHTML = ""; return; }
  const parts = [];
  const stateTxt = { connecting: "connecting…", recording: "recording", stopping: "finishing file…", stopped: "saved", error: "error", stalled: "no frames arriving!" }[r.state] || r.state;
  parts.push(`<b>${stateTxt}</b>`);
  if (r.resolution) parts.push(`${r.resolution}${r.frame_rate ? " @ " + r.frame_rate : ""}`);
  parts.push(`${r.frames} frames`, fmtBytes(r.bytes));
  let html = parts.join(" · ");
  html += `<div class="mono">${r.files.map((f) => f.split("/").pop()).join(", ")}</div>`;
  if (r.error) html += `<div class="err">${escapeHtml(r.error)}</div>`;
  box.innerHTML = html;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

// ---------- actions ----------

async function zoom(name, speed) {
  try { await api(`/api/camera/zoom?${q(name)}`, { body: { speed } }); }
  catch (err) { banner(err.message); }
}

async function camSettings(name, changes) {
  try { await api(`/api/camera/settings?${q(name)}`, { body: changes }); await refresh(); }
  catch (err) { banner(err.message); }
}

async function patchSettings(changes) {
  try { await api("/api/settings", { method: "PATCH", body: changes }); await refresh(); }
  catch (err) { banner(err.message); }
}

$("#rec-btn").addEventListener("click", async () => {
  const recording = state.session && state.session.active;
  busy = true;
  render();
  try {
    if (recording) await api("/api/record/stop", { body: {} });
    else await api("/api/record/start", { body: { name: $("#take-name").value } });
    banner(null);
  } catch (err) {
    banner(err.message);
  } finally {
    busy = false;
    await refresh();
  }
});

$("#choose-btn").addEventListener("click", async () => {
  banner("A folder chooser has opened on this Mac (it may be behind the browser window).", "info");
  try {
    const { path } = await api("/api/fs/choose", { body: {} });
    banner(null);
    if (path) await patchSettings({ folder: path });
  } catch (err) { banner(err.message); }
});

$("#reveal-btn").addEventListener("click", () => {
  if (state.session) api("/api/fs/reveal", { body: { path: state.session.dir } });
});

$("#opt-previews").addEventListener("change", (e) => patchSettings({ previews: e.target.checked }));
$("#opt-proxy").addEventListener("change", (e) => patchSettings({ proxy: e.target.checked }));
$("#opt-extra-ips").addEventListener("change", (e) => patchSettings({ extra_ips: e.target.value.trim() }));

// ---------- in-page folder browser ----------

let browsePath = "";
$("#browse-btn").addEventListener("click", async () => {
  const vols = await api("/api/volumes");
  const box = $("#volumes");
  box.replaceChildren(...vols.map((v) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "vol" + (v.internal ? "" : " external");
    b.innerHTML = `${escapeHtml(v.name)}<small>${v.internal ? "internal" : "external"} · ${v.fs} · ${fmtBytes(v.free)} free</small>`;
    b.onclick = () => browse(v.path);
    return b;
  }));
  await browse(state.folder.exists ? state.folder.path : (vols.find((v) => !v.internal) || vols[0] || { path: "/Volumes" }).path);
  $("#browser").showModal();
});

async function browse(path) {
  const d = await api(`/api/fs/list?path=${encodeURIComponent(path)}`);
  browsePath = d.path;
  $("#crumbs").textContent = d.path;
  const list = $("#dirs");
  const items = [];
  if (d.parent) {
    const up = document.createElement("li");
    up.className = "up";
    up.textContent = "↑ ..";
    up.onclick = () => browse(d.parent);
    items.push(up);
  }
  for (const name of d.dirs) {
    const li = document.createElement("li");
    li.textContent = "📁 " + name;
    li.onclick = () => browse(d.path.replace(/\/$/, "") + "/" + name);
    items.push(li);
  }
  list.replaceChildren(...items);
}

$("#use-dir-btn").addEventListener("click", async () => {
  $("#browser").close();
  await patchSettings({ folder: browsePath });
});

$("#mkdir-btn").addEventListener("click", async () => {
  const name = prompt("New folder name:", "NDI Recordings");
  if (!name) return;
  try {
    const { path } = await api("/api/fs/mkdir", { body: { path: browsePath, name } });
    await browse(path);
  } catch (err) { alert(err.message); }
});

// ---------- polling ----------

async function refresh() {
  try {
    state = await api("/api/state");
    if (lostConnection) { lostConnection = false; banner(null); }
    render();
  } catch (err) {
    lostConnection = true;
    banner("Lost connection to the recorder server: " + err.message);
  }
}

window.addEventListener("blur", () => {
  // safety: never leave a camera zooming if the window loses focus mid-press
  for (const card of cards.values()) {
    for (const b of card.el.querySelectorAll(".zoom button.active")) {
      b.classList.remove("active");
      zoom(card.name, 0);
    }
  }
});
window.addEventListener("beforeunload", (e) => {
  if (state && state.session && state.session.active) e.preventDefault();
});

refresh();
setInterval(refresh, 1000);
