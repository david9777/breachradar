# BreachRadar — Live SEC cybersecurity-breach filing tracker

A dashboard that tracks **public-company data-breach / cybersecurity
disclosures** straight from SEC EDGAR. The gold-standard signal is the SEC's
mandatory **8-K Item 1.05 — "Material Cybersecurity Incidents"** (every public
company must file one when it suffers a material breach; in force since
Dec 18, 2023). BreachRadar also catches breaches disclosed under *other* 8-K
items (7.01, 8.01, …) by keyword-searching the filing text.

It also pulls **state Attorney-General breach-notification portals** (California,
Washington, Oregon), which cover breaches at **private** companies that never
file with the SEC — often with an "individuals affected" count.

Same architecture as SettleSearch: a zero-dependency Python-stdlib server plus a
vanilla HTML/CSS/JS dashboard. No API key, no account — every source is public;
SEC only asks for a descriptive `User-Agent`.

The dashboard **opens on the mandatory Item 1.05 disclosures** (the cleanest,
highest-signal list). Use the **scope filter** to widen to all SEC 8-K breach
filings, the state-AG notices, or everything at once.

## Run it

```powershell
cd "C:\Users\DSamson\.claude\BreachRadar"
python breach_server.py
```

Open **http://localhost:8770**. On first run it pulls the last 45 days
automatically. Click **⟳ Refresh** any time to pull the newest filings.

```powershell
python breach_server.py --refresh-once   # pull once, print JSON, exit
python breach_server.py --backfill        # pull every Item 1.05 + keyword
                                           # filing since 2023-12-18
python breach_server.py --port 9000        # custom port
```

To put it online (so anyone can press Refresh), see **[DEPLOY.md](DEPLOY.md)**.

## What it does

- **⟳ Refresh** — pulls the latest 8-K filings, dedups by accession number, and
  reports how many new ones were added (and how many are Item 1.05).
- **🔔 Alerts** — turn on desktop browser notifications. The page polls the API
  every 60 s; when a new filing appears (especially a red Item 1.05), it pops a
  toast and a desktop notification. Item 1.05 disclosures get a louder alert.
- **Scope filter** — *Item 1.05 — SEC mandatory* (the default), *All SEC 8-K
  breach filings*, *State AG breach notices*, or *All sources*.
- **Filters** — industry (derived from SIC code), filing/report-date range,
  full-text search across company, ticker, item, state, source, and keyword.
- **Every card links to the source** — SEC cards link to the real filing on
  sec.gov; state-AG cards link to the AG's notice/listing.
- **CSV export** of the current filtered view (includes affected counts).

## How a filing is matched

| Signal | What it means |
|--------|---------------|
| Red **Item 1.05** chip | The SEC's mandatory "Material Cybersecurity Incidents" item — a real, material breach disclosure. Highest confidence. |
| Green keyword chips (`data breach`, `ransomware`, `unauthorized access`, …) | The 8-K text matched a breach keyword. Catches disclosures filed under other items, but is noisier — a financing 8-K whose risk factors mention "ransomware" can surface here. Use the **Item 1.05 only** scope for the clean list. |
| Blue **Item N.NN** chips | Every 8-K item the filing was made under, so you can judge context at a glance. |

## Sources

| Source | Covers | Notes |
|--------|--------|-------|
| [SEC EDGAR full-text search](https://efts.sec.gov) | Public-company 8-K filings | Item 1.05 + breach keywords. Sporadic 5xx under load → connector retries with backoff and never crashes a refresh. |
| [California AG](https://oag.ca.gov/privacy/databreach/list) | Breaches affecting CA residents (public + private) | Org, breach date(s), reported date. |
| [Washington AG](https://www.atg.wa.gov/data-breach-notifications) | Breaches affecting WA residents | Adds **# Washingtonians affected**. |
| [Oregon DOJ](https://justice.oregon.gov/consumer/DataBreach/) | Breaches reported to OR | Adds **# affected**, discovery & notice dates. |

State portals are HTML tables (newest-reported first); each connector keeps the
recent window, caps row count to bound the store, and degrades to an empty list
on any layout change or block — logged, never fatal.

## Honest trade-offs

- **Item 1.05 only exists from Dec 2023.** Earlier SEC coverage relies on
  keyword matching. Keyword passes are noisier than the dedicated item — use the
  default *Item 1.05* scope for the clean list.
- **State portals lag.** Notices are filed months after the incident, so the
  state pass uses a wider date window than the SEC pass.
- **Not every breach appears.** State portals only cover their own residents,
  and some states (e.g. Maine, HHS healthcare "wall of shame") need different
  scrapers — natural next connectors to add.

## Files

| File | Purpose |
|------|---------|
| `breach_server.py` | Local server + EDGAR + state-AG pipeline (stdlib only) |
| `index.html` / `styles.css` / `app.js` | The dashboard |
| `breaches.json` | Data store (read + updated by the server; deduped by id) |

## Configuration (env vars)

- `BREACH_UA` — set to `"Your App you@example.com"` to identify yourself to SEC.
- `AUTO_REFRESH_HOURS` — background refresh cadence (default `3`; `0` disables).
- `PORT` / `HOST` — for hosting (binds `0.0.0.0` when `PORT` is set, else local-only).
- `SITE_NAME` — optional branding shown next to the title.
