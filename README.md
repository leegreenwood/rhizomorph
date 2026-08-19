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

- **Host**: this Mac, reachable via Tailscale (`lees-mac-mini.taile2c8d3.ts.net`).
- **Session**: one persistent, named Herdr session/workspace ("Rhizomorph") — long-lived, not spun up on demand. This repo's `cwd` is that workspace.
- **Agent runtime**: Claude Code running inside that Herdr session.
- **Alerting**: `moshi-hook` is paired and running as a persistent `brew services` daemon, with Claude Code hooks installed. It reports Claude Code's own approval prompts. Point-to-point notifications (e.g. from the Netlify receiver below) instead POST directly to Moshi's webhook API (`https://api.getmoshi.app/api/webhook`) with a bearer-style `token` — a separate, simpler mechanism from the hook-based approval flow.
- **Reachability for external sources** (anything not already on the tailnet, e.g. Netlify): exposed via Tailscale Funnel, path-routed per service off the same hostname (see below).

## Signal sources

### 1. Netlify deploy notifications — live

[`receivers/netlify/`](receivers/netlify/) is a small Express receiver that verifies Netlify's JWS-signed deploy webhook, then posts a Moshi push: quiet confirmation on `deploy_succeeded`, alert with the error detail on `deploy_failed`. It never mutates anything — pure notify, consistent with the no-autonomous-actions principle (there's nothing to approve here).

- **Runs**: as a plain background Node process inside this Herdr workspace (started with `nohup`, not a system service — see [Why not launchd](#why-not-launchd) below), listening on `127.0.0.1:8788`.
- **Exposed at**: `https://lees-mac-mini.taile2c8d3.ts.net/netlify/webhook` via `tailscale funnel --set-path=/netlify/webhook`, alongside Hyphae's existing `/mcp` path on the same hostname.
- **Config**: `receivers/netlify/.env` (gitignored) needs `PORT`, `NETLIFY_WEBHOOK_SECRET` (the JWS secret, also pasted into each site's Netlify notification config), and `MOSHI_WEBHOOK_TOKEN`.
- **Wired up sites**: iyvs, zanshinarchery, greenwoodbushcraft, ownpaceoutfitters (added manually via each site's *Project configuration → Notifications → Deploy notifications*, since the Netlify API's `createHookBySiteId` rejected every payload shape tried with an unhelpful 422 — the dashboard path was faster and is the officially documented method anyway).
- **To restart**: from within the Rhizomorph workspace, `cd receivers/netlify && set -a && source .env && set +a && nohup node server.js >> logs/out.log 2>&1 &` — see the process-naming gotcha below first.

#### Why not launchd

A `launchd` LaunchAgent pointed at anything under `/Volumes/Flexdrive` fails to spawn at all (`EX_CONFIG`, no logs ever written) — confirmed the identical plist works instantly from `/tmp`. Rather than deploy a separate local-disk copy, the receiver runs inside this persistent Herdr session instead, matching the project's own preference for keeping everything in one coherent agent identity (see Scheduling approach below).

#### Process-naming gotcha

More than one unrelated Node service on this Mac is literally named `server.js` (Hyphae's MCP HTTP proxy is another). **Never** `pkill -f "node server.js"` or kill by that bare pattern — it will also kill Hyphae's service, which happened once already during setup. Always target by full path: `pgrep -fl "receivers/netlify/server.js"`.

### 2. OmniFocus daily digest — not planned

Was floated as a candidate signal source; confirmed **not** an actual planned feature for now. `.mcp.json` still wires up the OmniFocus MCP server in case this changes later, but there's no receiver, schedule, or routine built for it.

## Scheduling approach

Favoring **in-session `/schedule`** over headless `claude -p` + cron/launchd: since the Herdr session is meant to stay up long-term anyway, session-scoped routines survive as long as Rhizomorph does, and everything — webhook handling and scheduled checks — lives in one coherent agent identity rather than being split across a separate cron script. (No routines registered yet — the Netlify receiver above is a standalone process, not a `/schedule` routine.)

## Open questions / next steps

- [x] Confirm the name — "Rhizomorph" confirmed, workspace is labeled accordingly.
- [ ] Scope the permission policy precisely: which tools are read-only/auto-approved vs. which require a Moshi approval round-trip.
- [x] Build the Netlify webhook receiver + Tailscale Funnel exposure — live for 4 sites; 4 more `deploy_failed` hooks + 3 more sites' `deploy_succeeded` hooks still to be added manually in the Netlify dashboard.
- [ ] Decide whether Rhizomorph stays a single general-purpose agent covering all signal sources, or whether some domains warrant splitting out later (currently leaning toward single agent, single session).
