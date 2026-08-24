/**
 * Cloudflare Worker: lets the dashboard toggle a car on/off the stock-probe
 * watchlist with a plain fetch() — no redirect to GitHub, no token in the
 * page. The GitHub token lives only here, as a Worker secret (GITHUB_TOKEN).
 *
 * Deploy: paste this file into the Cloudflare dashboard's Worker editor
 * ("Quick edit"), set the GITHUB_TOKEN secret under Settings > Variables,
 * and set REPO below to match this repo.
 *
 * POST body: {"action": "add" | "remove", "key": "US:some-handle"}
 */

const REPO = "arisz325-blip/ari-hw-monitor";
const BRANCH = "main";
const KEY_RE = /^(US|AU):[a-z0-9-]+$/;

// This endpoint is unauthenticated by design (the dashboard is a static
// page — any token it held would be readable in its source) and its URL is
// public. So assume a stranger can call it, and bound what that costs:
// every watchlisted item is fetched on each fast check, so an unbounded
// list is a way to push the scraper's request volume into territory that
// got us rate-limited by Mattel before. Removals are always allowed.
const MAX_WATCHLIST = 25;

function cors(resp) {
  resp.headers.set("Access-Control-Allow-Origin", "*");
  resp.headers.set("Access-Control-Allow-Methods", "POST, OPTIONS");
  resp.headers.set("Access-Control-Allow-Headers", "Content-Type");
  return resp;
}

async function githubJson(path, token, init = {}) {
  const resp = await fetch(`https://api.github.com/repos/${REPO}${path}`, {
    ...init,
    headers: {
      "Authorization": `Bearer ${token}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "ari-hw-monitor-watchlist-worker",
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
  });
  if (!resp.ok) {
    throw new Error(`GitHub API ${resp.status}: ${await resp.text()}`);
  }
  return resp.json();
}

// Workers' atob/btoa are byte-oriented; this keeps UTF-8 (e.g. car titles
// with curly quotes) intact across the round trip.
function b64ToText(b64) {
  return decodeURIComponent(escape(atob(b64.replace(/\n/g, ""))));
}
function textToB64(text) {
  return btoa(unescape(encodeURIComponent(text)));
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return cors(new Response(null, { status: 204 }));
    }
    if (request.method !== "POST") {
      return cors(new Response("Method not allowed", { status: 405 }));
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return cors(new Response(JSON.stringify({ error: "bad json" }), { status: 400 }));
    }

    const { action, key } = body || {};
    if (!["add", "remove"].includes(action) || typeof key !== "string" || !KEY_RE.test(key)) {
      return cors(new Response(JSON.stringify({ error: "invalid action/key" }), { status: 400 }));
    }

    try {
      const file = await githubJson("/contents/config.json?ref=" + BRANCH, env.GITHUB_TOKEN);
      const cfg = JSON.parse(b64ToText(file.content));
      const probe = cfg.stock_probe || (cfg.stock_probe = {});
      const watchlist = probe.watchlist || (probe.watchlist = []);

      const has = watchlist.includes(key);
      if (action === "add" && !has && watchlist.length >= MAX_WATCHLIST) {
        return cors(new Response(JSON.stringify({
          error: `Watchlist is full (${MAX_WATCHLIST} max) — remove one first.`,
        }), { status: 409, headers: { "Content-Type": "application/json" } }));
      }
      if (action === "add" && !has) watchlist.push(key);
      if (action === "remove" && has) watchlist.splice(watchlist.indexOf(key), 1);
      watchlist.sort();

      if ((action === "add") === has) {
        // Already in the requested state — no commit needed.
        return cors(new Response(JSON.stringify({ ok: true, watchlist, changed: false }),
          { status: 200, headers: { "Content-Type": "application/json" } }));
      }

      await githubJson("/contents/config.json", env.GITHUB_TOKEN, {
        method: "PUT",
        body: JSON.stringify({
          message: `watchlist: ${action} ${key} (via dashboard)`,
          content: textToB64(JSON.stringify(cfg, null, 2) + "\n"),
          sha: file.sha,
          branch: BRANCH,
        }),
      });

      return cors(new Response(JSON.stringify({ ok: true, watchlist, changed: true }),
        { status: 200, headers: { "Content-Type": "application/json" } }));
    } catch (err) {
      return cors(new Response(JSON.stringify({ error: String(err) }), { status: 502 }));
    }
  },
};
