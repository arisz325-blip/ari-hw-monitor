# Hot Wheels Stock Monitor

Watches the **Mattel Creations US and AU** storefronts for collector Hot Wheels,
checks every hour for free on GitHub Actions, pushes a **native Android
notification** the moment something drops or restocks, and keeps a phone-friendly
dashboard of everything it's tracking.

- 🆕 new listings
- 🔥 sold out → back in stock
- 📅 upcoming drops and pre-orders (with drop time where the site publishes it)
- 💰 price changes
- ⏱ how fast each car sold out last time

---

## Setup (about 10 minutes, all free)

### 1. Get the notification app

Install **ntfy** on your phone — [Play Store](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
or [F-Droid](https://f-droid.org/packages/io.heckel.ntfy/).

Open it, tap **+**, and subscribe to a topic. **Make up a long random name** —
anyone who knows your topic name can read it, so `hotwheels` is a bad choice and
`hw-ari-7f3k9qz2x` is a good one. Write it down.

### 2. Put this repo on GitHub

Create a new **private** repository, then upload every file in this folder
(keeping the structure — `.github/workflows/`, `docs/`, `tests/`).

If you have git locally:

```bash
git init && git add . && git commit -m "Hot Wheels monitor"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/hot-wheels-monitor.git
git push -u origin main
```

### 3. Tell it your ntfy topic

In the repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `NTFY_TOPIC` | the topic name from step 1 |

(`NTFY_SERVER` and `NTFY_TOKEN` are optional — only needed if you self-host ntfy
or use a protected topic.)

### 4. Turn on the dashboard

**Settings → Pages → Source: Deploy from a branch → `main` / `/docs`** → Save.

After a minute your dashboard is live at
`https://YOUR-USERNAME.github.io/hot-wheels-monitor/`.
Open it on your phone and use Chrome's **Add to Home screen** — it behaves like an app.

> Private repos need GitHub Pro for Pages. On the free plan either make the repo
> public (there's nothing secret in it — but then pick an unguessable ntfy topic,
> and consider `NTFY_TOKEN` too), or just use the notifications and skip Pages.

### 5. Kick it off

**Actions → Hot Wheels stock check → Run workflow**. Run it three times:

1. `notify-test` — a test notification should hit your phone within seconds.
2. `self-test` — prints what it found on the live site. Should list products and no warnings.
3. `normal` — records the baseline.

**The first normal run deliberately sends no alerts.** It has nothing to compare
against yet, so it just records what's there. From the next hour on, you only
hear about actual changes.

---

## Tuning what you get told about

Everything lives in **`config.json`**.

**Watch every Hot Wheels item, not just collector lines:**

```json
"filters": { "mode": "all" }
```

**Watch specific cars only** — set `mode` to `collector` and put your own words in
`include_keywords`, e.g. `["rlc", "elite 64", "porsche", "datsun", "skyline"]`.
Matching is case-insensitive across title, tags and product type.

**Quieter or louder alerts** — `notify` toggles each event type. Turning on
`sold_out` tells you when things sell out (useful for gauging demand, noisy if
you'd rather not know what you missed).

**Only watch one region** — set `"enabled": false` on `US` or `AU` under `regions`.

**Check more often** — edit the `cron` in `.github/workflows/check.yml`.
`"*/30 * * * *"` is every 30 minutes. Bear in mind GitHub's scheduler is
best-effort on free runners and can run late under load, and that more frequent
polling makes you more conspicuous to Mattel's bot protection. Hourly is the
sweet spot.

---

## About stock counts

Mattel does not publish inventory numbers anywhere on the storefront. What the
site actually exposes is status (In Stock / Sold Out / Pre-Order / Coming Soon),
price, and sometimes a drop time — that's what this reports, and it never invents
a number.

There is a known Shopify technique for reading the real count: add an absurd
quantity to the cart and read the ceiling back out of the 422 error. It's
implemented here in `probe_stock()` but **switched off by default**, because
Mattel's own `robots.txt` disallows `/cart/` and `/cart.js` to automated clients.
An hourly runner hitting that endpoint is exactly the traffic they've asked bots
not to send, and the realistic downside isn't a telling-off — it's their bot
protection quietly blocking the runner so the whole monitor stops working without
you noticing.

If you want it anyway, in `config.json`:

```json
"stock_probe": { "enabled": true, "delay_seconds": 4, "max_products_per_run": 8 }
```

It only probes items already showing as in stock, caps how many it touches per
run, and empties the cart after each probe. When it gets a number it shows up on
the dashboard and in the notification as "N left".

As a middle ground, the dashboard already tracks **sell-out speed** — "sold out
after 40 min" tells you most of what a stock number would, without touching the
cart.

---

## When something breaks

**Run `self-test` from the Actions tab first.** It prints exactly what the
checker sees and is the fastest way to tell "nothing changed" from "the site
moved".

| Symptom | Likely cause |
|---|---|
| `could not load /collections/... on any known path prefix` | Mattel moved the AU storefront. Update `path_prefix` under `regions.AU` in `config.json`. |
| `no product links found on the collection page` | The page markup changed, or you're being served a bot-check page. |
| `No products matched in any region` | Usually your `include_keywords` are too narrow — try `"mode": "all"` to confirm the scraper still works. |
| Notifications stopped | Check `NTFY_TOPIC` is still set, and that ntfy isn't being battery-optimised on your phone (Android Settings → Apps → ntfy → Battery → Unrestricted). |
| Workflow stopped running | GitHub disables scheduled workflows in repos with ~60 days of no activity. Push any commit, or hit **Run workflow** manually, to re-arm it. |

The dashboard shows a yellow banner whenever the last run logged a warning, so
you'll see a silent breakage rather than just assuming Hot Wheels got boring.

---

## Running it on your own machine

```bash
pip install -r requirements.txt
export NTFY_TOPIC=your-topic
python checker.py --self-test   # what does the site look like right now?
python checker.py --dry-run     # full check, notifies nothing
python checker.py               # for real
```

## Tests

The scraper is covered by an offline test suite that runs against a local mock
Shopify store — no network, no hitting Mattel:

```bash
python -m tests.test_checker
```

22 checks covering region handling, filters, restock/new/drop/price detection,
sell-out timing, first-run silence, and graceful degradation when the site
structure changes.

---

## What's in here

```
checker.py                    the scraper, differ and notifier
config.json                   everything you'd want to tune
state.json                    generated — what it saw last time
docs/index.html               the dashboard
docs/data.json                generated — what the dashboard reads
.github/workflows/check.yml   the hourly schedule
tests/                        offline test suite + mock storefront
```

Be a good citizen with this: it's polite by default (1.5s between requests,
retries with backoff, hourly), and it's worth keeping it that way.
