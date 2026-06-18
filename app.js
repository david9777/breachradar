/* BreachRadar front-end — vanilla JS, no build step.
   Loads filings from /api/breaches, renders cards, and polls for new ones to
   fire in-app + desktop browser notifications. */
"use strict";

const PAGE = 60;            // cards rendered per "Show more" chunk
const POLL_MS = 60_000;     // how often we re-check the API for new filings

const state = {
  all: [],                  // every filing from the API
  view: [],                 // filtered + sorted
  shown: PAGE,              // how many of `view` are on screen
  knownIds: new Set(),      // accession numbers we've already seen (for alerts)
  primed: false,            // first load done? (don't alert on the initial batch)
  alertsOn: false,
  scope: "105",             // active tab
};

// Source tabs. `match(r)` decides which records belong to each tab; `key`
// indexes into the counts object from the API.
const TABS = [
  { id: "105",        label: "⚠️ Item 1.05", key: "item_105",  match: (r) => r.is_item_105 },
  { id: "sec",        label: "SEC 8-Ks",      key: "sec",       match: (r) => (r.source_type || "sec") === "sec" },
  { id: "state",      label: "State AG",      key: "state_ag",  match: (r) => r.source_type === "state_ag" },
  { id: "ransomware", label: "🔒 Ransomware", key: "ransomware",match: (r) => r.source_type === "ransomware" },
  { id: "hibp",       label: "HIBP breaches", key: "hibp",      match: (r) => r.source_type === "hibp" },
  { id: "all",        label: "All sources",   key: "total",     match: () => true },
];

const $ = (id) => document.getElementById(id);
const ITEM_LABELS = {
  "1.05": "1.05 Material Cybersecurity Incidents",
  "7.01": "7.01 Reg FD Disclosure",
  "8.01": "8.01 Other Events",
  "2.02": "2.02 Results of Operations",
  "9.01": "9.01 Exhibits",
};

// ---- data ----------------------------------------------------------------
function clientCounts(arr) {
  const c = { sec: 0, state_ag: 0, ransomware: 0, hibp: 0, item_105: 0, total: arr.length };
  arr.forEach((r) => {
    const st = r.source_type || "sec";
    c[st] = (c[st] || 0) + 1;
    if (r.is_item_105) c.item_105++;
  });
  return c;
}

async function loadData() {
  let data;
  try {
    const res = await fetch("/api/breaches", { cache: "no-store" });
    if (!res.ok) throw new Error("no-api");
    data = await res.json();
    if (!data.breaches) throw new Error("no-api");
  } catch (_) {
    // Static deployment (e.g. GitHub Pages): no backend, so read the data file
    // committed to the repo (kept fresh by the scheduled GitHub Action).
    const res = await fetch("breaches.json", { cache: "no-store" });
    const arr = await res.json();
    data = {
      breaches: arr, total: arr.length,
      counts: clientCounts(arr),
      item_105_count: arr.filter((r) => r.is_item_105).length,
      last_updated: null,
    };
  }
  const recs = data.breaches || [];

  // Detect newly-arrived filings (only after the first successful load).
  if (state.primed) {
    const fresh = recs.filter((r) => !state.knownIds.has(r.id));
    if (fresh.length) announceNew(fresh);
  }
  recs.forEach((r) => state.knownIds.add(r.id));

  state.all = recs;
  state.primed = true;
  renderStats(data);
  applyFilters();
}

async function refresh() {
  const btn = $("refreshBtn");
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "⟳ Refreshing…";
  showStatus("Pulling the latest breaches from SEC EDGAR, state AGs, ransomware.live & HIBP…");
  try {
    const res = await fetch("/api/refresh", { method: "POST" });
    if (!res.ok) throw new Error("no-backend"); // static host: 404/405
    const r = await res.json();
    if (!r.ok) throw new Error(r.error || "refresh failed");
    if (r.note) {
      showStatus(r.note);
    } else {
      showStatus(`Added ${r.added} new, ${r.updated} updated · ${r.total} total (${r.item_105_total} Item 1.05).`);
    }
    await loadData();
  } catch (e) {
    // No live backend (static GitHub Pages deploy): the data file is kept fresh
    // by a scheduled GitHub Action, so just reload the latest committed data.
    try {
      await loadData();
      showStatus("This is a static deployment — showing the latest auto-refreshed data (updated periodically). Run the server for on-demand pulls.");
    } catch (e2) {
      showStatus("Couldn't load data: " + e2.message, true);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
    setTimeout(() => $("status").setAttribute("hidden", ""), 7000);
  }
}

// ---- alerts ---------------------------------------------------------------
function announceNew(fresh) {
  const top = fresh[0];
  const more = fresh.length > 1 ? ` (+${fresh.length - 1} more)` : "";
  const has105 = fresh.some((r) => r.is_item_105);
  const lead = has105 ? "🔴 New Item 1.05 breach disclosure: "
                      : `New breach (${top.source}): `;
  const msg = `${lead}${top.company}${more}`;
  toast(msg);
  if (state.alertsOn && "Notification" in window && Notification.permission === "granted") {
    new Notification("BreachRadar", {
      body: msg + (top.ticker ? ` (${top.ticker})` : ""),
      tag: "breachradar",
    });
  }
}

async function toggleAlerts() {
  if (!("Notification" in window)) {
    toast("This browser doesn't support desktop notifications.");
    return;
  }
  if (Notification.permission !== "granted") {
    const p = await Notification.requestPermission();
    if (p !== "granted") { toast("Notifications blocked in browser settings."); return; }
  }
  state.alertsOn = !state.alertsOn;
  const btn = $("alertsBtn");
  btn.textContent = state.alertsOn ? "🔔 Alerts: on" : "🔔 Alerts: off";
  btn.classList.toggle("on", state.alertsOn);
  if (state.alertsOn) toast("Desktop alerts on — you'll be pinged on new filings.");
}

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.removeAttribute("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.setAttribute("hidden", ""), 7000);
}

function showStatus(text, isErr) {
  const s = $("status");
  s.textContent = text;
  s.classList.toggle("err", !!isErr);
  s.removeAttribute("hidden");
}

// ---- rendering ------------------------------------------------------------
function renderStats(data) {
  const recs = data.breaches || [];
  const now = Date.now();
  const within = (d, days) =>
    d && (now - new Date(d + "T00:00:00Z").getTime()) <= days * 864e5;
  const last30 = recs.filter((r) => within(r.filed, 30)).length;
  const upd = data.last_updated
    ? new Date(data.last_updated).toLocaleString()
    : "click Refresh";
  $("stats").innerHTML = `
    <div class="stat"><div class="n">${recs.length}</div><div class="l">Breach records tracked</div></div>
    <div class="stat alert"><div class="n">${data.item_105_count || 0}</div><div class="l">Mandatory Item 1.05 disclosures</div></div>
    <div class="stat"><div class="n">${last30}</div><div class="l">Filed/reported last 30 days</div></div>
    <div class="stat"><div class="n" style="font-size:15px;padding-top:7px">${upd}</div><div class="l">Last updated</div></div>`;
  renderTabs(data.counts || {});
  buildIndustryFilter(recs);
}

function renderTabs(counts) {
  $("tabs").innerHTML = TABS.map((t) => {
    const n = t.id === "all" ? state.all.length : (counts[t.key] || 0);
    const active = state.scope === t.id ? " active" : "";
    return `<button class="tab${active}" data-tab="${t.id}" role="tab" aria-selected="${state.scope === t.id}">
      ${t.label} <span class="tabn">${n}</span></button>`;
  }).join("");
  $("tabs").querySelectorAll(".tab").forEach((b) =>
    b.addEventListener("click", () => {
      state.scope = b.dataset.tab;
      renderTabs(counts);
      applyFilters();
    }));
}

function buildIndustryFilter(recs) {
  const sel = $("industryFilter");
  if (sel.dataset.built) return;
  const inds = [...new Set(recs.map((r) => r.industry).filter(Boolean))].sort();
  inds.forEach((i) => {
    const o = document.createElement("option");
    o.value = i; o.textContent = i; sel.appendChild(o);
  });
  sel.dataset.built = "1";
}

function applyFilters() {
  const q = $("search").value.trim().toLowerCase();
  const tab = TABS.find((t) => t.id === state.scope) || TABS[0];
  const ind = $("industryFilter").value;
  const range = parseInt($("rangeFilter").value, 10);
  const sort = $("sortBy").value;
  const now = Date.now();

  let v = state.all.filter((r) => {
    if (!tab.match(r)) return false;
    if (ind && r.industry !== ind) return false;
    if (range && r.filed) {
      const age = (now - new Date(r.filed + "T00:00:00Z").getTime()) / 864e5;
      if (age > range) return false;
    }
    if (q) {
      const hay = [r.company, r.ticker, r.state, r.industry, r.source, r.breach_date,
                   (r.items || []).join(" "), (r.matched || []).join(" ")]
        .join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  v.sort((a, b) => {
    if (sort === "company") return (a.company || "").localeCompare(b.company || "");
    if (sort === "filed_asc") return (a.filed || "").localeCompare(b.filed || "");
    return (b.filed || "").localeCompare(a.filed || ""); // newest first
  });

  state.view = v;
  state.shown = PAGE;
  renderCards();
}

function esc(s) {
  return (s || "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function fmtAffected(n) {
  if (!n) return "";
  return n.toLocaleString() + " affected";
}

function cardHtml(r) {
  const st = r.source_type || "sec";
  const srcChipBase = `<span class="chip src">${esc(r.source)}</span>`;

  if (st === "ransomware") {
    const group = (r.matched || [])[0];
    return `
    <article class="card ransom">
      <div class="row1">
        <span class="company">${esc(r.company) || "(unnamed victim)"}</span>
        <span class="date">posted ${esc(r.filed)}</span>
      </div>
      <div class="chips">
        ${srcChipBase}
        ${group ? `<span class="chip ransomg">🔒 ${esc(group)}</span>` : ""}
        ${r.industry ? `<span class="chip">${esc(r.industry)}</span>` : ""}
      </div>
      <div class="meta">
        ${r.breach_date ? `<span>🗓️ attack: ${esc(r.breach_date)}</span>` : ""}
        ${r.state ? `<span>🌐 ${esc(r.state)}</span>` : ""}
      </div>
      <div class="links">
        <a href="${esc(r.filing_url)}" target="_blank" rel="noopener">🔗 View victim on ransomware.live →</a>
        ${r.index_url && r.index_url !== r.filing_url ? `<a href="${esc(r.index_url)}" target="_blank" rel="noopener">gang profile</a>` : ""}
      </div>
    </article>`;
  }

  if (st === "hibp") {
    const classes = (r.matched || []).map((m) => `<span class="chip kw">${esc(m)}</span>`).join("");
    return `
    <article class="card hibp">
      <div class="row1">
        <span class="company">${esc(r.company) || "(unnamed)"}</span>
        ${r.affected ? `<span class="date">${esc(fmtAffected(r.affected))}</span>` : ""}
      </div>
      <div class="chips">${srcChipBase}${classes}</div>
      <div class="meta">
        ${r.breach_date ? `<span>🗓️ breach: ${esc(r.breach_date)}</span>` : ""}
        <span>added ${esc(r.filed)}</span>
      </div>
      <div class="links">
        <a href="${esc(r.filing_url)}" target="_blank" rel="noopener">🔗 Have I Been Pwned</a>
      </div>
    </article>`;
  }

  const isState = st === "state_ag";
  const items = (r.items || []).map((i) => {
    const red = i === "1.05";
    return `<span class="chip ${red ? "red" : "item"}" title="${esc(ITEM_LABELS[i] || "Item " + i)}">Item ${esc(i)}</span>`;
  }).join("");
  const kws = (r.matched || []).filter((m) => m !== "Item 1.05")
    .map((m) => `<span class="chip kw">${esc(m)}</span>`).join("");
  const srcChip = `<span class="chip src">${esc(r.source)}</span>`;

  if (isState) {
    return `
    <article class="card state">
      <div class="row1">
        <span class="company">${esc(r.company) || "(unnamed)"}</span>
        <span class="date">reported ${esc(r.filed)}</span>
      </div>
      <div class="chips">
        ${srcChip}
        ${r.affected ? `<span class="chip num">${esc(fmtAffected(r.affected))}</span>` : ""}
      </div>
      <div class="meta">
        ${r.breach_date ? `<span>🗓️ breach: ${esc(r.breach_date)}</span>` : ""}
        ${r.state ? `<span>📍 ${esc(r.state)}</span>` : ""}
      </div>
      <div class="links">
        <a href="${esc(r.filing_url)}" target="_blank" rel="noopener">🔗 View notice / source</a>
      </div>
    </article>`;
  }

  return `
    <article class="card ${r.is_item_105 ? "item105" : ""}">
      <div class="row1">
        <span class="company">${esc(r.company) || "(unnamed filer)"}</span>
        ${r.ticker ? `<span class="ticker">${esc(r.ticker)}</span>` : ""}
        <span class="date">${esc(r.filed)}</span>
      </div>
      <div class="chips">${items}${kws}</div>
      <div class="meta">
        ${r.industry ? `<span>🏢 ${esc(r.industry)}</span>` : ""}
        ${r.state ? `<span>📍 ${esc(r.state)}</span>` : ""}
        <span>${esc(r.form)} · acc# ${esc(r.id)}</span>
      </div>
      <div class="links">
        <a href="${esc(r.index_url)}" target="_blank" rel="noopener">📄 View 8-K filing on SEC.gov →</a>
        ${r.filing_url && r.filing_url !== r.index_url ? `<a href="${esc(r.filing_url)}" target="_blank" rel="noopener">↳ matched document</a>` : ""}
      </div>
    </article>`;
}

function renderCards() {
  const root = $("results");
  if (!state.view.length) {
    root.innerHTML = `<div class="empty">No filings match your filters. Try widening the date range or clearing search.</div>`;
    $("showMoreWrap").setAttribute("hidden", "");
    return;
  }
  const slice = state.view.slice(0, state.shown);
  root.innerHTML = slice.map(cardHtml).join("");
  const wrap = $("showMoreWrap");
  if (state.view.length > state.shown) {
    wrap.removeAttribute("hidden");
    $("showMore").textContent = `Show more (${state.view.length - state.shown} hidden)`;
  } else {
    wrap.setAttribute("hidden", "");
  }
}

function exportCsv() {
  const rows = [["Company", "Ticker", "CIK", "Filed/Reported", "Source", "Form",
                 "Items", "Item 1.05", "Matched", "Breach date", "Affected",
                 "Industry", "State", "URL"]];
  state.view.forEach((r) => rows.push([
    r.company, r.ticker, r.cik, r.filed, r.source, r.form, (r.items || []).join(" "),
    r.is_item_105 ? "YES" : "", (r.matched || []).join(" "), r.breach_date || "",
    r.affected || "", r.industry, r.state, r.filing_url,
  ]));
  const csv = rows.map((row) =>
    row.map((c) => `"${String(c == null ? "" : c).replace(/"/g, '""')}"`).join(",")
  ).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "breachradar.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---- wire up --------------------------------------------------------------
function init() {
  fetch("/api/config").then((r) => r.json()).then((c) => {
    if (c.site_name) $("siteName").textContent = " · " + c.site_name;
  }).catch(() => {});

  $("refreshBtn").addEventListener("click", refresh);
  $("alertsBtn").addEventListener("click", toggleAlerts);
  $("csvBtn").addEventListener("click", exportCsv);
  ["search", "industryFilter", "rangeFilter", "sortBy"]
    .forEach((id) => $(id).addEventListener("input", applyFilters));
  $("showMore").addEventListener("click", () => { state.shown += PAGE; renderCards(); });

  loadData().catch((e) =>
    showStatus("Couldn't reach the server: " + e.message + ". Run: python breach_server.py", true));

  // Background poll: re-check the API so new filings alert even without a manual refresh.
  setInterval(() => loadData().catch(() => {}), POLL_MS);
}

document.addEventListener("DOMContentLoaded", init);
