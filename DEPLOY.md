# Deploying BreachRadar online

You have two ways to put this on the internet. The difference is **what the
⟳ Refresh button does** — because the data sources (SEC EDGAR, the state AG
sites, ransomware.live) **do not allow cross-origin browser requests**, a
visitor's browser cannot pull them directly. Only a server can.

| | Live refresh for *anyone*? | Cost | Setup |
|---|---|---|---|
| **A. Render (live server)** | ✅ Yes — Refresh pulls fresh data server-side | Free tier works | ~3 min |
| **B. GitHub Pages (static)** | ⚠️ No live pull — shows data auto-refreshed by a scheduled Action; Refresh just reloads it | Free, forever | ~2 min |

Most people want **A** for "anyone can press Refresh and it actually pulls."
**B** is the zero-server option that still stays current on its own.

---

## A. Render — a live site where anyone can Refresh

1. Push this repo to GitHub (see below).
2. On [render.com](https://render.com): **New → Blueprint** → connect this repo.
   It reads `render.yaml` automatically. (Or **New → Web Service**, Build
   `pip install -r requirements.txt`, Start `python breach_server.py`.)
3. Deploy. Render gives you a public URL like `https://breachradar.onrender.com`.
   The ⟳ Refresh button pulls live from every source for any visitor.

`REFRESH_COOLDOWN=120` (set in `render.yaml`) means real pulls happen at most
once every 2 minutes, so visitors mashing Refresh can't hammer the upstreams —
in between, it serves the cached data instantly. A 6-hourly background refresh
keeps it current with zero clicks. Free Render instances sleep when idle and the
data file resets on redeploy; the background/scheduled refresh repopulates it.

## B. GitHub Pages — free static site, auto-refreshed

1. Push this repo to GitHub.
2. Repo **Settings → Pages** → Source: deploy from `main` (root). Your app is at
   `https://<you>.github.io/<repo>/`.
3. The included Action **`.github/workflows/refresh.yml`** runs daily (and on the
   manual **Run workflow** button in the **Actions** tab), pulls all sources, and
   commits a fresh `breaches.json`. Pages redeploys automatically, so the site
   stays current with no server.

On Pages the app reads the committed `breaches.json` directly (it falls back from
the missing `/api`), so it works fully — search, tabs, filters, CSV. The Refresh
button reloads the latest committed data and says so.

> Want truly on-demand pulls *and* free static hosting? Do both: host the Render
> server (A) and point visitors there, while Pages stays as a $0 mirror.

---

## Pushing to GitHub (first time)

`gh` is not installed on this machine, so create the empty repo in the browser:

1. Go to <https://github.com/new>, name it e.g. **breachradar**, leave it empty
   (no README/.gitignore/license), and create it.
2. Then, from this folder:

```powershell
cd "C:\Users\DSamson\.claude\BreachRadar"
git init
git add .
git commit -m "BreachRadar — standalone breach-disclosure dashboard"
git branch -M main
git remote add origin https://github.com/<you>/breachradar.git
git push -u origin main
```

(If you later install the GitHub CLI, `gh repo create breachradar --public
--source=. --push` does steps 1–2 in one command.)

## Be a good SEC citizen
Set `BREACH_UA` to `"YourApp your-email@example.com"` (env var on Render, or in
the workflow file). SEC EDGAR asks every client to identify itself.
