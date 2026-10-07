# board-labels

Tag an email `To Do` in Superhuman → a label comes out of the Brother QL-810W → the email is retagged `@Triaged`. Putting the label on the card board is the act of pulling the item into active work. One script, no AI call, no dashboard.

## How it fits Rhizomorph

A scheduled check like `repeat-after-completion`: own folder, own `.env` (gitignored), JSON-lines log in `logs/`, failures pushed through the shared Moshi webhook, run by a per-user launchd LaunchAgent (this repo is on local disk, so the Flexdrive launchd problem does not apply). It mutates only threads the user has opted in by tagging them, and only swaps that one tag, so it is exempt from the approval gate the same way `repeat-after-completion` is. Don't extend that exemption.

## Mail access: Superhuman MCP (chosen) vs Gmail API

Superhuman's remote MCP server (`https://mcp.mail.superhuman.com/mcp`) works unattended. Its auth server supports OAuth dynamic client registration, PKCE and **refresh tokens** (`offline_access`), so sign-in is one browser step and the refresh token then carries every later run with no browser and across reboots. The SDK's `OAuthClientProvider` does registration, the authorization-code flow and refreshes; tokens and client registration live in one login-Keychain item (`security` service `rhizomorph-board-labels`, account `superhuman-mcp`), never in the repo. The Gmail API fallback was not built.

The auth server also advertises a device-code grant, but its `/device` page is Superhuman's web app, which hands off to the installed Mac app and never confirms the code, so that route is a dead end. The one-time sign-in therefore uses the standard authorization-code flow with a loopback redirect on `127.0.0.1:8790`, which means it has to be run in a browser **on the Mac Studio itself**.

Tools used: `list_threads` (filter `labels: ["To Do"]`) and `update_thread` (`add_labels` / `remove_labels`). Both are pinned to `lee.greenwood@gmail.com` via `acting_email`.

## One-time setup

1. In Superhuman (or Gmail), make sure both labels exist: `To Do` (the trigger, already exists) and `@Triaged`. The MCP server has no create-label tool and rejects unknown labels; until both exist each run logs `label_missing` and does nothing. Both names are constants at the top of the script.
2. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. `cp .env.example .env` and fill in `MOSHI_WEBHOOK_TOKEN`. Set `PRINTER_IP` only if the QL-810W is on Wi-Fi (see Printing).
4. `.venv/bin/python board_labels.py --auth` on the Mac Studio — a browser tab opens on Superhuman's login; sign in as the Gmail account and the tab ends on "Signed in". Tokens are saved to the Keychain. Repeat only if Superhuman revokes the grant (the run then logs a `fatal` naming `--auth` and pushes a Moshi alert).

## Usage

```
.venv/bin/python board_labels.py --dry-run   # render each matching email to logs/dry-run-<thread>.png, print nothing, change no labels
.venv/bin/python board_labels.py             # print, then swap To Do → @Triaged
```

Each run: list threads labelled `To Do` → for each, render (subject bold on up to three lines, sender and ISO date smaller, ellipsised, emoji stripped; 696 px wide for 62 mm tape, height grows with the subject) → print with auto-cut → record the thread id in `printed-ids.json` (gitignored safety net; a thread in that file is never printed again even if the label swap failed) → `update_thread` swaps the labels. A print failure logs `failed`, leaves the labels alone, and pushes a Moshi alert; the next run retries.

`printed-ids.json` was pre-seeded on 7 Oct 2026 with the 107 threads that already carried `To Do`, so switching to that label did not print the whole backlog. To put one of those on the board, remove its thread id from the file (or re-tag it after clearing `To Do`); it prints on the next run.

Log: `logs/board-labels.log` (`run_summary` gives matched / printed / skipped / failed).

## Printing

`brother_ql_next` (fork of `pklaus/brother_ql`, whose last release was 2019; `brother_ql_next` 0.12.0 was released May 2026 and lists the QL-810W). Roll type is the `ROLL` constant, default `62` (continuous 62 mm); the cutter fires after each label.

- `PRINTER_IP` set → network backend, port 9100.
- `PRINTER_IP` unset → USB fallback: the raster is handed raw to the CUPS queue macOS creates for the USB-attached printer (`lp -d Brother_QL_810W -o raw`). No libusb needed. Override the queue name with `PRINTER_CUPS_QUEUE` if `lpstat -p` shows a different one.

## Scheduling

`com.rhizomorph.board-labels.plist` runs it every 5 minutes (`StartInterval` 300).

```
cp com.rhizomorph.board-labels.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rhizomorph.board-labels.plist
launchctl kickstart gui/$(id -u)/com.rhizomorph.board-labels      # run now
launchctl bootout gui/$(id -u)/com.rhizomorph.board-labels        # remove
```

The Keychain item is readable from launchd while the login Keychain is unlocked, i.e. while you are logged in on the Mac Studio.
