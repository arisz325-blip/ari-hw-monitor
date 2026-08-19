"""A tiny stand-in for Mattel's Shopify storefront, for offline testing.

Serves the three shapes checker.py depends on:
  /{prefix}/collections/hot-wheels     -> HTML with product cards + badges
  /{prefix}/products/{handle}.js       -> Shopify product JSON
  /cart/add.js                         -> the cart-ceiling stock probe (422 body)

A catalog entry may set "stock": N to have the cart probe report N remaining;
without it, the probe endpoint 404s, same as a variant Mattel won't sell.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# handle -> product definition. Tests mutate this between runs.
CATALOG: dict[str, dict] = {}
# collection name -> handles it lists. A collection not listed here (e.g. the
# default "hot-wheels") falls back to showing the whole CATALOG.
COLLECTIONS: dict[str, list[str]] = {}


def set_catalog(products: dict[str, dict]) -> None:
    CATALOG.clear()
    CATALOG.update(products)


def set_collections(mapping: dict[str, list[str]]) -> None:
    COLLECTIONS.clear()
    COLLECTIONS.update(mapping)


def _product_js(handle: str, region_price: int) -> dict:
    product = CATALOG[handle]
    return {
        "id": abs(hash(handle)) % 10**10,
        "title": product["title"],
        "handle": handle,
        "available": product["available"],
        "tags": product.get("tags", []),
        "product_type": product.get("product_type", "Vehicle"),
        "published_at": "2026-08-01T00:00:00Z",
        "images": [f"https://cdn.example/{handle}.jpg"],
        "variants": [{
            "id": abs(hash(handle + "v")) % 10**10,
            "title": "Default",
            "price": region_price,
            "available": product["available"],
            "inventory_policy": "deny",
        }],
    }


def _collection_html(region_price_note: str, collection: str = "hot-wheels") -> str:
    handles = COLLECTIONS.get(collection, list(CATALOG.keys()))
    cards = []
    for handle in handles:
        product = CATALOG[handle]
        badge = product["badge"]
        drop = product.get("drop_time")
        drop_html = f'<span class="drop">Drops {drop}</span>' if drop else ""
        cards.append(f"""
        <div class="product-card">
          <a href="/products/{handle}" class="card-link">
            <img src="https://cdn.example/{handle}.jpg" alt="">
            <h3 class="title">{product['title']}</h3>
            <span class="price">{region_price_note}</span>
            {drop_html}
            <button class="cta">{badge}</button>
          </a>
        </div>""")
    return f"""<!doctype html><html><head><title>Hot Wheels</title></head>
<body><div class="collection-grid">{''.join(cards)}</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        is_au = path.startswith("/en-au")
        path = path[len("/en-au"):] if is_au else path
        price = 4999 if is_au else 2999
        note = "AUD $49.99" if is_au else "USD $29.99"

        if path.startswith("/collections/"):
            collection = path[len("/collections/"):].rstrip("/")
            # Only page 1 has products; page 2+ is empty, as on a real store.
            if int(query.get("page", ["1"])[0]) > 1:
                return self._send(200, "<html><body></body></html>", "text/html")
            return self._send(200, _collection_html(note, collection), "text/html")

        if path.startswith("/products/") and path.endswith(".js"):
            handle = path[len("/products/"):-len(".js")]
            if handle in CATALOG:
                return self._send(200, json.dumps(_product_js(handle, price)), "application/json")
            return self._send(404, "{}", "application/json")

        self._send(404, "not found", "text/plain")

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/cart/add.js":
            return self._send(404, "not found", "text/plain")

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}
        variant_id = payload.get("id")

        handle = next(
            (h for h in CATALOG if abs(hash(h + "v")) % 10**10 == variant_id), None
        )
        stock = CATALOG.get(handle, {}).get("stock") if handle else None
        if stock is None:
            return self._send(404, "{}", "application/json")

        body = json.dumps({
            "description": f"Only {stock} available for Default Title",
            "message": "Cart error",
        })
        return self._send(422, body, "application/json")


def start(port: int = 0) -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"
