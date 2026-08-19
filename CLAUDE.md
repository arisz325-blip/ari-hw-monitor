# Hot Wheels stock monitor — working notes

Read this before changing anything. It records things that were expensive to
learn and are not obvious from the code.

## What this is

Ari collects Hot Wheels. This watches **Mattel Creations US and AU** for
collector cars (RLC / Elite 64), checks hourly on free GitHub Actions, pushes a
native Android notification via **ntfy.sh** when something drops or restocks,
and publishes a phone-friendly dashboard to GitHub Pages.

Live and working since 2026-08-19.

- Repo: `github.com/arisz325-blip/ari-hw-monitor` (public — Pages needs it)
- Dashboard: `https://arisz325-blip.github.io/ari-hw-monitor/`
- Push to `main` **is** the deploy. Actions runs the checker and commits
  `state.json` + `docs/data.json` back to the repo.

```
checker.py                    scraper + differ + notifier
config.json                   all tuning: regions, filters, notify toggles, probe flag
state.json                    generated — last seen state, the diff baseline
docs/index.html               dashboard
docs/data.json                generated — what the dashboard reads
.github/workflows/check.yml   hourly cron; writes diagnostics to the run Summary
tests/                        offline suite, 29 checks, mock storefront + mock ntfy
```

## Facts that cost real debugging time

**The AU store is a separate storefront, not a locale.**
`au.creations.mattel.com` has its own catalogue, its own product handles, and
its own stock. A US handle returns 404 there. `creations.mattel.com/en-au`
looks like it works but silently serves the **US catalogue with AUD prices** —
it produced 17 phantom AU items that did not exist in Australia. Do not
"simplify" the AU region back to a locale path.

**Mattel's bulk product API is off.** `/collections/<x>/products.json` returns
`{"products":[]}`. Per-product `/products/{handle}.js` works and is the
structured source; the collection HTML supplies the status badge (it is the only
place "Coming Soon" / "Pre-Order" appear).

**robots.txt matters here.** It disallows `/cart/` and `/cart.js` to automated
clients, and disallows filter/sort query params. It also advertises an official
agent endpoint at `creations.mattel.com/api/ucp/mcp` (POST/SSE — 404s on GET).
That endpoint is unexplored and is the sanctioned way to get catalogue data.

**Mattel publishes no inventory numbers.** Status, price and drop time are all
the storefront exposes. Never invent a count.

**HTTP headers are latin-1.** An emoji in ntfy's `Title` header makes `requests`
raise `UnicodeEncodeError`, which `send_ntfy` swallows — every notification dies
silently while the scraper tests stay green. Emoji belong in the `Tags` header
(ntfy renders them into the title anyway). `header_safe()` guards this. Mattel
product titles also routinely contain `’` and `–`, neither of which is latin-1.

**An unset GitHub secret is an empty string, not an absent env var.**
`os.environ.get("NTFY_SERVER", "https://ntfy.sh")` returns `""` when the secret
was never created, and the URL loses its scheme. Use `or`, never a `get()`
default, for any optional secret.

**Test the notifier, not just the scraper.** Two of the four production bugs
lived in the notification path, which originally had no test at all. The suite
passed through both.

## Running things

```bash
python -m tests.test_checker        # 29 offline checks, no network, ~30s
python checker.py --self-test       # what does the live site look like right now?
python checker.py --dry-run         # full check, notifies nothing, writes nothing
NTFY_TOPIC=... python checker.py --notify-test   # one test notification
```

In Actions: **Run workflow** with mode `normal` / `dry-run` / `self-test` /
`notify-test`. Every run writes a **Diagnostics** and **Checker output** block
to the run Summary page — check there first, not the raw logs.

Deleting `state.json` forces a baseline rebuild: the next `normal` run records
everything and deliberately sends no alerts.

## Conventions

- Be polite to Mattel: 1.5s between requests, retries with backoff, hourly.
  Do not raise the frequency without a reason.
- `config.json` is the knob panel. Prefer adding a config option over hardcoding.
- Any change to `send_ntfy` or the scraper needs a test in `tests/`.
- Ari is in **Australia/Sydney (UTC+10)**; the workflow cron is UTC.
- **Ari communicates in Chinese — reply in Chinese.** He is not a developer:
  give exact click paths, not just concepts, and one instruction at a time.

## Open items

1. **Membership item leaks the filter.** AU returns "1-Year RLC Digital
   Membership". Add `membership` and `digital` to `exclude_keywords`.
2. **Stock counts.** `probe_stock()` implements the Shopify cart-ceiling trick
   (POST `/cart/add.js` with quantity 99999, read the number out of the 422).
   It is `enabled: false` on purpose — that path is robots-disallowed, and if
   Mattel's bot protection blocks the runner the *whole monitor* dies, not just
   the probe. Never enable it without Ari explicitly asking. Untested live.
3. **`drop_time` is never populated** in real runs — `DROP_TIME_RE` has not
   matched Mattel's actual countdown markup. Needs a look at live product HTML.
4. **The UCP/MCP endpoint** is the interesting unexplored lead for real stock
   data, legitimately. Needs a POST/SSE client.
5. **The ntfy topic is guessable** — `hw-ari-7f3k9qz2x` is the example name from
   a public README. Rotating it means changing the phone subscription and the
   `NTFY_TOPIC` repo secret together.
