# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Rhizomorph is a signal-handling agent (webhooks, scheduled checks) running as a persistent, named Herdr session/workspace on this Mac — a sibling to **Hyphae**, the personal-context MCP server at `/Volumes/Flexdrive/Code/hyphae`. Full architecture and rationale: [README.md](README.md).

**Core design principle — no autonomous actions.** Anything that mutates state must go through Claude Code's normal permission-prompt gate (never run this session with `--dangerously-skip-permissions`) so it surfaces as a Moshi approval on the user's phone via `moshi-hook`. Read-only work runs freely. Keep this invariant when adding any new signal source or receiver.

## Commands

Netlify receiver (`receivers/netlify/`):
```
npm install                # install deps
npm start                  # run in foreground (node server.js)
```
No test suite, linter, or build step exists in this repo yet.

To restart the live receiver process (it runs as a plain background process inside this workspace, not a system service — see "launchd" below):
```
cd receivers/netlify
set -a && source .env && set +a
nohup node server.js >> logs/out.log 2>&1 &
```

## Architecture

Each signal source lives under `receivers/<source>/` as an independent, self-contained Node service (own `package.json`, own `.env`). There is currently one: `receivers/netlify/`.

**`receivers/netlify/server.js`** — Express app, one route (`POST /netlify/webhook`):
1. Verifies Netlify's `X-Webhook-Signature` header as a JWS (HS256, issuer `netlify`, `sha256` claim checked against a SHA-256 of the raw request body) against `NETLIFY_WEBHOOK_SECRET`. Rejects with 403 on any failure — the endpoint is public via Tailscale Funnel, so this check is load-bearing, not decorative.
2. Reads `payload.state` (`"ready"` = success, `"error"` = failure) rather than trusting which Netlify event fired the request, since one endpoint receives multiple event subscriptions.
3. Resolves a friendly site name via `sites.json` (`by_site_id`/`by_name` override maps; falls back to the payload's own site name — new sites need no code change, just a webhook pointed at this URL).
4. Fires a one-way push through Moshi's webhook API (`POST https://api.getmoshi.app/api/webhook` with `{token, title, message}`) — this is notify-only and distinct from `moshi-hook`'s approval mechanism; nothing here waits for a response.

Exposure: Tailscale Funnel path-routes `/netlify/webhook` to `127.0.0.1:8788` off the same tailnet hostname Hyphae's `/mcp` proxy already uses (`tailscale funnel status` shows both). Adding a new signal source means adding another `--set-path` route rather than a new hostname/port scheme elsewhere.

## Known environment gotchas (this Mac specifically)

- **launchd cannot spawn from `/Volumes/Flexdrive`.** A LaunchAgent pointed at this repo fails immediately (`EX_CONFIG`, no logs ever written) even though the same code runs fine interactively. This is why the Netlify receiver runs as a `nohup`'d background process inside the Herdr session instead of a `brew services`/launchd-managed daemon. Don't reach for launchd for future receivers on this repo without expecting the same failure.
- **Multiple unrelated services are literally named `server.js`** (Hyphae's MCP HTTP proxy is one). Never `pkill`/`kill` by the bare pattern `"node server.js"` — it will hit Hyphae's process too. Target by full path instead: `pgrep -fl "receivers/netlify/server.js"`.
