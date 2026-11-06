# spores

Handles the spores Lee flags with **Z** in [Spores](../../../spores-app). Each one is read, an action is proposed, Lee approves or declines it on his phone through Moshi, and only then does anything happen. Today the one action is adding an Apple Reminder.

## Behaviour

Two parts, so an idle minute costs nothing:

1. **`tick.js`** runs every minute under launchd. It runs `spores list --flagged`. If something is flagged and no worker is running, it starts the worker in the **Spores pane** of the Rhizomorph Herdr workspace (bottom right). It only starts the worker if the pane is at a bare shell, because `herdr pane run` types into whatever is there.
2. **`process.js`** is the worker. It stays in the pane until nothing is flagged, checking for newly flagged spores every 30 seconds. It sends one card at a time, oldest flag first, and the next follows within 30 seconds of the answer. When two cards were out at once, approving one lost the other from the phone while its request carried on waiting. For each spore it:
   - proposes an action, using a Claude call with no tools (Haiku by default). The call gets the live list names and Hyphae's `systems/Reminders.md` as guidance.
   - checks the proposal in code: a known action, a list that really exists, a real date that isn't past. Three failed attempts count as a failure.
   - asks with `moshi-hook ask --require-remote`, so only the phone can approve.
   - on approval, re-reads the spore. If it has been edited or unflagged since, nothing happens.
   - runs the fixed RemCTL command.

### Outcomes

| What happened | Spore afterwards | Tag |
|---|---|---|
| Approved and added | `processed`, flag cleared | `rhizomorph` |
| Declined | still `new`, flag cleared | `rhizomorph-declined` |
| No action proposed, or an RSS spore | still `new`, flag cleared, Moshi push | `rhizomorph-unsure` |
| Classifying or adding failed | still `new`, flag cleared, Moshi error push | `rhizomorph-failed` |

**Flag a spore again to retry it.** Nothing retries by itself, as a retried add could create a duplicate. A new outcome replaces the old tag.

- **No answer:** the card expires after 2 hours and a new one is sent, until it's answered or the spore is unflagged. Later spores wait behind it, and the short expiry means a lost card can't stall them for long.
- **Moshi unreachable:** tried again every 5 minutes, using the saved proposal.
- **Spore edited or unflagged while a card is out:** the approval is withdrawn. The card stays on the phone, but answering it does nothing.
- **Worker stopped while a card is out:** the next run sends a new card, and the old one does nothing.

## Why the model can't act

The model only ever proposes. It has no tools, settings or MCP servers, so a note can talk it into a bad proposal but into nothing else, and Lee sees that proposal before it happens. This is deliberately not a full Claude Code session waiting on permission prompts: Lee's user settings already allow commands such as `git push` and `gh` without asking, which a session reading captured text shouldn't inherit, and a raw command makes a poor lock screen card.

RSS spores are written by third parties, so their text never reaches the model. They get the `rhizomorph-unsure` outcome for now.

## Adding an action

Add it to the `action` enum in `proposalSchema`, describe it in `systemPrompt`, check it in `validateProposal`, word its card in `approvalQuestion`, and run it from `settle` in `process.js`. Keep it to a fixed command with checked arguments, never anything the model composes.

## Usage

```
node process.js --dry-run     # show what would be asked for what's flagged now; changes nothing
node process.js               # run the worker in this terminal
node --test                   # unit tests, plus the worker against a throwaway Spores database with stand-ins for Claude, Moshi and RemCTL
```

Logs: `logs/spores.log` (JSON lines, including spore text; `run_summary` gives the counts). State: `logs/state.json` holds saved proposals, and any reminder added whose spore hasn't been updated yet, so a crash can't add it twice.

Optional overrides in `.env`: `SPORES_CHECK_MODEL`, `SPORES_CHECK_ASK_TIMEOUT`, `SPORES_BIN`, `REMCTL_PATH`, `MOSHI_HOOK_BIN`, `CLAUDE_BIN`, `REMINDERS_NOTE`.

## Scheduling

`com.rhizomorph.spores.plist` is a per-user LaunchAgent, every 60 seconds and at login.

```
cp com.rhizomorph.spores.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rhizomorph.spores.plist
launchctl kickstart gui/$(id -u)/com.rhizomorph.spores      # tick now
launchctl bootout gui/$(id -u)/com.rhizomorph.spores        # remove
```

To stop a running worker, press Ctrl-C in the Spores pane. If spores are still flagged, the next tick starts a new worker, so boot the agent out first if you want it to stay stopped.

`tick.js` needs the Herdr server and the built Spores CLI. If either is missing, it logs `tick_failed` and pushes an error at most once an hour. Moshi must be running for cards and alerts, and `suppress-push-without-mux` is one reason the worker lives in a Herdr pane.
