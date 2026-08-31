"""End-to-end tests for the Hot Wheels monitor, run against a local mock store.

    python -m tests.test_checker
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import mock_store  # noqa: E402
import checker as checker_mod  # noqa: E402
from checker import (  # noqa: E402
    Fetcher, header_safe, melbourne_time, parse_human_drop_time,
)

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{f' — {detail}' if detail and not condition else ''}")
    if not condition:
        FAILURES.append(label)


BASE_CATALOG = {
    "hot-wheels-rlc-1985-audi-quattro": {
        "title": "Hot Wheels RLC Exclusive 1985 Audi Sport quattro S1",
        "available": True, "badge": "Add to Cart", "tags": ["RLC", "Red Line Club"],
    },
    "hot-wheels-elite-64-mazda-rx7": {
        "title": "Hot Wheels Elite 64 Mazda RX-7 Liberty Walk",
        "available": False, "badge": "Sold Out", "tags": ["Elite 64"],
    },
    "hot-wheels-rlc-hoodie": {
        "title": "Hot Wheels RLC Logo Hoodie",
        "available": True, "badge": "Add to Cart", "tags": ["RLC", "Apparel"],
    },
    "hot-wheels-mainline-5-pack": {
        "title": "Hot Wheels Mainline 5-Pack",
        "available": True, "badge": "Add to Cart", "tags": ["Mainline"],
    },
}


def run_checker(workdir: Path, base_url: str, extra: list[str] | None = None) -> str:
    env = dict(os.environ, BASE_OVERRIDE=base_url, NTFY_TOPIC="", DELAY_OVERRIDE="0")
    result = subprocess.run(
        [sys.executable, str(workdir / "checker.py"), "--dry-run", *(extra or [])],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"checker exited {result.returncode}")
    return result.stdout


def run_watchlist_check(workdir: Path, base_url: str) -> str:
    env = dict(os.environ, BASE_OVERRIDE=base_url, NTFY_TOPIC="", DELAY_OVERRIDE="0")
    result = subprocess.run(
        [sys.executable, str(workdir / "checker.py"), "--watchlist-check"],
        cwd=workdir, env=env, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"checker exited {result.returncode}")
    return result.stdout


def persist_state(workdir: Path, base_url: str) -> None:
    """--dry-run does not save state, so do a real (notification-free) run."""
    env = dict(os.environ, BASE_OVERRIDE=base_url, NTFY_TOPIC="", DELAY_OVERRIDE="0")
    subprocess.run([sys.executable, str(workdir / "checker.py")],
                   cwd=workdir, env=env, capture_output=True, text=True, timeout=180, check=True)


def main() -> int:
    mock_store.set_catalog(json.loads(json.dumps(BASE_CATALOG)))
    server, base_url = mock_store.start()
    print(f"Mock store on {base_url}\n")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "monitor"
        workdir.mkdir()
        for name in ("checker.py", "config.json"):
            (workdir / name).write_bytes((ROOT / name).read_bytes())
        (workdir / "docs").mkdir()

        print("Run 1 — baseline")
        persist_state(workdir, base_url)
        state = json.loads((workdir / "state.json").read_text())
        data = json.loads((workdir / "docs" / "data.json").read_text())

        keys = set(state["items"])
        check("collector items tracked in both regions", len(keys) == 4,
              f"got {len(keys)}: {sorted(keys)}")
        check("apparel excluded by filters",
              not any("hoodie" in k for k in keys), f"{sorted(keys)}")
        check("non-collector mainline excluded",
              not any("mainline" in k for k in keys), f"{sorted(keys)}")
        check("US and AU both scanned",
              {k.split(':')[0] for k in keys} == {"US", "AU"},
              f"{ {k.split(':')[0] for k in keys} }")
        check("first run sends no alerts", data["recent_events"] == [],
              f"{data['recent_events']}")
        check("AU price uses AUD",
              any(r.get("currency") == "AUD" and "49.99" in (r.get("price") or "")
                  for r in data["items"]),
              f"{[(r['region'], r.get('price')) for r in data['items']]}")
        check("sold-out item detected",
              any(r["status"] == "sold_out" for r in data["items"]),
              f"{[(r['title'], r['status']) for r in data['items']]}")
        check("in-stock item detected",
              any(r["status"] == "in_stock" for r in data["items"]))
        check("no parse warnings", data["warnings"] == [], f"{data['warnings']}")

        print("\nRun 2 — restock + brand-new listing + upcoming drop")
        catalog = json.loads(json.dumps(BASE_CATALOG))
        catalog["hot-wheels-elite-64-mazda-rx7"].update(available=True, badge="Add to Cart")
        catalog["hot-wheels-rlc-1989-porsche-944"] = {
            "title": "Hot Wheels RLC Exclusive 1989 Porsche 944 Turbo",
            "available": False, "badge": "Coming Soon", "tags": ["RLC"],
            "drop_time": "2026-09-01T17:00:00Z",
        }
        mock_store.set_catalog(catalog)
        persist_state(workdir, base_url)
        data = json.loads((workdir / "docs" / "data.json").read_text())
        events = data["recent_events"]
        types = [e["type"] for e in events]

        check("restock event fired", "restock" in types, f"{types}")
        check("new-listing event fired", "new" in types, f"{types}")
        check("restock names the right car",
              any(e["type"] == "restock" and "RX-7" in e["title"] for e in events),
              f"{[(e['type'], e['title']) for e in events]}")
        check("events raised for both regions",
              len({e["region"] for e in events}) == 2,
              f"{ {e['region'] for e in events} }")
        # Regression: the dashboard used to list every new event twice,
        # because run() prepended events to state and write_dashboard_data
        # then prepended the same list again on top of it.
        # region matters: the same car legitimately fires one event per store.
        seen_events = [(e["at"], e["type"], e["region"], e["title"], e["detail"])
                       for e in events]
        check("each event appears once in the dashboard feed",
              len(seen_events) == len(set(seen_events)),
              f"{len(seen_events)} entries, {len(set(seen_events))} unique")
        state_after = json.loads((workdir / "state.json").read_text())
        check("the dashboard feed matches the persisted activity log",
              data["recent_events"] == state_after.get("recent_events", [])[:40],
              f"data={len(data['recent_events'])} state={len(state_after.get('recent_events', []))}")
        check("coming-soon status captured",
              any(r["status"] == "coming_soon" for r in data["items"]),
              f"{[(r['title'], r['status']) for r in data['items']]}")
        check("drop time parsed from card",
              any(r.get("drop_time") for r in data["items"]),
              f"{[(r['title'], r.get('drop_time')) for r in data['items']]}")

        print("\nRun 3 — sell-out timing")
        catalog = json.loads(json.dumps(catalog))
        catalog["hot-wheels-rlc-1985-audi-quattro"].update(available=False, badge="Sold Out")
        mock_store.set_catalog(catalog)
        persist_state(workdir, base_url)
        state = json.loads((workdir / "state.json").read_text())
        audi = [r for k, r in state["items"].items() if "audi" in k]

        check("sell-out duration recorded",
              all(r.get("last_sellout_duration") for r in audi),
              f"{[(r['title'], r.get('last_sellout_duration')) for r in audi]}")
        check("sold-out timestamp recorded", all(r.get("sold_out_at") for r in audi))
        check("history trail kept", all(len(r.get("history", [])) >= 2 for r in audi),
              f"{[len(r.get('history', [])) for r in audi]}")

        print("\nRun 4 — price change")
        out = run_checker(workdir, base_url)
        check("stable run is quiet", "0 event(s)" in out, out.strip().splitlines()[-1])

        print("\nRun 5 — self-test mode")
        out = run_checker(workdir, base_url, ["--self-test"])
        report = json.loads(out[out.index("{"):])
        # 3 collector cars (audi, rx-7, porsche) across 2 regions; hoodie and
        # mainline 5-pack are filtered out.
        check("self-test reports products", report["products_found"] == 6,
              f"{report['products_found']}")
        check("self-test reports no warnings", report["warnings"] == [],
              f"{report['warnings']}")

        print("\nRun 6 — site structure changed (regression guard)")
        broken = Path(tmp) / "broken"
        broken.mkdir()
        for name in ("checker.py", "config.json"):
            (broken / name).write_bytes((ROOT / name).read_bytes())
        (broken / "docs").mkdir()
        mock_store.set_catalog({})
        out = run_checker(broken, base_url)
        broken_data = json.loads((broken / "docs" / "data.json").read_text())
        check("empty catalogue produces a warning, not a crash",
              len(broken_data["warnings"]) > 0, f"{broken_data['warnings']}")

        print("\nRun 7 — stock probe only touches watchlisted cars")
        catalog = json.loads(json.dumps(BASE_CATALOG))
        catalog["hot-wheels-rlc-1985-audi-quattro"]["stock"] = 3
        mock_store.set_catalog(catalog)

        cfg_path = workdir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["stock_probe"].update(enabled=True, delay_seconds=0,
                                   watchlist=["US:hot-wheels-rlc-1985-audi-quattro"])
        cfg_path.write_text(json.dumps(cfg))

        persist_state(workdir, base_url)
        data = json.loads((workdir / "docs" / "data.json").read_text())
        by_key = {r["key"]: r for r in data["items"]}

        check("watchlisted car gets a real stock number",
              by_key.get("US:hot-wheels-rlc-1985-audi-quattro", {}).get("stock") == 3,
              f"{by_key.get('US:hot-wheels-rlc-1985-audi-quattro', {}).get('stock')}")
        check("same car in a non-watchlisted region is left alone",
              by_key.get("AU:hot-wheels-rlc-1985-audi-quattro", {}).get("stock") is None,
              f"{by_key.get('AU:hot-wheels-rlc-1985-audi-quattro', {}).get('stock')}")
        check("dashboard data exposes the configured watchlist",
              data.get("watchlist") == ["US:hot-wheels-rlc-1985-audi-quattro"],
              f"{data.get('watchlist')}")

        cfg["stock_probe"].update(enabled=False, watchlist=[])
        cfg_path.write_text(json.dumps(cfg))

        print("\nRun 8 — per-region collections + cross-collection de-dup + sitemap fallback")
        overlap_catalog = {
            "hot-wheels-rlc-car-a": {
                "title": "Hot Wheels RLC Car A", "available": True,
                "badge": "Add to Cart", "tags": ["RLC"],
            },
            "hot-wheels-rlc-car-b": {
                "title": "Hot Wheels RLC Car B", "available": True,
                "badge": "Add to Cart", "tags": ["RLC"],
            },
            "hot-wheels-rlc-car-c": {
                "title": "Hot Wheels RLC Car C", "available": True,
                "badge": "Add to Cart", "tags": ["RLC"],
            },
        }
        mock_store.set_catalog(overlap_catalog)
        # car-b is listed on both collections — this is what used to get
        # double-fetched and double-counted before scan_region tracked
        # seen_handles across collections.
        mock_store.set_collections({
            "hot-wheels": ["hot-wheels-rlc-car-a", "hot-wheels-rlc-car-b"],
            "cars-vehicles": ["hot-wheels-rlc-car-b", "hot-wheels-rlc-car-c"],
        })

        overlap_dir = Path(tmp) / "overlap"
        overlap_dir.mkdir()
        (overlap_dir / "checker.py").write_bytes((ROOT / "checker.py").read_bytes())
        (overlap_dir / "docs").mkdir()
        overlap_cfg = json.loads((ROOT / "config.json").read_text())
        overlap_cfg["stock_probe"].update(enabled=False, watchlist=[])
        (overlap_dir / "config.json").write_text(json.dumps(overlap_cfg))

        persist_state(overlap_dir, base_url)
        data = json.loads((overlap_dir / "docs" / "data.json").read_text())
        us_handles = {r["handle"] for r in data["items"] if r["key"].startswith("US:")}
        au_handles = {r["handle"] for r in data["items"] if r["key"].startswith("AU:")}
        all_three = {"hot-wheels-rlc-car-a", "hot-wheels-rlc-car-b", "hot-wheels-rlc-car-c"}

        check("US de-dupes a car listed in two collections (3 unique, not 4)",
              us_handles == all_three, f"{us_handles}")
        check("AU's collections override kept it off the (nonexistent) cars-vehicles collection",
              data["warnings"] == [], f"{data['warnings']}")
        check("AU still finds car-c via the sitemap despite no cars-vehicles collection there",
              au_handles == all_three, f"{au_handles}")

        print("\nRun 9 — sold-out watchlist entries are auto-removed")
        mock_store.set_collections({})  # back to the Run 8 overrides' default
        autoclean_catalog = json.loads(json.dumps(BASE_CATALOG))
        autoclean_catalog["hot-wheels-rlc-1985-audi-quattro"]["stock"] = 5
        mock_store.set_catalog(autoclean_catalog)

        autoclean_dir = Path(tmp) / "autoclean"
        autoclean_dir.mkdir()
        (autoclean_dir / "checker.py").write_bytes((ROOT / "checker.py").read_bytes())
        (autoclean_dir / "docs").mkdir()
        ac_cfg = json.loads((ROOT / "config.json").read_text())
        ac_cfg["stock_probe"].update(enabled=True, delay_seconds=0,
                                      watchlist=["US:hot-wheels-rlc-1985-audi-quattro"])
        ac_cfg_path = autoclean_dir / "config.json"
        ac_cfg_path.write_text(json.dumps(ac_cfg))

        persist_state(autoclean_dir, base_url)
        data = json.loads((autoclean_dir / "docs" / "data.json").read_text())
        check("watchlist survives while the car is still in stock",
              data.get("watchlist") == ["US:hot-wheels-rlc-1985-audi-quattro"],
              f"{data.get('watchlist')}")

        autoclean_catalog["hot-wheels-rlc-1985-audi-quattro"].update(
            available=False, badge="Sold Out")
        mock_store.set_catalog(autoclean_catalog)
        persist_state(autoclean_dir, base_url)
        data = json.loads((autoclean_dir / "docs" / "data.json").read_text())
        saved_cfg = json.loads(ac_cfg_path.read_text())

        check("sold-out entry drops off the dashboard's watchlist",
              data.get("watchlist") == [], f"{data.get('watchlist')}")
        check("sold-out entry is actually removed from config.json on disk",
              saved_cfg["stock_probe"]["watchlist"] == [],
              f"{saved_cfg['stock_probe']['watchlist']}")

        print("\nRun 10 — unlaunched cars read as upcoming, not sold out")
        check("parses the US store's wording",
              parse_human_drop_time('Launches August 20, 2026 9:00 am PT')
              == "2026-08-20T16:00:00+00:00")
        check("parses the AU store's wording (ordinal day, no minutes, AEST)",
              parse_human_drop_time('Launches 20th August 2026 9am AEST')
              == "2026-08-19T23:00:00+00:00")
        check("notification times render in Melbourne time, winter (AEST, UTC+10)",
              melbourne_time("2026-08-20T16:00:00+00:00") == "21 Aug 2026, 02:00 AM AEST",
              melbourne_time("2026-08-20T16:00:00+00:00"))
        check("notification times render in Melbourne time, summer (AEDT, UTC+11)",
              melbourne_time("2026-01-05T03:00:00+00:00") == "05 Jan 2026, 02:00 PM AEDT",
              melbourne_time("2026-01-05T03:00:00+00:00"))

        # Every region must pin its market via ?country=, or Shopify prices
        # by runner geolocation — that's what produced CAD figures labeled
        # USD and a stream of bogus price-change alerts (see CLAUDE.md).
        shipped_cfg = json.loads((ROOT / "config.json").read_text())
        for region_name, region in shipped_cfg["regions"].items():
            country = (region.get("query") or {}).get("country")
            check(f"config pins a country for {region_name}, so pricing can't follow runner geo",
                  bool(country), f"{region.get('query')}")

        us_url = checker_mod.region_url(shipped_cfg["regions"]["US"], "/products/x.js")
        au_url = checker_mod.region_url(shipped_cfg["regions"]["AU"], "/products/x.js")
        check("US product URLs carry country=US", "country=US" in us_url, us_url)
        check("AU product URLs carry country=AU", "country=AU" in au_url, au_url)

        fetcher_probe = Fetcher({"request_delay_seconds": 0})
        fetcher_probe.pin_region({"base": "https://creations.mattel.com", "currency": "USD"})
        us_lang = fetcher_probe.session.headers["Accept-Language"]
        fetcher_probe.pin_region({"base": "https://au.creations.mattel.com", "currency": "AUD"})
        au_lang = fetcher_probe.session.headers["Accept-Language"]
        check("pin_region sets US Accept-Language, not the old hardcoded en-AU",
              us_lang.startswith("en-US"), f"{us_lang}")
        check("pin_region switches Accept-Language when re-pinned for AU",
              au_lang.startswith("en-AU"), f"{au_lang}")

        # Anything header_safe returns must survive latin-1 encoding, or
        # requests raises and send_ntfy swallows it — killing every
        # notification while the scraper tests stay green.
        nasty = "Hot Wheels RLC ’81 Toyota – Set 4 \U0001f525…"
        try:
            header_safe(nasty).encode("latin-1")
            encodable = True
        except UnicodeEncodeError:
            encodable = False
        check("header_safe output is latin-1 encodable (emoji, curly quotes, en dash)",
              encodable, f"{header_safe(nasty)!r}")
        check("header_safe keeps the text readable rather than mangling it",
              header_safe("RLC ’81 Toyota – Set 4").startswith("RLC '81 Toyota - Set 4"),
              f"{header_safe(chr(39) + '81')!r}")
        check("plain ASCII passes through header_safe untouched",
              header_safe("Back in stock (US)") == "Back in stock (US)")

        future = (datetime.now() + timedelta(days=200)).strftime("%B %d, %Y %I:%M %p") + " PT"
        past = "January 1, 2020 9:00 am PT"
        drop_catalog = {
            "hot-wheels-rlc-not-launched": {
                "title": "Hot Wheels RLC Not Launched Yet", "available": False,
                "badge": "Details", "tags": ["RLC"], "launches": future,
            },
            "hot-wheels-rlc-long-gone": {
                "title": "Hot Wheels RLC Long Gone", "available": False,
                "badge": "Details", "tags": ["RLC"], "launches": past,
            },
            "hot-wheels-rlc-no-countdown": {
                "title": "Hot Wheels RLC No Countdown Block", "available": False,
                "badge": "Details", "tags": ["RLC"],
            },
        }
        mock_store.set_catalog(drop_catalog)

        drop_dir = Path(tmp) / "drop"
        drop_dir.mkdir()
        (drop_dir / "checker.py").write_bytes((ROOT / "checker.py").read_bytes())
        (drop_dir / "docs").mkdir()
        (drop_dir / "config.json").write_bytes((ROOT / "config.json").read_bytes())

        out = run_checker(drop_dir, base_url, ["--self-test"])
        report = json.loads(out[out.index("{"):])
        by_handle = {s["handle"]: s for s in report["sample"]}
        # self-test only samples the first 3 items, which this 3-item
        # catalog fits exactly (both regions see the same 3, but sample
        # dedupes by insertion order across US then AU handles).

        check("a countdown that hasn't happened yet reads as coming_soon",
              by_handle.get("hot-wheels-rlc-not-launched", {}).get("status") == "coming_soon",
              f"{by_handle.get('hot-wheels-rlc-not-launched')}")
        check("its drop_time is populated and in the future",
              bool(by_handle.get("hot-wheels-rlc-not-launched", {}).get("drop_time")),
              f"{by_handle.get('hot-wheels-rlc-not-launched', {}).get('drop_time')}")
        check("a countdown that already passed still reads as sold_out",
              by_handle.get("hot-wheels-rlc-long-gone", {}).get("status") == "sold_out",
              f"{by_handle.get('hot-wheels-rlc-long-gone')}")
        check("no countdown block at all still reads as sold_out",
              by_handle.get("hot-wheels-rlc-no-countdown", {}).get("status") == "sold_out",
              f"{by_handle.get('hot-wheels-rlc-no-countdown')}")

        print("\nRun 11 — sitemap discovers cars no collection lists yet")
        sitemap_catalog = {
            "hot-wheels-rlc-listed": {
                "title": "Hot Wheels RLC Listed Car", "available": True,
                "badge": "Add to Cart", "tags": ["RLC"],
            },
            "hot-wheels-rlc-unlisted": {
                "title": "Hot Wheels RLC Unlisted Car", "available": True,
                "badge": "Add to Cart", "tags": ["RLC"],
            },
            "mega-blocks-irrelevant": {
                "title": "MEGA Blocks Something Irrelevant", "available": True,
                "badge": "Add to Cart", "tags": [],
            },
            "hot-wheels-rlc-ancient": {
                "title": "Hot Wheels RLC Ancient Discontinued Car", "available": False,
                "badge": "Sold Out", "tags": ["RLC"], "published_at": "2020-01-01T00:00:00Z",
            },
        }
        mock_store.set_catalog(sitemap_catalog)
        # Only the "listed" car is on the collection card; "unlisted" and
        # "irrelevant" exist (reachable by direct URL, in the sitemap) but
        # aren't in any collection listing — exactly today's real bug.
        mock_store.set_collections({"hot-wheels": ["hot-wheels-rlc-listed"]})

        sitemap_dir = Path(tmp) / "sitemap"
        sitemap_dir.mkdir()
        (sitemap_dir / "checker.py").write_bytes((ROOT / "checker.py").read_bytes())
        (sitemap_dir / "docs").mkdir()
        sm_cfg = json.loads((ROOT / "config.json").read_text())
        sm_cfg["collections"] = ["hot-wheels"]
        sm_cfg["regions"]["AU"]["collections"] = ["hot-wheels"]
        sm_cfg["stock_probe"].update(enabled=False, watchlist=[])
        (sitemap_dir / "config.json").write_text(json.dumps(sm_cfg))

        persist_state(sitemap_dir, base_url)
        data = json.loads((sitemap_dir / "docs" / "data.json").read_text())
        handles_found = {r["handle"] for r in data["items"]}

        check("collection-listed car is tracked",
              "hot-wheels-rlc-listed" in handles_found, f"{handles_found}")
        check("an old, unavailable, sitemap-only car is not dragged in forever",
              "hot-wheels-rlc-ancient" not in handles_found, f"{handles_found}")
        check("sitemap-only car (in no collection) is discovered anyway",
              "hot-wheels-rlc-unlisted" in handles_found, f"{handles_found}")
        check("irrelevant sitemap product is pre-filtered out before a fetch",
              "mega-blocks-irrelevant" not in handles_found, f"{handles_found}")

        # An already-tracked car that Mattel drops from the collections once
        # it sells out must keep being checked, even though it is now old
        # enough to fail the recency gate. Otherwise its state freezes and a
        # later restock is never noticed — found live 2026-08-31 with three
        # cars still reading in_stock days after selling out.
        sitemap_catalog["hot-wheels-rlc-listed"].update(
            available=False, badge="Sold Out", published_at="2020-01-01T00:00:00Z")
        mock_store.set_collections({"hot-wheels": []})  # gone from the collection too
        mock_store.set_catalog(sitemap_catalog)
        persist_state(sitemap_dir, base_url)
        aged = json.loads((sitemap_dir / "docs" / "data.json").read_text())
        aged_rows = {r["key"]: r for r in aged["items"]}
        row = aged_rows.get("US:hot-wheels-rlc-listed", {})
        check("a tracked car aged past the recency gate is still re-checked",
              row.get("present") is True, f"present={row.get('present')}")
        check("and its sell-out is actually recorded, not frozen at in_stock",
              row.get("status") == "sold_out", f"status={row.get('status')}")
        check("the recency gate still blocks never-tracked old junk",
              "hot-wheels-rlc-ancient" not in {r["handle"] for r in aged["items"]})

        print("\nRun 12 — watchlist-check catches a restock without a full scan")
        # Give the watched car a live future countdown, same as the real R32
        # right now — otherwise it classifies as plain sold_out on the
        # baseline run and the auto-clean-up from Run 9 immediately drops it
        # off the watchlist before watchlist-check ever gets to run.
        fast_catalog = {
            "hot-wheels-rlc-watched": {
                "title": "Hot Wheels RLC Watched Car", "available": False,
                "badge": "Details", "tags": ["RLC"], "launches": future,
            },
            "hot-wheels-rlc-unwatched": {
                "title": "Hot Wheels RLC Unwatched Car", "available": False,
                "badge": "Sold Out", "tags": ["RLC"],
            },
        }
        mock_store.set_catalog(fast_catalog)
        mock_store.set_collections({})

        fast_dir = Path(tmp) / "fast"
        fast_dir.mkdir()
        (fast_dir / "checker.py").write_bytes((ROOT / "checker.py").read_bytes())
        (fast_dir / "docs").mkdir()
        fast_cfg = json.loads((ROOT / "config.json").read_text())
        fast_cfg["collections"] = ["hot-wheels"]
        fast_cfg["regions"]["AU"]["collections"] = ["hot-wheels"]
        fast_cfg["stock_probe"].update(enabled=False, watchlist=["US:hot-wheels-rlc-watched"])
        (fast_dir / "config.json").write_text(json.dumps(fast_cfg))

        persist_state(fast_dir, base_url)  # seed a baseline via a full run first
        baseline = json.loads((fast_dir / "state.json").read_text())
        baseline_cfg = json.loads((fast_dir / "config.json").read_text())
        check("baseline run recorded the watched car as coming_soon (not sold_out)",
              baseline["items"].get("US:hot-wheels-rlc-watched", {}).get("status") == "coming_soon",
              f"{baseline['items'].get('US:hot-wheels-rlc-watched')}")
        check("it's still on the watchlist afterwards (coming_soon doesn't get auto-cleaned)",
              baseline_cfg["stock_probe"]["watchlist"] == ["US:hot-wheels-rlc-watched"],
              f"{baseline_cfg['stock_probe']['watchlist']}")

        # A poll where nothing moved must leave the files byte-identical, or
        # the workflow commits every run — 288 a day, each triggering a Pages
        # rebuild, which got the repo starved of Actions runners.
        state_before = (fast_dir / "state.json").read_bytes()
        data_before = (fast_dir / "docs" / "data.json").read_bytes()
        run_watchlist_check(fast_dir, base_url)
        check("an uneventful fast check leaves state.json untouched",
              (fast_dir / "state.json").read_bytes() == state_before)
        check("an uneventful fast check leaves docs/data.json untouched",
              (fast_dir / "docs" / "data.json").read_bytes() == data_before)

        fast_catalog["hot-wheels-rlc-watched"].update(available=True, badge="Add to Cart")
        fast_catalog["hot-wheels-rlc-unwatched"].update(available=True, badge="Add to Cart")
        mock_store.set_catalog(fast_catalog)
        run_watchlist_check(fast_dir, base_url)

        after = json.loads((fast_dir / "state.json").read_text())
        watched = after["items"].get("US:hot-wheels-rlc-watched", {})
        unwatched = after["items"].get("US:hot-wheels-rlc-unwatched", {})
        fast_data = json.loads((fast_dir / "docs" / "data.json").read_text())

        check("watchlist-check catches the watched car going back in stock",
              watched.get("status") == "in_stock", f"{watched}")
        check("a restock event is recorded for it",
              any(e.get("type") == "restock" and "Watched" in e.get("title", "")
                  for e in fast_data.get("recent_events", [])),
              f"{fast_data.get('recent_events')}")
        # It must land in state too, or the next full scan's dashboard write
        # silently drops it from the activity list.
        check("the fast check's event persists in state, not just the dashboard",
              any(e.get("type") == "restock" and "Watched" in e.get("title", "")
                  for e in after.get("recent_events", [])),
              f"{after.get('recent_events')}")
        check("watchlist-check leaves a non-watchlisted car alone, even though it also restocked",
              unwatched.get("status") == "sold_out", f"{unwatched}")

        print("\nRun 13 — watchlist-check downgrades a lapsed countdown")
        lapsed_catalog = {
            "hot-wheels-rlc-lapsed": {
                "title": "Hot Wheels RLC Lapsed Car", "available": False,
                "badge": "Details", "tags": ["RLC"], "launches": future,
            },
        }
        mock_store.set_catalog(lapsed_catalog)
        mock_store.set_collections({})

        lapsed_dir = Path(tmp) / "lapsed"
        lapsed_dir.mkdir()
        (lapsed_dir / "checker.py").write_bytes((ROOT / "checker.py").read_bytes())
        (lapsed_dir / "docs").mkdir()
        lapsed_cfg = json.loads((ROOT / "config.json").read_text())
        lapsed_cfg["collections"] = ["hot-wheels"]
        lapsed_cfg["regions"]["AU"]["collections"] = ["hot-wheels"]
        lapsed_cfg["stock_probe"].update(enabled=False, watchlist=["US:hot-wheels-rlc-lapsed"])
        (lapsed_dir / "config.json").write_text(json.dumps(lapsed_cfg))

        persist_state(lapsed_dir, base_url)  # baseline: countdown genuinely in the future
        seeded = json.loads((lapsed_dir / "state.json").read_text())
        check("baseline run recorded the lapsed-to-be car as coming_soon",
              seeded["items"].get("US:hot-wheels-rlc-lapsed", {}).get("status") == "coming_soon",
              f"{seeded['items'].get('US:hot-wheels-rlc-lapsed')}")

        # Same as reality: the car is still not available, but its own
        # countdown time has now passed — Mattel didn't flip it live on
        # schedule. watchlist-check should catch this on its own, without
        # waiting for the next full scan.
        lapsed_catalog["hot-wheels-rlc-lapsed"]["launches"] = "January 1, 2020 9:00 am PT"
        mock_store.set_catalog(lapsed_catalog)
        run_watchlist_check(lapsed_dir, base_url)

        downgraded = json.loads((lapsed_dir / "state.json").read_text())["items"].get(
            "US:hot-wheels-rlc-lapsed", {})
        check("watchlist-check downgrades it to sold_out once the countdown lapses",
              downgraded.get("status") == "sold_out", f"{downgraded}")

    server.shutdown()
    print(f"\n{'='*60}")
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
