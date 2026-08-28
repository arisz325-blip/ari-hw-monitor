/**
 * Cloudflare Worker for the Hot Wheels monitor. Two jobs:
 *
 * 1. fetch()     — lets the dashboard toggle a car on/off the stock-probe
 *                  watchlist with a plain POST, no redirect to GitHub and
 *                  no token in the page.
 *                  Body: {"action": "add" | "remove", "key": "US:handle"}
 *
 * 2. scheduled() — fires the watchlist fast check on a cron trigger,
 *                  because GitHub's own scheduler is best-effort: measured
 *                  here at a 32-minute median (94 runs, all successful,
 *                  gaps 20–117 min) against a requested 5. Working since
 *                  2026-08-24, verified punctual (dispatches landed at
 *                  17:40:05 and 17:45:05 UTC, exactly 5 minutes apart).
 *
 * !! READ THIS BEFORE EDITING THIS FILE !!
 *
 * Redeploying from the dashboard's Quick Edit silently breaks the cron
 * trigger's binding to the deployed version. Settings > Triggers keeps
 * showing "Every 5 minutes" and the API keeps listing the schedule, but
 * scheduled() is never called again — no heartbeat, no errors, nothing.
 * It cost hours to find, because every symptom points at the code.
 *
 * So: after ANY deploy from the dashboard editor, delete the cron trigger
 * and add it back. Then confirm with a `cron fired:` line in Observability
 * (or a repository_dispatch run in the repo's Actions tab) rather than
 * trusting the Settings page, which lies about this.
 *
 * That is also why watchlist-check.yml keeps its own GitHub schedule: this
 * binding will break again on the next edit, and the fallback means the
 * monitor degrades to ~30 minutes instead of stopping.
 *
 * The GitHub token lives only here, as a Worker secret (GITHUB_TOKEN). It
 * needs Contents: read and write on this one repo — the same permission the
 * config.json edits already use; repository_dispatch rides on it too.
 *
 * Deploy: paste this file into the Cloudflare dashboard's Worker editor
 * ("Quick edit"), set the GITHUB_TOKEN secret under Settings > Variables,
 * and add an every-5-minutes Cron Trigger under Settings > Triggers.
 * (The literal expression is on CRON_HINT below — a cron string starting
 * with a star and a slash cannot be written inside a block comment,
 * because the slash-star pair would close the comment early.)
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

// The Cron Trigger to set in the Cloudflare dashboard (Settings > Triggers).
// Cloudflare stores this itself; the constant is here so the intended
// cadence is visible next to the code it drives.
const CRON_HINT = "*/5 * * * *";  // every 5 minutes

// repository_dispatch event_types — each must match the `types:` list in the
// corresponding workflow, or the dispatch is accepted with a 204 and then
// silently does nothing.
const FAST_CHECK_EVENT = "watchlist-check";   // .github/workflows/watchlist-check.yml
const FULL_SCAN_EVENT = "stock-check";        // .github/workflows/check.yml

// Which of the twelve 5-minute slots in an hour do what.
//
// GitHub throttles its own `schedule:` triggers on this repo hard — the
// hourly full scan degraded to 9-10 hour gaps and stayed there, while
// repository_dispatch from here has been honoured hundreds of times without
// a single miss. So the full scan is driven from here too. Everything still
// *runs* on GitHub; this only presses the button.
//
// Both workflows share one concurrency group, hence the gaps:
//   :00        full scan (takes ~17.5 min)
//   :05 :10 :15  skipped — the scan owns the runner; a fast check dispatched
//                now could only sit pending and be cancelled by the next one
//   :55        skipped — leaves the runway clear, so the scan doesn't arrive
//                behind a still-running fast check and risk being the
//                *pending* run that the next dispatch cancels
//   rest       fast check
const FULL_SCAN_MINUTE = 0;
const SKIP_MINUTES = new Set([5, 10, 15, 55]);

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

async function dispatch(eventType, token) {
  // Note this deliberately does not go through githubJson(): /dispatches
  // answers 204 with an empty body, so parsing it as JSON would throw on
  // the success path.
  const resp = await fetch(`https://api.github.com/repos/${REPO}/dispatches`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "ari-hw-monitor-watchlist-worker",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event_type: eventType }),
  });
  // Logged either way, on purpose. Logging only failures made "no log
  // line" ambiguous between "it worked", "it never ran", and "the log
  // just isn't captured here" — which cost a diagnosis cycle. Nothing
  // retries: the next tick is 5 minutes away, and both workflows still
  // carry their own GitHub schedule underneath.
  const detail = resp.ok ? "" : `: ${await resp.text()}`;
  console.log(`dispatch ${eventType} -> HTTP ${resp.status}${detail}`);
  return resp.ok;
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
  // Cron Trigger entry point (see CRON_HINT). Kept separate from fetch() on
  // purpose: this is the one privileged action here, and it must not be
  // reachable from the public, unauthenticated HTTP endpoint.
  async scheduled(event, env, ctx) {
    // Heartbeat first, before anything that can fail, so the log
    // distinguishes "the cron never reached this code" from "it ran and
    // the dispatch failed". Awaited rather than handed to ctx.waitUntil()
    // so both lines are attributed to this invocation.
    const minute = new Date(event.scheduledTime).getUTCMinutes();
    const slot = String(minute).padStart(2, "0");

    if (minute === FULL_SCAN_MINUTE) {
      console.log(`cron fired at :${slot} — full scan`);
      await dispatch(FULL_SCAN_EVENT, env.GITHUB_TOKEN);
      return;
    }
    if (SKIP_MINUTES.has(minute)) {
      console.log(`cron fired at :${slot} — skipped (see SKIP_MINUTES)`);
      return;
    }
    console.log(`cron fired at :${slot} — fast check`);
    await dispatch(FAST_CHECK_EVENT, env.GITHUB_TOKEN);
  },

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
