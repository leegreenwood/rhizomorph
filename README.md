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

- **Host**: this Mac, reachable via Tailscale (already set up).
- **Session**: one persistent, named Herdr session ("Rhizomorph") — long-lived, not spun up on demand.
- **Agent runtime**: Claude Code running inside that Herdr session.
- **Alerting**: `moshi-hook`, paired and running as a persistent service (`brew services start moshi-hook`), reads `$HERDR_ENV`/`$HERDR_SESSION` automatically so every event is tagged to the Rhizomorph session/workspace. Tapping an alert in Moshi reconnects straight into that session.
- **Reachability for external sources** (anything not already on the tailnet, e.g. Netlify): needs Tailscale Funnel to expose the receiving endpoint publicly.

## Initial signal sources (candidates)

1. **Netlify deploy notifications** — Outgoing webhook on `deploy_succeeded` / `deploy_failed`, posted to a receiver in the Rhizomorph session, which raises a Moshi alert (success = quiet confirmation, failure = alert with the error detail from the payload). Optional: verify Netlify's JWS signature since the endpoint is public.
2. **OmniFocus daily digest** — scheduled via Claude Code's `/schedule` (Routines) *inside* the persistent session (rather than external cron/launchd), querying OmniFocus via its MCP server and sending a Moshi summary at a set time each day.

## Scheduling approach

Favoring **in-session `/schedule`** over headless `claude -p` + cron/launchd: since the Herdr session is meant to stay up long-term anyway, session-scoped routines survive as long as Rhizomorph does, and everything — webhook handling and scheduled checks — lives in one coherent agent identity rather than being split across a separate cron script.

## Open questions / next steps

- [ ] Confirm the name — currently "Rhizomorph" (a mycelial term for the thick nutrient/signal-carrying cords in a fungal network, distinct from the fine individual hyphae).
- [ ] Scope the permission policy precisely: which tools are read-only/auto-approved vs. which require a Moshi approval round-trip.
- [ ] Build the Netlify webhook receiver + Tailscale Funnel exposure.
- [ ] Write the OmniFocus digest `/schedule` prompt.
- [ ] Decide whether Rhizomorph stays a single general-purpose agent covering all signal sources, or whether some domains warrant splitting out later (currently leaning toward single agent, single session).
