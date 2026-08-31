#!/usr/bin/env python3
"""
Mattel Creations Hot Wheels stock monitor.

Checks the US and AU Mattel Creations storefronts for Hot Wheels collector
items, diffs against the previous run, pushes notifications to an Android
phone via ntfy.sh, and writes a JSON feed for the dashboard.

Usage:
    python checker.py                    # normal run
    python checker.py --dry-run          # check + report, send no notifications
    python checker.py --self-test        # verify the site is still parseable
    python checker.py --notify-test      # send one test notification and exit
    python checker.py --watchlist-check  # fast poll of just the watchlist, no full scan

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
from zoneinfo import ZoneInfo

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

# What the product *page* actually uses (collection cards don't carry this) —
# and the US and AU stores word it differently:
#   US: `Launches August 20, 2026 9:00 am PT`
#   AU: `Launches 20th August 2026 9am AEST`
# This block is static boilerplate present on every product template — even
# ones long since released — so it's only meaningful once parsed and checked
# against "now" (see upcoming_drop_from_product_page below).
DROP_TIME_HUMAN_RE = re.compile(
    r'Launches?\s+(?:'
    r'(?P<us>[A-Z][a-z]+ \d{1,2}, \d{4} \d{1,2}(?::\d{2})?\s*[ap]m)'
    r'|'
    r'(?P<au>\d{1,2}(?:st|nd|rd|th) [A-Z][a-z]+ \d{4} \d{1,2}(?::\d{2})?\s*[ap]m)'
    r')(?:\s+(?P<tz>[A-Z]{2,5}))?',
    re.I,
)

TZ_ABBREV = {
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
}


def parse_human_drop_time(html: str) -> str | None:
    """Extract and normalize Mattel's 'Launches <date> <time> <tz>' text to
    ISO 8601 UTC, in either the US store's or the AU store's wording.
    Returns None if the text isn't there or doesn't parse — never guess a
    time zone beyond defaulting unmarked ones to Pacific, which is what the
    US store means when it omits one."""
    match = DROP_TIME_HUMAN_RE.search(html)
    if not match:
        return None
    zone = TZ_ABBREV.get((match.group("tz") or "PT").upper(), "America/Los_Angeles")

    if match.group("us"):
        text = match.group("us").strip()
        formats = ("%B %d, %Y %I:%M %p", "%B %d, %Y %I %p")
    else:
        text = re.sub(r"(\d)(st|nd|rd|th)", r"\1", match.group("au").strip(), flags=re.I)
        formats = ("%d %B %Y %I:%M %p", "%d %B %Y %I %p")
    text = re.sub(r"(\d)([ap]m)", r"\1 \2", text, flags=re.I)  # "9am" -> "9 am"

    naive = None
    for fmt in formats:
        try:
            naive = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if naive is None:
        return None
    aware = naive.replace(tzinfo=ZoneInfo(zone))
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


MELBOURNE = ZoneInfo("Australia/Melbourne")


def melbourne_time(iso: str | None) -> str | None:
    """Render a UTC ISO timestamp in Ari's local time (AEST/AEDT, DST-aware)
    for notification text. The dashboard does its own client-side
    conversion; this is only for the ntfy push, which is plain text."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return iso
    return dt.astimezone(MELBOURNE).strftime("%d %b %Y, %I:%M %p %Z")


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------

def load_config() -> dict:
    with CONFIG_PATH.open() as fh:
        return json.load(fh)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


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
        })
        # DELAY_OVERRIDE exists so the offline test suite can run without waiting.
        self.delay = float(os.environ.get("DELAY_OVERRIDE")
                           or http_cfg.get("request_delay_seconds", 1.5))
        self.jitter = 0.6 if self.delay else 0.0
        self.timeout = float(http_cfg.get("timeout_seconds", 25))
        self.retries = int(http_cfg.get("max_retries", 3))
        self.request_count = 0

    def pin_region(self, region_cfg: dict) -> None:
        """Send an Accept-Language matching the region being scanned.

        Secondary to the real currency control, which is the `country`
        query param in each region's config (see region_url) — that's what
        actually decides which Shopify market prices a response. This just
        stops us claiming en-AU while scanning the US store, which was the
        old hardcoded session default.

        Note: setting a `cart_currency` cookie does NOT work here — tested
        2026-08-24, `/products/{handle}.js` ignores it and the server
        overwrites it in the response. Don't reintroduce that.
        """
        currency = region_cfg.get("currency", "")
        lang = "en-AU" if currency == "AUD" else "en-US"
        self.session.headers["Accept-Language"] = f"{lang},en;q=0.9"

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


def iter_collection_handles(fetcher: Fetcher, region_cfg: dict, collection: str,
                            prefix: str, cfg: dict) -> list[tuple[str, str]]:
    """Paginate a collection listing, returning (handle, page_html) pairs in
    first-seen order. page_html is the card markup badge_for_handle reads."""
    max_pages = int(cfg["http"].get("max_collection_pages", 5))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for page in range(1, max_pages + 1):
        path = f"/collections/{collection}"
        if page > 1:
            path += f"?page={page}"
        html = fetcher.get(region_url(region_cfg, path, prefix=prefix))
        if not html:
            break
        page_handles = extract_handles(html)
        fresh = [h for h in page_handles if h not in seen]
        if not fresh:
            break
        for handle in fresh:
            seen.add(handle)
            out.append((handle, html))
        if len(page_handles) < 4:
            break
    return out


SITEMAP_INDEX_RE = re.compile(r"<loc>(https?://[^<]*sitemap_products[^<]*)</loc>", re.I)
SITEMAP_PRODUCT_URL_RE = re.compile(
    r"<loc>https?://[^/]+/products/([a-z0-9][a-z0-9\-_%]{2,120})</loc>", re.I
)


def sitemap_product_handles(fetcher: Fetcher, region_cfg: dict) -> set[str]:
    """Shopify's XML sitemap lists every published product regardless of
    collection membership, and Mattel keeps it live-updated — unlike
    collection pages, which a product may not be added to until launch.
    Confirmed 2026-08-20: a drop was in the sitemap hours before it appeared
    in any collection. Best-effort — any failure here just falls back to
    collection-only discovery, same as before this existed."""
    base = (os.environ.get("BASE_OVERRIDE") or region_cfg["base"]).rstrip("/")
    index = fetcher.get(f"{base}/sitemap.xml", quiet=True)
    if not index:
        return set()
    handles: set[str] = set()
    for sitemap_url in SITEMAP_INDEX_RE.findall(index):
        xml = fetcher.get(sitemap_url.replace("&amp;", "&"), quiet=True)
        if xml:
            handles.update(SITEMAP_PRODUCT_URL_RE.findall(xml))
    return handles


def looks_relevant(handle: str, filters: dict) -> bool:
    """Cheap pre-filter on handle text alone, before paying for a fetch —
    the sitemap covers the whole store (apparel, other brands, everything),
    not just what we track."""
    if filters.get("mode") == "all":
        return True
    norm = handle.replace("-", "").replace("_", "").lower()
    return any(kw.replace(" ", "").replace("-", "").lower() in norm
               for kw in filters.get("include_keywords", []))


def is_recent(product: dict, days: int = 60) -> bool:
    """The sitemap's own <lastmod> turned out useless for this — Mattel
    touches every product's entry on some shared cadence, so a car from
    2023 and one from this week can carry the same lastmod. published_at /
    created_at (from the product's own .js, already fetched either way) is
    the real signal: without it, sitemap discovery would drag in every
    RLC/Elite64 car ever made that still resolves, sold out or not, forever."""
    for field in ("published_at", "created_at"):
        raw = product.get(field)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days <= days
    return False


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


def upcoming_drop_from_product_page(fetcher: Fetcher, region_cfg: dict, handle: str,
                                    prefix: str) -> tuple[bool, str | None]:
    """Collection cards never carry Mattel's countdown; the product page does,
    but as boilerplate present on every product regardless of status (see
    parse_human_drop_time). Only worth calling for items that look
    unavailable and weren't already resolved by a collection-card badge —
    it costs one extra request. Returns (is_upcoming, drop_time_iso)."""
    html = fetcher.get(region_url(region_cfg, f"/products/{handle}", prefix=prefix), quiet=True)
    if not html:
        return False, None
    drop_iso = parse_human_drop_time(html)
    if not drop_iso:
        return False, None
    try:
        is_future = datetime.fromisoformat(drop_iso) > datetime.now(timezone.utc)
    except ValueError:
        return False, None
    return is_future, drop_iso if is_future else None


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

def scan_region(fetcher: Fetcher, name: str, region_cfg: dict, cfg: dict,
                known_keys: set[str] | None = None) -> tuple[list[dict], list[str]]:
    """Return (items, warnings) for one region.

    known_keys is the set of state keys already being tracked. The recency
    gate on sitemap discoveries is skipped for those — see the call site.
    """
    known_keys = known_keys or set()
    fetcher.pin_region(region_cfg)
    warnings: list[str] = []
    items: list[dict] = []
    filters = cfg["filters"]
    probe_cfg = cfg.get("stock_probe", {})
    watchlist = set(probe_cfg.get("watchlist", []))
    probes_done = 0
    seen_handles: set[str] = set()

    def handle_item(handle: str, prefix: str, html: str,
                    only_if_recent: bool = False) -> dict | None:
        """Fetch and classify one product. html is its collection-card
        markup if we found it via a collection listing, else "" (e.g. a
        sitemap-only find) — badge_for_handle degrades to 'unknown' either
        way, and upcoming_drop_from_product_page picks up the slack.

        only_if_recent (sitemap discoveries only): the sitemap covers every
        product Mattel has ever published, so without this an old,
        permanently sold-out car would get pulled in and re-checked forever,
        never having been relevant to begin with. Skip it before paying for
        the extra countdown-page fetch if it's unavailable and not new."""
        nonlocal probes_done
        product = fetcher.get(
            region_url(region_cfg, f"/products/{handle}.js", prefix=prefix),
            as_json=True,
            quiet=True,
        )
        if not isinstance(product, dict) or not product.get("title"):
            return None
        if not matches_filters(product, filters):
            return None
        if only_if_recent and not product.get("available") and not is_recent(product):
            return None

        badge = badge_for_handle(html, handle) if html else "unknown"
        status = classify(product, badge)

        # Collection cards never carry Mattel's countdown, so an item that
        # hasn't launched yet looks identical to one that's dead — both are
        # just "available: false" with no clear badge. Check the product
        # page itself before assuming it's sold out for good.
        human_drop_time = None
        if status in ("sold_out", "unknown") and not product.get("available"):
            is_upcoming, human_drop_time = upcoming_drop_from_product_page(
                fetcher, region_cfg, handle, prefix)
            if is_upcoming:
                status = "coming_soon"

        variants = product.get("variants") or []
        first_available = next(
            (v for v in variants if v.get("available")), variants[0] if variants else {}
        )

        key = f"{name}:{handle}"
        stock = None
        if (probe_cfg.get("enabled")
                and status == "in_stock"
                and key in watchlist
                and probes_done < int(probe_cfg.get("max_products_per_run", 8))):
            probes_done += 1
            time.sleep(float(probe_cfg.get("delay_seconds", 4)))
            stock = probe_stock(fetcher, region_cfg, first_available.get("id"), probe_cfg)

        return {
            "key": key,
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
            "drop_time": human_drop_time or (extract_drop_time(html, handle) if html else None),
            "published_at": product.get("published_at"),
        }

    default_prefix = region_cfg.get("path_prefix", "")

    for collection in region_cfg.get("collections", cfg["collections"]):
        prefix = resolve_region_prefix(fetcher, name, region_cfg, collection)
        if prefix is None:
            warnings.append(
                f"{name}: could not load /collections/{collection} on any known "
                f"path prefix — the region URL may have changed"
            )
            continue

        handles = list(iter_collection_handles(fetcher, region_cfg, collection, prefix, cfg))
        if not handles:
            warnings.append(f"{name}/{collection}: no product links found on the collection page")
            continue

        log(f"  {name}/{collection}: {len(handles)} products linked")

        for handle, html in handles:
            if handle in seen_handles:
                continue
            seen_handles.add(handle)
            item = handle_item(handle, prefix, html)
            if item:
                items.append(item)

    # A product can have a live, linkable page — with real countdown data —
    # before Mattel ever adds it to a browsable collection (confirmed
    # 2026-08-20: the site's XML sitemap listed a drop hours before it
    # appeared in any collection). The sitemap is real-time and covers the
    # whole store, so pre-filter by handle text before paying for a fetch.
    for handle in sitemap_product_handles(fetcher, region_cfg):
        if handle in seen_handles or not looks_relevant(handle, filters):
            continue
        seen_handles.add(handle)
        # The recency gate is for *discovery* only — it stops the sitemap
        # dragging in every RLC car ever made. Applying it to something
        # already tracked silently stops watching it: Mattel drops sold-out
        # items out of the collections, so an older car that sells out
        # disappears from the scan entirely and its state freezes at
        # whatever it last was. Found 2026-08-31 with three cars still
        # reading in_stock days after selling out, and the real cost is not
        # the stale label — it's that a restock on any of them would never
        # have been noticed, which is the whole point of the monitor.
        already_tracked = f"{name}:{handle}" in known_keys
        item = handle_item(handle, default_prefix, "", only_if_recent=not already_tracked)
        if item:
            items.append(item)
            if not already_tracked:
                log(f"  {name}: found via sitemap, not yet in a collection — {handle}")

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
                                  + (f" — drops {melbourne_time(item['drop_time'])}"
                                     if item.get("drop_time") else ""),
                    })

            if (item.get("drop_time") and prev.get("drop_time") != item["drop_time"]
                    and notify_cfg.get("drop", True)):
                events.append({
                    "type": "drop", "item": item,
                    "detail": f"Drop time set: {melbourne_time(item['drop_time'])}",
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
    "new":      ("New Hot Wheels", "rotating_light", 5),
    "restock":  ("Back in stock",  "fire",           5),
    "drop":     ("Upcoming drop",  "calendar",       4),
    "price":    ("Price change",   "moneybag",       3),
    "sold_out": ("Sold out",       "x",              2),
}


_HEADER_SUBS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
}


def header_safe(text: str) -> str:
    """Make a string safe to put in an HTTP header.

    Headers are latin-1. Mattel titles routinely contain ' and -, and an
    emoji anywhere in one makes requests raise UnicodeEncodeError — which
    send_ntfy catches and logs, so *every* notification dies silently
    while the scraper tests stay green. Emoji belong in ntfy's Tags
    header, which renders them into the title anyway.
    """
    for bad, good in _HEADER_SUBS.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


def send_ntfy(events: list[dict], dry_run: bool) -> int:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").strip().rstrip("/")
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
        headers = {k: header_safe(v) for k, v in {
            "Title": f"{title_prefix} ({item['region']})",
            "Priority": str(priority),
            "Tags": tag,
            "Click": item["url"],
            "Actions": f"view, Open on Mattel, {item['url']}",
        }.items()}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # A dropped notification is gone for good — the event is recorded as
        # handled either way and never re-fires — so a blip at ntfy.sh means
        # silently missing the restock this whole project exists to catch.
        # Retry before giving up, and let the caller surface what didn't land.
        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    f"{server}/{topic}",
                    data=body.encode("utf-8"),
                    headers=headers,
                    timeout=20,
                )
                resp.raise_for_status()
                sent += 1
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    log(f"  notification FAILED after {attempt} attempts: {exc}")
                else:
                    time.sleep(2 * attempt)
    log(f"  sent {sent}/{len(events)} notifications")
    return sent


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------

def record_events(state: dict, events: list[dict]) -> None:
    """Prepend events to the state's rolling activity log.

    Every path that produces events must call this *before*
    write_dashboard_data, which reads the log rather than taking events
    separately — that split is what previously made each event show up
    twice on the dashboard (run() prepended them to state, then
    write_dashboard_data prepended the same list again on top).
    """
    state["recent_events"] = ([
        {"type": e["type"], "detail": e["detail"], "title": e["item"]["title"],
         "region": e["item"]["region"], "url": e["item"]["url"], "at": now_iso()}
        for e in events
    ] + state.get("recent_events", []))[:60]


def write_dashboard_data(state: dict, items: list[dict],
                         warnings: list[str], started: str, cfg: dict) -> None:
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
        "probe_enabled": bool(cfg.get("stock_probe", {}).get("enabled")),
        "watchlist": sorted(cfg.get("stock_probe", {}).get("watchlist", [])),
        "recent_events": state.get("recent_events", [])[:40],
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
        items, warnings = scan_region(fetcher, name, region_cfg, cfg,
                                      known_keys=set(state["items"]))
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

    sent = send_ntfy(events, args.dry_run)
    # Only meaningful when notifications were actually meant to go out —
    # an unset topic is a deliberate local/dry configuration, not a fault.
    if events and not args.dry_run and os.environ.get("NTFY_TOPIC", "").strip():
        undelivered = len(events) - sent
        if undelivered:
            all_warnings.append(
                f"{undelivered} of {len(events)} notification(s) could not be delivered "
                f"to ntfy — those alerts are lost, they do not re-fire."
            )

    probe_cfg = cfg.get("stock_probe", {})
    watchlist = probe_cfg.get("watchlist", [])
    dropped = [k for k in watchlist if state["items"].get(k, {}).get("status") == "sold_out"]
    if dropped:
        probe_cfg["watchlist"] = [k for k in watchlist if k not in dropped]
        for k in dropped:
            log(f"  watchlist: auto-removed {k} (sold out)")
        if not args.dry_run:
            save_config(cfg)

    record_events(state, events)
    state["runs"] = ([{"at": started, "items": len(all_items), "events": len(events),
                       "warnings": all_warnings}] + state.get("runs", []))[:50]

    write_dashboard_data(state, all_items, all_warnings, started, cfg)
    if not args.dry_run:
        save_state(state)

    for warning in all_warnings:
        log(f"WARN {warning}")
    return 0


def watchlist_check() -> int:
    """Lightweight poll for the watchlist only: one .js fetch per item, no
    collection scan, no sitemap. Meant to run every few minutes so a
    restock isn't missed for up to an hour, without the request volume of a
    full scan (which is what the hourly run is for).

    Detects a car *becoming* available immediately. Going the other way
    (sells out after having been in_stock) is left to the full scan, which
    already tracks sell-out duration correctly via
    became_available_at/sold_out_at in diff() — handling that transition
    here too would race it and could double-count or clobber that
    bookkeeping.

    For a still-unavailable item, this also re-checks whether its
    countdown is still genuinely in the future (one more request, only
    when needed) and downgrades coming_soon -> sold_out once it's lapsed.
    Without this, a stale coming_soon just sits there getting its
    last_seen refreshed every 5 minutes with nothing actually re-verified
    — exactly the item this list exists to watch closely, showing the
    least accurate status of anything tracked. Confirmed live 2026-08-20:
    the AU Mercedes G63's countdown text read 20 Aug 9am AEST, that time
    came and went with the car still not actually on sale, and it sat
    reading coming_soon for hours until the next full scan happened to
    catch it — this closes that gap on the same ~5-minute cadence as the
    restock check.
    """
    cfg = load_config()
    watchlist = cfg.get("stock_probe", {}).get("watchlist", [])
    if not watchlist:
        log("watchlist is empty, nothing to check")
        return 0

    state = load_state()
    fetcher = Fetcher(cfg["http"])
    events: list[dict] = []
    checked: list[dict] = []
    changed = False

    for key in watchlist:
        region, _, handle = key.partition(":")
        region_cfg = cfg["regions"].get(region)
        record = state["items"].get(key)
        if not region_cfg or record is None:
            continue  # nothing to compare against yet — let the hourly scan seed it first
        fetcher.pin_region(region_cfg)  # same session may have just served a different region
        product = fetcher.get(
            region_url(region_cfg, f"/products/{handle}.js"), as_json=True, quiet=True
        )
        if not isinstance(product, dict):
            continue

        stamp = now_iso()
        record["last_seen"] = stamp
        checked.append({"key": key})

        if product.get("available"):
            if record.get("status") != "in_stock":
                changed = True
                record["status"] = "in_stock"
                record["status_label"] = STATUS_LABEL["in_stock"]
                record["became_available_at"] = stamp
                record.pop("sold_out_at", None)
                log(f"  watchlist: {key} just became available")
                if cfg.get("notify", {}).get("restock", True):
                    events.append({
                        "type": "restock", "item": dict(record, key=key),
                        "detail": "Now live",
                    })
        elif record.get("status") in ("sold_out", "coming_soon", "unknown"):
            prefix = region_cfg.get("path_prefix", "")
            is_upcoming, drop_iso = upcoming_drop_from_product_page(fetcher, region_cfg, handle, prefix)
            new_status = "coming_soon" if is_upcoming else "sold_out"
            if new_status != record.get("status"):
                changed = True
                log(f"  watchlist: {key} status {record.get('status')} -> {new_status}"
                    f" (countdown {'still future' if is_upcoming else 'has lapsed'})")
                record["status"] = new_status
                record["status_label"] = STATUS_LABEL[new_status]
            if is_upcoming and record.get("drop_time") != drop_iso:
                changed = True
                record["drop_time"] = drop_iso

    if not checked:
        log("  watchlist check: no items had a baseline to compare against yet")
        return 0

    # Write nothing when nothing moved, so the workflow has nothing to commit.
    # This used to save unconditionally, and because last_seen changes every
    # time, that meant a commit every 5 minutes: 288 a day, each one also
    # triggering a Pages rebuild. GitHub started starving the repo of runners
    # for it (a job seen queued 31 hours; the hourly full scan degraded from
    # hourly to 5-11 hourly, and its schedule: trigger stopped firing at all).
    # The cost is only that "last checked" on the dashboard now tracks the
    # last meaningful change rather than the last poll — cheap next to
    # losing the scan cadence that finds new cars in the first place.
    if not changed:
        log(f"  watchlist check: {len(checked)} item(s), nothing changed — writing nothing")
        return 0

    send_ntfy(events, dry_run=False)
    # Same log the full scan writes to, so a restock caught here survives
    # in the dashboard's activity list instead of vanishing at the next scan.
    record_events(state, events)
    save_state(state)
    write_dashboard_data(state, checked, [], now_iso(), cfg)
    log(f"  watchlist check: {len(checked)} item(s), {len(events)} event(s)")
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
    parser.add_argument("--watchlist-check", action="store_true",
                        help="fast poll of just the watchlist (no full scan)")
    args = parser.parse_args()

    if args.notify_test:
        return notify_test()
    if args.watchlist_check:
        return watchlist_check()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
