#!/usr/bin/env python3
"""
Mattel Creations Hot Wheels stock monitor.

Checks the US and AU Mattel Creations storefronts for Hot Wheels collector
items, diffs against the previous run, pushes notifications to an Android
phone via ntfy.sh, and writes a JSON feed for the dashboard.

Usage:
    python checker.py                 # normal run
    python checker.py --dry-run       # check + report, send no notifications
    python checker.py --self-test     # verify the site is still parseable
    python checker.py --notify-test   # send one test notification and exit

Environment:
    NTFY_TOPIC     ntfy.sh topic to publish to      (required to notify)
    NTFY_SERVER    override ntfy server             (default https://ntfy.sh)
    NTFY_TOKEN     bearer token for protected topic (optional)
    BASE_OVERRIDE  point every region at this base  (testing only)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
DATA_PATH = ROOT / "docs" / "data.json"

# Badge wording Mattel uses on collection cards, mapped to our status vocabulary.
BADGE_PATTERNS: list[tuple[str, str]] = [
    (r"sold\s*out", "sold_out"),
    (r"out\s*of\s*stock", "sold_out"),
    (r"notify\s*me", "sold_out"),
    (r"join\s*waitlist", "sold_out"),
    (r"pre[\s-]*order", "preorder"),
    (r"coming\s*soon", "coming_soon"),
    (r"drops?\s*(soon|in)", "coming_soon"),
    (r"buy\s*now", "in_stock"),
    (r"add\s*to\s*(cart|bag)", "in_stock"),
    (r"shop\s*now", "in_stock"),
]

STATUS_LABEL = {
    "in_stock": "In Stock",
    "sold_out": "Sold Out",
    "preorder": "Pre-Order",
    "coming_soon": "Coming Soon",
    "unknown": "Unknown",
}

PRODUCT_LINK_RE = re.compile(r'/products/([a-z0-9][a-z0-9\-_%]{2,120})', re.I)

# ISO-ish timestamps that Mattel embeds for drop countdowns.
DROP_TIME_RE = re.compile(
    r'(?:drop|launch|available|release|starts?)[^{}<>]{0,60}?'
    r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)',
    re.I,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------

def load_config() -> dict:
    with CONFIG_PATH.open() as fh:
        return json.load(fh)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 2, "items": {}, "runs": []}
    try:
        with STATE_PATH.open() as fh:
            state = json.load(fh)
    except json.JSONDecodeError:
        log("WARN state.json was corrupt; starting fresh")
        return {"version": 2, "items": {}, "runs": []}
    state.setdefault("items", {})
    state.setdefault("runs", [])
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Fetcher:
    def __init__(self, http_cfg: dict):
        self.cfg = http_cfg
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": http_cfg.get("user_agent", "Mozilla/5.0"),
            "Accept-Language": "en-AU,en;q=0.9",
        })
        # DELAY_OVERRIDE exists so the offline test suite can run without waiting.
        self.delay = float(os.environ.get("DELAY_OVERRIDE")
                           or http_cfg.get("request_delay_seconds", 1.5))
        self.jitter = 0.6 if self.delay else 0.0
        self.timeout = float(http_cfg.get("timeout_seconds", 25))
        self.retries = int(http_cfg.get("max_retries", 3))
        self.request_count = 0

    def get(self, url: str, as_json: bool = False, quiet: bool = False):
        """GET with retry + polite delay. Returns text/dict, or None on failure."""
        last_err: str = ""
        for attempt in range(1, self.retries + 1):
            try:
                if self.request_count and self.delay:
                    time.sleep(self.delay + random.uniform(0, self.jitter))
                self.request_count += 1
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 404:
                    return None
                if resp.status_code == 430 or resp.status_code == 429:
                    wait = min(60, 5 * attempt)
                    log(f"  rate limited ({resp.status_code}), backing off {wait}s")
                    time.sleep(wait)
                    last_err = f"HTTP {resp.status_code}"
                    continue
                resp.raise_for_status()
                if as_json:
                    return resp.json()
                return resp.text
            except Exception as exc:  # noqa: BLE001 - network layer, report and retry
                last_err = str(exc)
                if attempt < self.retries:
                    time.sleep(2 * attempt)
        if not quiet:
            log(f"  FAILED {url}: {last_err}")
        return None


# --------------------------------------------------------------------------
# region urls
# --------------------------------------------------------------------------

def region_url(region_cfg: dict, path: str, prefix: str | None = None) -> str:
    base = os.environ.get("BASE_OVERRIDE") or region_cfg["base"]
    base = base.rstrip("/")
    if prefix is None:
        prefix = region_cfg.get("path_prefix", "")
    prefix = prefix.rstrip("/")
    url = f"{base}{prefix}{path}"
    query = region_cfg.get("query") or {}
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
    return url


def resolve_region_prefix(fetcher: Fetcher, name: str, region_cfg: dict,
                          collection: str) -> str | None:
    """Find a path prefix that actually serves this region's collection page.

    Mattel serves AU through Shopify Markets; the exact prefix has moved
    before, so we try the configured one then the fallbacks and remember
    whichever answers.
    """
    candidates = [region_cfg.get("path_prefix", "")]
    candidates += region_cfg.get("path_prefix_fallbacks", [])
    seen: set[str] = set()
    for prefix in candidates:
        if prefix in seen:
            continue
        seen.add(prefix)
        url = region_url(region_cfg, f"/collections/{collection}", prefix=prefix)
        html = fetcher.get(url, quiet=True)
        if html and PRODUCT_LINK_RE.search(html):
            if prefix != region_cfg.get("path_prefix", ""):
                log(f"  {name}: using fallback prefix '{prefix or '(none)'}'")
            return prefix
    return None


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def extract_handles(html: str) -> list[str]:
    """Ordered, de-duplicated product handles linked from a collection page."""
    out: list[str] = []
    seen: set[str] = set()
    for match in PRODUCT_LINK_RE.finditer(html):
        handle = urllib.parse.unquote(match.group(1)).strip().lower()
        handle = handle.split("?")[0].split("#")[0]
        if handle and handle not in seen:
            seen.add(handle)
            out.append(handle)
    return out


def badge_for_handle(html: str, handle: str) -> str:
    """Best-effort read of the status badge on a product's collection card.

    Looks at the markup immediately after each link to the product. This is
    supplementary — variant availability from the product JSON is
    authoritative — but it is the only place 'Coming Soon' and 'Pre-Order'
    surface, so it is worth reading.
    """
    status = "unknown"
    for match in re.finditer(re.escape(f"/products/{handle}"), html, re.I):
        window = html[match.end(): match.end() + 2500]
        # Stop at the next product card so we don't read a neighbour's badge.
        next_product = PRODUCT_LINK_RE.search(window)
        if next_product:
            window = window[: next_product.start()]
        text = re.sub(r"<[^>]+>", " ", window)
        for pattern, value in BADGE_PATTERNS:
            if re.search(pattern, text, re.I):
                # sold_out / preorder / coming_soon are more informative than
                # in_stock, so let them win if any card says so.
                if status == "unknown" or value != "in_stock":
                    status = value
                if value != "in_stock":
                    return status
    return status


def extract_drop_time(html: str, handle: str) -> str | None:
    for match in re.finditer(re.escape(f"/products/{handle}"), html, re.I):
        window = html[max(0, match.start() - 1500): match.end() + 2500]
        found = DROP_TIME_RE.search(window)
        if found:
            return found.group(1)
    return None


def money(amount: Any, currency: str) -> str | None:
    """Shopify .js prices are integer cents."""
    if amount is None:
        return None
    try:
        value = float(amount) / 100.0
    except (TypeError, ValueError):
        return None
    return f"{value:,.2f} {currency}"


def classify(product_js: dict, badge: str) -> str:
    """Combine structured availability with the on-card badge."""
    tags = " ".join(product_js.get("tags") or []).lower()
    available = bool(product_js.get("available"))

    if badge in ("coming_soon", "preorder"):
        return badge
    if re.search(r"pre[\s-]*order", tags):
        return "preorder" if available else "sold_out"
    if available:
        return "in_stock"
    if badge == "sold_out":
        return "sold_out"
    return "sold_out" if product_js.get("variants") else "unknown"


def matches_filters(product_js: dict, filters: dict) -> bool:
    haystack = " ".join([
        str(product_js.get("title", "")),
        str(product_js.get("handle", "")),
        str(product_js.get("product_type", "")),
        " ".join(product_js.get("tags") or []),
    ]).lower()

    for word in filters.get("exclude_keywords", []):
        if word.lower() in haystack:
            return False

    if filters.get("mode") == "all":
        return True

    for word in filters.get("include_keywords", []):
        if word.lower() in haystack:
            return True
    return False


# --------------------------------------------------------------------------
# stock probe (opt-in)
# --------------------------------------------------------------------------

QTY_RE = re.compile(r"only\s+(\d+)\s+", re.I)


def probe_stock(fetcher: Fetcher, region_cfg: dict, variant_id: Any,
                probe_cfg: dict) -> int | None:
    """Read remaining inventory via Shopify's cart quantity ceiling.

    Disabled by default. Mattel's robots.txt disallows /cart/ and /cart.js to
    automated clients; enabling this risks the runner being blocked.
    """
    if not variant_id:
        return None
    url = region_url(region_cfg, "/cart/add.js")
    try:
        resp = fetcher.session.post(
            url,
            json={"id": variant_id, "quantity": int(probe_cfg.get("probe_quantity", 99999))},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=fetcher.timeout,
        )
        if resp.status_code == 422:
            body = resp.json()
            text = f"{body.get('description', '')} {body.get('message', '')}"
            match = QTY_RE.search(text)
            if match:
                return int(match.group(1))
            digits = re.findall(r"\b(\d{1,6})\b", text)
            return int(digits[0]) if digits else None
        if resp.ok:
            qty = resp.json().get("quantity")
            # Clean up so we never leave a loaded cart behind.
            fetcher.session.post(
                url.replace("/cart/add.js", "/cart/change.js"),
                json={"id": variant_id, "quantity": 0},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=fetcher.timeout,
            )
            return int(qty) if qty else None
    except Exception as exc:  # noqa: BLE001
        log(f"  probe failed for variant {variant_id}: {exc}")
    return None


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

def scan_region(fetcher: Fetcher, name: str, region_cfg: dict,
                cfg: dict) -> tuple[list[dict], list[str]]:
    """Return (items, warnings) for one region."""
    warnings: list[str] = []
    items: list[dict] = []
    filters = cfg["filters"]
    probe_cfg = cfg.get("stock_probe", {})
    max_pages = int(cfg["http"].get("max_collection_pages", 5))
    probes_done = 0

    for collection in cfg["collections"]:
        prefix = resolve_region_prefix(fetcher, name, region_cfg, collection)
        if prefix is None:
            warnings.append(
                f"{name}: could not load /collections/{collection} on any known "
                f"path prefix — the region URL may have changed"
            )
            continue

        handles: list[str] = []
        page_html: dict[str, str] = {}
        for page in range(1, max_pages + 1):
            path = f"/collections/{collection}"
            if page > 1:
                path += f"?page={page}"
            html = fetcher.get(region_url(region_cfg, path, prefix=prefix))
            if not html:
                break
            page_handles = extract_handles(html)
            fresh = [h for h in page_handles if h not in handles]
            if not fresh:
                break
            for handle in fresh:
                handles.append(handle)
                page_html[handle] = html
            if len(page_handles) < 4:
                break

        if not handles:
            warnings.append(f"{name}/{collection}: no product links found on the collection page")
            continue

        log(f"  {name}/{collection}: {len(handles)} products linked")

        for handle in handles:
            product = fetcher.get(
                region_url(region_cfg, f"/products/{handle}.js", prefix=prefix),
                as_json=True,
                quiet=True,
            )
            if not isinstance(product, dict) or not product.get("title"):
                continue
            if not matches_filters(product, filters):
                continue

            html = page_html.get(handle, "")
            badge = badge_for_handle(html, handle) if html else "unknown"
            status = classify(product, badge)
            variants = product.get("variants") or []
            first_available = next(
                (v for v in variants if v.get("available")), variants[0] if variants else {}
            )

            stock = None
            if (probe_cfg.get("enabled")
                    and status == "in_stock"
                    and probes_done < int(probe_cfg.get("max_products_per_run", 8))):
                probes_done += 1
                time.sleep(float(probe_cfg.get("delay_seconds", 4)))
                stock = probe_stock(fetcher, region_cfg, first_available.get("id"), probe_cfg)

            items.append({
                "key": f"{name}:{handle}",
                "region": name,
                "handle": handle,
                "title": product.get("title", handle),
                "url": region_url(region_cfg, f"/products/{handle}", prefix=prefix),
                "price": money(first_available.get("price"), region_cfg.get("currency", "")),
                "price_cents": first_available.get("price"),
                "currency": region_cfg.get("currency", ""),
                "status": status,
                "status_label": STATUS_LABEL.get(status, status),
                "badge": badge,
                "stock": stock,
                "variant_id": first_available.get("id"),
                "variant_count": len(variants),
                "image": (product.get("images") or [None])[0],
                "tags": product.get("tags") or [],
                "drop_time": extract_drop_time(html, handle) if html else None,
                "published_at": product.get("published_at"),
            })

    return items, warnings


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

def human_duration(start: str, end: str) -> str | None:
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    minutes = int((b - a).total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def diff(state: dict, items: list[dict], notify_cfg: dict) -> list[dict]:
    events: list[dict] = []
    stamp = now_iso()
    known = state["items"]
    first_run = not known

    for item in items:
        key = item["key"]
        prev = known.get(key)

        record = dict(prev or {})
        record.update({k: item[k] for k in (
            "region", "handle", "title", "url", "price", "price_cents", "currency",
            "status", "status_label", "stock", "variant_id", "image", "tags",
            "drop_time", "published_at",
        )})
        record["last_seen"] = stamp
        record.setdefault("first_seen", stamp)
        record.setdefault("history", [])

        if prev is None:
            if not first_run and notify_cfg.get("new", True):
                events.append({"type": "new", "item": item, "detail": "New listing"})
            record["history"].append({"at": stamp, "event": "first_seen", "status": item["status"]})
        else:
            was, is_now = prev.get("status"), item["status"]
            if was != is_now:
                record["history"].append({"at": stamp, "event": "status", "from": was, "to": is_now})

                if is_now == "in_stock" and was in ("sold_out", "coming_soon", "unknown"):
                    detail = "Back in stock" if was == "sold_out" else "Now live"
                    if notify_cfg.get("restock", True):
                        events.append({"type": "restock", "item": item, "detail": detail})
                    record["became_available_at"] = stamp
                    record.pop("sold_out_at", None)

                elif is_now == "sold_out" and was == "in_stock":
                    record["sold_out_at"] = stamp
                    lasted = human_duration(prev.get("became_available_at", ""), stamp)
                    record["last_sellout_duration"] = lasted
                    if lasted:
                        record.setdefault("sellout_log", []).append(
                            {"at": stamp, "lasted": lasted}
                        )
                    if notify_cfg.get("sold_out", False):
                        events.append({
                            "type": "sold_out", "item": item,
                            "detail": f"Sold out{f' after {lasted}' if lasted else ''}",
                        })

                elif is_now in ("preorder", "coming_soon") and notify_cfg.get("drop", True):
                    events.append({
                        "type": "drop", "item": item,
                        "detail": f"{STATUS_LABEL[is_now]}"
                                  + (f" — drops {item['drop_time']}" if item.get("drop_time") else ""),
                    })

            if (item.get("drop_time") and prev.get("drop_time") != item["drop_time"]
                    and notify_cfg.get("drop", True)):
                events.append({
                    "type": "drop", "item": item,
                    "detail": f"Drop time set: {item['drop_time']}",
                })

            old_price, new_price = prev.get("price_cents"), item.get("price_cents")
            if (old_price and new_price and old_price != new_price
                    and notify_cfg.get("price_change", True)):
                events.append({
                    "type": "price", "item": item,
                    "detail": f"Price changed {money(old_price, item['currency'])} → {item['price']}",
                })

        if item["status"] == "in_stock":
            record.setdefault("became_available_at", stamp)

        known[key] = record

    if first_run:
        log(f"  first run — recording {len(items)} items as the baseline, no alerts sent")

    return events


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

EVENT_META = {
    "new":      ("\U0001F195 New Hot Wheels", "rotating_light", 5),
    "restock":  ("\U0001F525 Back in stock",  "fire",           5),
    "drop":     ("\U0001F4C5 Upcoming drop",  "calendar",       4),
    "price":    ("\U0001F4B0 Price change",   "moneybag",       3),
    "sold_out": ("❌ Sold out",           "x",              2),
}


def send_ntfy(events: list[dict], dry_run: bool) -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    token = os.environ.get("NTFY_TOKEN", "").strip()

    if not events:
        return 0
    if dry_run:
        for event in events:
            log(f"  [dry-run] would notify: {event['type']} — {event['item']['title']}")
        return 0
    if not topic:
        log("  NTFY_TOPIC is not set — skipping notifications")
        return 0

    sent = 0
    for event in events:
        item = event["item"]
        title_prefix, tag, priority = EVENT_META.get(event["type"], ("Update", "bell", 3))
        stock_line = f"\nStock left: {item['stock']}" if item.get("stock") is not None else ""
        body = (
            f"{item['title']}\n"
            f"{item['region']} · {item['price'] or 'price n/a'} · {item['status_label']}\n"
            f"{event['detail']}{stock_line}"
        )
        headers = {
            "Title": f"{title_prefix} ({item['region']})",
            "Priority": str(priority),
            "Tags": tag,
            "Click": item["url"],
            "Actions": f"view, Open on Mattel, {item['url']}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = requests.post(
                f"{server}/{topic}",
                data=body.encode("utf-8"),
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log(f"  notification failed: {exc}")
    log(f"  sent {sent}/{len(events)} notifications")
    return sent


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------

def write_dashboard_data(state: dict, items: list[dict], events: list[dict],
                         warnings: list[str], started: str) -> None:
    live_keys = {i["key"] for i in items}
    rows = []
    for key, record in state["items"].items():
        row = dict(record)
        row["key"] = key
        row["present"] = key in live_keys
        rows.append(row)

    order = {"in_stock": 0, "preorder": 1, "coming_soon": 2, "unknown": 3, "sold_out": 4}
    rows.sort(key=lambda r: (order.get(r.get("status"), 5), r.get("title", "")))

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps({
        "generated_at": now_iso(),
        "started_at": started,
        "counts": {
            "total": len(rows),
            "in_stock": sum(1 for r in rows if r.get("status") == "in_stock"),
            "sold_out": sum(1 for r in rows if r.get("status") == "sold_out"),
            "upcoming": sum(1 for r in rows if r.get("status") in ("preorder", "coming_soon")),
        },
        "warnings": warnings,
        "recent_events": [
            {
                "type": e["type"],
                "detail": e["detail"],
                "title": e["item"]["title"],
                "region": e["item"]["region"],
                "url": e["item"]["url"],
                "at": now_iso(),
            } for e in events
        ] + state.get("recent_events", [])[:40],
        "items": rows,
    }, indent=2) + "\n")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    state = load_state()
    fetcher = Fetcher(cfg["http"])
    started = now_iso()

    all_items: list[dict] = []
    all_warnings: list[str] = []

    for name, region_cfg in cfg["regions"].items():
        if not region_cfg.get("enabled", True):
            continue
        log(f"Scanning {name}...")
        items, warnings = scan_region(fetcher, name, region_cfg, cfg)
        log(f"  {name}: {len(items)} matching products")
        all_items.extend(items)
        all_warnings.extend(warnings)

    if not all_items and not args.self_test:
        all_warnings.append(
            "No products matched in any region. Either the filters are too narrow "
            "or the page structure changed — run with --self-test."
        )

    events = diff(state, all_items, cfg["notify"])
    log(f"{len(events)} event(s) to report")

    if args.self_test:
        print(json.dumps({
            "products_found": len(all_items),
            "regions": sorted({i["region"] for i in all_items}),
            "statuses": {s: sum(1 for i in all_items if i["status"] == s)
                         for s in {i["status"] for i in all_items}},
            "warnings": all_warnings,
            "sample": all_items[:3],
        }, indent=2))
        return 0 if all_items and not all_warnings else 1

    send_ntfy(events, args.dry_run)

    state["recent_events"] = ([
        {"type": e["type"], "detail": e["detail"], "title": e["item"]["title"],
         "region": e["item"]["region"], "url": e["item"]["url"], "at": now_iso()}
        for e in events
    ] + state.get("recent_events", []))[:60]
    state["runs"] = ([{"at": started, "items": len(all_items), "events": len(events),
                       "warnings": all_warnings}] + state.get("runs", []))[:50]

    write_dashboard_data(state, all_items, events, all_warnings, started)
    if not args.dry_run:
        save_state(state)

    for warning in all_warnings:
        log(f"WARN {warning}")
    return 0


def notify_test() -> int:
    fake = {
        "type": "restock",
        "detail": "This is a test from your Hot Wheels monitor",
        "item": {
            "title": "Hot Wheels RLC Exclusive — Test Car",
            "region": "AU", "price": "39.99 AUD", "status_label": "In Stock",
            "stock": None, "url": "https://creations.mattel.com/collections/hot-wheels",
        },
    }
    return 0 if send_ntfy([fake], dry_run=False) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Mattel Creations Hot Wheels monitor")
    parser.add_argument("--dry-run", action="store_true", help="check but send nothing")
    parser.add_argument("--self-test", action="store_true", help="verify the site is parseable")
    parser.add_argument("--notify-test", action="store_true", help="send one test notification")
    args = parser.parse_args()

    if args.notify_test:
        return notify_test()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
