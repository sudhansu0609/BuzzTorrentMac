"use strict";
// Security note: every piece of text that comes from a search result is inserted
// with textContent / attributes only. Nothing from the network touches innerHTML.

const CATS = [["all", "All"], ["movies", "Movies"], ["tv", "TV"], ["games", "Games"],
  ["software", "Software"], ["books", "Books"], ["music", "Music"]];
const CAT_LABEL = Object.fromEntries([...CATS, ["other", "Other"], ["adult", "18+"]]);
const LANGS = [["any", "Any language"], ["en", "English"], ["hi", "Hindi · हिंदी"], ["bn", "Bengali · বাংলা"],
  ["ur", "Urdu · اردو"], ["bho", "Bhojpuri · भोजपुरी"], ["ta", "Tamil · தமிழ்"], ["te", "Telugu · తెలుగు"],
  ["mr", "Marathi · मराठी"], ["pa", "Punjabi · ਪੰਜਾਬੀ"]];
const LANG_SHORT = { en: "English", hi: "Hindi", bn: "Bengali", ur: "Urdu", bho: "Bhojpuri", ta: "Tamil",
  te: "Telugu", mr: "Marathi", pa: "Punjabi" };
const GENRES = [["any", "All genres"], ["stories", "Stories · कहानियाँ"], ["poetry", "Poetry & shayari · कविता"],
  ["fiction", "Fiction & novels · उपन्यास"], ["nonfiction", "Non-fiction"], ["religion", "Religion & spirituality"]];
const VERDICTS = [["safe", "Safe"], ["caution", "Caution"], ["danger", "Dangerous"]];
const SEEDS = [["0", "Any"], ["1", "1+"], ["10", "10+"], ["100", "100+"]];
const GB = 1024 ** 3;
const SIZES = [["any", "Any"], ["lt1", "< 1 GB"], ["1to5", "1–5 GB"], ["gt5", "> 5 GB"]];
// exact title > near ("…: Movie Review") > loose match
const matchTier = (r) => (r.relevance == null || r.relevance >= 0.97 ? 4 : r.relevance >= 0.9 ? 3 : r.relevance >= 0.8 ? 2 : 1);
const VERDICT_ORDER = { safe: 0, caution: 1, unknown: 2, danger: 3 };
const QUALITIES = [["any", "Any"], ["720", "720p+"], ["1080", "1080p+"], ["2160", "4K+"], ["4320", "8K"]];
const THEMES = [["system", "System"], ["light", "Light"], ["dark", "Dark"]];
const MAGNET_RE = /^magnet:\?xt=urn:btih:[0-9a-f]{40}(&[A-Za-z0-9._~%=&+-]*)?$/;
const EXT_ICON = {
  movies: /\.(mkv|mp4|avi|mov|m4v|wmv|webm|mpg|mpeg|ogv|m2ts|ts)$/i,
  music: /\.(mp3|flac|ogg|wav|m4a|aac|opus|wma)$/i,
  books: /\.(pdf|epub|mobi|azw3|djvu|cbz|cbr|txt)$/i,
};

/* ------------------------------------------------------------- helpers */
const $ = (s, root = document) => root.querySelector(s);
const NS = "http://www.w3.org/2000/svg";

function icon(name) {
  const svg = document.createElementNS(NS, "svg");
  const use = document.createElementNS(NS, "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

const store = {
  get(k, d) { try { const v = localStorage.getItem("bt." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("bt." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

function fmtSize(n) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i >= 3 ? 2 : i === 2 ? 1 : 0)} ${u[i]}`;
}
const fmtNum = (n) => Number(n).toLocaleString();
function fmtDate(s) {
  const d = s ? new Date(s) : null;
  return d && !isNaN(d) ? d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : null;
}
function safeHttpUrl(u) {
  try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x : null; } catch { return null; }
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-BuzzTorrent": "1" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  let data;
  try { data = await res.json(); } catch { throw new Error(`server returned ${res.status}`); }
  if (res.status === 403 && data.locked) { exitPrivate("The private section locked itself"); }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg, kind = "check") {
  const t = h("div", { class: "toast", role: "status" }, icon(kind), h("span", { text: msg }));
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 2600);
}

/* --------------------------------------------------------------- state */
const state = {
  q: "", cat: "all", lang: "any", genre: "any", data: null, ms: 0, searchSeq: 0,
  verdicts: { safe: true, caution: true, danger: !store.get("hideDanger", true) },
  minSeeds: "0", size: "any", minQuality: "any", sort: store.get("sortV2", "quality"),   // V2: best quality became the default
  scans: new Map(),
};

/* --------------------------------------------------------------- theme */
function applyTheme(t) {
  if (t === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  store.set("theme", t);
  renderSegmented($("#setTheme"), THEMES, t, applyTheme);
}
function effectiveTheme() {
  const t = document.documentElement.getAttribute("data-theme");
  return t || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
}

/* --------------------------------------------------------- small widgets */
function renderSegmented(root, options, value, onPick) {
  root.replaceChildren(...options.map(([id, label]) => h("button", {
    type: "button", role: "radio", "aria-checked": String(id === value), text: label,
    onclick: () => onPick(id),
  })));
}

function confirmDanger(r) {
  if (r.verdict !== "danger") return Promise.resolve(true);
  const wrap = $("#modal");
  const reasons = r.flags.filter((f) => f.level === "danger");
  $("#modalBody").replaceChildren(
    h("p", { class: "muted", text: `“${r.title}” shows signs of being fake or malicious:` }),
    h("ul", {}, reasons.map((f) => h("li", { text: f.msg }))));
  wrap.hidden = false;
  $("#modalCancel").focus();
  return new Promise((resolve) => {
    const done = (ok) => {
      wrap.hidden = true;
      wrap.onclick = $("#modalOk").onclick = $("#modalCancel").onclick = null;
      document.removeEventListener("keydown", onKey, true);
      resolve(ok);
    };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
    document.addEventListener("keydown", onKey, true);
    $("#modalOk").onclick = () => done(true);
    $("#modalCancel").onclick = () => done(false);
    wrap.onclick = (e) => { if (e.target === wrap) done(false); };
  });
}

/* --------------------------------------------------------------- home */
function renderCatTiles() {
  $("#catTiles").replaceChildren(...CATS.map(([id, label]) => h("button", {
    type: "button", class: "cat-tile", title: `Browse popular ${label.toLowerCase()}`,
    onclick: () => showBrowse({ cat: id, col: "", sort: "popular", decade: "any", page: 1 }),
  }, icon(id), h("span", { text: label }), h("span", { class: "tile-hint", text: "Browse" }))));
}

function setMode(mode) {
  document.body.className = "mode-" + mode;
  $("#home").hidden = mode !== "home";
  $("#resultsView").hidden = mode !== "results";
  $("#scanView").hidden = mode !== "scanner";
  $("#libraryView").hidden = mode !== "library";
  $("#browseView").hidden = mode !== "browse";
  if (mode !== "browse") closeDetail();
  if (mode === "scanner") startScanPolling(); else stopScanPolling();
}

function goHome(push = true) {
  state.q = ""; state.data = null;
  if (state.cat === "adult") state.cat = "all";
  $("#privateBar").hidden = true;
  $("#qHero").value = $("#qTop").value = "";
  if (push) history.pushState(null, "", "/");
  setMode("home");
  document.title = "BuzzTorrent";
  renderCatTiles();
  loadPersonal();
  loadShelves();
  $("#qHero").focus();
}

/* ------------------------------------------------------------- search */
function searchUrl() {
  const p = new URLSearchParams({ q: state.q, cat: state.cat });
  if (state.lang !== "any") p.set("lang", state.lang);
  if (state.genre !== "any" && genreApplies(state.cat)) p.set("genre", state.genre);
  return p;
}
const genreApplies = (cat) => cat === "books" || cat === "all";

async function search(q, cat, push = true, opts = {}) {
  q = q.trim();
  if (!q) return;
  state.q = q; state.cat = cat;
  if (opts.lang !== undefined) state.lang = opts.lang;
  if (opts.genre !== undefined) state.genre = opts.genre;
  if (!genreApplies(cat)) state.genre = "any";
  $("#qTop").value = $("#qHero").value = q;
  $("#heroLang").value = state.lang;
  const priv = cat === "adult";
  if (priv) history.replaceState(null, "", "/");          // no browser history for private searches
  else if (push) history.pushState(null, "", "/?" + searchUrl());
  document.title = priv ? "BuzzTorrent" : `${q} · BuzzTorrent`;
  $("#privateBar").hidden = !priv;
  setMode("results");
  renderSkeleton();
  const seq = ++state.searchSeq;
  const t0 = performance.now();
  try {
    const data = await api(`/api/search?${searchUrl()}&smart=${smartParam()}`);
    if (seq !== state.searchSeq) return; // a newer search started
    state.data = data;
    state.ms = performance.now() - t0;
    render();
    renderQueryNote(data);
  } catch (err) {
    if (seq !== state.searchSeq) return;
    state.data = null;
    $("#results").replaceChildren();
    $("#resultsTitle").textContent = `“${q}”`;
    $("#resultsSub").textContent = "";
    showEmpty("alert", "Search failed", err.message, null);
  }
}

function renderSkeleton() {
  $("#resultsTitle").textContent = `“${state.q}”`;
  $("#resultsSub").textContent = "Searching every source…";
  $("#sourceErrors").replaceChildren();
  $("#queryNote").replaceChildren();
  $("#empty").hidden = true;
  const tpl = $("#tpl-skeleton").content.firstElementChild;
  $("#results").replaceChildren(...Array.from({ length: 6 }, () => tpl.cloneNode(true)));
  renderFacets();
}

/* ------------------------------------------------------------ filtering */
function passes(r, ignore) {
  if (ignore !== "verdict" && !state.verdicts[r.verdict === "unknown" ? "caution" : r.verdict]) return false;
  const min = Number(state.minSeeds);
  if (min > 0 && !(r.trusted_source && r.seeders == null) && !((r.seeders ?? 0) >= min)) return false;
  if (state.size !== "any") {
    const s = r.size;
    if (!s) return false;
    if (state.size === "lt1" && s >= GB) return false;
    if (state.size === "1to5" && (s < GB || s > 5 * GB)) return false;
    if (state.size === "gt5" && s <= 5 * GB) return false;
  }
  if (state.minQuality !== "any" && !((r.quality?.height || 0) >= Number(state.minQuality) * 0.95)) return false;
  return true;
}

function sorted(list) {
  const by = {
    best: null,
    seeders: (a, b) => (b.seeders ?? (b.trusted_source ? 0.5 : -1)) - (a.seeders ?? (a.trusted_source ? 0.5 : -1)),
    // Safe before risky, real matches before loosely related videos, then the sharpest copy
    quality: (a, b) => (VERDICT_ORDER[a.verdict] ?? 2) - (VERDICT_ORDER[b.verdict] ?? 2)
      || matchTier(b) - matchTier(a)
      || (b.quality?.height || 0) - (a.quality?.height || 0),
    "size-desc": (a, b) => (b.size || 0) - (a.size || 0),
    "size-asc": (a, b) => (a.size || Infinity) - (b.size || Infinity),
    newest: (a, b) => (Date.parse(b.published) || 0) - (Date.parse(a.published) || 0),
  }[state.sort];
  return by ? [...list].sort(by) : list;
}

/* --------------------------------------------------------------- facets */
function renderFacets() {
  const results = state.data?.results || [];
  const vCount = { safe: 0, caution: 0, danger: 0 };
  for (const r of results) if (passes(r, "verdict")) vCount[r.verdict === "unknown" ? "caution" : r.verdict]++;
  $("#facetVerdict").replaceChildren(...VERDICTS.map(([id, label]) => h("button", {
    type: "button", class: `facet-item v-${id}`, "aria-pressed": String(state.verdicts[id]),
    onclick: () => { state.verdicts[id] = !state.verdicts[id]; render(); },
  }, icon(id), h("span", { text: label }), h("span", { class: "count", text: state.data ? vCount[id] : "" }))));

  const cCount = {};
  if (state.cat === "all") for (const r of results) cCount[r.category] = (cCount[r.category] || 0) + 1;
  const cats = privateOpen ? [...CATS, ["adult", "🔒 18+"]] : CATS;
  $("#facetCat").replaceChildren(...cats.map(([id, label]) => h("button", {
    type: "button", class: "facet-item", role: "radio", "aria-checked": String(id === state.cat),
    onclick: () => { if (id !== state.cat) search(state.q, id); closeFilters(); },
  }, icon(id), h("span", { text: label }),
  h("span", { class: "count", text: id === "all" ? (state.cat === "all" && state.data ? results.length : "") : (cCount[id] || "") }))));

  const fl = $("#facetLang");
  if (!fl.options.length) {
    fillSelect(fl, LANGS, state.lang);
    fl.addEventListener("change", () => { search(state.q, state.cat, true, { lang: fl.value }); closeFilters(); });
  }
  fl.value = state.lang;
  $("#facetGenreBox").hidden = !genreApplies(state.cat);
  $("#facetGenre").replaceChildren(...GENRES.map(([id, label]) => h("button", {
    type: "button", class: "facet-item", role: "radio", "aria-checked": String(id === state.genre),
    onclick: () => { if (id !== state.genre) search(state.q, state.cat, true, { genre: id }); closeFilters(); },
  }, icon(id === "poetry" ? "music" : id === "religion" ? "star" : id === "any" ? "books" : "other"), h("span", { text: label }))));

  renderSegmented($("#facetSeeds"), SEEDS, state.minSeeds, (v) => { state.minSeeds = v; render(); });
  renderSegmented($("#facetSize"), SIZES, state.size, (v) => { state.size = v; render(); });
  renderSegmented($("#facetQuality"), QUALITIES, state.minQuality, (v) => { state.minQuality = v; render(); });

  const sources = state.data?.sources || [];
  $("#facetSources").replaceChildren(...(sources.length ? sources.map((s) => h("div", {
    class: "source-row", title: s.ok ? `${s.count} results in ${s.ms} ms` : s.error,
  }, h("span", { class: "dot " + (s.ok ? "ok" : "bad") }), h("span", { text: s.name }),
  h("span", { class: "meta-r", text: s.ok ? `${s.count} · ${(s.ms / 1000).toFixed(1)}s` : "error" })))
    : [h("div", { class: "source-row muted", text: state.data ? "No sources" : "…" })]));
}

/* -------------------------------------------------------------- results */
function render() {
  renderFacets();
  const data = state.data;
  if (!data) return;
  const visible = sorted(data.results.filter((r) => passes(r)));
  const hiddenDanger = data.results.filter((r) => r.verdict === "danger" && !state.verdicts.danger && passes(r, "verdict")).length;

  $("#resultsTitle").textContent = `“${data.query}”`;
  const parts = [`${fmtNum(visible.length)} of ${fmtNum(data.results.length)} results`];
  if (data.category !== "all") parts.push(`in ${CAT_LABEL[data.category]}`);
  if (hiddenDanger) parts.push(`${hiddenDanger} dangerous hidden`);
  parts.push(`${(state.ms / 1000).toFixed(1)}s`);
  $("#resultsSub").textContent = parts.join(" · ");

  $("#sourceErrors").replaceChildren(...data.errors.map((e) => h("div", { class: "notice warn" },
    icon("alert"), h("span", { text: `${e.source} didn't respond properly: ${e.error}` }))));

  const tpl = $("#tpl-card").content.firstElementChild;
  $("#results").replaceChildren(...visible.map((r) => {
    const li = tpl.cloneNode(true);
    fillCard(li, r);
    const scan = state.scans.get(r.id);
    if (scan && scan.open) renderScan(li, r, scan.data);
    return li;
  }));

  if (!data.results.length) {
    const onlyBuiltin = data.sources.length === 1;
    showEmpty("search", "Nothing found",
      onlyBuiltin ? "The built-in source only covers public-domain and openly licensed material. Try a different spelling or category, or connect Jackett/Prowlarr for more sources."
        : "Try a different spelling, fewer words, or another category.",
      onlyBuiltin ? ["Connect more sources", openSettingsAdd] : null);
  } else if (!visible.length) {
    showEmpty("filter", "Everything is hidden by your filters", "Loosen the filters on the left to see more.",
      ["Reset filters", resetFilters]);
  } else {
    $("#empty").hidden = true;
  }
}

function showEmpty(ic, title, text, action) {
  const box = $("#empty");
  box.replaceChildren(icon(ic), h("h3", { text: title }), h("p", { text }),
    action && h("button", { type: "button", class: "btn btn-ghost", text: action[0], onclick: action[1] }));
  box.hidden = false;
}

function resetFilters() {
  state.verdicts = { safe: true, caution: true, danger: true };
  state.minSeeds = "0"; state.size = "any"; state.minQuality = "any";
  render();
}

function seedClass(r) {
  if (r.seeders == null) return null;
  return r.seeders === 0 ? "seeds none" : r.seeders < 10 ? "seeds low" : "seeds";
}

function fillCard(li, r) {
  li.className = `card v-${r.verdict}`;
  li.onclick = (e) => { if (!e.target.closest(".card-actions, .scan-panel, a, button")) openDetail(r); };
  li.tabIndex = 0;
  li.onkeydown = (e) => { if (e.key === "Enter" && e.target === li) openDetail(r); };
  li.dataset.id = r.id;
  const v = $(".verdict", li);
  v.className = `verdict ${r.verdict}`;
  v.setAttribute("aria-label", `${r.verdict}, risk score ${r.score} of 100`);
  v.title = "Safety score: 100 = no warning signs found";
  $("use", v).setAttribute("href", `#i-${r.verdict === "unknown" ? "shield" : r.verdict}`);
  $(".verdict-label", v).textContent = r.verdict === "danger" ? "Risky" : r.verdict;
  $(".verdict-score", v).textContent = r.score;

  $(".card-title", li).textContent = r.title;
  if (r.relevance != null && r.relevance < 0.9) $(".card-title", li).append(h("span", { class: "match-pill", text: "close match" }));
  $(".card-title", li).title = r.title;

  const meta = $(".meta", li);
  meta.replaceChildren(h("span", {}, icon(r.category in CAT_LABEL && r.category !== "other" ? r.category : "other"),
    CAT_LABEL[r.category] || "Other"), h("span", {}, icon("drive"), fmtSize(r.size)));
  if (r.quality) {
    meta.append(h("span", { class: `q-pill q-${r.quality.label.toLowerCase()}`, text: r.quality.label,
      title: r.quality.exact ? `Video resolution: ${r.quality.height}p (measured from the files)` : "Quality from the release name" }));
  }
  if (r.seeders != null) {
    meta.append(h("span", { class: seedClass(r), title: "Seeders" }, icon("up"), fmtNum(r.seeders)),
      h("span", { title: "Leechers" }, icon("down"), r.leechers == null ? "?" : fmtNum(r.leechers)));
  } else if (r.trusted_source) {
    meta.append(h("span", { class: "seeds", title: "Downloads straight from the source's servers, even with no peers" },
      icon("up"), "Web-seeded"));
  }
  for (const code of (r.language || []).slice(0, 2)) {
    if (LANG_SHORT[code]) meta.append(h("span", { class: "lang-pill", text: LANG_SHORT[code] }));
  }
  const date = fmtDate(r.published);
  if (date) meta.append(h("span", {}, icon("calendar"), date));
  meta.append(h("span", { class: "src" }, icon("globe"), r.sources.join(", ")));

  const shown = r.flags.filter((f) => f.level !== "info" || /hash|curated/i.test(f.msg));
  $(".flags", li).replaceChildren(...shown.map((f) => h("span", {
    class: `pill pill-${f.level === "info" ? (/curated/i.test(f.msg) ? "safe" : "info") : f.level}`, title: f.msg,
  }, icon(f.level === "danger" ? "danger" : f.level === "warn" ? "alert" : /curated/i.test(f.msg) ? "check" : "info"),
  /curated/i.test(f.msg) ? "Curated source" : f.msg)));

  const magnet = bindActions(li, r);
  const scan = $(".act-scan", li);
  scan.hidden = !magnet && !r.has_torrent;
  scan.title = r.has_torrent ? "Deep scan: check the file list" : "Safety check";
  scan.onclick = () => toggleScan(li, r);
}

/** Wires whichever of .act-open / .act-copy / .act-dl / .act-src exist under root. Returns the magnet. */
function bindActions(root, r) {
  const magnet = r.magnet && MAGNET_RE.test(r.magnet) ? r.magnet : null;
  const stop = (e) => e.stopPropagation(); // cards are clickable; actions shouldn't also open the popup
  const get = $(".act-get", root);
  if (get) {
    get.hidden = !dlState.available || (!magnet && !r.has_torrent);
    get.onclick = async (e) => {
      stop(e);
      const risky = r.verdict === "danger";
      if (risky && !(await confirmDanger(r))) return;
      get.disabled = true;
      try {
        await api("/api/downloads/add", { id: r.id, force: risky });
        toast("Downloading in BuzzTorrent. Progress: Downloads button, top right");
        refreshDownloads();
      } catch (err) {
        toast(err.message, "alert");
      } finally {
        get.disabled = false;
      }
    };
  }
  const open = $(".act-open", root);
  if (open) {
    open.hidden = !magnet;
    if (magnet) open.href = magnet; else open.removeAttribute("href");
    open.onclick = async (e) => {
      stop(e);
      if (r.verdict !== "danger") { sentToast("Sent to your torrent app"); return; }
      e.preventDefault();
      if (await confirmDanger(r)) { location.href = magnet; sentToast("Sent to your torrent app"); }
    };
  }
  const copy = $(".act-copy", root);
  if (copy) {
    copy.hidden = !magnet;
    copy.onclick = async (e) => {
      stop(e);
      if (!(await confirmDanger(r))) return;
      try { await navigator.clipboard.writeText(magnet); toast("Magnet link copied"); }
      catch { prompt("Copy the magnet link:", magnet); }
    };
  }
  const dl = $(".act-dl", root);
  if (dl) {
    dl.hidden = !r.has_torrent;
    const dlBase = `/api/torrent?id=${encodeURIComponent(r.id)}`;
    dl.href = dlBase;
    dl.onclick = async (e) => {
      stop(e);
      if (r.verdict !== "danger") { sentToast("Verified .torrent downloading"); return; }
      e.preventDefault();
      if (await confirmDanger(r)) { location.href = dlBase + "&force=1"; sentToast("Verified .torrent downloading"); }
    };
  }
  const src = $(".act-src", root);
  if (src) {
    const u = r.details_url && safeHttpUrl(r.details_url);
    src.hidden = !u;
    src.onclick = stop;
    if (u) { src.href = u.href; src.title = `Open source page (${u.hostname})`; }
  }
  return magnet;
}

/* ---------------------------------------------------------------- scan */
async function toggleScan(li, r) {
  const panel = $(".scan-panel", li);
  const cached = state.scans.get(r.id);
  if (cached?.data) {
    cached.open = panel.hidden;
    if (cached.open) renderScan(li, r, cached.data); else panel.hidden = true;
    return;
  }
  panel.hidden = false;
  panel.replaceChildren(h("div", { class: "scan-summary neutral" }, h("span", { class: "spinner" }),
    "Reading the file list (nothing is downloaded)…"));
  try {
    const data = await api(`/api/inspect?id=${encodeURIComponent(r.id)}`);
    Object.assign(r, data.result);
    state.scans.set(r.id, { open: true, data });
    render(); // verdict / facets may have changed
    const fresh = $(`.card[data-id="${CSS.escape(r.id)}"]`);
    if (!fresh) toast(`Scan finished: “${r.title}” is now hidden by your filters`, "info");
    else fresh.scrollIntoView({ block: "nearest", behavior: "smooth" });
  } catch (err) {
    panel.replaceChildren(h("div", { class: "scan-summary neutral" }, icon("alert"), `Scan failed: ${err.message}`));
  }
}

function fileIcon(path, bad) {
  if (bad) return icon("alert");
  for (const [name, rx] of Object.entries(EXT_ICON)) if (rx.test(path)) return icon(name);
  return icon("other");
}

function renderScan(li, r, data) {
  renderScanPanel($(".scan-panel", li), data);
}

function renderScanPanel(panel, data) {
  panel.hidden = false;
  const files = data.files || [];
  let summary;
  if (data.status === "ok" && files.length) {
    const bad = data.flagged_files || 0;
    summary = h("div", { class: "scan-summary " + (bad ? "bad" : "ok") },
      icon(bad ? "danger" : "safe"),
      h("span", { text: bad ? `${bad} of ${fmtNum(data.file_count)} files look dangerous` : `No suspicious files among ${fmtNum(data.file_count)} files` }),
      data.private && h("span", { class: "pill pill-info", text: "private tracker" }),
      h("span", { class: "spacer" }));
    if (bad && files.length > 8) {
      const cb = h("input", { type: "checkbox" });
      cb.onchange = () => panel.querySelectorAll(".files li:not(.bad)").forEach((x) => (x.hidden = cb.checked));
      summary.append(h("label", {}, cb, "Only flagged"));
    }
  } else {
    summary = h("div", { class: "scan-summary neutral" }, icon("info"),
      h("span", { text: data.note || "No file list available." }));
  }
  const list = files.length ? h("ul", { class: "files" }, files.map((f) => {
    const cut = f.path.lastIndexOf("/");
    return h("li", { class: f.warning ? "bad" : null },
      fileIcon(f.path, f.warning),
      h("span", { class: "path" }, cut > 0 && h("span", { class: "dir", text: f.path.slice(0, cut + 1) }), f.path.slice(cut + 1)),
      h("span", { class: "fsize", text: fmtSize(f.size) }),
      f.warning && h("span", { class: "why", text: "⚠ " + f.warning }));
  }), data.file_count > files.length && h("li", { class: "more", text: `…and ${fmtNum(data.file_count - files.length)} more` }))
    : null;
  panel.replaceChildren(summary, list || "");
}

/* ------------------------------------------------------------ settings */
let draft = null;

function setDirty(d) {
  $("#saveSettings").disabled = !d;
  $("#dirtyNote").hidden = !d;
}

async function openSettings() {
  const drawer = $("#drawer");
  drawer.classList.add("open");
  drawer.removeAttribute("inert");
  drawer.setAttribute("aria-hidden", "false");
  $("#scrim").hidden = false;
  $("#closeDrawer").focus();
  $("#setHideDanger").checked = store.get("hideDanger", true);
  $("#setFamily").checked = store.get("family", true);
  $("#setSmart").checked = store.get("smart", true);
  try {
    const s = await api("/api/settings");
    draft = { archive_org: s.archive_org, torznab: s.torznab.map((x) => ({ ...x, apikey: "" })) };
    $("#version").textContent = "v" + s.version;
    renderDraft();
    setDirty(false);
  } catch (err) {
    toast("Couldn't load settings: " + err.message, "alert");
  }
  loadTv();
  loadAdult();
}

/* private 18+ section */
let privateOpen = false;
async function loadAdult() {
  try {
    const s = await api("/api/adult/state");
    $("#setAdult").checked = s.turned_on;
    $("#setAdultTv").checked = s.on_tv;
    $("#adultOpen").hidden = !s.turned_on;
    $("#adultMsg").textContent = s.configured ? "" : "Choose a PIN, turn it on, then Save.";
  } catch { $("#adultSection").hidden = true; }
}
async function saveAdult() {
  const pin = $("#adultPin").value.trim();
  try {
    const s = await api("/api/adult/config", { enabled: $("#setAdult").checked, on_tv: $("#setAdultTv").checked, pin: pin || null });
    $("#adultPin").value = "";
    $("#adultMsg").textContent = s.turned_on ? "Saved. Open it with the PIN." : "Saved. The private section is off.";
    loadAdult();
  } catch (err) {
    $("#adultMsg").textContent = err.message;
  }
}
async function openPrivate() {
  try {
    const s = await api("/api/adult/state");
    if (!s.unlocked) await api("/api/adult/unlock", { pin: $("#adultPin").value.trim() });
  } catch (err) {
    $("#adultMsg").textContent = err.message;
    return;
  }
  $("#adultPin").value = "";
  closeSettings();
  privateOpen = true;
  state.q = ""; state.data = null; state.cat = "adult";
  history.replaceState(null, "", "/");
  document.title = "BuzzTorrent";
  setMode("results");
  $("#privateBar").hidden = false;
  $("#results").replaceChildren();
  $("#resultsTitle").textContent = "Private section";
  $("#resultsSub").textContent = "Search with the box at the top.";
  renderFacets();
  $("#qTop").value = "";
  $("#qTop").focus();
}
function exitPrivate(msg) {
  if (!privateOpen && state.cat !== "adult") return;
  privateOpen = false;
  if (msg) toast(msg, "alert");
  goHome();
}
async function lockPrivate() {
  try { await api("/api/adult/lock", {}); } catch { /* locked anyway */ }
  exitPrivate("Private section locked");
}

/* living-room TV access */
async function loadTv() {
  try { renderTv(await api("/api/tv/status")); } catch { $("#tvSection").hidden = true; }
}
function renderTv(s) {
  $("#tvSection").hidden = false;
  $("#setTv").checked = s.enabled;
  $("#setTvAuto").checked = s.autostart;
  $("#tvBox").hidden = !s.enabled;
  if (!s.enabled) return;
  const addr = s.addresses[0] ? `${s.addresses[0]}:${s.port}` : `<this PC's IP>:${s.port}`;
  $("#tvPin").textContent = s.pin || "------";
  $("#tvAddr").textContent = addr;
  $("#tvApkUrl").textContent = `http://${addr}/BuzzTorrentTV.apk`;
  $("#tvWebUrl").textContent = `http://${addr}/tv`;
  const notes = [];
  let warn = false;
  if (!s.listening) { notes.push(s.app_mode ? "Starting TV access…" : "Restart BuzzTorrent to let the TV connect."); warn = true; }
  if (s.firewall === false) { notes.push("Windows Firewall may block the TV: click “Allow through Windows Firewall”."); warn = true; }
  if (!s.magnet_app) { notes.push("No torrent app on this PC opens magnet links, so “Download on PC” won't work yet."); warn = true; }
  if (!s.apk) notes.push("The TV app isn't built yet (android\build_apk.py); a TV browser still works.");
  else if (s.apk_version) notes.push(`TV app version ${s.apk_version.name} is ready to install.`);
  notes.push(s.paired ? `${s.paired} TV${s.paired > 1 ? "s" : ""} paired.` : "No TV paired yet.");
  if (s.magnet_app) notes.push(`Downloads start in ${s.magnet_app}.`);
  $("#tvState").textContent = notes.join(" ");
  $("#tvState").classList.toggle("warn", warn);
  $("#tvFirewall").hidden = s.firewall !== false;
}
async function tvAction(body, busyMsg) {
  try {
    const s = await api(busyMsg === "firewall" ? "/api/tv/firewall" : "/api/tv/config", body);
    if (s.restarting) {
      toast(body.enabled ? "Turning on TV access…" : "Turning off TV access…");
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 700));
        try {
          const now = await api("/api/tv/status");
          if (now.listening === now.enabled) { renderTv(now); toast(now.enabled ? "TV access is on" : "TV access is off"); return; }
        } catch { /* restarting */ }
      }
      toast("BuzzTorrent didn't come back. Start it again from the shortcut.", "alert");
      return;
    }
    renderTv(s);
    if (s.restart_needed) toast("Restart BuzzTorrent to apply", "alert");
  } catch (err) {
    toast(err.message, "alert");
    loadTv();
  }
}
function openSettingsAdd() {
  openSettings();
  $("#addSource").open = true;
  setTimeout(() => $("#newName").focus(), 250);
}
function closeSettings() {
  const drawer = $("#drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("inert", "");
  drawer.setAttribute("aria-hidden", "true");
  if (!$("#sidebar").classList.contains("open")) $("#scrim").hidden = true;
}

function renderDraft() {
  $("#setArchive").checked = draft.archive_org;
  $("#setSources").replaceChildren(...draft.torznab.map((s, i) => {
    const sw = h("input", { type: "checkbox" });
    sw.checked = s.enabled;
    sw.onchange = () => { s.enabled = sw.checked; setDirty(true); };
    const msg = h("div", { class: "test-msg" });
    return h("div", { class: "source-card" },
      h("div", { class: "source-card-main" },
        h("div", { class: "source-name" }, s.name,
          h("span", { class: "pill " + (s.has_key || s.apikey ? "pill-info" : "pill-warn"), text: s.has_key || s.apikey ? "key set" : "no key" })),
        h("div", { class: "source-url", text: s.url, title: s.url }), msg),
      h("button", {
        type: "button", class: "icon-btn", title: "Test connection", "aria-label": `Test ${s.name}`,
        onclick: () => testSource(s, msg),
      }, icon("plug")),
      h("button", {
        type: "button", class: "icon-btn", title: "Remove", "aria-label": `Remove ${s.name}`,
        onclick: () => { draft.torznab.splice(i, 1); renderDraft(); setDirty(true); },
      }, icon("trash")),
      h("label", { class: "switch" }, sw, h("span"), h("span", { class: "sr-only", text: `Enable ${s.name}` })));
  }));
}

async function testSource(src, msgEl) {
  msgEl.className = "test-msg";
  msgEl.textContent = "Testing…";
  try {
    const res = await api("/api/sources/test", { name: src.name, url: src.url, apikey: src.apikey || "" });
    msgEl.className = "test-msg " + (res.ok ? "ok" : "bad");
    msgEl.textContent = (res.ok ? "✓ " : "✕ ") + res.message;
    return res.ok;
  } catch (err) {
    msgEl.className = "test-msg bad";
    msgEl.textContent = "✕ " + err.message;
    return false;
  }
}

function newSourceFromForm() {
  const s = { name: $("#newName").value.trim(), url: $("#newUrl").value.trim(), apikey: $("#newKey").value.trim(), enabled: true };
  const msg = $("#testMsg");
  if (!s.name || !s.url) { msg.className = "test-msg bad"; msg.textContent = "Name and URL are required."; return null; }
  if (draft.torznab.some((x) => x.name.toLowerCase() === s.name.toLowerCase())) {
    msg.className = "test-msg bad"; msg.textContent = "A source with that name already exists."; return null;
  }
  return s;
}

async function saveSettings() {
  const btn = $("#saveSettings");
  btn.disabled = true;
  try {
    const s = await api("/api/settings", draft);
    draft = { archive_org: s.archive_org, torznab: s.torznab.map((x) => ({ ...x, apikey: "" })) };
    renderDraft();
    setDirty(false);
    toast("Sources saved");
    loadFooter();
    if (state.q) search(state.q, state.cat, false);
  } catch (err) {
    toast(err.message, "alert");
    btn.disabled = false;
  }
}

/* --------------------------------------------------------- mobile filters */
function openFilters() { $("#sidebar").classList.add("open"); $("#scrim").hidden = false; }
function closeFilters() {
  $("#sidebar").classList.remove("open");
  if (!$("#drawer").classList.contains("open")) $("#scrim").hidden = true;
}

/* ------------------------------------------------------- download scanner */
const scan = { status: null, timer: null, expanded: false, dirty: false };
const STATE_UI = {
  clean: ["safe", "Clean"], suspicious: ["caution", "Suspicious"], dangerous: ["danger", "Dangerous"],
  unverified: ["info", "Not virus-scanned"], waiting: ["clock", "Downloading"], queued: ["clock", "Queued"],
  scanning: [null, "Scanning"], unscanned: ["shield", "Not scanned"], gone: ["x", "Removed"], error: ["alert", "Error"],
  quarantined: ["lock", "Quarantined"], empty: ["other", "Empty"],
};

function fmtAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function sentToast(what) {
  const s = scan.status;
  toast(s?.enabled ? `${what}. It will be virus-scanned when it finishes downloading.`
    : `${what}. Turn on the Download scanner to virus-scan it when it finishes.`, s?.enabled ? "safe" : "info");
}

function showScanner(push = true) {
  if (push) history.pushState(null, "", "/?view=scanner");
  document.title = "Download scanner · BuzzTorrent";
  setMode("scanner");
  refreshScanner();
}

/* ---------------------------------------------- built-in downloader */
const dlState = { available: true, items: [] };
const DL_STATUS = { active: "Downloading", metadata: "Finding the files…", waiting: "Queued", paused: "Paused",
  error: "Failed", complete: "Done", seeding: "Done · sharing back", removed: "Stopped" };
function fmtEta(s) {
  if (s == null) return "";
  if (s < 60) return `${s}s left`;
  if (s < 3600) return `${Math.round(s / 60)} min left`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min left`;
}
async function refreshDownloads() {
  let d;
  try { d = await api("/api/downloads"); } catch { return; }
  dlState.available = d.available;
  dlState.items = d.items;
  const active = d.items.filter((x) => ["active", "metadata", "waiting"].includes(x.status)).length;
  const badge = $("#dlBadge");
  badge.hidden = !active;
  badge.textContent = String(active);
  $("#downloadsBtn").title = active ? `Downloads: ${active} in progress` : "Downloads";
  if (!$("#scanView").hidden) renderDownloads();
}
function renderDownloads() {
  const items = dlState.items.filter((x) => x.status !== "removed");
  $("#dlSection").hidden = !items.length;
  const active = items.filter((x) => ["active", "metadata"].includes(x.status));
  const speed = active.reduce((n, x) => n + x.down, 0);
  $("#dlSummary").textContent = active.length ? `${active.length} downloading · ${fmtSize(speed)}/s` : "";
  $("#dlItems").replaceChildren(...items.map((x) => {
    const pct = x.total ? Math.floor((x.done / x.total) * 100) : 0;
    const fill = h("span", { class: "dl-fill" });
    fill.style.width = `${pct}%`;
    const info = x.status === "active"
      ? [`${pct}%`, `${fmtSize(x.done)} of ${fmtSize(x.total)}`, `${fmtSize(x.down)}/s`, `${x.peers} peers`, fmtEta(x.eta)]
      : x.status === "seeding" ? [fmtSize(x.total), `shared ${fmtSize(x.uploaded)}`, "stops by itself"]
        : x.status === "error" ? [x.error || "Download failed"] : [x.total ? `${pct}% · ${fmtSize(x.total)}` : ""];
    const btn = (label, action) => h("button", { type: "button", class: "btn btn-ghost btn-sm", text: label, onclick: async () => {
      try { await api("/api/downloads/action", { gid: x.gid, action }); refreshDownloads(); } catch (err) { toast(err.message, "alert"); }
    } });
    const actions = [];
    if (["active", "waiting", "metadata"].includes(x.status)) actions.push(btn("Pause", "pause"));
    if (x.status === "paused") actions.push(btn("Resume", "resume"));
    actions.push(btn(x.status === "seeding" ? "Stop sharing" : ["complete", "error"].includes(x.status) ? "Clear" : "Stop", "remove"));
    return h("li", { class: `dl-item dl-${x.status}` },
      h("div", { class: "dl-main" },
        h("div", { class: "dl-title", dir: "auto", text: x.title }),
        h("div", { class: "dl-bar" }, fill),
        h("div", { class: "muted small" }, h("b", { text: DL_STATUS[x.status] || x.status }),
          ...info.filter(Boolean).map((t) => ` · ${t}`))),
      h("div", { class: "dl-actions" }, actions));
  }));
}

async function refreshScanner() {
  refreshDownloads();
  try { scan.status = await api("/api/scanner"); } catch { return; }
  updateBadge();
  if (!$("#scanView").hidden) renderScanner();
}
function startScanPolling() { stopScanPolling(); scan.timer = setInterval(refreshScanner, 3000); }
function stopScanPolling() { clearInterval(scan.timer); scan.timer = null; }

function updateBadge() {
  const s = scan.status;
  const n = s.counts.dangerous;
  const badge = $("#scannerBadge");
  badge.hidden = !n;
  badge.textContent = n > 9 ? "9+" : String(n);
  $("#scannerBtn").title = !s.enabled ? "Download scanner (off)" : n ? `${n} dangerous download(s) found` : "Download scanner: watching";
  $("#setScanState").textContent = s.enabled ? `Watching ${s.folder}` : "Off";
}

function scard(iconName, label, dotCls, value, sub, extra) {
  return h("div", { class: "scard" },
    h("div", { class: "scard-label" }, icon(iconName), label),
    h("div", { class: "scard-value" }, h("span", { class: "dot " + dotCls }), value),
    sub && h("div", { class: "scard-sub", text: sub }), extra);
}

function renderScanner() {
  const s = scan.status;
  if (!s) return;
  const e = s.engine;
  let engDot = "ok", engSub;
  const engVal = e ? e.name : "No antivirus found";
  if (!e) { engDot = "bad"; engSub = "Content checks still run. Turn on Microsoft Defender for full virus scans."; }
  else if (e.enabled === false) { engDot = "warn"; engSub = "Defender is turned off (another antivirus may be active)"; }
  else engSub = [e.realtime ? "Real-time protection on" : e.realtime === false ? "Real-time protection OFF" : null,
    e.signatures ? `signatures ${e.signatures}` : null].filter(Boolean).join(" · ");
  $("#scanCards").replaceChildren(
    scard("folder", "Watching", s.enabled ? "ok" : "", s.enabled ? "On" : "Off",
      s.enabled ? s.folder : "Choose your download folder below",
      s.enabled && h("button", { type: "button", class: "link small", onclick: () => scanAction("reveal", "") }, icon("folder"), "Open folder")),
    scard("safe", "Antivirus", engDot, engVal, engSub),
    scard("globe", "VirusTotal", s.virustotal ? "ok" : "", s.virustotal ? "On" : "Off",
      s.virustotal ? "Second opinion by file hash. Files are never uploaded" : "Optional second opinion"));

  const panel = $("#scanSetup");
  const collapsed = s.enabled && !scan.expanded;
  panel.classList.toggle("collapsed", collapsed);
  $("#setupTitle").textContent = s.enabled ? "Scanner settings" : "Watch your downloads";
  $("#scanStop").hidden = !s.enabled || collapsed;
  $("#scanSave").querySelector("span").textContent = collapsed ? "Edit settings" : s.enabled ? "Save changes" : "Start watching";
  $("#scanSave").classList.toggle("btn-primary", !collapsed);
  $("#scanSave").classList.toggle("btn-ghost", collapsed);
  if (!scan.dirty) {
    $("#scanFolder").value = s.folder || s.suggestions[0] || "";
    $("#scanAutoQ").checked = s.auto_quarantine;
    $("#vtClear").hidden = !s.virustotal;
    $("#vtKey").placeholder = s.virustotal ? "Key saved. Paste a new one to replace it" : "VirusTotal API key (64 characters)";
  }
  $("#folderSuggest").replaceChildren(...s.suggestions.map((f) => h("button", {
    type: "button", title: f, onclick: () => { $("#scanFolder").value = f; scan.dirty = true; },
  }, icon("folder"), f)));

  const items = s.items;
  const bad = s.counts.dangerous; // excludes downloads whose flagged files are all quarantined
  const fixed = items.filter((r) => r.state === "done" && r.verdict === "dangerous").length - bad;
  const active = items.filter((r) => ["waiting", "queued", "scanning"].includes(r.state) && r.file_count).length;
  $("#scanSummary").textContent = items.length
    ? [`${items.length} item(s)`, active && `${active} in progress`, bad && `${bad} need attention`,
      fixed && `${fixed} quarantined`].filter(Boolean).join(" · ") : "";
  $("#scanItems").replaceChildren(...items.map(renderScanItem));
  const empty = $("#scanEmpty");
  if (!items.length && s.enabled) {
    empty.replaceChildren(icon("download"), h("h3", { text: "No downloads yet" }),
      h("p", { text: "Start a torrent that saves into the watched folder. It shows up here, and gets scanned automatically once it finishes." }));
    empty.hidden = false;
  } else empty.hidden = true;
}

function renderScanItem(r) {
  const flaggedFiles = r.state === "done" ? [...new Set([...(r.engine?.threats || []).map((t) => t.file),
    ...r.findings.filter((f) => f.level === "danger").map((f) => f.file)])] : [];
  const allFixed = flaggedFiles.every((f) => (r.quarantined || []).some((q) => q.ok && q.file === f));
  let key = r.state === "done" ? r.verdict : r.state;
  if (key === "dangerous" && allFixed) key = "quarantined";
  if (key === "waiting" && r.file_count === 0) key = "empty";
  const [ic, label] = STATE_UI[key] || ["shield", key];
  const stateTile = h("div", { class: `sstate ${key}` }, ic ? icon(ic) : h("span", { class: "spinner" }), h("span", { text: label }));
  const body = h("div", { class: "sbody" }, h("h3", { class: "sname", text: r.label || r.name }));
  const meta = [fmtSize(r.size), `${fmtNum(r.file_count)} file${r.file_count === 1 ? "" : "s"}`,
    r.scanned_at ? `scanned ${fmtAgo(r.scanned_at)}` : `seen ${fmtAgo(r.seen)}`];
  body.append(h("div", { class: "meta" }, meta.map((m) => h("span", { text: m }))));
  const line = (cls, ic2, ...txt) => h("div", { class: "sline " + cls }, icon(ic2), h("span", {}, ...txt));

  if (key === "empty") body.append(line("info", "info", "This folder is empty."));
  else if (r.state === "waiting") body.append(line("info", "clock",
    `Still downloading, or changed in the last ${scan.status.settle_seconds} s. It's scanned automatically once finished.`));
  if (r.state === "unscanned") body.append(line("info", "info", "This was already in the folder when watching started."));
  if (r.state === "scanning") body.append(line("info", "safe", "Antivirus scan and content inspection in progress…"));
  if (r.state === "gone") body.append(line("info", "info", "No longer in the download folder."));
  if (r.state === "error" && r.error) body.append(line("warn", "alert", r.error));

  if (r.state !== "done" && r.quarantined?.length) {
    for (const q of r.quarantined.filter((x) => x.ok)) body.append(line("ok", "lock", `Earlier: neutralised by ${q.by}: `, h("code", { text: q.file })));
  }
  if (r.state === "done") {
    const eng = r.engine || {};
    const fixed = new Set((r.quarantined || []).filter((q) => q.ok).map((q) => q.file));
    if (eng.threats?.length) {
      for (const t of eng.threats) body.append(line("bad", "danger", `${t.engine} found `, h("b", { text: t.name }),
        t.file ? " in " : "", t.file ? h("code", { text: t.file }) : ""));
    } else if (eng.ok) {
      body.append(line("ok", "safe", `${eng.name}: no threats found`));
    } else {
      body.append(line("warn", "alert", `Antivirus scan unavailable${eng.error ? ": " + eng.error : ""}`));
    }
    const vt = r.virustotal;
    if (vt) body.append(vt.error ? line("warn", "globe", `VirusTotal: ${vt.error}`)
      : line(vt.hits.length ? "warn" : "ok", "globe",
        `VirusTotal: ${vt.checked} risky file(s) checked by fingerprint` +
        (vt.hits.length ? `, ${vt.hits.length} flagged` : ", none flagged") +
        (vt.unknown ? ` (${vt.unknown} never seen before)` : "")));
    const signed = [...new Set((r.signatures || []).filter((x) => x.status === "Valid").map((x) => x.signer))];
    if (signed.length) body.append(line("ok", "check", `Programs signed by: ${signed.slice(0, 4).join(", ")}`));
    if (r.findings.length) {
      const order = { danger: 0, warn: 1, info: 2 };
      const list = [...r.findings].sort((a, b) => order[a.level] - order[b.level]);
      body.append(h("ul", { class: "sfindings" }, list.slice(0, 12).map((f) => h("li", { class: f.level },
        icon(f.level === "danger" ? "danger" : f.level === "warn" ? "alert" : "info"),
        h("span", {}, f.msg, h("span", { class: "file", text: f.file + (fixed.has(f.file) ? "  (quarantined)" : "") }))))));
      if (list.length > 12) body.append(h("div", { class: "sline info", text: `…and ${list.length - 12} more` }));
    }
    for (const q of r.quarantined || []) {
      body.append(q.ok
        ? line("ok", "lock", q.by === "BuzzTorrent" ? "Moved to quarantine (renamed .blocked): " : `Neutralised by ${q.by}: `, h("code", { text: q.file }))
        : line("bad", "alert", "Could not quarantine ", h("code", { text: q.file }), q.error ? `: ${q.error}` : ""));
    }
  }

  const actions = h("div", { class: "sactions" });
  if (flaggedFiles.length && !allFixed) actions.append(h("button", {
    type: "button", class: "btn btn-danger", onclick: () => scanAction("quarantine", r.name),
  }, icon("lock"), "Quarantine"));
  if (["unscanned", "done", "error", "waiting"].includes(r.state)) actions.append(h("button", {
    type: "button", class: "btn btn-ghost", onclick: () => scanAction("scan", r.name),
  }, icon(r.state === "done" || r.state === "error" ? "refresh" : "safe"), r.state === "done" || r.state === "error" ? "Rescan" : "Scan now"));
  if (r.state !== "gone") actions.append(h("button", {
    type: "button", class: "icon-btn", title: "Show in folder", "aria-label": "Show in folder", onclick: () => scanAction("reveal", r.name),
  }, icon("folder")));
  actions.append(h("button", {
    type: "button", class: "icon-btn", title: "Remove from this list", "aria-label": "Remove from list", onclick: () => scanAction("forget", r.name),
  }, icon("x")));
  return h("li", { class: `sitem s-${key}` }, stateTile, body, actions);
}

async function scanAction(action, name) {
  try {
    const res = await api(`/api/scanner/${action}`, { name });
    if (action === "quarantine") {
      const ok = res.done.filter((d) => d.ok).length;
      toast(ok ? `${ok} file(s) quarantined` : "Nothing could be quarantined", ok ? "lock" : "alert");
    } else if (action === "scan") toast("Scan queued", "safe");
    if (res.items) { scan.status = res; updateBadge(); renderScanner(); }
  } catch (err) {
    toast(err.message, "alert");
  }
}

async function saveScanner(enabled) {
  const s = scan.status;
  if (s?.enabled && !scan.expanded && enabled) { scan.expanded = true; renderScanner(); $("#scanFolder").focus(); return; }
  const msg = $("#scanMsg");
  msg.className = "test-msg"; msg.textContent = "Saving…";
  try {
    const res = await api("/api/scanner/config", {
      enabled, folder: $("#scanFolder").value.trim(), auto_quarantine: $("#scanAutoQ").checked,
      virustotal_key: $("#vtKey").value.trim(),
    });
    scan.status = res; scan.dirty = false; scan.expanded = false;
    $("#vtKey").value = "";
    msg.textContent = "";
    toast(enabled ? "Watching for finished downloads" : "Download scanner stopped", enabled ? "safe" : "info");
    updateBadge(); renderScanner();
  } catch (err) {
    msg.className = "test-msg bad"; msg.textContent = "✕ " + err.message;
  }
}

async function clearVirusTotal() {
  try {
    const s = scan.status;
    scan.status = await api("/api/scanner/config",
      { enabled: s.enabled, folder: s.folder || "", auto_quarantine: s.auto_quarantine, clear_virustotal: true });
    toast("VirusTotal key removed", "info");
    renderScanner();
  } catch (err) { toast(err.message, "alert"); }
}

async function browseFolder() {
  const btn = $("#browseFolder");
  btn.disabled = true;
  toast("A folder picker opened. It may be behind this window", "folder");
  try {
    const res = await api("/api/scanner/browse", {});
    if (res.folder) { $("#scanFolder").value = res.folder; scan.dirty = true; }
  } catch (err) { toast(err.message, "alert"); }
  btn.disabled = false;
}

/* ---------------------------------------------------------------- browse */
const browse = { options: null, source: "", cat: "movies", col: "", sort: "popular", decade: "any", lang: "any",
  genre: "any", page: 1, seq: 0, data: null };
const SHELVES = [
  { cat: "movies", title: "Popular movies" },
  { cat: "books", lang: "hi", genre: "stories", title: "हिंदी कहानियाँ · Hindi stories", icon: "books" },
  { cat: "tv", title: "Classic TV" },
  { cat: "books", lang: "ur", genre: "poetry", title: "اردو شاعری · Urdu shayari", icon: "books" },
  { cat: "games", title: "Retro games" },
  { cat: "books", lang: "hi", genre: "poetry", title: "हिंदी कविता · Hindi poetry", icon: "books" },
  { cat: "music", col: "MudritSangeet", title: "Old Hindi film songs & ghazals", icon: "music" },
  { cat: "books", lang: "bn", genre: "fiction", title: "বাংলা উপন্যাস · Bengali novels", icon: "books" },
  { cat: "movies", lang: "hi", title: "Hindi movies" },
  { cat: "books", title: "Free English books" },
  { cat: "music", title: "Live music & records" },
  { cat: "software", title: "Classic software" },
];
const ASPECT = { books: "tall", music: "square" };
const BADGE = { safe: ["safe", "Safe"], caution: ["caution", "Caution"], danger: ["danger", "Risky"], unknown: ["shield", "Unrated"] };
const fmtCompact = (n) => new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);

const familyParam = () => (store.get("family", true) ? "1" : "0");

async function loadBrowseOptions() {
  if (!browse.options) browse.options = (await api("/api/browse/options")).sources;
  return browse.options;
}

const catWord = (cat) => (cat === "tv" ? "TV" : CAT_LABEL[cat].toLowerCase());

function catIcon(cat) { return icon(cat in CAT_LABEL && cat !== "other" ? cat : "other"); }

function pickCard(r, aspectCat) {
  const li = $("#tpl-pick").content.firstElementChild.cloneNode(true);
  li.dataset.id = r.id;
  if (r.verdict === "danger") li.classList.add("v-danger");
  const thumb = $(".pick-thumb", li);
  const aspect = ASPECT[aspectCat] || ASPECT[r.category];
  if (aspect) thumb.classList.add(aspect);
  const ph = $(".pick-ph", li);
  ph.append(catIcon(r.category));
  const img = $("img", li);
  if (r.thumb && r.thumb.startsWith("/api/thumb?")) {
    img.addEventListener("load", () => { img.classList.add("loaded"); ph.hidden = true; });
    img.addEventListener("error", () => img.remove());
    img.src = r.thumb;
  } else img.remove();
  const [bIcon, bLabel] = BADGE[r.verdict] || BADGE.unknown;
  const badge = $(".pick-badge", li);
  badge.classList.add(r.verdict);
  $("use", badge).setAttribute("href", "#i-" + bIcon);
  $("b", badge).textContent = bLabel;
  badge.title = `Safety score ${r.score}/100`;
  $(".pick-title", li).textContent = r.title;
  li.title = r.title;
  const meta = $(".pick-meta", li);
  const lang = (r.language || []).find((c) => c !== "en" && LANG_SHORT[c]);
  if (lang) meta.append(h("span", { class: "lang-pill", text: LANG_SHORT[lang] }));
  if (r.year) meta.append(h("span", { text: String(r.year) }));
  if (r.size) meta.append(h("span", { text: fmtSize(r.size) }));
  if (r.downloads) meta.append(h("span", { title: "Downloads on the source" }, icon("down"), fmtCompact(r.downloads)));
  else if (r.seeders != null) meta.append(h("span", { title: "Seeders" }, icon("up"), fmtNum(r.seeders)));
  bindActions(li, r);
  $(".act-more", li).addEventListener("click", (e) => { e.stopPropagation(); openDetail(r); });
  li.addEventListener("click", () => openDetail(r));
  li.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === li) openDetail(r); });
  return li;
}

function pickSkeleton(aspectCat) {
  const thumb = h("div", { class: "pick-thumb" + (ASPECT[aspectCat] ? " " + ASPECT[aspectCat] : "") });
  return h("li", { class: "pick skeleton", "aria-hidden": "true" }, thumb,
    h("div", { class: "pick-body" }, h("div", { class: "sk sk-line w70" }), h("div", { class: "sk sk-line w40" })));
}

/* home shelves: a few popular picks for every category */
async function loadShelves() {
  const root = $("#discover");
  if (root.dataset.loaded) return;
  root.dataset.loaded = "1";
  let opts;
  try { opts = await loadBrowseOptions(); } catch { root.dataset.loaded = ""; return; }
  const source = Object.keys(opts).find((s) => s === "Internet Archive") || Object.keys(opts)[0];
  if (!source) { root.hidden = true; return; }
  // shelves fetch when scrolled near, so a dozen rows don't all load at once
  const observer = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { observer.unobserve(e.target); e.target._load(); }
  }, { rootMargin: "500px 0px" }) : null;
  root.replaceChildren(...SHELVES.map((sh) => {
    const grid = h("ul", { class: "pick-grid" }, Array.from({ length: 5 }, () => pickSkeleton(sh.cat)));
    const params = { source, cat: sh.cat, col: sh.col || "", sort: "popular", decade: "any", lang: sh.lang || "any",
      genre: sh.genre || "any", page: 1 };
    const section = h("section", { class: "shelf" },
      h("div", { class: "shelf-head" },
        h("h2", { dir: "auto" }, icon(sh.icon || sh.cat), sh.title),
        h("button", { type: "button", class: "link small", onclick: () => showBrowse(params) }, "See all →")),
      grid);
    section._load = () => {
      const q = new URLSearchParams({ source, cat: sh.cat, page: 1, family: familyParam() });
      if (sh.col) q.set("col", sh.col);
      if (sh.lang) q.set("lang", sh.lang);
      if (sh.genre) q.set("genre", sh.genre);
      api("/api/browse?" + q)
        .then((d) => grid.replaceChildren(...d.results.slice(0, 5).map((r) => pickCard(r, sh.cat))))
        .catch(() => grid.replaceChildren(h("li", { class: "shelf-error", text: "Couldn't load suggestions right now." })));
    };
    if (observer) observer.observe(section); else section._load();
    return section;
  }));
}

/* browse page */
function browseUrl() {
  const p = new URLSearchParams({ view: "browse", cat: browse.cat });
  if (browse.col) p.set("col", browse.col);
  if (browse.sort !== "popular") p.set("sort", browse.sort);
  if (browse.decade !== "any") p.set("decade", browse.decade);
  if (browse.lang !== "any") p.set("lang", browse.lang);
  if (browse.genre !== "any") p.set("genre", browse.genre);
  if (browse.source && browse.source !== "Internet Archive") p.set("source", browse.source);
  if (browse.page > 1) p.set("page", browse.page);
  return "/?" + p;
}

function showBrowse(opts = {}, push = true) {
  Object.assign(browse, opts);
  if (push) history.pushState(null, "", browseUrl());
  setMode("browse");
  loadBrowse();
}

function fillSelect(sel, options, value) {
  sel.replaceChildren(...options.map(([id, label]) => h("option", { value: id, text: label })));
  sel.value = value;
  sel.disabled = options.length < 2;
}

async function loadBrowse() {
  const grid = $("#bGrid");
  $("#browseError").replaceChildren();
  let opts;
  try { opts = await loadBrowseOptions(); } catch (err) {
    $("#browseError").replaceChildren(h("div", { class: "notice warn" }, icon("alert"), h("span", { text: "Couldn't load: " + err.message })));
    return;
  }
  const sources = Object.keys(opts);
  if (!sources.length) {
    $("#browseError").replaceChildren(h("div", { class: "notice info" }, icon("info"), h("span", { text: "None of your sources support browsing." })));
    return;
  }
  if (!opts[browse.source]) browse.source = sources.includes("Internet Archive") ? "Internet Archive" : sources[0];
  const so = opts[browse.source];
  if (!so.collections[browse.cat]) browse.cat = "all";
  const cols = so.collections[browse.cat];
  if (!cols.some((c) => c.id === browse.col)) browse.col = cols[0].id;
  if (!so.sorts.some((s) => s.id === browse.sort)) browse.sort = so.sorts[0].id;
  if (!so.decades.some((d) => d.id === browse.decade)) browse.decade = so.decades[0].id;
  const langs = so.languages || [{ id: "any", label: "Any language" }];
  const genres = genreApplies(browse.cat) ? (so.genres || []) : [];
  if (!langs.some((l) => l.id === browse.lang)) browse.lang = "any";
  if (!genres.some((g) => g.id === browse.genre)) browse.genre = "any";

  fillSelect($("#bSource"), sources.map((s) => [s, s]), browse.source);
  fillSelect($("#bCat"), CATS.filter(([id]) => so.collections[id]), browse.cat);
  fillSelect($("#bCol"), cols.map((c) => [c.id, c.label]), browse.col);
  fillSelect($("#bSort"), so.sorts.map((s) => [s.id, s.label]), browse.sort);
  fillSelect($("#bDecade"), so.decades.map((d) => [d.id, d.label]), browse.decade);
  fillSelect($("#bLang"), langs.map((l) => [l.id, l.label]), browse.lang);
  fillSelect($("#bGenre"), genres.length ? genres.map((g) => [g.id, g.label]) : [["any", "Books only"]], browse.genre);

  const catName = browse.cat === "all" ? "everything" : catWord(browse.cat);
  $("#browseTitle").textContent = `Browse ${catName}`;
  const colLabel = cols.find((c) => c.id === browse.col)?.label;
  const langLabel = browse.lang !== "any" ? LANG_SHORT[browse.lang] : null;
  const genreLabel = browse.genre !== "any" ? GENRES.find(([g]) => g === browse.genre)?.[1] : null;
  $("#browseSub").textContent = [langLabel, genreLabel, colLabel, browse.source,
    browse.source === "Internet Archive" ? "public-domain & openly licensed" : "latest releases from your indexers"].filter(Boolean).join(" · ");
  document.title = `Browse ${catName} · BuzzTorrent`;
  $("#browseCount").textContent = "";
  grid.replaceChildren(...Array.from({ length: 15 }, () => pickSkeleton(browse.cat)));

  const seq = ++browse.seq;
  const q = new URLSearchParams({ source: browse.source, cat: browse.cat, col: browse.col, sort: browse.sort,
    decade: browse.decade, lang: browse.lang, genre: browse.genre, page: browse.page, family: familyParam() });
  try {
    const d = await api("/api/browse?" + q);
    if (seq !== browse.seq) return;
    browse.data = d;
    browse.page = d.page;
    renderPicks();
  } catch (err) {
    if (seq !== browse.seq) return;
    grid.replaceChildren();
    $("#bPager").replaceChildren();
    $("#browseError").replaceChildren(h("div", { class: "notice warn" }, icon("alert"), h("span", { text: err.message })));
  }
}

function renderPicks() {
  const d = browse.data;
  const grid = $("#bGrid");
  if (!d.results.length) {
    grid.replaceChildren();
    $("#browseError").replaceChildren(h("div", { class: "notice info" }, icon("info"),
      h("span", { text: "Nothing here. Try another collection, decade or sort order." })));
  } else {
    grid.replaceChildren(...d.results.map((r) => pickCard(r, browse.cat)));
  }
  const first = (d.page - 1) * d.per_page + 1;
  $("#browseCount").textContent = d.results.length
    ? `Showing ${fmtNum(first)}–${fmtNum(first + d.results.length - 1)} of ${fmtNum(d.total)}` : "";
  renderPager(d.page, d.pages);
}

function renderPager(page, pages) {
  const nav = $("#bPager");
  if (pages <= 1) { nav.replaceChildren(); return; }
  const go = (p) => { showBrowse({ page: p }); window.scrollTo({ top: 0, behavior: "smooth" }); };
  const num = (p) => h("button", { type: "button", "aria-current": p === page ? "page" : null, "aria-label": `Page ${p}`,
    onclick: () => p !== page && go(p) }, String(p));
  const wanted = new Set([1, pages, page - 2, page - 1, page, page + 1, page + 2].filter((p) => p >= 1 && p <= pages));
  const items = [];
  let last = 0;
  for (const p of [...wanted].sort((a, b) => a - b)) {
    if (p - last > 1) items.push(h("span", { class: "gap", text: "…" }));
    items.push(num(p));
    last = p;
  }
  nav.replaceChildren(
    h("button", { type: "button", class: "prev", disabled: page <= 1, onclick: () => go(page - 1) }, icon("chev"), "Prev"),
    ...items,
    h("button", { type: "button", class: "next", disabled: page >= pages, onclick: () => go(page + 1) }, "Next", icon("chev")));
}

/* detail popup */
const detail = { id: null, opener: null };
const smartParam = () => (store.get("smart", true) ? "1" : "0");
const POSTER_TALL = new Set(["movies", "tv", "books", "other"]);

function setPoster(src, cat) {
  const img = $("#detailImg");
  const ph = $("#detailPh");
  $(".detail-poster").classList.toggle("wide", !POSTER_TALL.has(cat));
  ph.replaceChildren(catIcon(cat));
  if (!src || !/^\/api\/(thumb|image)\?/.test(src)) { img.hidden = true; img.removeAttribute("src"); ph.hidden = false; return; }
  if (img.getAttribute("src") === src) return;
  img.hidden = true;
  ph.hidden = false;
  img.onload = () => { img.hidden = false; ph.hidden = true; };
  img.onerror = () => { img.hidden = true; ph.hidden = false; };
  img.src = src;
}

function renderDetailInfo(r, d) {
  const [bIcon, bLabel] = BADGE[r.verdict] || BADGE.unknown;
  $("#detailBadge").className = "badge-inline " + r.verdict;
  $("#detailBadge").replaceChildren(icon(bIcon), `${bLabel} · ${r.score}/100`);
  const year = d?.year || r.year;
  $("#detailTitle").textContent = (d?.title || r.title) + (year && !(d?.title || r.title).includes(String(year)) ? ` (${year})` : "");
  $("#detailTagline").textContent = d?.tagline || "";
  const meta = $("#detailMeta");
  meta.replaceChildren(h("span", {}, catIcon(r.category), CAT_LABEL[r.category] || "Other"));
  if (d?.runtime) meta.append(h("span", {}, icon("clock"), d.runtime));
  if (d?.language) meta.append(h("span", {}, icon("globe"), d.language));
  if (d?.country) meta.append(h("span", { text: d.country }));
  if (r.size) meta.append(h("span", {}, icon("drive"), fmtSize(r.size)));
  if (r.downloads) meta.append(h("span", {}, icon("down"), `${fmtCompact(r.downloads)} downloads`));
  if (r.seeders != null) meta.append(h("span", { class: seedClass(r) }, icon("up"), `${fmtNum(r.seeders)} seeders`));
  meta.append(h("span", { class: "src" }, icon("globe"), r.sources.join(", ")));
  $("#detailGenres").replaceChildren(...(d?.genres || []).map((g) => h("span", { text: g })));
  const facts = $("#detailFacts");
  facts.replaceChildren(...(d?.facts || []).flatMap((f) => [h("dt", { text: f.label }), h("dd", { text: f.values.join(", ") })]));
  const shown = r.flags.filter((f) => f.level !== "info" || /hash|curated/i.test(f.msg));
  $("#detailFlags").replaceChildren(...shown.map((f) => {
    const curated = /curated/i.test(f.msg);
    return h("span", { class: `pill pill-${f.level === "info" ? (curated ? "safe" : "info") : f.level}`, title: f.msg },
      icon(f.level === "danger" ? "danger" : f.level === "warn" ? "alert" : curated ? "check" : "info"),
      curated ? "Curated source" : f.msg);
  }));
  bindActions($("#detail"), r);
}

function paragraphs(text) {
  return text.split(/\n{2,}|\n(?=[A-Z])/).map((p) => p.trim()).filter(Boolean).map((p) => h("p", { text: p, dir: "auto" }));
}

/* highlights: short bullet points above the summary */
const ABBREV = /^(mr|mrs|ms|dr|st|jr|sr|vs|no|vol|ft|mt|co|inc|ltd|u\.s|u\.k|[a-z])$/i;

function firstSentence(text, max = 180) {
  const t = (text || "").replace(/\s+/g, " ").trim();
  const re = /[.!?](?=\s+["'(]?[A-Z0-9])/g;
  let m;
  while ((m = re.exec(t))) {
    const word = t.slice(0, m.index).split(" ").pop();
    if (!ABBREV.test(word.replace(/^\W+/, ""))) { // "George A. Romero" is not a sentence end
      return shorten(t.slice(0, m.index + 1), max);
    }
  }
  return shorten(t, max);
}

function shorten(s, max) {
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const comma = cut.lastIndexOf(", ");                 // end on a clause, not mid-name
  return (comma > max * 0.55 ? cut.slice(0, comma) : cut.replace(/\s+\S*$/, "")) + "…";
}

function highlightPoints(d) {
  const pts = [];
  const cast = d.cast || [];
  if (cast.length) pts.push({ icon: "star", label: "Starring", names: cast.slice(0, 4), more: cast.length > 4 ? `+${cast.length - 4} more` : null });
  for (const f of (d.facts || []).slice(0, 2)) pts.push({ icon: "user", label: f.label, names: f.values.slice(0, 3) });
  const when = [d.year, d.runtime, d.language].filter(Boolean).join(" · ");
  if (when) pts.push({ icon: "calendar", label: d.runtime ? "Year · runtime" : "Year", text: when });
  if (d.genres?.length) pts.push({ icon: "tag", label: "Genre", text: d.genres.slice(0, 3).join(", ") });
  const up = d.upload;
  if (!d.matched && up) {                                  // no Wikipedia match: use what the uploader gave
    if (up.creator) pts.push({ icon: "user", label: "By", names: [up.creator] });
    if (up.runtime && !d.runtime) pts.push({ icon: "clock", label: "Runtime", text: up.runtime });
    if (up.subjects?.length && !d.genres?.length) pts.push({ icon: "tag", label: "Tags", text: up.subjects.slice(0, 4).join(", ") });
  }
  const story = d.summary || up?.description;
  if (story) pts.push({ icon: "info", label: "In short", text: firstSentence(story) });
  return pts;
}

function renderHighlights(d) {
  const ul = $("#detailHighlights");
  const pts = d ? highlightPoints(d) : [];
  ul.hidden = !pts.length;
  ul.replaceChildren(...pts.map((p) => h("li", {},
    icon(p.icon),
    h("span", { class: "hl-label", text: p.label }),
    h("span", { class: "hl-value" },
      ...(p.names ? p.names.flatMap((n, i) => [i ? ", " : "", h("b", { class: "hl-name", text: n })]) : [p.text]),
      p.more ? h("span", { class: "hl-more", text: " " + p.more }) : null))));
}

/** Paragraph elements with the given names wrapped in <mark>. Built from text nodes only (no innerHTML). */
function markedParagraphs(text, names) {
  const list = [...new Set((names || []).filter((n) => n && n.length > 3))].sort((a, b) => b.length - a.length);
  if (!list.length) return paragraphs(text);
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`(?<![\\w])(${list.map(esc).join("|")})(?![\\w])`, "g");
  return paragraphs(text).map((p) => {
    const src = p.textContent;
    const out = h("p", { dir: "auto" });
    let last = 0;
    for (const m of src.matchAll(re)) {
      out.append(src.slice(last, m.index), h("mark", { class: "hl", text: m[0] }));
      last = m.index + m[0].length;
    }
    out.append(src.slice(last));
    return out;
  });
}

function renderDetailBody(r, d) {
  const summary = $("#detailSummary");
  const credit = $("#detailCredit");
  const upload = d?.upload;
  renderHighlights(d);
  if (d === undefined) {                      // still loading
    summary.replaceChildren(...["w90", "w80", "w60"].map((w) => h("div", { class: `sk sk-block ${w}` })));
    credit.textContent = "";
    $("#detailCastBox").hidden = $("#detailUploadBox").hidden = true;
    return;
  }
  const links = (d?.links || []).map((l) => {
    const u = safeHttpUrl(l.url);
    return u && h("a", { href: u.href, target: "_blank", rel: "noopener noreferrer", text: l.label });
  }).filter(Boolean);
  const people = [...(d?.cast || []), ...(d?.facts || []).flatMap((f) => f.values)];
  if (d?.summary) {
    summary.replaceChildren(...markedParagraphs(d.summary, people));
    credit.replaceChildren("Summary from Wikipedia (CC BY-SA)", ...links.flatMap((a) => [" · ", a]));
  } else if (upload?.description) {
    summary.replaceChildren(...markedParagraphs(upload.description, people));
    credit.replaceChildren("Description from the uploader", ...links.flatMap((a) => [" · ", a]));
  } else {
    summary.replaceChildren(h("p", { class: "muted", text: d ? "No description found for this title." : "Details couldn't be loaded." }));
    credit.replaceChildren(...links.flatMap((a, i) => (i ? [" · ", a] : [a])));
  }
  const cast = d?.cast || [];
  $("#detailCastBox").hidden = !cast.length;
  $("#detailCast").replaceChildren(...cast.map((name) => h("span", {},
    h("i", { text: name.split(/\s+/).map((w) => w[0]).filter(Boolean).slice(0, 2).join("").toUpperCase() }), name)));

  const showUpload = upload && (d?.summary ? upload.description : true) && (upload.description || upload.subjects?.length || upload.creator);
  $("#detailUploadBox").hidden = !showUpload;
  if (showUpload) {
    const box = $("#detailUpload");
    const parts = [];
    if (d?.summary && upload.description) {
      const text = h("div", { class: "detail-upload" + (upload.description.length > 420 ? " clamped" : ""), text: upload.description });
      parts.push(text);
      if (upload.description.length > 420) {
        const more = h("button", { type: "button", class: "link small", text: "Show more" });
        more.onclick = () => { const c = text.classList.toggle("clamped"); more.textContent = c ? "Show more" : "Show less"; };
        parts.push(more);
      }
    }
    const chips = [];
    if (upload.creator) chips.push(h("span", { text: `By ${upload.creator}` }));
    if (upload.runtime) chips.push(h("span", { text: `Runtime ${upload.runtime}` }));
    for (const s of (upload.subjects || []).slice(0, 8)) chips.push(h("span", { text: s }));
    const lic = upload.license && safeHttpUrl(upload.license);
    if (lic) chips.push(h("span", {}, h("a", { href: lic.href, target: "_blank", rel: "noopener noreferrer", text: "License" })));
    if (chips.length) parts.push(h("div", { class: "detail-upload-meta" }, chips));
    box.replaceChildren(...parts);
  }
}

/* ------------------------------------------------- play + watchlist (PC) */
const isFree = (r) => (r.sources || []).includes("Internet Archive");
function wirePlayAndWatch(r) {
  const play = $("#detail .act-play");
  play.hidden = !isFree(r) || r.category === "adult";
  play.onclick = async () => {
    try {
      const d = await api(`/api/tv/play?id=${encodeURIComponent(r.id)}`);
      if (!d.files.length) { toast(d.note || "Nothing playable in this upload", "alert"); return; }
      closeDetail();
      openPlayer({ title: $("#detailTitle").textContent || r.title, files: d.files, index: 0, progress: d.progress, thumb: r.thumb || "" });
    } catch (err) { toast(err.message, "alert"); }
  };
  const watch = $("#detail .act-watch");
  const cat = ["movies", "tv", "music"].includes(r.category) ? r.category : "movies";
  watch.hidden = r.category === "adult";
  let entry = null;
  const sync = () => { watch.textContent = entry ? "★ On watchlist" : "☆ Watchlist"; };
  sync();
  if (r.category !== "adult") {
    api("/api/tv/watchlist").then((d) => {
      const t = r.title.toLowerCase();
      entry = d.items.find((w) => w.category === cat && (w.title.toLowerCase() === t)) || null;
      sync();
    }).catch(() => {});
  }
  watch.onclick = async () => {
    try {
      if (entry) {
        await api("/api/tv/watch", { action: "remove", id: entry.id });
        entry = null;
        toast("Removed from your watchlist");
      } else {
        const d = await api("/api/tv/watch", { title: $("#detailTitle").textContent || r.title, category: cat, year: r.year,
          thumb: r.thumb || "", quality: r.quality?.height || 0, free: isFree(r) });
        entry = d.item;
        toast("Added. You'll be told when a better or free copy turns up");
      }
      sync();
    } catch (err) { toast(err.message, "alert"); }
  };
}

/* ------------------------------------------------------- PC home rows */
async function loadPersonal() {
  const root = $("#personal");
  const sections = [];
  const shelf = (title, iconName, items) => h("section", { class: "shelf" },
    h("div", { class: "shelf-head" }, h("h2", {}, icon(iconName), title)),
    h("ul", { class: "pick-grid" }, items));
  try {
    const c = await api("/api/tv/continue");
    if (c.items.length) sections.push(shelf("Continue watching", "clock", c.items.slice(0, 10).map(continueCardPc)));
  } catch { /* offline */ }
  try {
    const w = await api("/api/tv/watchlist");
    for (const a of (w.alerts || []).slice(0, 3)) toast(`🔔 ${a.text}`);
    if ((w.alerts || []).length) api("/api/tv/alerts/seen", {}).catch(() => {});
    if (w.items.length) sections.push(shelf("Your watchlist", "star", w.items.slice(0, 10).map(watchCardPc)));
  } catch { /* offline */ }
  root.replaceChildren(...sections);
  if (root.dataset.classics) return;
  root.dataset.classics = "1";
  try {
    const s = await api("/api/tv/shelves?cat=all");
    const box = h("div");
    root.after(box);
    for (const sh of s.shelves) {
      const grid = h("ul", { class: "pick-grid" }, Array.from({ length: 5 }, () => pickSkeleton("movies")));
      box.append(h("section", { class: "shelf" }, h("div", { class: "shelf-head" }, h("h2", {}, icon("movies"), sh.label)), grid));
      api(`/api/tv/shelf?${new URLSearchParams({ source: sh.source, id: sh.id })}`)
        .then((d) => grid.replaceChildren(...d.results.slice(0, 5).map((r) => pickCard(r, r.category))))
        .catch(() => grid.replaceChildren(h("li", { class: "shelf-error", text: "Couldn't load this row right now." })));
    }
  } catch { root.dataset.classics = ""; }
}
function thumbOrPh(src, title) {
  const ph = h("span", { class: "pick-ph", text: (title || "").slice(0, 40) });
  if (!src || !src.startsWith("/api/")) return ph;
  const img = h("img", { src, alt: "", loading: "lazy" });
  img.addEventListener("error", () => img.replaceWith(ph), { once: true });
  return img;
}
function continueCardPc(c) {
  const pct = c.dur ? Math.min(100, Math.round((c.pos / c.dur) * 100)) : 0;
  const fill = h("span", { class: "dl-fill" });
  fill.style.width = `${pct}%`;
  return h("li", { class: "pick", tabindex: "0", onclick: () => resumePc(c), onkeydown: (e) => { if (e.key === "Enter") resumePc(c); } },
    h("div", { class: "pick-thumb" }, thumbOrPh(c.thumb, c.title)),
    h("div", { class: "pick-body" }, h("h3", { class: "pick-title", dir: "auto", text: c.name ? `${c.title} · ${c.name}` : c.title }),
      h("div", { class: "dl-bar" }, fill),
      h("div", { class: "pick-meta", text: `${fmtClock(c.pos)} of ${fmtClock(c.dur)}` })));
}
function watchCardPc(w) {
  return h("li", { class: "pick", tabindex: "0", onclick: () => search(w.title, w.category),
    onkeydown: (e) => { if (e.key === "Enter") search(w.title, w.category); } },
  h("div", { class: "pick-thumb" }, thumbOrPh(w.thumb, w.title), w.new ? h("span", { class: "pick-badge new-badge", text: "NEW" }) : null),
  h("div", { class: "pick-body" }, h("h3", { class: "pick-title", dir: "auto", text: w.title }),
    h("div", { class: "pick-meta", text: w.new ? "Better copy found" : w.free ? "Free copy available" : "On your watchlist" })));
}
function fmtClock(s) {
  s = Math.max(0, Math.floor(s || 0));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = String(s % 60).padStart(2, "0");
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}
async function resumePc(c) {
  try {
    if (c.key.startsWith("lib:")) {
      const d = await api("/api/tv/library");
      const it = d.items.find((x) => x.key === c.key);
      if (!it) { toast("That video is no longer in the download folder", "alert"); return; }
      return playLibraryPc(it, d.progress);
    }
    const d = await api(`/api/tv/play?ident=${encodeURIComponent(c.key.split(":")[1])}`);
    const i = Math.max(0, d.files.findIndex((f) => f.key === c.key));
    openPlayer({ title: c.title, files: d.files, index: i, progress: d.progress, thumb: c.thumb || "" });
  } catch (err) { toast(err.message, "alert"); }
}

/* ------------------------------------------------------------ My PC */
async function showLibrary(push = true) {
  if (push) history.pushState(null, "", "/?view=library");
  document.title = "My PC · BuzzTorrent";
  setMode("library");
  const grid = $("#libGrid");
  grid.replaceChildren(h("li", { class: "shelf-error" }, h("span", { class: "spinner" }), " Looking at your downloads…"));
  let d;
  try { d = await api("/api/tv/library"); } catch (err) { grid.replaceChildren(h("li", { class: "shelf-error", text: err.message })); return; }
  $("#libBusy").replaceChildren(...(d.downloading || []).map((x) => h("p", { class: "muted small" },
    `⬇ Downloading: ${x.title} · ${x.total ? Math.floor((x.done / x.total) * 100) : 0}% (appears here once finished and scanned)`)));
  if (d.state === "scanner_off") {
    grid.replaceChildren(h("li", { class: "shelf-error", text: "Turn on the Download scanner (shield button) and choose your downloads folder." }));
    return;
  }
  $("#libSummary").textContent = d.items.length ? `${d.items.length} video${d.items.length > 1 ? "s" : ""}` : "";
  if (!d.items.length) {
    grid.replaceChildren(h("li", { class: "shelf-error", text: "Nothing yet. Press ⬇ Download on any result: once it finishes and passes the virus scan, it shows up here." }));
    return;
  }
  grid.replaceChildren(...d.items.map((it) => h("li", { class: "pick lib-item" },
    h("div", { class: "pick-thumb" }, h("span", { class: "pick-ph", text: it.title.slice(0, 40) }),
      h("span", { class: "pick-badge" }, h("b", { text: "✓ Scanned" }))),
    h("div", { class: "pick-body" },
      h("h3", { class: "pick-title", dir: "auto", text: it.title }),
      h("div", { class: "pick-meta", text: [it.year, it.label, fmtSize(it.size), it.ext.toUpperCase(), it.subs.length ? "CC" : "",
        it.resume ? `resume ${fmtClock(it.resume)}` : ""].filter(Boolean).join(" · ") })),
    h("div", { class: "pick-actions" },
      h("button", { type: "button", class: "btn btn-primary btn-sm", text: "▶ Play", onclick: () => playLibraryPc(it, d.progress) }),
      h("button", { type: "button", class: "btn btn-ghost btn-sm", text: "Open in video player", title: "Plays in VLC or your usual video app",
        onclick: () => api("/api/library/open", { id: it.id }).catch((err) => toast(err.message, "alert")) })))));
}
function playLibraryPc(it, progress) {
  const versions = (it.versions || []).map((v) => ({ url: v.stream, label: v.label, format: v.ext.toUpperCase(), size: v.size, id: v.id }));
  openPlayer({ title: it.title, progress, thumb: "", lib: it.id,
    files: [{ name: it.title, key: it.key, resume: it.resume || 0, seconds: 0, subs: it.subs, versions }], index: 0 });
}

/* --------------------------------------------------------- PC player */
const pcp = { files: [], i: 0, v: 0, title: "", progress: "", thumb: "", saved: 0, cues: null, lib: null, vlc: false };
const pcVideo = () => $("#pcpVideo");
const BROWSER_OK = { mp4: "video/mp4", m4v: "video/mp4", webm: "video/webm", ogv: "video/ogg", mkv: "video/webm" };
function browserCanPlay(v) {
  const path = (v.url || "").split("?")[0];
  const urlExt = /\.([a-z0-9]{2,4})$/i.test(path) ? path.split(".").pop() : "";      // library links have none
  const ext = (urlExt || v.format || "").toLowerCase();
  const type = BROWSER_OK[ext] || (/^(mp4|h\.264|mpeg4|matroska)/i.test(v.format || "") ? (/^matroska/i.test(v.format) ? "video/webm" : "video/mp4") : "");
  return !!type && pcVideo().canPlayType(type) !== "";
}
async function openPlayer(o) {
  Object.assign(pcp, { files: o.files, i: o.index || 0, title: o.title, progress: o.progress || "", thumb: o.thumb || "", lib: o.lib || null });
  try { pcp.vlc = (await api("/api/player/info")).vlc; } catch { pcp.vlc = false; }
  $("#pcPlayer").hidden = false;
  playFilePc(pcp.i, pcp.files[pcp.i].resume || 0);
  const eps = $("#pcpEps");
  eps.hidden = pcp.files.length < 2;
  eps.replaceChildren(...pcp.files.map((f, i) => h("button", { type: "button", class: "btn btn-ghost btn-sm", text: f.name,
    onclick: () => { savePcProgress(); playFilePc(i, f.resume || 0); } })));
}
function playFilePc(i, resumeFrom) {
  pcp.i = i;
  const f = pcp.files[i];
  const order = f.versions.map((v, n) => n);
  pcp.v = order.find((n) => browserCanPlay(f.versions[n])) ?? 0;   // sharpest copy this browser can play
  $("#pcpTitle").textContent = pcp.files.length > 1 ? `${pcp.title} · ${f.name}` : pcp.title;
  fillSelect($("#pcpQuality"), f.versions.map((v, n) => [String(n), `${v.label || v.format}${browserCanPlay(v) ? "" : " (player app)"}`]), String(pcp.v));
  fillSelect($("#pcpSubs"), [["off", "Off"], ...(f.subs || []).map((s, n) => [String(n), s.label])], "off");
  $("#pcpSubs").parentElement.hidden = !(f.subs || []).length;
  $("#pcpExternal").hidden = !(pcp.lib || pcp.vlc);
  pcp.cues = null;
  $("#pcpSub").textContent = "";
  startVersion(resumeFrom);
  const restart = $("#pcpRestart");
  restart.hidden = !(resumeFrom > 0);
  if (resumeFrom > 0) {
    toast(`Resumed from ${fmtClock(resumeFrom)}`);
    setTimeout(() => { restart.hidden = true; }, 15000);
  }
}
function startVersion(at) {
  const v = pcp.files[pcp.i].versions[pcp.v];
  const video = pcVideo();
  $("#pcpMsg").hidden = true;
  video.src = v.url;
  if (at > 0) video.addEventListener("loadedmetadata", () => { video.currentTime = at; }, { once: true });
  video.play().catch(() => {});
}
function pcpFail() {
  const f = pcp.files[pcp.i];
  const at = pcVideo().currentTime || 0;
  const next = f.versions.findIndex((v, n) => n > pcp.v && browserCanPlay(v));
  if (next >= 0) {
    toast(`${f.versions[pcp.v].label || "This copy"} won't play here. Trying ${f.versions[next].label || "another copy"}…`);
    pcp.v = next;
    $("#pcpQuality").value = String(next);
    startVersion(at);
    return;
  }
  const m = $("#pcpMsg");
  m.textContent = (pcp.lib || pcp.vlc) ? "This format won't play in the app window. Use “Open in video player”." :
    "This format won't play in the app window. Install VLC, or download it and watch on the TV.";
  m.hidden = false;
}
function savePcProgress() {
  const f = pcp.files[pcp.i];
  const video = pcVideo();
  if (!f || !f.key || !video.duration) return;
  api("/api/tv/progress", { key: f.key, pos: video.currentTime, dur: video.duration, title: pcp.title,
    name: pcp.files.length > 1 ? f.name : "", thumb: pcp.thumb, quality: f.versions[pcp.v]?.label || "" }).catch(() => {});
}
function closePlayerPc() {
  savePcProgress();
  const video = pcVideo();
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("#pcPlayer").hidden = true;
  if (!$("#home").hidden) loadPersonal();
}
function wirePlayer() {
  const video = pcVideo();
  video.addEventListener("error", () => { if (!$("#pcPlayer").hidden && video.getAttribute("src")) pcpFail(); });
  video.addEventListener("pause", savePcProgress);
  video.addEventListener("timeupdate", () => {
    if (Date.now() - pcp.saved > 15000) { pcp.saved = Date.now(); savePcProgress(); }
    const cues = pcp.cues;
    if (!cues) return;
    const t = video.currentTime;
    const cue = cues.find((c) => c.start <= t && t <= c.end);
    const text = cue ? cue.text : "";
    if ($("#pcpSub").textContent !== text) $("#pcpSub").textContent = text;
  });
  video.addEventListener("ended", () => {
    savePcProgress();
    if (pcp.i + 1 < pcp.files.length) playFilePc(pcp.i + 1, 0);
  });
  $("#pcpQuality").addEventListener("change", (e) => {
    const at = video.currentTime || 0;
    pcp.v = Number(e.target.value);
    const v = pcp.files[pcp.i].versions[pcp.v];
    if (!browserCanPlay(v)) { $("#pcpExternal").click(); return; }
    startVersion(at);
  });
  $("#pcpSubs").addEventListener("change", async (e) => {
    pcp.cues = null;
    $("#pcpSub").textContent = "";
    if (e.target.value === "off") return;
    try { pcp.cues = (await api(pcp.files[pcp.i].subs[Number(e.target.value)].url)).cues; }
    catch { toast("Couldn't load the subtitles", "alert"); }
  });
  $("#pcpRestart").addEventListener("click", () => { video.currentTime = 0; $("#pcpRestart").hidden = true; });
  $("#pcpExternal").addEventListener("click", async () => {
    const v = pcp.files[pcp.i].versions[pcp.v];
    try {
      if (pcp.lib) await api("/api/library/open", { id: v.id || pcp.lib });
      else await api("/api/play/external", { url: v.url });
      video.pause();
      toast("Opening in your video player");
    } catch (err) { toast(err.message, "alert"); }
  });
  $("#pcpClose").addEventListener("click", closePlayerPc);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#pcPlayer").hidden) closePlayerPc(); });
}

/* ------------------------------------------------------- voice (PC) */
const SPEECH_LANG = { hi: "hi-IN", bn: "bn-IN", ur: "ur-IN", ta: "ta-IN", te: "te-IN", mr: "mr-IN", pa: "pa-IN", bho: "hi-IN" };
function cleanSpoken(q) {
  const t = String(q || "").trim().replace(/[.!?।]+$/u, "");
  const cmd = /^(please\s+)?(search(\s+for)?|find|play|show(\s+me)?|watch|look\s+for|open)\s+/i;
  if (!cmd.test(t)) return t;
  const rest = t.replace(cmd, "");
  return rest.replace(/\s+(full\s+)?(movie|film|please)$/i, "") || rest;
}
function wireVoice() {
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Rec) return;
  const listen = (btn, done) => {
    const rec = new Rec();
    rec.lang = SPEECH_LANG[$("#heroLang").value || state.lang] || "en-IN";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    btn.classList.add("listening");
    rec.onresult = (e) => done(cleanSpoken(e.results[0][0].transcript));
    rec.onerror = (e) => toast(e.error === "not-allowed" ? "Allow the microphone to use voice search" : "Didn't catch that, try again", "alert");
    rec.onend = () => btn.classList.remove("listening");
    rec.start();
  };
  for (const [id, input, run] of [["#micTop", "#qTop", (q) => search(q, state.cat)],
    ["#micHero", "#qHero", (q) => search(q, $("#heroCat").value, true, { lang: $("#heroLang").value })]]) {
    const btn = $(id);
    btn.hidden = false;
    btn.addEventListener("click", () => listen(btn, (q) => { if (q) { $(input).value = q; run(q); } }));
  }
}

/* "Where to watch": a search link per streaming service (ZEE5, JioHotstar, SonyLIV, …) */
async function renderWatch(r, d) {
  const box = $("#detailWatchBox");
  if (!["movies", "tv", "all", "other"].includes(r.category)) { box.hidden = true; return; }
  const title = d?.title || r.title.replace(/[._]+/g, " ");
  const id = r.id;
  try {
    const { services } = await api(`/api/tv/services?title=${encodeURIComponent(title)}`);
    if (detail.id !== id) return;
    $("#detailWatch").replaceChildren(...services.map((sv) => {
      const u = safeHttpUrl(sv.web);
      return u && h("a", { href: u.href, target: "_blank", rel: "noopener noreferrer", title: sv.free || "Subscription" },
        sv.name, sv.free ? h("span", { class: "watch-free", text: " · Free" }) : null);
    }).filter(Boolean));
    box.hidden = false;
  } catch { box.hidden = true; }
}

async function openDetail(r) {
  const wrap = $("#detail");
  wirePlayAndWatch(r);
  detail.id = r.id;
  detail.opener = document.activeElement;
  wrap.hidden = false;
  $(".detail").scrollTop = 0;
  setPoster(r.thumb, r.category);
  renderDetailInfo(r, null);
  renderDetailBody(r, undefined);
  renderWatch(r, null);
  $("#detailClose").focus();

  const id = r.id;
  api(`/api/details?id=${encodeURIComponent(id)}&smart=${smartParam()}`)
    .then((d) => {
      if (detail.id !== id || wrap.hidden) return;
      r.details = d;
      if (d.poster) setPoster(d.poster, r.category);
      renderDetailInfo(r, d);
      renderDetailBody(r, d);
      renderWatch(r, d);
    })
    .catch(() => { if (detail.id === id) renderDetailBody(r, null); });

  const panel = $("#detailScan");
  const cached = state.scans.get(id);
  if (cached?.data) { renderScanPanel(panel, cached.data); return; }
  panel.hidden = false;
  panel.replaceChildren(h("div", { class: "scan-summary neutral" }, h("span", { class: "spinner" }),
    "Reading the file list (nothing is downloaded)…"));
  try {
    const data = await api(`/api/inspect?id=${encodeURIComponent(id)}`);
    Object.assign(r, data.result);
    state.scans.set(id, { open: false, data });
    if (detail.id !== id || wrap.hidden) return;
    renderDetailInfo(r, r.details || null);
    renderScanPanel(panel, data);
    refreshPickCards(r);
  } catch (err) {
    if (detail.id === id) panel.replaceChildren(h("div", { class: "scan-summary neutral" }, icon("alert"), `Scan failed: ${err.message}`));
  }
}

function refreshPickCards(r) {
  document.querySelectorAll(`.pick[data-id="${CSS.escape(r.id)}"]`).forEach((old) => {
    const aspect = old.querySelector(".pick-thumb.tall") ? "books" : old.querySelector(".pick-thumb.square") ? "music" : null;
    old.replaceWith(pickCard(r, aspect));
  });
}

function closeDetail() {
  const wrap = $("#detail");
  if (wrap.hidden) return;
  wrap.hidden = true;
  detail.id = null;
  detail.opener?.focus?.();
}

/* ------------------------------------------------------ search suggestions */
function attachSuggest(input, box, getCat, onPick) {
  let timer = null, seq = 0, items = [], active = -1;
  const hide = () => { box.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; };
  const select = (i) => {
    active = i;
    box.querySelectorAll(".suggest-item").forEach((el, j) => el.setAttribute("aria-selected", String(j === i)));
  };
  const pick = (title) => { input.value = title; hide(); onPick(title); };
  const render = (data) => {
    items = data.suggestions || [];
    const kids = [];
    if (data.suggestion && !items.some((s) => s.title.toLowerCase() === data.suggestion)) {
      kids.push(h("div", { class: "s-fix" }, icon("info"), "Did you mean",
        h("button", { type: "button", class: "link", text: data.suggestion, onmousedown: (e) => { e.preventDefault(); pick(data.suggestion); } }), "?"));
    }
    if (items.length) kids.push(h("div", { class: "s-head", text: "Suggestions" }));
    items.forEach((s, i) => {
      const thumb = h("span", { class: "s-thumb" });
      if (s.thumb && /^\/api\/(thumb|image)\?/.test(s.thumb)) {
        const img = h("img", { alt: "", loading: "lazy" });
        img.addEventListener("error", () => img.replaceWith(icon("other")));
        img.src = s.thumb;
        thumb.append(img);
      } else thumb.append(icon("search"));
      kids.push(h("button", {
        type: "button", class: "suggest-item", role: "option", "aria-selected": "false", tabindex: "-1",
        onmousedown: (e) => { e.preventDefault(); pick(s.title); }, onmouseenter: () => select(i),
      }, thumb, h("span", { style: null }, h("div", { class: "s-title", text: s.title, dir: "auto" }), s.description && h("div", { class: "s-desc", text: s.description })),
      h("span", { class: "s-year", text: s.year ? String(s.year) : "" })));
    });
    box.replaceChildren(...kids);
    const show = kids.length > 0 && document.activeElement === input;
    box.hidden = !show;
    input.setAttribute("aria-expanded", String(show));
    active = -1;
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 3 || smartParam() === "0") { hide(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const data = await api(`/api/suggest?q=${encodeURIComponent(q)}&cat=${getCat()}&smart=1`);
        if (mine === seq && input.value.trim() === q) render(data);
      } catch { /* suggestions are optional */ }
    }, 280);
  });
  input.addEventListener("keydown", (e) => {
    if (box.hidden) return;
    const n = box.querySelectorAll(".suggest-item").length;
    if (e.key === "ArrowDown" && n) { e.preventDefault(); select((active + 1) % n); }
    else if (e.key === "ArrowUp" && n) { e.preventDefault(); select((active - 1 + n) % n); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); pick(items[active].title); }
    else if (e.key === "Escape") { e.stopPropagation(); hide(); }
  });
  input.addEventListener("blur", () => setTimeout(hide, 120));
  input.form?.addEventListener("submit", () => { seq++; clearTimeout(timer); hide(); });
}

/* ------------------------------------------------------------ query notes */
function renderQueryNote(data) {
  const box = $("#queryNote");
  const parts = [];
  const quote = (t) => h("b", { text: `“${t}”` });
  if (data.corrected) {
    parts.push(icon("info"), h("span", {}, "No matches for ", quote(data.query), ". Showing results for ", quote(data.corrected), "."));
  } else if (data.also_searched) {
    parts.push(icon("info"), h("span", {}, "Also including results for ", quote(data.also_searched), "."));
  } else if (data.close_matches_only) {
    parts.push(icon("info"), h("span", {}, "No exact title match for ", quote(data.query), ". Showing the closest titles."));
  }
  const dym = (data.did_you_mean || []).filter((w) => !data.corrected || w.title.toLowerCase() !== data.corrected.toLowerCase());
  if (dym.length) {
    parts.push(h("span", { class: "dym" }, "Did you mean:", ...dym.map((w) => h("button", {
      type: "button", class: "chip-btn", title: w.description || "", onclick: () => search(w.title, state.cat),
    }, w.title, w.year ? h("span", { text: ` ${w.year}` }) : null))));
  }
  box.replaceChildren(...(parts.length ? [h("div", { class: "query-note" }, parts)] : []));
}

/* ---------------------------------------------------------------- boot */
function readUrl() {
  const p = new URLSearchParams(location.search);
  if (p.get("view") === "scanner") return showScanner(false);
  if (p.get("view") === "library") return showLibrary(false);
  if (p.get("view") === "browse") {
    const page = parseInt(p.get("page") || "1", 10);
    return showBrowse({ cat: CATS.some(([id]) => id === p.get("cat")) ? p.get("cat") : "movies", col: p.get("col") || "",
      sort: p.get("sort") || "popular", decade: p.get("decade") || "any", source: p.get("source") || "",
      lang: p.get("lang") || "any", genre: p.get("genre") || "any",
      page: page > 0 ? page : 1 }, false);
  }
  const q = (p.get("q") || "").trim();
  const cat = CATS.some(([id]) => id === p.get("cat")) ? p.get("cat") : "all";
  if (q) search(q, cat, false, { lang: LANGS.some(([l]) => l === p.get("lang")) ? p.get("lang") : "any",
    genre: GENRES.some(([g]) => g === p.get("genre")) ? p.get("genre") : "any" });
  else goHome(false);
}

async function loadFooter() {
  try {
    const d = await api("/api/sources");
    const foot = $("#homeFoot");
    foot.replaceChildren(`Searching: ${d.sources.join(", ")} · `,
      h("button", { type: "button", class: "link", text: "connect more sources", onclick: openSettingsAdd }));
  } catch { /* server offline: leave blank */ }
}

/* app-window presence: lets the server quit when the last window closes */
function startPresence() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const id = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  const ping = () => fetch(`/api/ping?c=${id}`, { cache: "no-store" }).catch(() => {});
  ping();
  setInterval(ping, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) ping(); });
  window.addEventListener("pagehide", () => navigator.sendBeacon(`/api/bye?c=${id}`));
}

function init() {
  startPresence();
  applyTheme(store.get("theme", "system"));
  $("#sort").value = state.sort;
  renderCatTiles();

  fillSelect($("#heroCat"), CATS.map(([id, label]) => [id, id === "all" ? "All categories" : label]), state.cat);
  $("#heroCat").disabled = false;
  $("#heroCat").addEventListener("change", (e) => { state.cat = e.target.value; });
  fillSelect($("#heroLang"), LANGS, state.lang);
  $("#heroLang").disabled = false;
  $("#searchHero").addEventListener("submit", (e) => {
    e.preventDefault();
    search($("#qHero").value, $("#heroCat").value, true, { lang: $("#heroLang").value });
  });
  $("#searchTop").addEventListener("submit", (e) => { e.preventDefault(); search($("#qTop").value, state.cat); });
  document.querySelectorAll("[data-try]").forEach((b) => b.addEventListener("click", () => {
    const [q, cat] = b.dataset.try.split("|");
    search(q, cat);
  }));
  $("#homeLink").addEventListener("click", (e) => { e.preventDefault(); goHome(); });
  $("#sort").addEventListener("change", (e) => { state.sort = e.target.value; store.set("sortV2", state.sort); render(); });

  $("#themeBtn").addEventListener("click", () => applyTheme(effectiveTheme() === "dark" ? "light" : "dark"));
  $("#settingsBtn").addEventListener("click", openSettings);
  $("#closeDrawer").addEventListener("click", closeSettings);
  $("#scrim").addEventListener("click", () => { closeSettings(); closeFilters(); });
  $("#addSourceLink").addEventListener("click", openSettingsAdd);
  $("#openFilters").addEventListener("click", openFilters);
  $("#closeFilters").addEventListener("click", closeFilters);

  $("#setArchive").addEventListener("change", (e) => { draft.archive_org = e.target.checked; setDirty(true); });
  $("#setHideDanger").addEventListener("change", (e) => {
    store.set("hideDanger", e.target.checked);
    state.verdicts.danger = !e.target.checked;
    render();
  });
  $("#setSmart").addEventListener("change", (e) => {
    store.set("smart", e.target.checked);
    toast(e.target.checked ? "Smart search & details on" : "Smart search & details off: Wikipedia won't be contacted", "info");
  });
  attachSuggest($("#qHero"), $("#suggestHero"), () => $("#heroCat").value,
    (t) => search(t, $("#heroCat").value, true, { lang: $("#heroLang").value }));
  attachSuggest($("#qTop"), $("#suggestTop"), () => state.cat, (t) => search(t, state.cat));
  $("#setFamily").addEventListener("change", (e) => {
    store.set("family", e.target.checked);
    $("#discover").dataset.loaded = "";           // reload suggestions with the new setting
    if (document.body.classList.contains("mode-home")) loadShelves();
    if (document.body.classList.contains("mode-browse")) loadBrowse();
    toast(e.target.checked ? "Family-friendly suggestions on" : "Family-friendly suggestions off", "info");
  });
  $("#testNew").addEventListener("click", () => { const s = newSourceFromForm(); if (s) testSource(s, $("#testMsg")); });
  $("#addNew").addEventListener("click", () => {
    const s = newSourceFromForm();
    if (!s) return;
    draft.torznab.push(s);
    renderDraft();
    setDirty(true);
    $("#newName").value = $("#newUrl").value = $("#newKey").value = "";
    $("#testMsg").className = "test-msg ok";
    $("#testMsg").textContent = `Added “${s.name}”. Click Save sources to apply.`;
  });
  $("#saveSettings").addEventListener("click", saveSettings);

  $("#scannerBtn").addEventListener("click", () => showScanner());
  $("#browseBtn").addEventListener("click", () => showBrowse({ page: 1 }));
  $("#bSource").addEventListener("change", (e) => showBrowse({ source: e.target.value, col: "", sort: "popular", decade: "any", page: 1 }));
  $("#bCat").addEventListener("change", (e) => showBrowse({ cat: e.target.value, col: "", page: 1 }));
  $("#bCol").addEventListener("change", (e) => showBrowse({ col: e.target.value, page: 1 }));
  $("#bSort").addEventListener("change", (e) => showBrowse({ sort: e.target.value, page: 1 }));
  $("#bDecade").addEventListener("change", (e) => showBrowse({ decade: e.target.value, page: 1 }));
  $("#bLang").addEventListener("change", (e) => showBrowse({ lang: e.target.value, page: 1 }));
  $("#bGenre").addEventListener("change", (e) => showBrowse({ genre: e.target.value, page: 1 }));
  $("#detailClose").addEventListener("click", closeDetail);
  $("#detail").addEventListener("click", (e) => { if (e.target === $("#detail")) closeDetail(); });
  $("#featureScanner").addEventListener("click", () => showScanner());
  $("#openScannerFromSettings").addEventListener("click", () => { closeSettings(); showScanner(); });
  $("#downloadsBtn").addEventListener("click", () => showScanner());
  $("#libraryBtn").addEventListener("click", () => showLibrary());
  wirePlayer();
  wireVoice();
  $("#dlOpenFolder").addEventListener("click", () => api("/api/downloads/open", {}).catch((err) => toast(err.message, "alert")));
  refreshDownloads();
  setInterval(refreshDownloads, 10000);
  $("#adultSave").addEventListener("click", saveAdult);
  $("#adultOpen").addEventListener("click", openPrivate);
  $("#adultPin").addEventListener("keydown", (e) => { if (e.key === "Enter") openPrivate(); });
  $("#adultLock").addEventListener("click", lockPrivate);
  $("#setTv").addEventListener("change", (e) => tvAction({ enabled: e.target.checked }));
  $("#setTvAuto").addEventListener("change", (e) => tvAction({ autostart: e.target.checked }));
  $("#tvNewPin").addEventListener("click", () => tvAction({ new_pin: true }));
  $("#tvForget").addEventListener("click", () => tvAction({ forget: true }).then(() => toast("All TVs unpaired")));
  $("#tvFirewall").addEventListener("click", () => { toast("Approve the Windows prompt"); tvAction({}, "firewall"); });
  $("#scanSave").addEventListener("click", () => saveScanner(true));
  $("#scanStop").addEventListener("click", () => saveScanner(false));
  $("#browseFolder").addEventListener("click", browseFolder);
  $("#vtClear").addEventListener("click", clearVirusTotal);
  for (const id of ["#scanFolder", "#vtKey", "#scanAutoQ"]) $(id).addEventListener("input", () => { scan.dirty = true; });
  setInterval(refreshScanner, 20000);

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
    if (e.key === "/" && !typing) {
      e.preventDefault();
      const input = document.body.classList.contains("mode-home") ? $("#qHero") : $("#qTop");
      input.focus(); input.select();
    } else if (e.key === "Escape") {
      closeDetail(); closeSettings(); closeFilters();
    } else if (!typing && $("#detail").hidden && document.body.classList.contains("mode-browse") && browse.data
               && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
      const next = browse.page + (e.key === "ArrowRight" ? 1 : -1);
      if (next >= 1 && next <= browse.data.pages) { showBrowse({ page: next }); window.scrollTo({ top: 0 }); }
    }
  });
  window.addEventListener("popstate", readUrl);

  loadFooter();
  refreshScanner();
  readUrl();
}

init();
