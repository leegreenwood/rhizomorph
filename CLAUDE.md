# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Rhizomorph is a signal-handling agent (webhooks, scheduled checks) running as a persistent, named Herdr session/workspace on this Mac — a sibling to **Hyphae**, the personal-context MCP server at `/Volumes/Flexdrive/Code/hyphae`. Full architecture and rationale: [README.md](README.md).

**Core design principle — no autonomous actions.** Anything that mutates state must go through Claude Code's normal permission-prompt gate (never run this session with `--dangerously-skip-permissions`) so it surfaces as a Moshi approval on the user's phone via `moshi-hook`. Read-only work runs freely. Keep this invariant when adding any new signal source or receiver.

## Commands

Each receiver (`receivers/netlify/`, `receivers/github/`):
```
npm install                # install deps
npm start                  # run in foreground (node server.js)
```
No test suite, linter, or build step exists in this repo yet.

Each receiver runs as a launchd LaunchAgent (`com.rhizomorph.receiver-<netlify|github>`, plist beside its `server.js`; started at login, restarted if it dies, logs to `logs/out.log`). The process is launched with the **absolute** path to `server.js`, so `pgrep -fl "receivers/<name>/server.js"` still matches. After editing a plist, copy it to `~/Library/LaunchAgents/` and `launchctl bootout` + `bootstrap` it.
```
launchctl kickstart -k gui/$(id -u)/com.rhizomorph.receiver-github   # restart (use -netlify for the other)
launchctl print gui/$(id -u)/com.rhizomorph.receiver-github | grep -E "state|pid|runs|last exit"
```

The Herdr "Rhizomorph" workspace (live status board + merged log tail) is built by `bin/rhizomorph-workspace` (idempotent; re-run after a Herdr/Mac restart). The board itself is `bin/rhizomorph-status` (read-only).

## Architecture

Each signal source lives under `receivers/<source>/` as an independent, self-contained Node service (own `package.json`, own `.env`, own LaunchAgent plist). Two exist: `receivers/netlify/` and `receivers/github/`.

**`receivers/netlify/server.js`** — Express app, one route (`POST /netlify/webhook`):
1. Verifies Netlify's `X-Webhook-Signature` header as a JWS (HS256, issuer `netlify`, `sha256` claim checked against a SHA-256 of the raw request body) against `NETLIFY_WEBHOOK_SECRET`. Rejects with 403 on any failure — the endpoint is public via Tailscale Funnel, so this check is load-bearing, not decorative.
2. Reads `payload.state` (`"ready"` = success, `"error"` = failure) rather than trusting which Netlify event fired the request, since one endpoint receives multiple event subscriptions.
3. Resolves a friendly site name via `sites.json` (`by_site_id`/`by_name` override maps; falls back to the payload's own site name — new sites need no code change, just a webhook pointed at this URL).
4. Fires a one-way push through Moshi's webhook API (`POST https://api.getmoshi.app/api/webhook` with `{token, title, message}`) — this is notify-only and distinct from `moshi-hook`'s approval mechanism; nothing here waits for a response.

**`receivers/github/server.js`** — Express app, one route (`POST /github/webhook`):
1. Verifies GitHub's `X-Hub-Signature-256` header (plain HMAC-SHA256 of the raw request body, `sha256=<hex>`) against `GITHUB_WEBHOOK_SECRET` via `crypto.timingSafeEqual`. Same load-bearing rationale as Netlify's check — public via Tailscale Funnel.
2. Only acts on `X-GitHub-Event: workflow_run` with `payload.action === "completed"` (workflow_run also fires on `requested`/`in_progress`, which have no `conclusion` yet) — each wired repo's webhook is configured to send only "Workflow runs" events, so other event types shouldn't normally arrive, but the header check is a second gate regardless.
3. Maps `payload.workflow_run.conclusion` (`"success"`/`"failure"`; anything else — `cancelled`, `skipped` — is ignored) to a Moshi push, same pattern as Netlify's `ready`/`error`. No friendly-name override file — `payload.repository.full_name` is used directly, and the message includes the run's `html_url` so a failure push can be tapped straight into the failed run's logs.
4. Fires the same one-way Moshi push as the Netlify receiver (identical `notifyMoshi()` function, same token/env var name pattern).

Repo scope is enforced by which repos have the webhook configured (GitHub Settings → Webhooks, per repo, events restricted to "Workflow runs") — the receiver itself doesn't allowlist by repo, matching how the Netlify receiver trusts whatever hits it with a valid signature.

Exposure: Tailscale Funnel path-routes `/netlify/webhook` and `/github/webhook` to `127.0.0.1:8788` and `127.0.0.1:8789` respectively, off the same tailnet hostname Hyphae's `/mcp` proxy already uses (`tailscale funnel status` shows all three). Adding a new signal source means adding another `--set-path` route rather than a new hostname/port scheme elsewhere.

## Scheduled checks

`checks/<name>/` holds scheduled scripts (own README, `.env`, plist). First one: `checks/repeat-after-completion/` — run `node --test` there for tests and `node repeat.js --dry-run` before any real run. It writes to Apple Reminders through `~/bin/remctl` (not on PATH), only for reminders carrying an `#after-*` tag, so it is exempt from the approval gate; never extend that exemption to other checks.

`checks/board-labels/` (Python, own `.venv`, `pip install -r requirements.txt`) prints a label per Gmail thread tagged `board` via Superhuman's MCP server and swaps the tag to `on-board`; run `board_labels.py --dry-run` before a real run, `--auth` once to store the OAuth refresh token in the Keychain. Its only write is that one tag swap on opted-in threads — the same narrow exemption, not a precedent.

## Local model helper

> **Retired 2026-10-28.** Claude no longer uses this helper; small jobs go to Codex agents on LM Studio or Azure, run in Herdr panes (see `~/Code/talkterms/MULTI-MODEL.md` and the `codex-agent` skill). The script and the MLX scripts are kept for reference.

`bin/local-llm` sends a one-shot prompt (plus `-f FILE` context) to the local MLX server on `127.0.0.1:8080` and prints the reply — use it to hand off small, self-contained jobs (boilerplate drafts, file summaries, test-case lists) instead of doing them yourself, then review the reply before using it. It only reads and prints, so it needs no approval; anything written from its output goes through the normal prompts. Thinking is off by default (`-r` turns it on; raise `-t` with it). If it reports no server, start one with `raycast/mlx-start.sh`. Every call is logged to `~/Library/Logs/local-llm.log`.

## Known environment gotchas (this Mac specifically)

- **launchd cannot spawn from `/Volumes/Flexdrive`.** A LaunchAgent pointed at a path on that volume fails immediately (`EX_CONFIG`, no logs ever written) even though the same code runs fine interactively. This repo now lives on the internal disk (`~/Code/rhizomorph`), which is why the receivers and checks can use launchd. If it is ever moved back onto Flexdrive, expect every agent here to stop spawning.
- **Multiple unrelated services are literally named `server.js`** (Hyphae's MCP HTTP proxy is one, and now two receivers in this repo). Never `pkill`/`kill` by the bare pattern `"node server.js"` — it will hit unrelated processes too. Target by full path instead: `pgrep -fl "receivers/netlify/server.js"` or `pgrep -fl "receivers/github/server.js"`.
- **`tailscale funnel --set-path=/foo` needs the path repeated in the proxy target, or it silently 404s.** `tailscale funnel --bg --set-path=/github/webhook 127.0.0.1:8789` creates a route that strips the path before proxying — requests to the public URL 404 even though the local server is fine. The fix is to include the full path in the target too: `tailscale funnel --bg --set-path=/github/webhook http://127.0.0.1:8789/github/webhook`. `tailscale serve status --json` shows the actual `Proxy` value per handler if a route seems to be dropping requests — compare it against a known-working one.
