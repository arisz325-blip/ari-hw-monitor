"""End-to-end tests for the Hot Wheels monitor, run against a local mock store.

    python -m tests.test_checker
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import mock_store  # noqa: E402

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

        print("\nRun 8 — per-region collections + cross-collection de-dup")
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

        out = run_checker(overlap_dir, base_url, ["--self-test"])
        report = json.loads(out[out.index("{"):])

        # US scans hot-wheels + cars-vehicles (3 unique cars); AU is
        # configured to only scan hot-wheels (2 cars) — 5 total, not 6.
        check("car seen in two collections is only counted once per region",
              report["products_found"] == 5, f"{report['products_found']}")
        check("AU's collections override kept it off cars-vehicles",
              report["warnings"] == [], f"{report['warnings']}")

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

    server.shutdown()
    print(f"\n{'='*60}")
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
