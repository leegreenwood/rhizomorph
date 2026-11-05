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
- **Alerting**: `moshi-hook` is paired and running as a persistent `brew services` daemon, with Claude Code and Codex hooks installed. It reports Claude Code's approval prompts; Codex's should work the same way, but that hasn't been confirmed end to end. Point-to-point notifications (e.g. from the Netlify receiver below) instead POST directly to Moshi's webhook API (`https://api.getmoshi.app/api/webhook`) with a bearer-style `token` — a separate, simpler mechanism from the hook-based approval flow.
- **Reachability for external sources** (anything not already on the tailnet, e.g. Netlify): exposed via Tailscale Funnel, path-routed per service off the same hostname (see below).

## Signal sources

### 1. Netlify deploy notifications — live

[`receivers/netlify/`](receivers/netlify/) is a small Express receiver that verifies Netlify's JWS-signed deploy webhook, then posts a Moshi push: quiet confirmation on `deploy_succeeded`, alert with the error detail on `deploy_failed`. It never mutates anything — pure notify, consistent with the no-autonomous-actions principle (there's nothing to approve here).

- **Runs**: as a launchd LaunchAgent (`com.rhizomorph.receiver-netlify`, see [Process supervision](#process-supervision)), listening on `127.0.0.1:8788`.
- **Exposed at**: `https://lees-mac-studio.taile2c8d3.ts.net/netlify/webhook` via `tailscale funnel --set-path=/netlify/webhook`. Hyphae's `/mcp` is not on the Funnel: it is tailnet-only, on port 8443 of the same hostname.
- **Config**: `receivers/netlify/.env` (gitignored) needs `PORT`, `NETLIFY_WEBHOOK_SECRET` (the JWS secret, also pasted into each site's Netlify notification config), and `MOSHI_WEBHOOK_TOKEN`.
- **Wired up sites**: all 12 sites on the account (havantdofe, ownpaceoutfitters, weareradiant, iyvsmicrolending, greenwoodbushcraft, hermes-rmp7955, encounter-spine, zanshinarchery, leegreenwoodblog, iyvs, sprightly-bombolone-c64d08, gphservices), each with a `url` hook for `deploy_created` (success) and `deploy_failed` pointing at the Funnel URL with the shared JWS secret. Created with the Netlify CLI (`brew install netlify-cli`, then `netlify login`): `netlify api createHookBySiteId --data '{"site_id":"…","body":{"type":"url","event":"deploy_created","data":{"url":"<funnel url>","signature_secret":"<secret>"}}}'` (the `body` wrapper is what the earlier 422s were missing); list with `netlify api listHooksBySiteId`. The old Hermes `url` hooks (on iyvs and hermes-rmp7955) were deleted. A new Netlify site needs these two hooks adding. Confirmed end-to-end with a real signed delivery on 5 Nov 2026 (a `zanshinarchery` deploy); deliveries show in `receivers/netlify/logs/out.log`.
- **To restart**: `launchctl kickstart -k gui/$(id -u)/com.rhizomorph.receiver-netlify`.

### 2. GitHub Actions run notifications — live

[`receivers/github/`](receivers/github/) is a small Express receiver that verifies GitHub's HMAC-signed webhook, then posts a Moshi push on Actions `workflow_run` completion: success or failure, including a link straight to the run's logs. Built specifically to cover deploys that go through GitHub Actions to Azure rather than through Netlify — same blind spot the Netlify receiver doesn't see. Notify-only, same as Netlify — nothing here mutates anything.

- **Runs**: as a launchd LaunchAgent (`com.rhizomorph.receiver-github`, see [Process supervision](#process-supervision)), listening on `127.0.0.1:8789`.
- **Exposed at**: `https://lees-mac-studio.taile2c8d3.ts.net/github/webhook` via `tailscale funnel --set-path=/github/webhook`, alongside `/netlify/webhook`.
- **Config**: `receivers/github/.env` (gitignored) needs `PORT`, `GITHUB_WEBHOOK_SECRET` (shared HMAC secret, also pasted into each repo's GitHub webhook config), and `MOSHI_WEBHOOK_TOKEN`.
- **Wired up repos**: `attested` (the actual motivating case — deploys to Azure Container Apps + Azure Static Web Apps via `deploy.yml`/`deploy-website.yml`, neither visible to Netlify), plus iyvs, zanshinarchery, greenwoodbushcraft, ownpaceoutfitters (also Netlify sites, added for consistency). All added via `gh api repos/{owner}/{repo}/hooks`, events restricted to `workflow_run` only. Ping delivery confirmed 200 on all five; success/failure notification logic verified against `attested` with signed simulated payloads (a real end-to-end test would require an actual push-triggered Azure deploy, which wasn't done just to test notifications).
- **Known limitation**: Moshi's webhook API rate-limits at 10 notifications/minute on the free tier (`429` on excess) — confirmed by hitting it during testing. The receiver doesn't retry on a dropped push, so a burst of failures in the same minute could mean a later one silently doesn't page.
- **To restart**: `launchctl kickstart -k gui/$(id -u)/com.rhizomorph.receiver-github`.

#### Process supervision

Both receivers run, as do the [repeat-after-completion](checks/repeat-after-completion/) and [board-labels](checks/board-labels/) checks, as per-user **launchd LaunchAgents**: started at login, restarted within ~10s if they crash or are killed (`KeepAlive`), stdout/stderr appended to each receiver's `logs/out.log`. They previously ran as `nohup` processes inside the Herdr session, which silently died on every reboot — launchd was ruled out then because it cannot spawn from `/Volumes/Flexdrive` (`EX_CONFIG`, no logs). The repo now lives on the internal disk, so that limit no longer applies; if it moves back to Flexdrive, every agent here will stop spawning. Plists live beside each `server.js` and are copied into `~/Library/LaunchAgents/`.

#### Herdr window

`bin/rhizomorph-workspace` creates (idempotently) a Herdr workspace named "Rhizomorph": left pane `bin/rhizomorph-status` (LaunchAgent state, listeners, Funnel routes, last check run, last webhook events), right pane a merged `tail -F` of the receiver and check logs. The `com.rhizomorph.workspace` LaunchAgent ([`bin/com.rhizomorph.workspace.plist`](bin/com.rhizomorph.workspace.plist)) runs it once at login, after waiting for the Herdr server, so the panes come back after a Mac restart. It only runs at login, so re-run the script by hand if Herdr alone restarts.

#### Process-naming gotcha

More than one unrelated Node service on this Mac is literally named `server.js` (Hyphae's MCP HTTP proxy is one, and now two receivers in this repo). **Never** `pkill -f "node server.js"` or kill by that bare pattern — it will also kill unrelated services, which happened once already during setup. Always target by full path: `pgrep -fl "receivers/netlify/server.js"` or `pgrep -fl "receivers/github/server.js"` — this only works if the process was launched with the absolute path per the restart commands above, not a bare relative `node server.js`.

### 3. Reminders repeat-after-completion — scheduled check

[`checks/repeat-after-completion/`](checks/repeat-after-completion/) gives Apple Reminders "repeat N days/weeks/months after completion" via `#after-<N><d|w|m|y>` tags, using the RemCTL CLI. Hourly (and at login) via a per-user **launchd** LaunchAgent, like the receivers: it is a deterministic script that must run whether or not the Herdr session is up. Mutates only Reminders the user has opted in with a tag, never deletes, so it isn't approval-gated; failures push through the same Moshi webhook as the receivers. Details, flags and install steps in its README.

### 4. Board labels — scheduled check

[`checks/board-labels/`](checks/board-labels/) prints a Brother QL-810W label for every Gmail thread tagged `To Do` (subject, sender, date) and retags it `@Triaged`, so sticking the label on the office card board is the act of pulling an email into active work. Plain Python, no AI call. Mail comes from Gmail over IMAP with per-account app passwords held in the login Keychain, so it runs unattended, with Superhuman's MCP server as the selectable alternative. Every 5 minutes via a per-user launchd LaunchAgent, same reasoning as `repeat-after-completion`; mutates only the one tag on threads the user opted in, so not approval-gated. Details, flags and setup in its README.

### 5. Hyphae pull — scheduled check

[`checks/pull-hyphae/`](checks/pull-hyphae/) runs `git pull --ff-only` on `~/Code/hyphae` at 06:30 through a per-user launchd LaunchAgent, to bring down the daily notes. It only fast-forwards the local working copy and never pushes, so it isn't approval-gated; failures push through the same Moshi webhook as the receivers. Details in its README.

### 6. OmniFocus daily digest — not planned

Was floated as a candidate signal source; confirmed **not** a planned feature. The OmniFocus MCP server has been removed from `.mcp.json`.

### 7. Spores — not wired up yet

Rhizomorph doesn't read [Spores](../spores-app) yet. When it does, it will act only on spores the user flags with **Z** (`spores list --flagged --json`), and flagged RSS text stays untrusted.

## Local model helper

Retired on 2026-10-28. `bin/local-llm` and the MLX scripts are kept for reference only. Small jobs now go to Codex agents in Herdr panes (see `~/Code/talkterms/MULTI-MODEL.md` and the `codex-agent` skill).

## Scheduling approach

Deterministic scripts and the webhook receivers run under **launchd** (see [Process supervision](#process-supervision)), so they survive reboots and don't depend on the Herdr session being alive. In-session `/schedule` routines remain the choice for checks that need the agent's judgement rather than a fixed script.

## Open questions / next steps

- [x] Confirm the name — "Rhizomorph" confirmed, workspace is labeled accordingly.
- [ ] Scope the permission policy precisely: which tools are read-only/auto-approved vs. which require a Moshi approval round-trip.
- [x] Build the Netlify webhook receiver + Tailscale Funnel exposure — live for all 12 sites.
- [x] Build the GitHub Actions webhook receiver + Tailscale Funnel exposure — live for 5 repos, configured via `gh api` rather than the dashboard.
- [ ] Decide whether Rhizomorph stays a single general-purpose agent covering all signal sources, or whether some domains warrant splitting out later (currently leaning toward single agent, single session).
