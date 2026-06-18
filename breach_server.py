#!/usr/bin/env python3
"""
BreachRadar — live SEC EDGAR cybersecurity-breach filing tracker.

Tracks public-company data-breach / cybersecurity disclosures filed on SEC
EDGAR, with the SEC's mandatory **8-K Item 1.05 ("Material Cybersecurity
Incidents")** as the gold-standard signal, plus keyword 8-Ks that disclose a
breach under other items (7.01, 8.01, etc.).

Serves the static web app AND exposes a small JSON API:

    GET  /api/breaches   -> { breaches: [...], last_updated: "...", item105_count: N }
    POST /api/refresh    -> pulls the latest filings from EDGAR, merges new ones,
                            and returns a summary of what was added.

Data source (no API key, no account required):
  * SEC EDGAR full-text search (efts.sec.gov) — the SEC asks only that every
    request carry a descriptive User-Agent. We dedup filings by accession
    number and link each record straight to the real filing on sec.gov.

Run it:
    python breach_server.py                 # serve on http://localhost:8770
    python breach_server.py --refresh-once  # run one refresh, print JSON, exit
    python breach_server.py --backfill       # pull every Item 1.05 since the rule
                                              # took effect (2023-12-18) + keywords
    python breach_server.py --port 9000     # custom port

Pure standard library — no pip install needed.
"""
import json, os, re, ssl, sys, time, threading, html
import urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
STORE = os.environ.get("BREACH_STORE", os.path.join(ROOT, "breaches.json"))
SITE_NAME = os.environ.get("SITE_NAME", "")

# SEC requires a descriptive User-Agent identifying the requester. Set
# BREACH_UA to your own "App name you@example.com" to be a good citizen.
UA = os.environ.get("BREACH_UA", "BreachRadar/1.0 (research; contact@example.com)")

EFTS = "https://efts.sec.gov/LATEST/search-index"

# Canonical field set every record carries (keeps the front-end simple).
# `source_type` is "sec" (an EDGAR 8-K) or "state_ag" (a state breach-notice
# portal). State records add `breach_date` and `affected`; SEC records add
# `cik`, `items`, and `is_item_105`.
FIELDS = ["id", "company", "ticker", "cik", "filed", "form", "items",
          "is_item_105", "matched", "category", "sic", "industry", "state",
          "filing_url", "index_url", "source", "source_type", "breach_date",
          "affected", "date_added"]

# Browser-style UA for state-AG sites that reject non-browser clients.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# The rule that created 8-K Item 1.05 took effect on this date; nothing earlier
# can carry that item, so backfill starts here.
RULE_EFFECTIVE = "2023-12-18"

# Search passes. Each is (label, EDGAR query). The dedicated Item 1.05 pass
# catches the mandatory disclosures even when worded oddly ("cyber event");
# the keyword passes catch breaches disclosed under other 8-K items. Every hit
# also reports its own `items` list, so 1.05 filings are flagged regardless of
# which pass surfaced them.
SEARCHES = [
    ("Item 1.05",          '"Material Cybersecurity Incidents"'),
    ("data breach",        '"data breach"'),
    ("cybersecurity incident", '"cybersecurity incident"'),
    ("ransomware",         '"ransomware"'),
    ("unauthorized access", '"unauthorized access"'),
    ("threat actor",       '"threat actor"'),
    ("network intrusion",  '"network intrusion"'),
]

# Map a few SIC code prefixes to a readable industry bucket for filtering.
_SIC_BUCKETS = [
    ("Finance / Banking", {"60", "61", "62", "63", "64", "67"}),
    ("Healthcare / Pharma", {"80", "28", "38"}),
    ("Technology / Software", {"73", "35", "36", "48"}),
    ("Retail / Consumer", {"52", "53", "54", "56", "57", "58", "59"}),
    ("Energy / Utilities", {"13", "29", "49"}),
    ("Manufacturing", {"20", "22", "26", "30", "33", "34", "37"}),
]


# ----------------------------------------------------------------------------
# HTTP helper — verifies TLS normally, falls back to unverified for machines
# behind a corporate SSL-inspection proxy (mirrors the SettleSearch helper).
# ----------------------------------------------------------------------------
def http_get(url, timeout=25, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h = headers
    req = urllib.request.Request(url, headers=h)
    try:
        return urllib.request.urlopen(req, timeout=timeout).read()
    except (ssl.SSLError, urllib.error.URLError) as e:
        if isinstance(e, urllib.error.HTTPError):
            raise
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=timeout, context=ctx).read()


def http_get_text(url, timeout=30):
    """Fetch an HTML page as text using a browser User-Agent."""
    raw = http_get(url, timeout=timeout,
                   headers={"User-Agent": BROWSER_UA, "Accept": BROWSER_ACCEPT})
    return raw.decode("utf-8", "replace")


def http_get_json(url, timeout=25):
    raw = http_get(url, timeout=timeout,
                   headers={"User-Agent": UA, "Accept": "application/json"})
    return json.loads(raw.decode("utf-8", "replace"))


# ---- tiny HTML-table helpers (no dependencies) -----------------------------
def _tables(h):
    return re.findall(r"<table[^>]*>(.*?)</table>", h, re.S | re.I)


def _rows(t):
    return re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I)


def _cells(r):
    return re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S | re.I)


def _txt(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _href(s):
    m = re.search(r'href=["\']([^"\']+)["\']', s or "")
    return m.group(1) if m else ""


def _to_int(s):
    d = re.sub(r"[^\d]", "", s or "")
    return int(d) if d else None


def _norm_date(s):
    """Return an ISO yyyy-mm-dd from a US m/d/Y string (or pass through ISO)."""
    s = (s or "").strip()
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mo, da, yr = m.groups()
        return "%s-%02d-%02d" % (yr, int(mo), int(da))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else ""


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------
# "COMPANY NAME  (TICK, TICK2)  (CIK 0001234567)"  ->  name, ticker, cik
_NAME_RE = re.compile(
    r"^(?P<name>.*?)\s*(?:\((?P<tick>[A-Z0-9.,\s/-]+)\)\s*)?\(CIK\s*(?P<cik>\d+)\)\s*$")


def parse_display_name(display):
    m = _NAME_RE.match(display or "")
    if not m:
        return (display or "").strip(), "", ""
    name = re.sub(r"\s+", " ", m.group("name")).strip().rstrip(",")
    tick = (m.group("tick") or "").strip()
    cik = m.group("cik")
    return name, tick, cik


def industry_for(sic):
    if not sic:
        return ""
    p2 = str(sic).zfill(4)[:2]
    for label, prefixes in _SIC_BUCKETS:
        if p2 in prefixes:
            return label
    return "Other"


def _filing_urls(cik, adsh, primary_doc):
    """Build the human-readable filing index URL and the primary-document URL."""
    cik_int = str(int(cik)) if str(cik).isdigit() else str(cik)
    nodash = adsh.replace("-", "")
    base = "https://www.sec.gov/Archives/edgar/data/%s/%s" % (cik_int, nodash)
    index_url = "%s/%s-index.htm" % (base, adsh)
    doc_url = ("%s/%s" % (base, primary_doc)) if primary_doc else index_url
    return index_url, doc_url


# ----------------------------------------------------------------------------
# EDGAR full-text search connector
# ----------------------------------------------------------------------------
def _efts_page(query, startdt, enddt, frm, tries=4):
    params = {"q": query, "forms": "8-K", "from": frm}
    if startdt:
        params["startdt"] = startdt
    if enddt:
        params["enddt"] = enddt
    url = EFTS + "?" + urllib.parse.urlencode(params)
    # EDGAR's full-text endpoint returns sporadic 5xx under load; the same URL
    # succeeds on retry. Back off and try again before giving up.
    for attempt in range(tries):
        try:
            return json.loads(http_get(url).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < tries - 1:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise
        except (ssl.SSLError, urllib.error.URLError):
            if attempt < tries - 1:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise


def fetch_edgar(startdt, enddt, max_per_search=2000):
    """Run every search pass over [startdt, enddt] and return deduped records
    keyed by accession number. Never raises on a single bad page."""
    by_adsh = {}
    for si, (label, query) in enumerate(SEARCHES):
        if si:
            time.sleep(0.4)  # space out passes so EDGAR doesn't throttle us
        frm = 0
        pulled = 0
        while pulled < max_per_search:
            try:
                data = _efts_page(query, startdt, enddt, frm)
            except Exception as e:
                print("[edgar] %s page %d failed: %s" % (label, frm, e))
                break
            hits = (data.get("hits") or {}).get("hits") or []
            if not hits:
                break
            for h in hits:
                src = h.get("_source") or {}
                adsh = src.get("adsh")
                if not adsh:
                    continue
                display = (src.get("display_names") or [""])[0]
                name, tick, cik = parse_display_name(display)
                items = src.get("items") or []
                primary_doc = (h.get("_id") or "").split(":", 1)[-1]
                index_url, doc_url = _filing_urls(cik, adsh,
                                                  primary_doc if ":" in (h.get("_id") or "") else "")
                sic = (src.get("sics") or [""])[0]
                rec = by_adsh.get(adsh)
                if rec is None:
                    rec = {
                        "id": adsh,
                        "company": name,
                        "ticker": tick,
                        "cik": cik,
                        "filed": src.get("file_date", ""),
                        "form": src.get("form", "8-K"),
                        "items": items,
                        "is_item_105": "1.05" in items,
                        "matched": [label],
                        "category": "Cybersecurity / Data Breach",
                        "sic": sic,
                        "industry": industry_for(sic),
                        "state": (src.get("biz_states") or [""])[0],
                        "filing_url": doc_url,
                        "index_url": index_url,
                        "source": "SEC EDGAR 8-K",
                        "source_type": "sec",
                        "breach_date": "",
                        "affected": None,
                    }
                    by_adsh[adsh] = rec
                else:
                    if label not in rec["matched"]:
                        rec["matched"].append(label)
                    # Prefer the primary 8-K document over an exhibit for the link.
                    if "ex" in rec["filing_url"].lower() and "ex" not in doc_url.lower():
                        rec["filing_url"] = doc_url
            pulled += len(hits)
            frm += len(hits)
            if len(hits) < 100:
                break
            time.sleep(0.12)  # be polite to EDGAR
    return list(by_adsh.values())


# ----------------------------------------------------------------------------
# State Attorney-General breach-notification portals
# ----------------------------------------------------------------------------
# These cover breaches at *private* companies (and public ones) that the SEC
# feed misses. Each returns normalized records and never raises — a layout
# change or a blocked request degrades to an empty list, logged by the caller.
# Tables are sorted newest-reported-first, so we keep rows within the window
# and stop at `cap` to bound the store.

def _state_record(source, st, org, reported, breach_dates, affected, url):
    slug = re.sub(r"[^a-z0-9]+", "-", (org.lower() + "-" + reported)).strip("-")[:90]
    return {
        "id": source.split()[0].lower() + ":" + slug,
        "company": org, "ticker": "", "cik": "",
        "filed": reported, "form": "Breach notice",
        "items": [], "is_item_105": False, "matched": [],
        "category": "Cybersecurity / Data Breach",
        "sic": "", "industry": "", "state": st,
        "filing_url": url, "index_url": url,
        "source": source, "source_type": "state_ag",
        "breach_date": breach_dates or "", "affected": affected,
    }


def fetch_ca_ag(window_start, cap=500):
    """California AG breach list. Columns: Org | Date(s) of breach | Reported."""
    text = http_get_text("https://oag.ca.gov/privacy/databreach/list")
    out = []
    for t in _tables(text):
        rows = _rows(t)
        if len(rows) < 5:
            continue
        for r in rows[1:]:
            c = _cells(r)
            if len(c) < 3:
                continue
            org = _txt(c[0])
            reported = _norm_date(c[-1])
            if not org or not reported or reported < window_start:
                continue
            url = _href(c[0])
            if url.startswith("/"):
                url = "https://oag.ca.gov" + url
            out.append(_state_record("California AG", "CA", org, reported,
                                     _txt(c[1]), None,
                                     url or "https://oag.ca.gov/privacy/databreach/list"))
            if len(out) >= cap:
                break
        break  # only the first substantial table
    return out


def fetch_wa_ag(window_start, cap=500):
    """Washington AG. Columns: Reported | Org(+link) | Breach date | #WA affected | Info."""
    out = []
    for page in range(0, 40):
        url = "https://www.atg.wa.gov/data-breach-notifications?page=%d" % page
        text = http_get_text(url)
        tbls = _tables(text)
        if not tbls:
            break
        rows = _rows(tbls[0])
        if len(rows) < 2:
            break
        stop = False
        for r in rows[1:]:
            c = _cells(r)
            if len(c) < 3:
                continue
            reported = _norm_date(c[0])
            org = _txt(c[1])
            if not org or not reported:
                continue
            if reported < window_start:
                stop = True
                continue
            link = _href(c[1])
            affected = _to_int(c[3]) if len(c) > 3 else None
            out.append(_state_record("Washington AG", "WA", org, reported,
                                     _txt(c[2]) if len(c) > 2 else "", affected,
                                     link or "https://www.atg.wa.gov/data-breach-notifications"))
            if len(out) >= cap:
                return out
        if stop:
            break
        time.sleep(0.2)
    return out


def fetch_or_doj(window_start, cap=500):
    """Oregon DOJ. Columns: Org | Reported | Breach dates | Discovery | Notice | #Affected."""
    text = http_get_text("https://justice.oregon.gov/consumer/DataBreach/")
    out = []
    for t in _tables(text):
        rows = _rows(t)
        if len(rows) < 5:
            continue
        for r in rows[1:]:
            c = _cells(r)
            if len(c) < 6:
                continue
            org = _txt(c[0])
            reported = _norm_date(c[1])
            if not org or not reported or reported < window_start:
                continue
            out.append(_state_record("Oregon DOJ", "OR", org, reported,
                                     _txt(c[2]), _to_int(c[5]),
                                     "https://justice.oregon.gov/consumer/DataBreach/"))
            if len(out) >= cap:
                break
        break
    return out


STATE_SOURCES = [fetch_ca_ag, fetch_wa_ag, fetch_or_doj]


# ----------------------------------------------------------------------------
# Threat-intel / breach-database feeds (free JSON APIs)
# ----------------------------------------------------------------------------
def fetch_ransomware_live(window_start, cap=500):
    """ransomware.live — recent victims posted on ransomware-gang leak sites.
    A leading-indicator feed: gangs name victims before any SEC/AG filing."""
    try:
        data = http_get_json("https://api.ransomware.live/v2/recentvictims")
    except Exception:
        # v1 fallback path if the v2 shape ever changes.
        data = http_get_json("https://api.ransomware.live/recentvictims")
    out = []
    for v in (data or []):
        victim = (v.get("victim") or "").strip()
        when = _norm_date(v.get("discovered") or v.get("attackdate") or "")
        if not victim or not when or when < window_start:
            continue
        group = (v.get("group") or "").strip()
        sector = (v.get("activity") or "").strip()
        if sector.lower() in ("not found", "unknown", ""):
            sector = ""
        slug = re.sub(r"[^a-z0-9]+", "-", (victim.lower() + "-" + when)).strip("-")[:90]
        out.append({
            "id": "rw:" + slug,
            "company": victim, "ticker": "", "cik": "",
            "filed": when, "form": "Leak-site post",
            "items": [], "is_item_105": False,
            "matched": [group] if group else [],
            "category": "Ransomware",
            "sic": "", "industry": sector, "state": (v.get("country") or "").strip(),
            # Per-victim page on ransomware.live (preferred), falling back to the
            # gang's profile page, then the recent feed.
            "filing_url": (v.get("url")
                           or (("https://www.ransomware.live/group/" + group) if group
                               else "https://www.ransomware.live/recent")),
            "index_url": ("https://www.ransomware.live/group/" + group) if group
                         else "https://www.ransomware.live/recent",
            "source": "ransomware.live", "source_type": "ransomware",
            "breach_date": _norm_date(v.get("attackdate") or ""), "affected": None,
        })
        if len(out) >= cap:
            break
    return out


def fetch_hibp(window_start, cap=500):
    """Have I Been Pwned — known credential/data breaches. Free, no key.
    PwnCount gives the number of accounts exposed; DataClasses what leaked."""
    data = http_get_json("https://haveibeenpwned.com/api/v3/breaches")
    out = []
    for b in (data or []):
        added = _norm_date(b.get("AddedDate") or "")
        if not added or added < window_start:
            continue
        name = b.get("Title") or b.get("Name") or ""
        if not name:
            continue
        out.append({
            "id": "hibp:" + re.sub(r"[^a-z0-9]+", "-", (b.get("Name") or name).lower())[:90],
            "company": name, "ticker": "", "cik": "",
            "filed": added, "form": "Breach database entry",
            "items": [], "is_item_105": False,
            "matched": (b.get("DataClasses") or [])[:3],
            "category": "Credential / Data Breach",
            "sic": "", "industry": "", "state": "",
            "filing_url": "https://haveibeenpwned.com/PwnedWebsites#" +
                          (b.get("Name") or ""),
            "index_url": "https://haveibeenpwned.com/PwnedWebsites",
            "source": "Have I Been Pwned", "source_type": "hibp",
            "breach_date": _norm_date(b.get("BreachDate") or ""),
            "affected": b.get("PwnCount") or None,
        })
        if len(out) >= cap:
            break
    return out


# Feeds that share the (window_start, cap) signature and fail gracefully.
FEED_SOURCES = STATE_SOURCES + [fetch_ransomware_live, fetch_hibp]


# ----------------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------------
def load_store():
    if not os.path.exists(STORE):
        return []
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_store(records):
    records.sort(key=lambda r: (r.get("filed", ""), r.get("id", "")), reverse=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STORE)


_REFRESH_LOCK = threading.Lock()
_STATE = {"last_updated": None, "last_pull": 0.0}
# Min seconds between real public refresh pulls (protects a hosted server where
# anyone can press Refresh). 0 disables. Mirrors SettleSearch's REFRESH_COOLDOWN.
REFRESH_COOLDOWN = int(os.environ.get("REFRESH_COOLDOWN", "0"))


def refresh(days=45, backfill=False):
    """Pull recent (or all) breach filings, merge new ones into the store."""
    now_dt = datetime.now(timezone.utc)
    enddt = now_dt.strftime("%Y-%m-%d")
    startdt = RULE_EFFECTIVE if backfill else \
        (now_dt - timedelta(days=days)).strftime("%Y-%m-%d")

    fetched = fetch_edgar(startdt, enddt)

    # State-AG portals + threat-intel feeds: notices/posts lag (or precede) the
    # incident, so use a wider window than the SEC pass. Each fails gracefully.
    feed_start = RULE_EFFECTIVE if backfill else \
        (now_dt - timedelta(days=max(days, 180))).strftime("%Y-%m-%d")
    feed_cap = 6000 if backfill else 600
    for fn in FEED_SOURCES:
        try:
            got = fn(feed_start, cap=feed_cap)
            fetched += got
            print("[feed] %s: %d" % (fn.__name__, len(got)))
        except Exception as e:
            print("[feed] %s failed: %s" % (fn.__name__, e))

    existing = {r["id"]: r for r in load_store()}
    added, updated = 0, 0
    now = datetime.now(timezone.utc).isoformat()
    for rec in fetched:
        old = existing.get(rec["id"])
        if old is None:
            rec["date_added"] = now
            existing[rec["id"]] = rec
            added += 1
        else:
            # Merge newly-matched keywords (SEC) and refresh mutable state-AG
            # fields (affected count can be corrected after the first notice).
            merged = sorted(set(old.get("matched", [])) | set(rec.get("matched", [])))
            changed = merged != old.get("matched")
            old["matched"] = merged
            old["is_item_105"] = old.get("is_item_105") or rec.get("is_item_105")
            # Backfill fields added in later versions onto pre-existing records.
            old.setdefault("source_type", rec.get("source_type", "sec"))
            old.setdefault("breach_date", rec.get("breach_date", ""))
            old.setdefault("affected", rec.get("affected"))
            if rec.get("affected") and rec.get("affected") != old.get("affected"):
                old["affected"] = rec["affected"]
                changed = True
            # Refresh the source links to the latest computed value so link-logic
            # fixes propagate to records already in the store.
            for k in ("filing_url", "index_url"):
                if rec.get(k) and rec[k] != old.get(k):
                    old[k] = rec[k]
                    changed = True
            if changed:
                updated += 1
    records = list(existing.values())
    save_store(records)
    _STATE["last_updated"] = now
    counts = _source_counts(records)
    summary = {"ok": True, "added": added, "updated": updated,
               "total": len(records), "item_105_total": counts["item_105"],
               "counts": counts,
               "window": "%s..%s" % (startdt, enddt), "last_updated": now}
    print("[refresh] +%d new, %d updated, %d total (%d Item 1.05, %d ransomware, %d hibp, %d state)"
          % (added, updated, len(records), counts["item_105"],
             counts["ransomware"], counts["hibp"], counts["state_ag"]))
    return summary


def _source_counts(records):
    c = {"sec": 0, "state_ag": 0, "ransomware": 0, "hibp": 0, "item_105": 0}
    for r in records:
        st = r.get("source_type", "sec")
        c[st] = c.get(st, 0) + 1
        if r.get("is_item_105"):
            c["item_105"] += 1
    return c


def refresh_guarded(**kw):
    if not _REFRESH_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "A refresh is already running."}
    try:
        if REFRESH_COOLDOWN:
            wait = REFRESH_COOLDOWN - (time.time() - _STATE["last_pull"])
            if wait > 0:
                # Serve the existing data instead of re-pulling; tell the client.
                recs = load_store()
                c = _source_counts(recs)
                return {"ok": True, "added": 0, "updated": 0,
                        "total": len(recs), "item_105_total": c["item_105"],
                        "counts": c, "cooldown": int(wait),
                        "note": "Showing cached data (refresh cooldown %ds)." % int(wait),
                        "last_updated": _STATE["last_updated"]}
        result = refresh(**kw)
        _STATE["last_pull"] = time.time()
        return result
    finally:
        _REFRESH_LOCK.release()


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/breaches":
            records = load_store()
            counts = _source_counts(records)
            return self._json({"breaches": records,
                               "last_updated": _STATE["last_updated"],
                               "total": len(records),
                               "item_105_count": counts["item_105"],
                               "counts": counts})
        if path == "/api/config":
            return self._json({"site_name": SITE_NAME})
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] == "/api/refresh":
            try:
                return self._json(refresh_guarded())
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        self.send_error(404)

    def log_message(self, fmt, *args):
        try:
            line = args[0] if args else ""
            if isinstance(line, str) and "/api/" in line:
                super().log_message(fmt, *args)
        except Exception:
            pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()


def _auto_refresh_loop(hours):
    interval = max(0.25, hours) * 3600
    while True:
        time.sleep(interval)
        try:
            refresh_guarded()
        except Exception as e:
            print("[auto-refresh] failed: %s" % e)


def main():
    if "--refresh-once" in sys.argv:
        print(json.dumps(refresh(), indent=2))
        return
    if "--backfill" in sys.argv:
        print(json.dumps(refresh(backfill=True), indent=2))
        return

    # First run with an empty store: do a quick recent pull so the page isn't blank.
    if not load_store():
        print("Empty store — pulling the last 45 days from EDGAR...")
        try:
            refresh()
        except Exception as e:
            print("Initial pull failed (start the server and click Refresh): %s" % e)

    env_port = os.environ.get("PORT")
    port = int(env_port) if env_port else 8770
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    host = os.environ.get("HOST") or ("0.0.0.0" if env_port else "127.0.0.1")

    hours = float(os.environ.get("AUTO_REFRESH_HOURS", "3"))
    if hours > 0:
        threading.Thread(target=_auto_refresh_loop, args=(hours,), daemon=True).start()
        print("Auto-refresh every %g h" % hours)

    httpd = ThreadingHTTPServer((host, port), Handler)
    where = "http://localhost:%d" % port if host == "127.0.0.1" else "%s:%d" % (host, port)
    print("BreachRadar running on %s  (%d filings)" % (where, len(load_store())))
    print("Live refresh endpoint: POST /api/refresh")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
