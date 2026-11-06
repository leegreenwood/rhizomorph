# pull-hyphae

At 08:00 local time, starts Hyphae's daily note workflow on GitHub, waits for it to finish, then runs `git pull --ff-only` on `~/Code/hyphae`, so the Mac has today's note.

GitHub has been starting the workflow's own schedule hours late, so this job starts it directly with `gh workflow run`. The schedule stays as a backup for days the Mac is off. The workflow skips if today's note already exists, so running it twice can't create a duplicate.

## Behaviour

- **Workflow:** starts only `daily-note.yml` on `leegreenwood/hyphae` and waits up to 5 minutes. If it fails or times out, the job alerts and still pulls.
- **Fast-forward only.** The pull never creates a merge commit or leaves conflict markers in the vault. If local work blocks it, git refuses and changes nothing. Uncommitted edits to other files are left alone.
- **Retries twice**, 1 and then 2 minutes apart, when starting the workflow or pulling fails, because the network can lag behind a wake from sleep.
- **A failure pushes through Moshi** (exit code 1). The usual cause is a local commit that hasn't been pushed, or an uncommitted edit that clashes with what is being pulled, such as today's journal. Fix it by hand.
- If the Mac is asleep at 08:00, launchd runs the job on wake. If it is off, the run is skipped until the next day.

Its only writes are the commit that workflow makes and the local fast-forward. It never pushes from this Mac.

## Usage

```
node pull-hyphae.js    # run it now
```

Needs `gh` logged in with the `workflow` scope. Logs: `logs/pull-hyphae.log` (JSON lines; `run_summary` gives `ok`, `commits`, `head` and `errors`). Config: `.env` (`MOSHI_WEBHOOK_TOKEN`, see `.env.example`).

## Scheduling

```
cp com.rhizomorph.pull-hyphae.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rhizomorph.pull-hyphae.plist
launchctl kickstart gui/$(id -u)/com.rhizomorph.pull-hyphae   # run once now
```
