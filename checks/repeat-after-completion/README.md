# repeat-after-completion

Apple Reminders can't repeat "N days/weeks/months after the last one was completed". This check adds it, driven by tags. Shells out to RemCTL only.

## Behaviour

A reminder opts in with a tag `#after-<N><unit>` — unit `d`, `w`, `m` or `y` (e.g. `#after-14d`, `#after-1m`). Each run:

1. `remctl export --json` → every reminder with its tags, recurrence and completion date.
2. **Completed** tagged reminder → next due = completion date + interval, written to the *same* reminder (`edit -d`), then reopened (`undone`). List, notes, tags, sharing etc. are untouched. Timed reminders keep their time of day; all-day stay date-only. Month/year maths clamps to the last valid day.
3. **Native recurrence on a tagged reminder is removed** (open or completed) — the tag owns the schedule, and a native rule would auto-advance the reminder before this check could see it completed.
4. Skipped and logged, never modified: unparseable `after-*` tags (`after-0d`, `after-3x`…), more than one `after-*` tag, or no due date.
5. If `completionDate` is unreadable the run time is used and `completion_date_unreadable` is logged.
6. Never deletes anything. Every write is re-read with `remctl info` and checked; a failure is logged and pushed via Moshi (exit code 1).

**Idempotent:** reopening makes the reminder non-completed, so it is not picked up again. Writes are ordered *due date first, then reopen*: if a run dies between them the reminder is still completed with the same completion date, so the next run computes the same date and finishes.

## Usage

```
node repeat.js --dry-run          # print what would change, write nothing
node repeat.js --only <id>        # restrict to one reminder id
node repeat.js                    # for real
node --test                       # unit tests
```

Logs: `logs/repeat-after-completion.log` (JSON lines; `run_summary` gives exported/tagged/rescheduled/skipped/failed counts). Config: `.env` (`MOSHI_WEBHOOK_TOKEN`, see `.env.example`).

## Scheduling

`com.rhizomorph.repeat-after-completion.plist` is a per-user LaunchAgent, hourly (`StartInterval` 3600).

```
cp com.rhizomorph.repeat-after-completion.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rhizomorph.repeat-after-completion.plist
launchctl kickstart gui/$(id -u)/com.rhizomorph.repeat-after-completion      # run now
launchctl bootout gui/$(id -u)/com.rhizomorph.repeat-after-completion        # remove
```

Unlike the receivers this *can* use launchd: the repo lives on the local disk, not `/Volumes/Flexdrive` (see the repo README). RemCTL needs no extra grants under launchd — Reminders, Automation and Full Disk Access belong to RemCTL's signed Capability Host, not the calling process (verified with a launchd dry run). If `remctl doctor` ever reports otherwise, run `remctl onboard`.
