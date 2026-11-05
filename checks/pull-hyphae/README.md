# pull-hyphae

Runs `git pull --ff-only` on `~/Code/hyphae` at 06:30 local time, so the Mac has the daily notes that Hyphae's GitHub Action commits.

## Behaviour

- **Fast-forward only.** It never creates a merge commit or leaves conflict markers in the vault. If local work blocks the pull, git refuses and changes nothing. Uncommitted edits to other files are left alone.
- **Retries twice**, 1 and then 2 minutes apart, because the network can lag behind a wake from sleep.
- **A failure pushes through Moshi** (exit code 1). The usual cause is a local commit or an uncommitted edit that clashes with what is being pulled, such as today's journal. Fix it by hand.
- If the Mac is asleep at 06:30, launchd runs the job on wake. If it is off, the run is skipped until the next day.

Writes only to the local working copy, and only by fast-forward. It never pushes.

## Usage

```
node pull-hyphae.js    # run it now
```

Logs: `logs/pull-hyphae.log` (JSON lines; `run_summary` gives `ok`, `commits` and `head`). Config: `.env` (`MOSHI_WEBHOOK_TOKEN`, see `.env.example`).

## Scheduling

```
cp com.rhizomorph.pull-hyphae.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rhizomorph.pull-hyphae.plist
launchctl kickstart gui/$(id -u)/com.rhizomorph.pull-hyphae   # run once now
```
