# board-labels

Tag an email `To Do` (in Superhuman or Gmail) → a label comes out of the Brother QL-810W → the email is retagged `@Triaged`. Putting the label on the card board is the act of pulling the item into active work. One script, no AI call, no dashboard.

## How it fits Rhizomorph

A scheduled check like `repeat-after-completion`: own folder, own `.env` (gitignored), JSON-lines log in `logs/`, failures pushed through the shared Moshi webhook, run by a per-user launchd LaunchAgent (this repo is on local disk, so the Flexdrive launchd problem does not apply). It mutates only threads the user has opted in by tagging them, and only swaps that one tag, so it is exempt from the approval gate the same way `repeat-after-completion` is. Don't extend that exemption.

## Mail access: Gmail over IMAP (default) or Superhuman MCP

Selected by `MAIL_BACKEND` in `.env` (`imap` is the default, `superhuman` the alternative). Nothing else in the script depends on which one is used.

**`imap` — independent of Superhuman.** Talks to `imap.gmail.com` with a per-account **Google app password**, using Gmail's IMAP extensions against *All Mail*: `X-GM-LABELS` finds the `To Do` messages, `X-GM-THRID` groups them into threads, and the label swap is `+X-GM-LABELS "@Triaged"` then `-X-GM-LABELS "To Do"` (add first, so a thread is never left with neither). Listing opens All Mail read-only. Thread ids are converted to the same lowercase hex the Gmail API uses, so `printed-ids.json` stays valid. App passwords don't expire unless you revoke them, so there is no token refresh to fail; each one lives in the login Keychain (service `rhizomorph-board-labels`, account `gmail-imap:<email>`), never in the repo.

**`superhuman` — the original route.** Superhuman's remote MCP server (`https://mcp.mail.superhuman.com/mcp`) with OAuth/PKCE, tokens in the Keychain, one-time `--auth` browser sign-in on the Mac Studio itself. Its access token lasts 5 days and, in practice, the refresh did not carry unattended runs past that (it failed from 12 Oct 2026), which is why IMAP became the default. Tools used: `list_threads`, `list_labels`, `update_thread`.

### Several Gmail accounts

Set `GMAIL_ACCOUNTS=a@gmail.com,b@gmail.com` in `.env` (default: just `lee.greenwood@gmail.com`). Every run goes through each account in turn:

- Each account has its own app password (`--imap-auth` asks for one per account).
- All accounts use the same two labels (`To Do` → `@Triaged`), which must exist in each.
- One account failing (bad password, IMAP off, network) is logged and alerted but never stops the others; the run then exits 1.
- Gmail thread ids are only unique within an account, so dedupe keys are namespaced: `lee.greenwood@gmail.com` keeps bare ids (the existing `printed-ids.json` stays valid), any other account is stored as `<email>:<thread id>`.
- `run_summary` is logged once per account (with an `account` field).
- To add an account: append it to `GMAIL_ACCOUNTS`, run `--imap-auth <email>`, and dry-run. A new account's existing `To Do` backlog **would all print** — seed it first by adding `<email>:<thread id>` entries to `printed-ids.json`, or clear the `To Do` label on threads you don't want printed.

## One-time setup

1. Make sure both labels exist in every account: `To Do` (the trigger) and `@Triaged`, and that each is set to **Show in IMAP** (Gmail → Settings → Labels). Each run logs `label_missing` and does nothing until they exist.
2. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. `cp .env.example .env` and fill in `MOSHI_WEBHOOK_TOKEN` (and `GMAIL_ACCOUNTS` for more than one account). Set `PRINTER_IP` only if the QL-810W is on Wi-Fi (see Printing).
4. For each account, create an app password at <https://myaccount.google.com/apppasswords> (needs 2-Step Verification), then run `.venv/bin/python board_labels.py --imap-auth [EMAIL ...]` in a terminal. It prompts for each password (hidden), proves it can log in, checks the labels are visible over IMAP, and saves it to the Keychain. Re-run only if you revoke a password.

## Usage

```
.venv/bin/python board_labels.py --dry-run   # render each not-yet-printed email to logs/dry-run-<thread>.png, print nothing, change no labels
.venv/bin/python board_labels.py             # print, then swap To Do → @Triaged
```

Each run, per account: list threads labelled `To Do` → for each, render (subject bold on up to three lines, sender and ISO date smaller, ellipsised, emoji stripped; 696 px wide for 62 mm tape, height grows with the subject) → print with auto-cut → record the thread id in `printed-ids.json` (gitignored safety net; a thread in that file is never printed again even if the label swap failed, and a dry run skips those too) → swap the labels. A print failure logs `failed`, leaves the labels alone, and pushes a Moshi alert; the next run retries.

`printed-ids.json` was pre-seeded on 7 Oct 2026 with the 107 threads that already carried `To Do`, so switching to that label did not print the whole backlog. To put one of those on the board, remove its thread id from the file (or re-tag it after clearing `To Do`); it prints on the next run.

Log: `logs/board-labels.log` (`run_summary` per account gives matched / printed / skipped / failed). A repeated identical failure pushes a Moshi alert at most every 6 hours (state in `logs/alert-state.json`), and the alert re-arms once a run succeeds.

Tests: `.venv/bin/python -m unittest -v` (IMAP parsing against a fake server, multi-account orchestration, dedupe, failure isolation, alert throttle).

## Printing

`brother_ql_next` (fork of `pklaus/brother_ql`, whose last release was 2019; `brother_ql_next` 0.12.0 was released May 2026 and lists the QL-810W). Roll type is the `ROLL` constant, default `62` (continuous 62 mm); the cutter fires after each label.

- `PRINTER_IP` set → network backend, port 9100.
- `PRINTER_IP` unset → USB fallback: the raster is handed raw to the CUPS queue macOS creates for the USB-attached printer (`lp -d Brother_QL_810W -o raw`). No libusb needed. Override the queue name with `PRINTER_CUPS_QUEUE` if `lpstat -p` shows a different one.

### Printer offline

`lp` reports success as soon as CUPS queues a job, even with the printer off or unplugged, so the USB path waits for the job to actually leave the queue (up to 60 s). If it doesn't, the job is cancelled (so it can't print later as a duplicate), the thread is **not** retagged or recorded in `printed-ids.json`, no further labels are tried that run, and a throttled Moshi alert goes out. The next 5-minute run retries once the printer is back. (The network path waits on the printer directly.)

## Scheduling

`com.rhizomorph.board-labels.plist` runs it every 5 minutes (`StartInterval` 300).

```
cp com.rhizomorph.board-labels.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rhizomorph.board-labels.plist
launchctl kickstart gui/$(id -u)/com.rhizomorph.board-labels      # run now
launchctl bootout gui/$(id -u)/com.rhizomorph.board-labels        # remove
```

The Keychain item is readable from launchd while the login Keychain is unlocked, i.e. while you are logged in on the Mac Studio.
