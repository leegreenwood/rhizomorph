# Rhizomorph

A named agent — a single, long-lived Herdr session on the Mac dedicated to handling inbound *signals*: webhooks and scheduled checks. Sibling system to **Hyphae** (the personal context server): where Hyphae holds context, Rhizomorph senses and responds to events.

Replaces **Hermes**, a previous Slack-based notification/routing tool that only surfaced things for manual triage. Rhizomorph's key upgrade is that it can *act* — not just notify — subject to the approval rule below.

## Core design principle

**No autonomous actions.** Every action that mutates state must be approved before it executes. Read-only queries (checking a status, summarizing something) can run freely; anything that writes, retries, or changes something waits for a human yes.

This is enforced structurally, not just by convention:
- Claude Code's default permission model already pauses on tool calls that would mutate state unless `--dangerously-skip-permissions` is passed — so simply *not* passing that flag gives the gating for free.
- Moshi's agent hooks (via `moshi-hook`) surface exactly this pause as an **approval** event — Approve/Deny from the lock screen, Dynamic Island, or Apple Watch, without opening the app or reconnecting to a terminal.
- Approvals and turn-completions arrive as separate, distinguishable event types in Moshi's inbox.

## Architecture

- **Host**: this Mac, reachable via Tailscale (`lees-mac-studio.taile2c8d3.ts.net`).
- **Session**: one persistent, named Herdr session/workspace ("Rhizomorph") — long-lived, not spun up on demand. This repo's `cwd` is that workspace.
- **Agent runtime**: Claude Code running inside that Herdr session.
- **Alerting**: `moshi-hook` is paired and running as a persistent `brew services` daemon, with Claude Code hooks installed. It reports Claude Code's own approval prompts. Point-to-point notifications (e.g. from the Netlify receiver below) instead POST directly to Moshi's webhook API (`https://api.getmoshi.app/api/webhook`) with a bearer-style `token` — a separate, simpler mechanism from the hook-based approval flow.
- **Reachability for external sources** (anything not already on the tailnet, e.g. Netlify): exposed via Tailscale Funnel, path-routed per service off the same hostname (see below).

## Signal sources

### 1. Netlify deploy notifications — live

[`receivers/netlify/`](receivers/netlify/) is a small Express receiver that verifies Netlify's JWS-signed deploy webhook, then posts a Moshi push: quiet confirmation on `deploy_succeeded`, alert with the error detail on `deploy_failed`. It never mutates anything — pure notify, consistent with the no-autonomous-actions principle (there's nothing to approve here).

- **Runs**: as a plain background Node process inside this Herdr workspace (started with `nohup`, not a system service — see [Why not launchd](#why-not-launchd) below), listening on `127.0.0.1:8788`.
- **Exposed at**: `https://lees-mac-studio.taile2c8d3.ts.net/netlify/webhook` via `tailscale funnel --set-path=/netlify/webhook`, alongside Hyphae's existing `/mcp` path on the same hostname.
- **Config**: `receivers/netlify/.env` (gitignored) needs `PORT`, `NETLIFY_WEBHOOK_SECRET` (the JWS secret, also pasted into each site's Netlify notification config), and `MOSHI_WEBHOOK_TOKEN`.
- **Wired up sites**: iyvs, zanshinarchery, greenwoodbushcraft, ownpaceoutfitters (added manually via each site's *Project configuration → Notifications → Deploy notifications*, since the Netlify API's `createHookBySiteId` rejected every payload shape tried with an unhelpful 422 — the dashboard path was faster and is the officially documented method anyway).
- **To restart**: from within the Rhizomorph workspace, `cd receivers/netlify && set -a && source .env && set +a && nohup node "$(pwd)/server.js" >> logs/out.log 2>&1 &` — see the process-naming gotcha below first.

### 2. GitHub Actions run notifications — live

[`receivers/github/`](receivers/github/) is a small Express receiver that verifies GitHub's HMAC-signed webhook, then posts a Moshi push on Actions `workflow_run` completion: success or failure, including a link straight to the run's logs. Built specifically to cover deploys that go through GitHub Actions to Azure rather than through Netlify — same blind spot the Netlify receiver doesn't see. Notify-only, same as Netlify — nothing here mutates anything.

- **Runs**: as a plain background Node process inside this Herdr workspace (`nohup`, not a system service — see [Why not launchd](#why-not-launchd)), listening on `127.0.0.1:8789`.
- **Exposed at**: `https://lees-mac-studio.taile2c8d3.ts.net/github/webhook` via `tailscale funnel --set-path=/github/webhook`, alongside `/netlify/webhook` and Hyphae's `/mcp` on the same hostname.
- **Config**: `receivers/github/.env` (gitignored) needs `PORT`, `GITHUB_WEBHOOK_SECRET` (shared HMAC secret, also pasted into each repo's GitHub webhook config), and `MOSHI_WEBHOOK_TOKEN`.
- **Wired up repos**: `attested` (the actual motivating case — deploys to Azure Container Apps + Azure Static Web Apps via `deploy.yml`/`deploy-website.yml`, neither visible to Netlify), plus iyvs, zanshinarchery, greenwoodbushcraft, ownpaceoutfitters (same four as the Netlify sites, added for consistency). All added via `gh api repos/{owner}/{repo}/hooks`, events restricted to `workflow_run` only. Ping delivery confirmed 200 on all five; success/failure notification logic verified against `attested` with signed simulated payloads (a real end-to-end test would require an actual push-triggered Azure deploy, which wasn't done just to test notifications).
- **Known limitation**: Moshi's webhook API rate-limits at 10 notifications/minute on the free tier (`429` on excess) — confirmed by hitting it during testing. The receiver doesn't retry on a dropped push, so a burst of failures in the same minute could mean a later one silently doesn't page.
- **To restart**: from within the Rhizomorph workspace, `cd receivers/github && set -a && source .env && set +a && nohup node "$(pwd)/server.js" >> logs/out.log 2>&1 &` — see the process-naming gotcha below first.

#### Why not launchd

A `launchd` LaunchAgent pointed at anything under `/Volumes/Flexdrive` fails to spawn at all (`EX_CONFIG`, no logs ever written) — confirmed the identical plist works instantly from `/tmp`. Rather than deploy a separate local-disk copy, each receiver runs inside this persistent Herdr session instead, matching the project's own preference for keeping everything in one coherent agent identity (see Scheduling approach below).

#### Process-naming gotcha

More than one unrelated Node service on this Mac is literally named `server.js` (Hyphae's MCP HTTP proxy is one, and now two receivers in this repo). **Never** `pkill -f "node server.js"` or kill by that bare pattern — it will also kill unrelated services, which happened once already during setup. Always target by full path: `pgrep -fl "receivers/netlify/server.js"` or `pgrep -fl "receivers/github/server.js"` — this only works if the process was launched with the absolute path per the restart commands above, not a bare relative `node server.js`.

### 3. OmniFocus daily digest — not planned

Was floated as a candidate signal source; confirmed **not** a planned feature. The OmniFocus MCP server has been removed from `.mcp.json`.

## Scheduling approach

Favoring **in-session `/schedule`** over headless `claude -p` + cron/launchd: since the Herdr session is meant to stay up long-term anyway, session-scoped routines survive as long as Rhizomorph does, and everything — webhook handling and scheduled checks — lives in one coherent agent identity rather than being split across a separate cron script. (No routines registered yet — the Netlify receiver above is a standalone process, not a `/schedule` routine.)

## Open questions / next steps

- [x] Confirm the name — "Rhizomorph" confirmed, workspace is labeled accordingly.
- [ ] Scope the permission policy precisely: which tools are read-only/auto-approved vs. which require a Moshi approval round-trip.
- [x] Build the Netlify webhook receiver + Tailscale Funnel exposure — live for 4 sites; 4 more `deploy_failed` hooks + 3 more sites' `deploy_succeeded` hooks still to be added manually in the Netlify dashboard.
- [x] Build the GitHub Actions webhook receiver + Tailscale Funnel exposure — live for the same 4 repos as the Netlify sites, configured via `gh api` rather than the dashboard.
- [ ] Decide whether Rhizomorph stays a single general-purpose agent covering all signal sources, or whether some domains warrant splitting out later (currently leaning toward single agent, single session).
