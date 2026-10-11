"use strict";
/* BuzzTorrent TV: remote-control UI for the living-room TV.
   Talks to the BuzzTorrent server on the PC. Inside the Android TV app, window.BuzzTV
   is a small native bridge (open OTT apps, Google TV search, external player). */

const $ = (s, el = document) => el.querySelector(s);
const BRIDGE = window.BuzzTV || null;

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...kids.flat().filter((k) => k != null && k !== false));
  return el;
}

const store = {
  get(k, d) { try { const v = localStorage.getItem("tv." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("tv." + k, JSON.stringify(v)); } catch { /* storage off */ } },
};

const TABS = [["movies", "Movies"], ["tv", "TV Shows"], ["music", "Music"], ["all", "Everything"]];
const LANGS = [["any", "All languages"], ["hi", "Hindi"], ["bho", "Bhojpuri"], ["bn", "Bengali"], ["ur", "Urdu"],
  ["ta", "Tamil"], ["te", "Telugu"], ["mr", "Marathi"], ["pa", "Punjabi"], ["en", "English"]];
const LANG_NAME = Object.fromEntries(LANGS);
const ROWS = [["popular", "Most popular"], ["trending", "Trending this week"], ["newest", "Recently added"]];
const CAT_NOUN = { movies: "Film", tv: "TV", music: "Music", books: "Book", games: "Game", software: "Software" };
const VERDICT = { safe: "Safe", caution: "Caution", danger: "Risky", unknown: "Unrated" };

const state = {
  cat: store.get("cat", "movies"),
  lang: store.get("lang", "any"),
  query: "",
  stack: [],          // open overlays, top last
  focusBack: [],      // element to refocus when each overlay closes
  playlist: null,
  view: 0,            // bumps on every new view so late responses are dropped
  byId: new Map(),    // results shown so far, for commands from the phone remote
};
if (!TABS.some(([id]) => id === state.cat)) state.cat = "movies";
if (!LANG_NAME[state.lang]) state.lang = "any";

/* ------------------------------------------------------------------ api */
class Offline extends Error {}
class NeedPair extends Error {}

async function api(path, body) {
  const opts = body === undefined ? { credentials: "same-origin", cache: "no-store" } : {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-BuzzTorrent": "1" }, body: JSON.stringify(body),
  };
  let res;
  try { res = await fetch(path, opts); } catch { throw new Offline("Can't reach BuzzTorrent on your PC"); }
  let data;
  try { data = await res.json(); } catch { throw new Error(`The PC answered with an error (${res.status})`); }
  if (res.status === 401 && data.pair) { showPair(); throw new NeedPair("pair first"); }
  if (res.status === 403 && data.locked) { leaveAdult("The private section locked itself"); }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg, kind = "") {
  const t = h("div", { class: `toast ${kind}`, role: "status", text: msg });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 4200);
}
const spinner = () => h("span", { class: "spin", "aria-hidden": "true" });
const qs = (o) => new URLSearchParams(o).toString();

function fmtSize(b) {
  if (!b) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b >= 10 || i < 2 ? Math.round(b) : b.toFixed(1)} ${u[i]}`;
}
function fmtTime(s) {
  if (!isFinite(s)) return "0:00";
  s = Math.max(0, Math.floor(s));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = String(s % 60).padStart(2, "0");
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}
const playable = (r) => (r.sources || []).includes("Internet Archive");

/* ------------------------------------------------------------ chrome */
const adult = { shown: false, unlocked: false };
function renderChips() {
  const tabs = adult.shown ? [...TABS, ["adult", "🔒 18+"]] : TABS;
  const chips = tabs.map(([id, label]) => h("button", {
    type: "button", class: "chip", "aria-pressed": String(id === state.cat), text: label,
    onclick: () => {
      if (id === "adult" && !adult.unlocked) { askAdultPin(); return; }
      state.cat = id;
      if (id !== "adult") store.set("cat", id);           // the private tab is never remembered
      renderChips(); refresh();
    },
  }));
  if (state.cat === "adult") chips.push(h("button", { type: "button", class: "chip", text: "Lock", onclick: lockAdult }));
  $("#tabs").replaceChildren(...chips);
  $("#langs").replaceChildren(...LANGS.map(([id, label]) => h("button", {
    type: "button", class: "chip", "aria-pressed": String(id === state.lang), text: label,
    onclick: () => { state.lang = id; store.set("lang", id); renderChips(); refresh(); },
  })));
}
const refresh = () => (state.query ? runSearch(state.query, false) : renderHome(false));

function offlineView(err) {
  const main = $("#main");
  const btns = [h("button", { type: "button", class: "act primary", text: "Try again", onclick: () => refresh() })];
  if (BRIDGE && BRIDGE.resetServer) {
    btns.push(h("button", { type: "button", class: "act", text: "Choose another PC", onclick: () => BRIDGE.resetServer() }));
  }
  main.replaceChildren(h("div", { class: "status big" },
    h("div", { text: err instanceof Offline ? "Can't reach BuzzTorrent on your PC." : err.message }),
    h("div", { class: "d-note", text: err instanceof Offline ? "Make sure the PC is on and BuzzTorrent is running." : "" }),
    h("div", { class: "d-actions center" }, btns)));
  btns[0].focus();
}

/* ------------------------------------------------------------- cards */
function placeholder(title) { return h("div", { class: "ph", text: (title || "").slice(0, 48) }); }

function card(r) {
  let img = null;
  if (r.thumb) {
    img = h("img", { src: r.thumb, alt: "", loading: "lazy" });
    img.addEventListener("error", () => img.replaceWith(placeholder(r.title)), { once: true });
  }
  const meta = [r.year, CAT_NOUN[r.category], fmtSize(r.size)].filter(Boolean).join(" · ");
  const q = r.quality?.label;
  state.byId.set(r.id, r);
  return h("button", { type: "button", class: "card", onclick: () => openDetail(r) },
    h("div", { class: "thumb" }, img || placeholder(r.title),
      h("span", { class: `badge ${r.verdict}`, text: VERDICT[r.verdict] || r.verdict }),
      playable(r) ? h("span", { class: "badge play", text: "▶ Free" }) : null,
      q ? h("span", { class: `badge q q-${q.toLowerCase()}`, text: q }) : null),
    h("div", { class: "card-t", dir: "auto", text: r.title }),
    h("div", { class: "card-m", text: meta }));
}

/* -------------------------------------------------------------- home */
function renderHome(focusFirst = true) {
  state.query = "";
  $("#q").value = "";
  const view = ++state.view;
  const main = $("#main");
  if (state.cat === "adult") {
    main.replaceChildren(h("div", { class: "status big" }, "🔒 Private section. Nothing is saved: no Continue watching, no watchlist, no history.",
      h("div", { class: "d-note", text: "Search with the box at the top." })));
    if (focusFirst) $("#q").focus();
    return;
  }
  main.replaceChildren(h("p", { class: "phone-hint" }, "📱 Search from your phone: open ",
    h("b", { text: `${location.host}/remote` }), " on a phone on the same Wi-Fi"));
  // Rows load in parallel; focus lands on the topmost row with cards, once every row above it is done
  let focused = !focusFirst;
  const tryFocus = () => {
    if (focused || view !== state.view) return;
    for (const el of main.querySelectorAll("section.row, [data-pending]")) {
      if (el.dataset.pending || el.querySelector(".spin")) return;
      const first = el.querySelector(".row-track .card");
      if (first) { focused = true; first.focus({ preventScroll: true }); return; }
    }
  };
  const addRow = (parent, label, loader, makeCard = card, empty = null) => {
    const track = h("div", { class: "row-track" }, h("div", { class: "status" }, spinner(), "Loading…"));
    const sec = h("section", { class: "row" }, h("h2", { text: label }), track);
    parent.append(sec);
    loader().then((items) => {
      if (view !== state.view) return;
      if (!items.length) {
        if (empty === null) sec.remove();
        else track.replaceChildren(h("div", { class: "status", text: empty }));
      } else {
        track.replaceChildren(...items.map(makeCard));
      }
      tryFocus();
    }).catch((err) => {
      if (view !== state.view || err instanceof NeedPair) return;
      if (err instanceof Offline) return offlineView(err);
      track.replaceChildren(h("div", { class: "status", text: err.message }));
      tryFocus();
    });
  };

  addRow(main, "Continue watching", () => api("/api/tv/continue").then((d) => d.items), continueCard);
  addRow(main, "Your watchlist", () => api("/api/tv/watchlist").then((d) => {
    for (const a of (d.alerts || []).slice(0, 3)) toast(`🔔 ${a.text}`, "ok");
    if ((d.alerts || []).length) api("/api/tv/alerts/seen", {}).catch(() => {});
    return d.items.filter((w) => state.cat === "all" || w.category === state.cat || state.cat === "movies" && w.category === "all");
  }), watchCard);
  if (state.lang === "any") {
    // Indian classics rows; the box holds their place while the shelf list loads
    const box = h("div", { "data-pending": "1" });
    main.append(box);
    api("/api/tv/shelves?" + qs({ cat: state.cat })).then((d) => {
      if (view !== state.view) return;
      d.shelves.forEach((sh) => addRow(box, sh.label, () => api("/api/tv/shelf?" + qs({ source: sh.source, id: sh.id }))
        .then((x) => x.results.filter((r) => r.verdict !== "danger"))));
    }).catch(() => {}).finally(() => { delete box.dataset.pending; tryFocus(); });
  }
  ROWS.forEach(([sort, label]) => addRow(main, label,
    () => api("/api/browse?" + qs({ cat: state.cat, sort, lang: state.lang, page: 1, family: 1 }))
      .then((d) => d.results.filter((r) => r.verdict !== "danger")),
    card, "Nothing here yet for this language."));
}

function continueCard(c) {
  const pct = c.dur ? Math.min(100, Math.round((c.pos / c.dur) * 100)) : 0;
  const fill = h("span", { class: "cw-fill" });
  fill.style.width = `${pct}%`;
  let img = null;
  if (c.thumb && c.thumb.startsWith("/api/")) {
    img = h("img", { src: c.thumb, alt: "", loading: "lazy" });
    img.addEventListener("error", () => img.replaceWith(placeholder(c.title)), { once: true });
  }
  return h("button", { type: "button", class: "card", onclick: () => resumeItem(c) },
    h("div", { class: "thumb" }, img || placeholder(c.title),
      c.quality ? h("span", { class: "badge q", text: c.quality }) : null,
      h("span", { class: "cw-bar" }, fill)),
    h("div", { class: "card-t", dir: "auto", text: c.name ? `${c.title} · ${c.name}` : c.title }),
    h("div", { class: "card-m", text: `${c.key.startsWith("lib:") ? "On your PC · " : ""}${fmtTime(c.pos)} of ${fmtTime(c.dur)}` }));
}

function watchCard(w) {
  let img = null;
  if (w.thumb && w.thumb.startsWith("/api/")) {
    img = h("img", { src: w.thumb, alt: "", loading: "lazy" });
    img.addEventListener("error", () => img.replaceWith(placeholder(w.title)), { once: true });
  }
  const best = w.best ? qualityLabel(w.best) : "";
  return h("button", { type: "button", class: "card", onclick: () => {
    if (TABS.some(([id]) => id === w.category)) { state.cat = w.category; store.set("cat", w.category); renderChips(); }
    runSearch(w.title);
  } },
  h("div", { class: "thumb" }, img || placeholder(w.title),
    w.new ? h("span", { class: "badge new", text: "NEW" }) : null,
    w.free ? h("span", { class: "badge play", text: "▶ Free" }) : null,
    best ? h("span", { class: "badge q", text: best }) : null),
  h("div", { class: "card-t", dir: "auto", text: w.title }),
  h("div", { class: "card-m", text: w.new ? "Better copy found" : "On your watchlist" }));
}
function qualityLabel(height) {
  return height >= 4000 ? "8K" : height >= 2000 ? "4K" : height >= 1000 ? "1080p" : height >= 690 ? "720p" : height >= 440 ? "480p" : "Low";
}

async function resumeItem(c) {
  try {
    if (c.key.startsWith("lib:")) {
      const d = await api("/api/tv/library");
      const it = d.items.find((x) => x.key === c.key);
      if (!it) { toast("That video is no longer in the PC's download folder", "bad"); return; }
      return playLibrary(it, d.progress, true);
    }
    const ident = c.key.split(":")[1];
    const d = await api("/api/tv/play?" + qs({ ident }));
    const i = Math.max(0, d.files.findIndex((f) => f.key === c.key));
    state.playingThumb = c.thumb || "";
    if (native()) return playNative(d, i, c.title, d.files[i]?.resume || 0);
    const inApp = d.files.filter((f) => f.url);
    if (!inApp.length) { toast("This copy needs the BuzzTorrent TV app to play", "bad"); return; }
    startPlayer(inApp, Math.max(0, inApp.indexOf(d.files[i])), c.title, d.files[i]?.resume || 0);
  } catch (err) {
    if (!(err instanceof NeedPair)) toast(err.message, "bad");
  }
}

/* ------------------------------------------------------------ search */
async function runSearch(q, focusFirst = true) {
  q = q.trim();
  if (!q) return renderHome();
  state.query = q;
  $("#q").value = q;
  const view = ++state.view;
  const main = $("#main");
  const label = (TABS.find(([id]) => id === state.cat) || [, "the private section"])[1];
  main.replaceChildren(h("h2", { class: "section-h", text: `“${q}” in ${label}` + (state.lang !== "any" ? ` · ${LANG_NAME[state.lang]}` : "") }),
    h("div", { class: "status" }, spinner(), "Searching… this can take a few seconds"));
  let d;
  try {
    d = await api("/api/search?" + qs({ q, cat: state.cat, lang: state.lang }));
  } catch (err) {
    if (view !== state.view || err instanceof NeedPair) return;
    if (err instanceof Offline) return offlineView(err);
    main.lastChild.replaceWith(h("div", { class: "status", text: err.message }));
    return;
  }
  if (view !== state.view) return;
  state.lastSearch = d;
  drawResults(focusFirst);
}

/* Filters on the results, the same as on the PC: minimum quality and "safe only" */
const QUALITY_FILTERS = [["0", "Any quality"], ["690", "720p+"], ["1000", "1080p+"], ["2000", "4K+"]];
function drawResults(focusFirst = false) {
  const d = state.lastSearch;
  const main = $("#main");
  if (!d) return;
  const minQ = Number(store.get("minq", "0"));
  const safeOnly = store.get("safeonly", false);
  const all = d.results.filter((r) => r.verdict !== "danger");
  const items = all.filter((r) => (!safeOnly || r.verdict === "safe") && (!minQ || (r.quality?.height || 0) >= minQ));
  const hidden = d.results.length - all.length;
  const filters = h("div", { class: "dym filters" },
    QUALITY_FILTERS.map(([v, label]) => h("button", { type: "button", class: "chip", "aria-pressed": String(String(minQ) === v), text: label,
      onclick: () => { store.set("minq", v); drawResults(); $(".filters .chip[aria-pressed=true]")?.focus(); } })),
    h("button", { type: "button", class: "chip", "aria-pressed": String(safeOnly), text: safeOnly ? "✓ Safe only" : "Safe only",
      onclick: () => { store.set("safeonly", !safeOnly); drawResults(); $(".filters .chip:last-child")?.focus(); } }));
  const parts = [main.firstChild, filters];
  const dym = (d.did_you_mean || []).slice(0, 4);
  if (dym.length) {
    parts.push(h("div", { class: "dym" }, h("span", { class: "dym-l", text: "Did you mean" }),
      dym.map((w) => h("button", { type: "button", class: "chip", text: w.year ? `${w.title} (${w.year})` : w.title, onclick: () => runSearch(w.title) }))));
  }
  parts.push(items.length ? h("div", { class: "grid" }, items.map(card))
    : h("div", { class: "status big", text: all.length ? `${all.length - items.length} results hidden by the filters above.`
      : state.cat === "tv" ? "No TV episodes found. Try Everything, or check spelling." : "Nothing found. Try another spelling or language." }));
  if (hidden) parts.push(h("div", { class: "status", text: `${hidden} risky result${hidden > 1 ? "s" : ""} hidden for your safety.` }));
  main.replaceChildren(...parts);
  if (focusFirst) $(".grid .card", main)?.focus({ preventScroll: true });
}

/* ----------------------------------------------------------- overlays */
function push(id) {
  state.focusBack.push(document.activeElement);
  state.stack.push(id);
  $("#" + id).hidden = false;
  $("#" + id).scrollTop = 0;
  try { history.pushState({ overlay: id }, ""); } catch { /* ignore */ }
}
function closeTop() {
  const id = state.stack.pop();
  if (!id) return;
  $("#" + id).hidden = true;
  if (id === "player") stopVideo();
  const back = state.focusBack.pop();
  if (back && document.contains(back)) back.focus({ preventScroll: true });
  setTimeout(reloadIfIdle, 300);
}
window.addEventListener("popstate", () => { if (state.stack.length) closeTop(); });
const topOverlay = () => state.stack[state.stack.length - 1];
/* Called by the Android app on the remote's Back button: true = handled here. */
window.buzzBack = () => {
  if (!$("#pair").hidden) return false;
  if (state.stack.length) { history.back(); return true; }
  if (state.query) { renderHome(); return true; }
  return false;
};

/* ------------------------------------------------------------- detail */
let installedCache = null;
const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9+]/g, "");
function installedApps() {
  if (!BRIDGE || !BRIDGE.installedApps) return null;
  if (!installedCache) {
    try { installedCache = JSON.parse(BRIDGE.installedApps()).map(norm); } catch { installedCache = []; }
  }
  return installedCache;
}
function hasApp(service, labels) {
  return service.apps.some((a) => {
    const n = norm(a);
    return labels.some((l) => l === n || (n.length >= 5 && l.startsWith(n)));
  });
}

async function renderOtt(title) {
  const box = $("#dOtt");
  const labels = installedApps();
  $("#dWatchNote").textContent = labels
    ? "Free options come first. Opens the app on this TV and searches for this title; a green outline means it's installed. Paid titles still need that service's subscription."
    : "Searches each service's website for this title.";
  let services;
  try { services = (await api("/api/tv/services?" + qs({ title }))).services; } catch { return; }
  if (labels) services.sort((a, b) => hasApp(b, labels) - hasApp(a, labels));
  const btns = [];
  if (BRIDGE && BRIDGE.globalSearch) {
    btns.push(h("button", { type: "button", class: "ott installed", onclick: () => {
      const res = BRIDGE.globalSearch(title);
      if (res !== "ok") toast("Google TV search isn't available on this TV", "bad");
    } }, "Search all my apps", h("small", { text: "Google TV" })));
  }
  for (const s of services) {
    const inst = labels ? hasApp(s, labels) : false;
    btns.push(h("button", { type: "button", class: "ott" + (inst ? " installed" : ""), onclick: () => openService(s, s.query || title) },
      s.name, s.free ? h("span", { class: "free", text: s.id === "youtube" ? "Free" : s.free }) : null,
      labels && !inst ? h("small", { text: "get app" }) : null));
  }
  box.replaceChildren(...btns);
}

function openService(s, title) {
  if (!BRIDGE) { window.open(s.web, "_blank", "noopener"); return; }
  const res = BRIDGE.openService(JSON.stringify(s.apps), s.name, title);
  const msg = {
    search: [`Searching ${s.name} for “${title}”`, "ok"],
    opened: [`Opened ${s.name}. Search for “${title}” there`, "ok"],
    store: [`${s.name} isn't installed. Opening the Play Store`, ""],
  }[res] || [`Couldn't open ${s.name}`, "bad"];
  toast(...msg);
}

async function openDetail(r) {
  push("detail");
  const poster = $("#dPoster");
  poster.src = r.thumb || "/favicon.svg";
  $("#dTitle").textContent = r.title;
  const langs = (r.language || []).map((c) => LANG_NAME[c] || c);
  $("#dMeta").textContent = [r.year, CAT_NOUN[r.category], fmtSize(r.size), ...langs, (r.sources || []).join(", ")].filter(Boolean).join(" · ");
  $("#dBadges").replaceChildren(h("span", { class: `v-${r.verdict}`, text: `${VERDICT[r.verdict]} · ${r.score}/100` }),
    playable(r) ? h("span", { class: "v-free", text: "Free to watch" }) : null,
    r.quality ? h("span", { class: "v-q", text: r.quality.label + (r.quality.exact ? "" : " (from title)") }) : null);
  $("#dSummary").textContent = "";
  $("#dFlags").replaceChildren(...(r.flags || []).filter((f) => f.level !== "info")
    .map((f) => h("li", { class: f.level, text: (f.level === "danger" ? "⛔ " : "⚠ ") + f.msg })));

  const actions = [];
  let playBtn = null;
  if (playable(r)) {
    playBtn = h("button", { type: "button", class: "act primary", onclick: () => play(r, $("#dTitle").textContent) }, "▶  Play on TV");
    actions.push(playBtn);
    // Learn the best copy up front so the button can say what it will play
    loadPlay(r).then((d) => {
      if (topOverlay() !== "detail" || !d) return;
      const best = bestLabel(d);
      if (best) playBtn.textContent = `▶  Play ${best}`;
    });
  }
  const send = h("button", { type: "button", class: "act" + (playable(r) ? "" : " primary"), onclick: () => sendToPc(r, send) }, "⬇  Download on PC");
  actions.push(send);
  const toTv = tvDownloadButton(r);
  if (toTv) actions.push(toTv);
  const priv = r.category === "adult";
  if (!priv) actions.push(watchButton(r));
  for (const el of [$("#detail .d-h"), $("#dWatchNote"), $("#dOtt")]) el.hidden = priv;
  $("#dActions").replaceChildren(...actions);
  actions[0].focus();
  if (!priv) renderOtt(r.title.replace(/[._]+/g, " "));

  try {
    const d = await api("/api/details?" + qs({ id: r.id }));
    if (topOverlay() !== "detail") return;
    if (d.title) $("#dTitle").textContent = d.title;
    if (d.poster) poster.src = d.poster;
    $("#dSummary").textContent = d.summary || d.upload?.description || "";
    const extra = [d.runtime, (d.genres || []).slice(0, 3).join(", ")].filter(Boolean);
    if (extra.length) $("#dMeta").textContent += " · " + extra.join(" · ");
    if (d.title && !priv) renderOtt(d.title);
  } catch { /* details are optional */ }
}

async function sendToPc(r, btn) {
  btn.disabled = true;
  try {
    const d = await api("/api/tv/send", { id: r.id });
    toast(d.app === "BuzzTorrent" ? "Downloading on your PC. It shows up in My PC once finished and virus-scanned"
      : `Downloading on your PC in ${d.app || "your torrent app"}`, "ok");
  } catch (err) {
    if (!(err instanceof NeedPair)) toast(err.message, "bad");
  } finally {
    btn.disabled = false;
    btn.focus();
  }
}

/* ------------------------------------------------------- watchlist */
function watchButton(r) {
  const btn = h("button", { type: "button", class: "act" }, "☆  Watchlist");
  let entry = null;
  const cat = ["movies", "tv", "music"].includes(r.category) ? r.category : "movies";
  const sync = () => { btn.textContent = entry ? "★  On watchlist" : "☆  Watchlist"; };
  api("/api/tv/watchlist").then((d) => {
    const t = ($("#dTitle").textContent || r.title).toLowerCase();
    entry = d.items.find((w) => w.title.toLowerCase() === t && w.category === cat) || null;
    sync();
  }).catch(() => {});
  btn.addEventListener("click", async () => {
    try {
      if (entry) {
        await api("/api/tv/watch", { action: "remove", id: entry.id });
        entry = null;
        toast("Removed from your watchlist");
      } else {
        const d = await api("/api/tv/watch", { title: $("#dTitle").textContent || r.title, category: cat, year: r.year,
          thumb: r.thumb || "", quality: r.quality?.height || 0, free: playable(r) });
        entry = d.item;
        toast("Added. You'll be told when a better or free copy turns up", "ok");
      }
      sync();
    } catch (err) {
      if (!(err instanceof NeedPair)) toast(err.message, "bad");
    }
  });
  return btn;
}

/* ------------------------------------------------------- PC library */
/* Videos the PC downloaded and its scanner checked clean, streamed from the PC */
async function openLibrary() {
  push("library");
  const grid = $("#libGrid");
  grid.replaceChildren(h("div", { class: "status" }, spinner(), "Looking at your PC's downloads…"));
  let d;
  try { d = await api("/api/tv/library"); } catch (err) {
    if (!(err instanceof NeedPair)) grid.replaceChildren(h("div", { class: "status", text: err.message }));
    return;
  }
  if (d.state === "scanner_off") {
    grid.replaceChildren(h("div", { class: "status big", text: "Turn on the Download scanner on the PC (Settings → Download scanner) and choose your downloads folder. Every video it checks as clean shows up here." }));
    return;
  }
  const busy = (d.downloading || []).map((x) => h("div", { class: "status" },
    `⬇ Downloading on your PC: ${x.title} · ${x.total ? Math.floor((x.done / x.total) * 100) : 0}%` +
    (x.down ? ` · ${fmtSize(x.down)}/s` : "") + " (appears here once finished and scanned)"));
  $("#library .d-note").replaceChildren("Videos your PC downloaded and its virus scanner checked. They stream from the PC.", ...busy);
  if (!d.items.length) {
    grid.replaceChildren(h("div", { class: "status big", text: "No scanned videos yet. Downloads appear here once the PC's scanner has checked them." }));
    return;
  }
  grid.replaceChildren(...d.items.map((it) => h("button", { type: "button", class: "card", onclick: () => playLibrary(it, d.progress) },
    h("div", { class: "thumb" }, placeholder(it.title),
      h("span", { class: "badge safe", text: "✓ Scanned" }),
      it.label ? h("span", { class: `badge q q-${it.label.toLowerCase()}`, text: it.label }) : null,
      it.resume ? h("span", { class: "badge play", text: "Resume" }) : null),
    h("div", { class: "card-t", dir: "auto", text: it.title }),
    h("div", { class: "card-m", text: [it.year, fmtSize(it.size), it.ext.toUpperCase(), it.subs.length ? "CC" : ""].filter(Boolean).join(" · ") }))));
  $("#libGrid .card")?.focus();
}

function playLibrary(it, progress, resumeNow = false) {
  const run = (from) => {
    if (native()) {
      state.playingThumb = "";
      // every copy of the video, best first: the player falls back if the TV can't decode one
      const versions = (it.versions || [{ stream: it.stream, label: it.label || it.ext.toUpperCase(), size: it.size }])
        .map((v) => ({ url: location.origin + v.stream, label: v.label, size: v.size }));
      playNative({ progress, files: [{ name: it.title, key: it.key, resume: from, seconds: 0, subs: it.subs, versions }] }, 0, it.title, from);
    } else {
      startPlayer([{ url: it.stream, name: it.title, key: it.key, label: it.label }], 0, it.title, from);
    }
  };
  if (resumeNow) run(it.resume || 0);
  else withResume(it.resume, it.title, run);
}

/* ------------------------------------------------------ download to TV */
const canSaveOnTv = () => !!(BRIDGE && BRIDGE.downloadToTv && BRIDGE.listDownloads);

function tvDownloadButton(r) {
  if (canSaveOnTv() && playable(r)) {
    return h("button", { type: "button", class: "act", onclick: () => downloadToTv(r, $("#dTitle").textContent) }, "⬇  Download to TV");
  }
  if (BRIDGE && BRIDGE.openMagnet && r.magnet) {
    // Not a free Archive copy: a torrent app on the TV can fetch it (no PC antivirus scan there)
    return h("button", { type: "button", class: "act", onclick: () => {
      if (BRIDGE.openMagnet(r.magnet) === "ok") toast("Sent to the torrent app on this TV", "ok");
      else toast("Install a torrent app such as LibreTorrent on the TV, or use Download on PC", "bad");
    } }, "⬇  Download to TV");
  }
  return null;
}

function storages() {
  try { return JSON.parse(BRIDGE.storage()).sort((a, b) => b.free - a.free); } catch { return []; }
}

async function downloadToTv(r, title) {
  const d = await loadPlay(r);
  if (!d) return;
  const files = d.files.filter((f) => f.versions && f.versions.length);
  if (!files.length) { toast(d.note || "Nothing here can be saved", "bad"); return; }
  const places = storages();
  if (!places.length) { toast("No storage available on this TV", "bad"); return; }
  let place = 0;
  push("picker");

  const start = (name, v) => BRIDGE.downloadToTv(JSON.stringify({
    url: v.url, title: name, label: v.label || v.format, size: v.size || 0, storage: places[place].id }));
  const report = (res, what) => {
    if (res === "ok") toast(`Downloading ${what} to the TV. Open Downloads to follow it`, "ok");
    else if (res === "fallback") toast(`Saving ${what} to the TV's own storage instead`, "ok");
    else toast(res, "bad");
  };

  const render = () => {
    const st = places[place];
    const fits = (size) => !size || size < st.free - 200 * 1024 * 1024;   // keep 200 MB spare
    $("#pTitle").textContent = `Download to TV: ${title}`;
    const head = [h("div", { class: "p-head", text: `Saving to ${st.label} · ${fmtSize(st.free)} free` })];
    if (places.length > 1) {
      const other = places[(place + 1) % places.length];
      head.push(h("button", { type: "button", class: "p-item", "data-key": "place",
        onclick: () => { place = (place + 1) % places.length; render(); } },
        h("span", { text: `Save to ${other.label} instead` }), h("span", { text: `${fmtSize(other.free)} free` })));
    }
    let items;
    if (files.length === 1) {
      const f = files[0];
      items = f.versions.map((v, i) => h("button", { type: "button", class: "p-item", "data-key": "v" + i,
        disabled: !fits(v.size), onclick: () => { report(start(title, v), v.label || "the video"); history.back(); } },
        h("span", { text: `${i === 0 ? "★ " : ""}${v.label || "Video"} · ${v.format}` }),
        h("span", { text: fits(v.size) ? fmtSize(v.size) : `${fmtSize(v.size)} · doesn't fit` })));
    } else {
      const total = files.reduce((n, f) => n + (f.versions[0].size || 0), 0);
      items = [h("button", { type: "button", class: "p-item", "data-key": "all", disabled: !fits(total),
        onclick: () => {
          let ok = 0;
          for (const f of files) if (["ok", "fallback"].includes(start(`${title} - ${f.name}`, f.versions[0]))) ok++;
          toast(`Downloading ${ok} of ${files.length} videos to the TV`, ok ? "ok" : "bad");
          history.back();
        } }, h("span", { text: `All ${files.length} videos (best quality)` }),
        h("span", { text: fits(total) ? fmtSize(total) : `${fmtSize(total)} · doesn't fit` }))];
      files.forEach((f, i) => {
        const v = f.versions[0];
        items.push(h("button", { type: "button", class: "p-item", "data-key": "e" + i, disabled: !fits(v.size),
          onclick: () => { report(start(`${title} - ${f.name}`, v), f.name); history.back(); } },
          h("span", { dir: "auto", text: f.name }), h("span", { text: [v.label, fmtSize(v.size)].filter(Boolean).join(" · ") })));
      });
    }
    $("#pList").replaceChildren(...head, ...items);
    ($("#pList .p-item:not([disabled]):not([data-key=place])") || $("#pList .p-item"))?.focus();
  };
  render();
}

/* the Downloads screen: progress, play offline, delete */
let dlTimer = 0;
const armedDelete = new Set();
function openDownloads() {
  push("downloads");
  renderDownloads();
  clearInterval(dlTimer);
  dlTimer = setInterval(() => { if (topOverlay() === "downloads") renderDownloads(); else clearInterval(dlTimer); }, 2000);
}
const PC_STATUS = { active: "Downloading", metadata: "Finding the files…", waiting: "Queued", paused: "Paused",
  error: "Failed", complete: "Done", seeding: "Done · sharing back" };
async function renderPcDownloads() {
  let d;
  try { d = await api("/api/downloads"); } catch (err) {
    if (!(err instanceof NeedPair)) $("#dlPc").replaceChildren(h("div", { class: "status", text: err.message }));
    return;
  }
  const keep = document.activeElement?.dataset?.key;
  const items = d.items.filter((x) => x.status !== "removed");
  if (!items.length) {
    $("#dlPc").replaceChildren(h("div", { class: "status", text: "Nothing downloading on the PC. Use ⬇ Download on PC on any title." }));
    return;
  }
  $("#dlPc").replaceChildren(...items.map((x) => {
    const pct = x.total ? Math.floor((x.done / x.total) * 100) : 0;
    const fill = h("span", { class: "dl-fill" });
    fill.style.width = `${pct}%`;
    const right = x.status === "active" ? `${pct}% · ${fmtSize(x.down)}/s` : PC_STATUS[x.status] || x.status;
    const act = (label, action, key) => h("button", { type: "button", class: "dl-del", "data-key": key + x.gid, text: label,
      onclick: async () => {
        if (action === "remove" && !armedDelete.has(x.gid)) {
          armedDelete.add(x.gid);
          setTimeout(() => armedDelete.delete(x.gid), 3000);
          renderPcDownloads();
          return;
        }
        armedDelete.delete(x.gid);
        try { await api("/api/downloads/action", { gid: x.gid, action }); } catch (err) { toast(err.message, "bad"); }
        renderPcDownloads();
      } });
    const btns = [];
    if (["active", "waiting", "metadata"].includes(x.status)) btns.push(act("Pause", "pause", "pp"));
    if (x.status === "paused") btns.push(act("Resume", "resume", "pr"));
    btns.push(act(armedDelete.has(x.gid) ? "Press again" : x.status === "seeding" ? "Stop sharing" : "Stop", "remove", "ps"));
    return h("div", { class: "dl-row" },
      h("button", { type: "button", class: "p-item", "data-key": "pi" + x.gid, onclick: () => toast(`${PC_STATUS[x.status] || x.status}: ${x.title}`) },
        h("span", { class: "dl-main" }, h("span", { dir: "auto", text: x.title }),
          ["seeding", "complete"].includes(x.status) ? null : h("span", { class: "dl-bar" }, fill)),
        h("span", { text: right })),
      ...btns);
  }));
  const again = keep && $(`#dlPc [data-key="${keep}"]`);
  if (again) again.focus({ preventScroll: true });
}

function renderDownloads() {
  renderPcDownloads();
  $("#dlTvBox").hidden = !canSaveOnTv();
  if (!canSaveOnTv()) {
    if (!$("#downloads").contains(document.activeElement)) setTimeout(() => $("#dlPc button")?.focus(), 300);
    return;
  }
  let list = [];
  try { list = JSON.parse(BRIDGE.listDownloads()); } catch { /* none */ }
  const keep = document.activeElement?.dataset?.key;
  $("#dlSpace").textContent = storages().map((s) => `${s.label}: ${fmtSize(s.free)} free`).join(" · ");
  if (!list.length) {
    $("#dlList").replaceChildren(h("div", { class: "status", text: "Nothing saved yet. Open a free movie and choose ⬇ Download to TV." }));
    return;
  }
  const STATUS = { running: "Downloading", pending: "Waiting", paused: "Paused (waiting for the network)", failed: "Failed", missing: "File was removed" };
  $("#dlList").replaceChildren(...list.map((x) => {
    const pct = x.total ? Math.floor((x.done / x.total) * 100) : 0;
    const right = x.status === "done" ? `▶ Play · ${fmtSize(x.total)}`
      : x.status === "running" ? `${pct}% · ${fmtSize(x.done)} of ${fmtSize(x.total)}` : STATUS[x.status] || x.status;
    const fill = h("span", { class: "dl-fill" });
    fill.style.width = `${pct}%`;
    const main = h("button", { type: "button", class: "p-item", "data-key": "p" + x.id, onclick: () => {
      if (x.status !== "done") { toast(STATUS[x.status] || "Not finished yet"); return; }
      if (BRIDGE.playDownload(String(x.id)) !== "ok") toast("Couldn't open the file", "bad");
    } }, h("span", { class: "dl-main" },
      h("span", { dir: "auto", text: `${x.title}${x.label ? " · " + x.label : ""}${x.removable ? " · USB" : ""}` }),
      x.status === "done" ? null : h("span", { class: "dl-bar" }, fill)),
    h("span", { text: right }));
    const del = h("button", { type: "button", class: "dl-del", "data-key": "d" + x.id,
      text: armedDelete.has(x.id) ? "Press again to delete" : (x.status === "done" ? "Delete" : "Cancel"), onclick: () => {
        if (!armedDelete.has(x.id)) {
          armedDelete.add(x.id);
          setTimeout(() => { armedDelete.delete(x.id); if (topOverlay() === "downloads") renderDownloads(); }, 3000);
          renderDownloads();
          return;
        }
        armedDelete.delete(x.id);
        BRIDGE.deleteDownload(String(x.id));
        toast("Deleted from the TV");
        renderDownloads();
      } });
    return h("div", { class: "dl-row" }, main, del);
  }));
  const again = keep && $(`#dlList [data-key="${keep}"]`);
  (again || $("#dlList .p-item"))?.focus({ preventScroll: !!again });
}

/* -------------------------------------------------------------- play */
/* Play: the best copy by default. The Android app has a native player that handles every
   format the TV can decode (MKV, HEVC, 1080p, 4K) and falls back to the next copy by itself;
   a plain TV browser plays the best .mp4/.webm copy in the page. */
const native = () => !!(BRIDGE && BRIDGE.playNative);
const playCache = new Map();
function loadPlay(r) {
  if (!playCache.has(r.id)) {
    playCache.set(r.id, api("/api/tv/play?" + qs({ id: r.id })).catch((err) => {
      playCache.delete(r.id);
      if (!(err instanceof NeedPair)) toast(err.message, "bad");
      return null;
    }));
  }
  return playCache.get(r.id);
}
function bestLabel(d) {
  const f = d.files[0];
  if (!f) return "";
  if (native()) return f.best?.label || f.label || "";
  return f.url ? f.label || "" : "";
}
/* Everything the native player needs: every copy (sharpest first, with bitrates for the
   Wi-Fi check), subtitles, where to resume, and where to save the position. */
function playNative(d, index, title, resumeFrom) {
  const abs = (u) => (u && u.startsWith("/") ? location.origin + u : u);
  const items = d.files.map((f, i) => ({
    name: f.name, key: f.key || "", thumb: state.playingThumb || "",
    resume: i === index ? resumeFrom || 0 : f.resume || 0,
    urls: f.versions.map((v) => v.url), labels: f.versions.map((v) => v.label || v.format),
    bitrates: f.versions.map((v) => (v.size && f.seconds ? Math.round((v.size * 8) / f.seconds) : 0)),
    subs: (f.subs || []).filter((x) => x.url).map((x) => ({ label: x.label, url: abs(x.url) })),
  }));
  if (BRIDGE.playNative(JSON.stringify({ title, index, items, progress: abs(d.progress) })) !== "ok") toast("Couldn't start the player", "bad");
}

/* "Resume from 34:10" or "Start over" when there's a saved position */
function withResume(resume, title, run) {
  if (!(resume > 0)) { run(0); return; }
  push("picker");
  $("#pTitle").textContent = title;
  const items = [
    h("button", { type: "button", class: "p-item", onclick: () => { history.back(); run(resume); } },
      h("span", { text: `▶  Resume from ${fmtTime(resume)}` }), h("span", { text: "" })),
    h("button", { type: "button", class: "p-item", onclick: () => { history.back(); run(0); } },
      h("span", { text: "↺  Start from the beginning" }), h("span", { text: "" })),
  ];
  $("#pList").replaceChildren(...items);
  items[0].focus();
}

async function play(r, title) {
  const d = await loadPlay(r);
  if (!d) return;
  if (!d.files.length) { toast(d.note || "Nothing playable here", "bad"); return; }
  state.playingThumb = r.thumb || "";
  if (native()) {
    const go = (i) => withResume(d.files[i].resume, title, (from) => playNative(d, i, title, from));
    if (d.files.length === 1) { go(0); return; }
    return episodePicker(d.files, title, go, (f) => [f.best?.label, f.resume ? "resume " + fmtTime(f.resume) : ""].filter(Boolean).join(" · "));
  }
  const ext = !!(BRIDGE && BRIDGE.playExternal);
  const files = d.files.filter((f) => f.url || (f.alt && ext));
  if (!files.length) {
    toast("This copy is only in a format a TV browser can't play. Use the BuzzTorrent TV app.", "bad");
    return;
  }
  const inApp = files.filter((f) => f.url);
  const playAt = (f) => (f.url ? startPlayer(inApp, inApp.indexOf(f), title) : BRIDGE.playExternal(f.alt.url, title));
  const one = files.length === 1 ? files[0] : null;
  if (one && !(one.alt && one.url && ext)) { playAt(one); return; }
  if (one) {   // older TV app: the sharper copy needs a player app; best one first
    push("picker");
    $("#pTitle").textContent = `${title}: choose quality`;
    const items = [
      h("button", { type: "button", class: "p-item", onclick: () => BRIDGE.playExternal(one.alt.url, title) },
        h("span", { text: `▶  ${one.alt.label || "Best"} · best quality, in your video player app` }),
        h("span", { text: [one.alt.format, fmtSize(one.alt.size)].filter(Boolean).join(" · ") })),
      h("button", { type: "button", class: "p-item", onclick: () => startPlayer(inApp, 0, title) },
        h("span", { text: `▶  ${one.label || "Standard"} · plays here` }),
        h("span", { text: fmtSize(one.size) })),
    ];
    $("#pList").replaceChildren(...items);
    items[0].focus();
    return;
  }
  episodePicker(files, title, (i) => playAt(files[i]), (f) => f.label);
}

function episodePicker(files, title, onPick, labelOf) {
  push("picker");
  $("#pTitle").textContent = `${title}: ${files.length} videos`;
  const items = files.map((f, i) => h("button", { type: "button", class: "p-item", onclick: () => onPick(i) },
    h("span", { dir: "auto", text: f.name }),
    h("span", { text: [labelOf(f), f.seconds ? fmtTime(f.seconds) : "", fmtSize(f.size)].filter(Boolean).join(" · ") })));
  $("#pList").replaceChildren(...items);
  items[0].focus();
}

let hudTimer = 0;
const video = $("#video");
function showHud() {
  $("#hud").classList.remove("hide");
  clearTimeout(hudTimer);
  hudTimer = setTimeout(() => { if (!video.paused) $("#hud").classList.add("hide"); }, 3500);
}
function startPlayer(files, i, title, resumeFrom = 0) {
  if (topOverlay() !== "player") push("player");
  state.playlist = { files, i, title, saved: 0 };
  const f = files[i];
  $("#playerMsg").hidden = true;
  $("#hudTitle").textContent = files.length > 1 ? `${title} · ${f.name}` : title;
  video.src = f.url;
  if (resumeFrom > 0) video.addEventListener("loadedmetadata", () => { video.currentTime = resumeFrom; }, { once: true });
  video.play().catch(() => {});
  const btns = [];
  if (i + 1 < files.length) btns.push(h("button", { type: "button", text: "Next ▶▶", onclick: () => startPlayer(files, i + 1, title) }));
  if (i > 0) btns.push(h("button", { type: "button", text: "◀◀ Previous", onclick: () => startPlayer(files, i - 1, title) }));
  if (BRIDGE && BRIDGE.playExternal) btns.push(h("button", { type: "button", text: "Open in another player", onclick: () => BRIDGE.playExternal(f.alt ? f.alt.url : f.url, title) }));
  $("#hudBtns").replaceChildren(...btns);
  $("#player").focus();
  document.activeElement?.blur?.();
  showHud();
}
function stopVideo() {
  video.pause();
  video.removeAttribute("src");
  video.load();
  state.playlist = null;
}
function saveWebProgress() {
  const p = state.playlist;
  const f = p && p.files[p.i];
  if (!f || !f.key || !video.duration) return;
  api("/api/tv/progress", { key: f.key, pos: video.currentTime, dur: video.duration, title: p.title,
    name: p.files.length > 1 ? f.name : "", thumb: state.playingThumb || "", quality: f.label || "" }).catch(() => {});
}
video.addEventListener("pause", saveWebProgress);
video.addEventListener("timeupdate", () => {
  const p = state.playlist;
  if (p && Date.now() - p.saved > 15000) { p.saved = Date.now(); saveWebProgress(); }
  $("#hudFill").style.width = video.duration ? `${(video.currentTime / video.duration) * 100}%` : "0";
  $("#hudTime").textContent = fmtTime(video.currentTime);
  $("#hudDur").textContent = fmtTime(video.duration);
});
video.addEventListener("pause", showHud);
video.addEventListener("ended", () => {
  const p = state.playlist;
  if (p && p.i + 1 < p.files.length) startPlayer(p.files, p.i + 1, p.title);
  else history.back();
});
video.addEventListener("error", () => {
  if (!state.playlist) return;
  const m = $("#playerMsg");
  m.textContent = "This video's format can't play in the TV app." + (BRIDGE && BRIDGE.playExternal ? " Press ▼ and choose “Open in another player” (e.g. VLC)." : "");
  m.hidden = false;
  showHud();
});
function playerKey(e) {
  const onBtn = e.target.closest && e.target.closest("#hudBtns");
  showHud();
  switch (e.key) {
    case "Enter": case " ": case "MediaPlayPause":
      if (onBtn) return false;
      video.paused ? video.play().catch(() => {}) : video.pause();
      return true;
    case "MediaPlay": video.play().catch(() => {}); return true;
    case "MediaPause": video.pause(); return true;
    case "ArrowLeft": if (onBtn) return false; video.currentTime = Math.max(0, video.currentTime - 10); return true;
    case "ArrowRight": if (onBtn) return false; video.currentTime = Math.min(video.duration || 0, video.currentTime + 10); return true;
    case "MediaRewind": video.currentTime = Math.max(0, video.currentTime - 30); return true;
    case "MediaFastForward": video.currentTime = Math.min(video.duration || 0, video.currentTime + 30); return true;
    case "ArrowDown": if (!onBtn) { $("#hudBtns button")?.focus(); return true; } return false;
    case "ArrowUp": if (onBtn) { e.target.blur(); return true; } return true;
    case "MediaTrackNext": { const p = state.playlist; if (p && p.i + 1 < p.files.length) startPlayer(p.files, p.i + 1, p.title); return true; }
    default: return false;
  }
}

/* ------------------------------------------------------------ pairing */
let pin = "";
function showPair() {
  const box = $("#pair");
  if (!box.hidden) return;
  box.hidden = false;
  pin = "";
  drawPin();
  const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "OK"];
  $("#pad").replaceChildren(...keys.map((k) => h("button", { type: "button", text: k, onclick: () => padKey(k) })));
  $("#pad button").focus();
}
function drawPin() {
  $("#pinShow").replaceChildren(...Array.from({ length: 6 }, (_, i) => h("span", { class: i === pin.length ? "on" : "", text: pin[i] ? "•" : "" })));
}
function padKey(k) {
  $("#pairErr").textContent = "";
  if (k === "⌫") pin = pin.slice(0, -1);
  else if (k === "OK") return submitPin();
  else if (pin.length < 6) pin += k;
  drawPin();
  if (pin.length === 6) submitPin();
}
async function submitPin() {
  if (pin.length !== 6) { $("#pairErr").textContent = "The PIN has 6 digits."; return; }
  try {
    await api("/api/tv/pair", { pin });
    $("#pair").hidden = true;
    toast("TV paired with your PC", "ok");
    remoteLoop();
    refresh();
  } catch (err) {
    $("#pairErr").textContent = err.message;
    pin = "";
    drawPin();
  }
}

/* ----------------------------------------------------- remote control */
function focusables() {
  const scope = (!$("#pair").hidden && $("#pair")) || (topOverlay() && $("#" + topOverlay())) || document;
  return [...scope.querySelectorAll("button:not(:disabled), input")].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
}
function move(dir) {
  const list = focusables();
  if (!list.length) return;
  const cur = document.activeElement;
  if (!list.includes(cur)) { list[0].focus(); list[0].scrollIntoView({ block: "center", inline: "nearest" }); return; }
  const a = cur.getBoundingClientRect();
  const ax = a.left + a.width / 2, ay = a.top + a.height / 2;
  let best = null, bestScore = Infinity;
  for (const el of list) {
    if (el === cur) continue;
    const b = el.getBoundingClientRect();
    const bx = b.left + b.width / 2, by = b.top + b.height / 2;
    let main, cross;
    if (dir === "right") { if (b.left < a.right - 2 && bx <= ax + 1) continue; main = Math.max(0, b.left - a.right); cross = Math.abs(by - ay); }
    else if (dir === "left") { if (b.right > a.left + 2 && bx >= ax - 1) continue; main = Math.max(0, a.left - b.right); cross = Math.abs(by - ay); }
    else if (dir === "down") { if (b.top < a.bottom - 2 && by <= ay + 1) continue; main = Math.max(0, b.top - a.bottom); cross = Math.abs(bx - ax); }
    else { if (b.bottom > a.top + 2 && by >= ay - 1) continue; main = Math.max(0, a.top - b.bottom); cross = Math.abs(bx - ax); }
    if ((dir === "left" || dir === "right") && cross > Math.max(a.height, b.height)) continue;   // stay on the row
    const score = main + cross * (dir === "left" || dir === "right" ? 3 : 0.6);
    if (score < bestScore) { bestScore = score; best = el; }
  }
  if (!best) return;
  best.focus({ preventScroll: true });
  best.scrollIntoView({ block: best.classList.contains("card") ? "center" : "nearest", inline: "nearest", behavior: "smooth" });
}

document.addEventListener("keydown", (e) => {
  if (!$("#pair").hidden && /^[0-9]$/.test(e.key)) { padKey(e.key); e.preventDefault(); return; }
  if (topOverlay() === "adultBox" && /^[0-9]$/.test(e.key)) { adultKey(e.key); e.preventDefault(); return; }
  if (topOverlay() === "player" && $("#pair").hidden) {
    if (playerKey(e)) { e.preventDefault(); return; }
  }
  if (["GoBack", "BrowserBack", "Escape", "Backspace"].includes(e.key) && e.target.tagName !== "INPUT") {
    if (window.buzzBack()) e.preventDefault();
    return;
  }
  const dir = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" }[e.key];
  if (!dir) return;
  const t = e.target;
  if (t.tagName === "INPUT" && (dir === "left" || dir === "right")) {
    const atEdge = dir === "left" ? t.selectionStart === 0 : t.selectionEnd === t.value.length;
    if (!atEdge) return;
  }
  e.preventDefault();
  move(dir);
});

/* -------------------------------------------------------------- start */
$("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); $("#q").blur(); runSearch($("#q").value); });
$("#homeBtn").addEventListener("click", () => renderHome());
$("#libBtn").addEventListener("click", openLibrary);
$("#dlBtn").addEventListener("click", openDownloads);

/* Updates on the PC reach the TV by themselves: when the TV page changes, reload it as
   soon as nothing is open (never in the middle of a video), keeping the current search. */
let uiVersion = null, updateWaiting = false;
async function checkForUpdate() {
  try {
    const hello = await api("/api/tv/hello");
    if (uiVersion === null) uiVersion = hello.ui;
    else if (hello.ui && hello.ui !== uiVersion) { updateWaiting = true; reloadIfIdle(); }
  } catch { /* PC offline: try again later */ }
}
function reloadIfIdle() {
  if (!updateWaiting || state.stack.length || !$("#pair").hidden) return;
  store.set("justUpdated", true);
  location.hash = state.query && state.cat !== "adult" ? qs({ q: state.query, cat: state.cat, lang: state.lang }) : "";
  location.reload();
}
setInterval(checkForUpdate, 60000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) checkForUpdate(); });

/* ----------------------------------------------------- private section */
let adultPin = "";
async function loadAdultState() {
  try {
    const s = await api("/api/adult/state");
    adult.shown = s.enabled;
    adult.unlocked = s.unlocked;
  } catch { adult.shown = adult.unlocked = false; }
  if (!adult.shown && state.cat === "adult") state.cat = "movies";
  renderChips();
}
function askAdultPin() {
  push("adultBox");
  adultPin = "";
  drawAdultPin();
  const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "OK"];
  $("#adultPad").replaceChildren(...keys.map((k) => h("button", { type: "button", text: k, onclick: () => adultKey(k) })));
  $("#adultErr").textContent = "";
  $("#adultPad button").focus();
}
function drawAdultPin() {
  $("#adultPinShow").replaceChildren(...Array.from({ length: Math.max(4, adultPin.length) }, (_, i) =>
    h("span", { class: i === adultPin.length ? "on" : "", text: adultPin[i] ? "•" : "" })));
}
async function adultKey(k) {
  $("#adultErr").textContent = "";
  if (k === "⌫") adultPin = adultPin.slice(0, -1);
  else if (k !== "OK" && adultPin.length < 8) adultPin += k;
  drawAdultPin();
  if (k !== "OK") return;
  try {
    await api("/api/adult/unlock", { pin: adultPin });
    adult.unlocked = true;
    history.back();                          // close the PIN pad
    state.cat = "adult";
    renderChips();
    renderHome();
  } catch (err) {
    $("#adultErr").textContent = err.message;
    adultPin = "";
    drawAdultPin();
  }
}
function leaveAdult(msg) {
  adult.unlocked = false;
  if (state.cat !== "adult") return;
  closeAll();
  state.cat = store.get("cat", "movies");
  if (msg) toast(msg);
  renderChips();
  renderHome();
}
async function lockAdult() {
  try { await api("/api/adult/lock", {}); } catch { /* locked anyway */ }
  leaveAdult("Private section locked");
}

/* ------------------------------------------------------- phone remote */
let remoteRunning = false;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function remoteLoop() {
  if (remoteRunning) return;
  remoteRunning = true;
  let seq = -1;
  while (true) {
    if (document.hidden) { await sleep(2000); continue; }
    try {
      const d = await api("/api/tv/remote/poll?" + qs({ after: seq }));
      for (const cmd of d.commands) await runRemote(cmd);
      seq = d.seq;
    } catch (err) {
      if (err instanceof NeedPair) { remoteRunning = false; return; }
      seq = -1;
      await sleep(5000);
    }
  }
}
function closeAll() {
  while (state.stack.length) closeTop();
}
async function getResult(id) {
  return state.byId.get(id) || (await api("/api/tv/result?" + qs({ id })));
}
async function runRemote(cmd) {
  try {
    if (cmd.action === "search") {
      closeAll();
      if (cmd.cat && TABS.some(([id]) => id === cmd.cat)) state.cat = cmd.cat;
      if (cmd.lang && LANG_NAME[cmd.lang]) state.lang = cmd.lang;
      renderChips();
      toast(`📱 ${cmd.q}`);
      await runSearch(cmd.q);
    } else if (cmd.action === "home") {
      closeAll();
      renderHome();
    } else if (cmd.action === "open" || cmd.action === "play") {
      const r = await getResult(cmd.id);
      closeAll();
      await openDetail(r);
      if (cmd.action === "play") play(r, $("#dTitle").textContent);
    }
  } catch (err) {
    if (!(err instanceof NeedPair)) toast(err.message, "bad");
  }
}

/* ------------------------------------------------------------- voice */
/* Spoken searches often come with extra words: "search for sholay movie" -> "sholay" */
function cleanQuery(q) {
  const t = String(q || "").trim().replace(/[.!?।]+$/u, "");
  const cmd = /^(please\s+)?(search(\s+for)?|find|play|show(\s+me)?|watch|look\s+for|open)\s+/i;
  if (!cmd.test(t)) return t;
  const rest = t.replace(cmd, "");
  return rest.replace(/\s+(full\s+)?(movie|film|please)$/i, "") || rest;
}
function startVoice(lang = "en-IN") {
  if (!(BRIDGE && BRIDGE.voiceSearch)) { $("#q").focus(); return; }
  if (BRIDGE.voiceSearch(lang) !== "ok") toast("Voice search isn't available on this TV. Use the keyboard's mic instead", "bad");
}
window.startVoice = startVoice;
/* Called by the Android app with what was heard */
window.onVoice = (text) => {
  const q = cleanQuery(text);
  if (!q) return;
  closeAll();
  runSearch(q);
};
if (BRIDGE && BRIDGE.voiceSearch) {
  $("#micBtn").hidden = $("#micHiBtn").hidden = false;
  $("#micBtn").addEventListener("click", () => startVoice("en-IN"));
  $("#micHiBtn").addEventListener("click", () => startVoice("hi-IN"));
}

async function start() {
  renderChips();
  if (store.get("justUpdated", false)) { store.set("justUpdated", false); toast("BuzzTorrent was updated", "ok"); }
  try {
    const hello = await api("/api/tv/hello");
    uiVersion = hello.ui;
    if (!hello.paired) { showPair(); return; }
  } catch (err) {
    return offlineView(err);
  }
  remoteLoop();
  loadAdultState();
  // Deep link, e.g. /tv#q=ramayan&cat=tv (handy as a TV-browser bookmark)
  const link = new URLSearchParams(location.hash.slice(1));
  if (TABS.some(([id]) => id === link.get("cat"))) state.cat = link.get("cat");
  if (LANG_NAME[link.get("lang")]) state.lang = link.get("lang");
  renderChips();
  if (link.get("q")) {
    await runSearch(link.get("q"));
    const n = parseInt(link.get("open") || "", 10);
    if (n > 0) document.querySelectorAll("#main .card")[n - 1]?.click();
  } else renderHome();
}
start();
