#!/usr/bin/env python3
"""Print a Brother QL label for every Gmail thread tagged "To Do", then retag it "@Triaged".

Mail comes from Gmail over IMAP with a per-account app password (MAIL_BACKEND=imap, default; works
for several accounts via GMAIL_ACCOUNTS) or from Superhuman's remote MCP server (MAIL_BACKEND=superhuman).
One-time: `board_labels.py --imap-auth [EMAIL ...]` (app passwords kept in the login Keychain).
Then:     `board_labels.py [--dry-run]` — unattended, every few minutes via launchd.
See README.md in this folder.
"""

import argparse
import asyncio
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from PIL import Image, ImageDraw, ImageFont
from pydantic import AnyUrl

from imap_mail import ImapMail

# ---- the few things you might change -------------------------------------------------------
LABEL_TODO = "To Do"          # Gmail/Superhuman label that means "print me"
LABEL_DONE = "@Triaged"      # swapped in after a successful print
ACCOUNT_EMAIL = "lee.greenwood@gmail.com"   # default account; its printed-ids.json entries are stored without a prefix
PRINTER_MODEL = "QL-810W"
ROLL = "62"                   # brother_ql label id: continuous 62mm tape (696 px printable width)
SUBJECT_LINES = 3             # subject wraps onto up to this many lines; the label grows to fit
FONT = "/System/Library/Fonts/Helvetica.ttc"   # index 1 = Bold, 0 = Regular
# ---------------------------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
MCP_URL = "https://mcp.mail.superhuman.com/mcp"
CALLBACK_PORT = 8790          # loopback redirect for the one-time sign-in (receivers use 8788/8789)
SCOPE = "openid email profile offline_access"
KEYCHAIN = ("rhizomorph-board-labels", "superhuman-mcp")   # (service, account)
PRINTED_FILE = HERE / "printed-ids.json"
ALERT_STATE_FILE = HERE / "logs" / "alert-state.json"
ALERT_EVERY_S = 6 * 3600       # repeat the same alert at most this often (the check itself runs every 5 minutes)
LOG_FILE = Path(os.environ.get("LOG_FILE", HERE / "logs" / "board-labels.log"))
LABEL_W = 696


def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def log(event, **fields):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **fields})
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")
    print(line)


def notify_moshi(title, message):
    token = os.environ.get("MOSHI_WEBHOOK_TOKEN")
    if not token:
        return log("alert_not_sent", reason="MOSHI_WEBHOOK_TOKEN not set", title=title)
    try:
        r = httpx2.post("https://api.getmoshi.app/api/webhook", json={"token": token, "title": title, "message": message, "unified": True}, timeout=15)
        if r.status_code >= 300:
            log("alert_failed", status=r.status_code)
    except Exception as e:  # noqa: BLE001
        log("alert_failed", error=str(e))


def alert(key, title, message):
    """notify_moshi, but the same alert (by key) is sent at most every ALERT_EVERY_S — a persistent fault
    would otherwise page every 5 minutes. A key that stops firing is forgotten, so it alerts again next time."""
    try:
        state = json.loads(ALERT_STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        state = {}
    now = time.time()
    if now - state.get(key, 0) < ALERT_EVERY_S:
        return log("alert_suppressed", key=key, title=title)
    state[key] = now
    ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE_FILE.write_text(json.dumps(state))
    notify_moshi(title, message)


def clear_alert(key):
    try:
        state = json.loads(ALERT_STATE_FILE.read_text())
        if state.pop(key, None) is not None:
            ALERT_STATE_FILE.write_text(json.dumps(state))
    except Exception:  # noqa: BLE001
        pass


# ---- Keychain-stored Gmail app passwords (IMAP backend), one item per account ------------------

def imap_account(email):
    return f"gmail-imap:{email}"


def kc_get(account):
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN[0], "-a", account, "-w"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def kc_set(account, value):
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN[0], "-a", account, "-X", value.encode().hex()], check=True, capture_output=True)


# ---- Keychain-backed OAuth (SDK provider; one-time browser sign-in, refresh tokens after) --------

def keychain_read():
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN[0], "-a", KEYCHAIN[1], "-w"], capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}


def keychain_write(data):
    hexpw = json.dumps(data).encode().hex()   # -X: hex avoids any quoting of the JSON
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN[0], "-a", KEYCHAIN[1], "-X", hexpw], check=True, capture_output=True)


class KeychainStorage:
    """mcp TokenStorage backed by one login-Keychain item holding {client_info, tokens, expires_at}."""

    async def get_tokens(self):
        d = keychain_read()
        if "tokens" not in d:
            return None
        t = OAuthToken.model_validate(d["tokens"])
        if d.get("expires_at"):   # re-derive expires_in so the provider refreshes instead of sending a stale token
            t.expires_in = max(1, int(d["expires_at"] - time.time()))
        return t

    async def set_tokens(self, tokens):
        d = keychain_read()
        d["tokens"] = tokens.model_dump(mode="json")
        d["expires_at"] = time.time() + (tokens.expires_in or 3600)
        keychain_write(d)

    async def get_client_info(self):
        d = keychain_read()
        return OAuthClientInformationFull.model_validate(d["client_info"]) if "client_info" in d else None

    async def set_client_info(self, info):
        d = keychain_read()
        d["client_info"] = info.model_dump(mode="json")
        keychain_write(d)


class KeychainOAuth(OAuthClientProvider):
    async def _initialize(self):
        await super()._initialize()
        if self.context.current_tokens:   # stored expiry → provider knows when to refresh
            self.context.update_token_expiry(self.context.current_tokens)


async def no_browser(url):
    raise RuntimeError("Superhuman sign-in needed (no valid token or refresh failed). Run: board_labels.py --auth")


async def open_browser(url):
    print(f"\nSign in as {ACCOUNT_EMAIL}. If a browser did not open, visit:\n  {url}\n", flush=True)
    subprocess.run(["open", url], check=False)


async def wait_for_callback():
    """One-shot loopback HTTP server on CALLBACK_PORT; returns the code/state Superhuman redirects back with."""
    got = asyncio.get_running_loop().create_future()

    async def handle(reader, writer):
        line = (await reader.readline()).decode()
        q = parse_qs(urlparse(line.split(" ")[1]).query) if " " in line else {}
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nSigned in. You can close this tab.\n")
        await writer.drain()
        writer.close()
        if "code" in q and not got.done():
            got.set_result(AuthorizationCodeResult(code=q["code"][0], state=q.get("state", [None])[0], iss=q.get("iss", [None])[0]))

    server = await asyncio.start_server(handle, "127.0.0.1", CALLBACK_PORT)
    async with server:
        return await asyncio.wait_for(got, timeout=300)


def oauth(interactive):
    meta = OAuthClientMetadata(client_name="rhizomorph-board-labels", redirect_uris=[AnyUrl(f"http://127.0.0.1:{CALLBACK_PORT}/callback")],
                               grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                               token_endpoint_auth_method="none", scope=SCOPE)
    return KeychainOAuth(MCP_URL, meta, KeychainStorage(),
                         redirect_handler=open_browser if interactive else no_browser,
                         callback_handler=wait_for_callback if interactive else None)


async def authorize():
    """One-time: full OAuth sign-in via the browser; the SDK registers the client and stores tokens in the Keychain."""
    if "tokens" in keychain_read():
        keychain_write({})   # start clean; a stale refresh token would be tried first otherwise
    http = create_mcp_http_client(auth=oauth(interactive=True))
    async with http, Client(streamable_http_client(MCP_URL, http_client=http, terminate_on_close=False)) as client:
        tools = await client.list_tools()
    print(f"Authorised. {len(tools.tools)} tools visible; tokens stored in the login Keychain.")


# ---- Label rendering ---------------------------------------------------------------------------

def fit(draw, text, font, width):
    """Truncate `text` with an ellipsis so it fits in `width` pixels."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1].rstrip()
    return text + "…"


def wrap(draw, text, font, width, max_lines):
    """Up to `max_lines` lines broken on spaces; the last line is ellipsised if there is more."""
    words, lines, line = text.split(), [], []
    for i, w in enumerate(words):
        if draw.textlength(" ".join(line + [w]), font=font) > width:
            if not line:   # a single word wider than the label
                line, i = [fit(draw, w, font, width)], i + 1
            lines.append(" ".join(line))
            rest = " ".join(words[i:])
            if not rest:
                return lines
            if len(lines) == max_lines - 1:
                return lines + [fit(draw, rest, font, width)]
            line = [w] if i < len(words) and words[i] == w else []
            continue
        line.append(w)
    return lines + ([" ".join(line)] if line else [])


def render(subject, sender, date):
    big = ImageFont.truetype(FONT, 52, index=1)
    small = ImageFont.truetype(FONT, 34, index=0)
    pad, line_h, width = 20, 60, LABEL_W - 40
    probe = ImageDraw.Draw(Image.new("1", (LABEL_W, 10), 1))
    subject = "".join(ch for ch in subject if ord(ch) < 0x2600 and not 0x200B <= ord(ch) <= 0x200F)   # no emoji/zero-width: Helvetica has no glyphs
    lines = wrap(probe, " ".join(subject.split()) or "(no subject)", big, width, SUBJECT_LINES)
    height = pad + len(lines) * line_h + 14 + 40 + pad
    img = Image.new("1", (LABEL_W, height), 1)
    d = ImageDraw.Draw(img)
    y = pad
    for line in lines:
        d.text((pad, y), line, font=big, fill=0)
        y += line_h
    y = height - pad - 40
    d.line([(pad, y - 12), (LABEL_W - pad, y - 12)], fill=0, width=3)
    date_w = d.textlength(date, font=small)
    d.text((LABEL_W - pad - date_w, y), date, font=small, fill=0)
    d.text((pad, y), fit(d, sender, small, width - date_w - 30), font=small, fill=0)
    return img


# ---- Printing ----------------------------------------------------------------------------------

class PrinterUnavailable(RuntimeError):
    """The printer did not take the job (off, unplugged, asleep). Nothing is retagged; the next run retries."""


PRINT_WAIT_S = 60   # a label takes seconds; longer than this means the printer isn't there


def wait_for_cups_job(queue, job_id, timeout=None):
    """True once CUPS no longer lists the job as pending; False if it is still pending after `timeout` seconds."""
    deadline = time.time() + (PRINT_WAIT_S if timeout is None else timeout)
    while True:
        out = subprocess.run(["lpstat", "-W", "not-completed", "-o", queue], capture_output=True, text=True).stdout
        if not any(line.split()[:1] == [job_id] for line in out.splitlines()):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(2)


def print_label(img):
    from brother_ql.backends.helpers import send
    from brother_ql.conversion import convert
    from brother_ql.raster import BrotherQLRaster

    qlr = BrotherQLRaster(PRINTER_MODEL)
    qlr.exception_on_warning = True
    data = convert(qlr, [img], ROLL, cut=True, dither=False, threshold=70)
    ip = os.environ.get("PRINTER_IP")
    if ip:
        send(data, printer_identifier=f"tcp://{ip}:9100", backend_identifier="network", blocking=True)
        return "network"
    # USB fallback: hand the raw raster to the CUPS queue macOS created for the USB-attached printer.
    queue = os.environ.get("PRINTER_CUPS_QUEUE", "Brother_QL_810W")
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(data)
    try:
        sent = subprocess.run(["lp", "-d", queue, "-o", "raw", f.name], check=True, capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    # `lp` succeeds as soon as CUPS queues the job, even with the printer off: wait for it to actually leave the queue.
    m = re.search(r"request id is (\S+)", sent.stdout)
    if not m:
        raise RuntimeError(f"could not read the CUPS job id from lp output: {sent.stdout!r}")
    if not wait_for_cups_job(queue, m.group(1)):
        subprocess.run(["cancel", m.group(1)], check=False, capture_output=True)   # else it would print later as a duplicate
        raise PrinterUnavailable(f"printer did not take the job within {PRINT_WAIT_S}s (is the QL-810W on and plugged in?); job cancelled")
    return f"usb/cups:{queue}"


# ---- Mail backends -----------------------------------------------------------------------------
# Both expose: async with Backend(account) -> labels_ok(), list_threads() -> [{thread_id, subject, sender, date, ...}],
# swap(thread). A thread dict carries whatever extra the backend needs to swap its labels.

def sender_of(msg):
    name = (msg.get("from_name") or "").strip()
    return name or msg.get("from", "?")


def date_of(msg):
    try:
        return datetime.fromisoformat(msg["sent_at"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return (msg.get("sent_at") or "")[:10]


class SuperhumanBackend:
    """Superhuman's remote MCP server (OAuth in the Keychain). One OAuth identity serves any linked account."""

    def __init__(self, account):
        self.account = account

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        http = await self._stack.enter_async_context(create_mcp_http_client(auth=oauth(interactive=False)))
        self._client = await self._stack.enter_async_context(Client(streamable_http_client(MCP_URL, http_client=http, terminate_on_close=False)))
        return self

    async def __aexit__(self, *exc):
        return await self._stack.__aexit__(*exc)

    async def labels_ok(self):
        res = await self._client.call_tool("list_labels", {"acting_email": self.account})
        labels = json.loads(res.content[0].text).get("labels", []) if not res.is_error else []
        if LABEL_DONE not in labels:   # don't print anything we could not then retag
            log("label_missing", account=self.account, label=LABEL_DONE, detail="create it in Superhuman/Gmail first")
            return False
        return True

    async def list_threads(self):
        threads, cursor = [], None
        while True:
            args = {"labels": [LABEL_TODO], "acting_email": self.account, "limit": 50}
            if cursor:
                args["cursor"] = cursor
            res = await self._client.call_tool("list_threads", args)
            if res.is_error:
                text = res.content[0].text if res.content else str(res)
                if "Unknown label" in text:   # label not created yet: nothing to do, don't alert every 5 minutes
                    log("label_missing", account=self.account, label=LABEL_TODO, detail=text)
                    return []
                raise RuntimeError(f"list_threads: {text}")
            body = json.loads(res.content[0].text)
            threads += body.get("threads", [])
            cursor = body.get("next_cursor")
            if not cursor:
                break
        out = []
        for t in threads:
            msg = (t.get("messages") or [{}])[-1]
            out.append({"thread_id": t["thread_id"], "subject": t.get("subject", ""), "sender": sender_of(msg), "date": date_of(msg), "_raw": t})
        return out

    async def swap(self, t):
        raw = t["_raw"]
        res = await self._client.call_tool("update_thread", {"thread_id": raw["thread_id"], "last_message_id": raw["last_message_id"],
                                                              "add_labels": [LABEL_DONE], "remove_labels": [LABEL_TODO], "acting_email": self.account})
        if res.is_error:
            raise RuntimeError(f"update_thread: {res.content[0].text if res.content else res}")


class ImapBackend:
    """Gmail over IMAP with this account's app password from the Keychain. Independent of Superhuman."""

    def __init__(self, account):
        self.account = account

    async def __aenter__(self):
        password = kc_get(imap_account(self.account))
        if not password:
            raise RuntimeError(f"no Gmail app password stored for {self.account}. Run: board_labels.py --imap-auth {self.account}")
        self._mail = ImapMail(self.account, password, LABEL_TODO, LABEL_DONE)
        self._mail.connect()
        return self

    async def __aexit__(self, *exc):
        self._mail.close()

    async def labels_ok(self):
        for label in (LABEL_DONE, LABEL_TODO):   # DONE first: never print what we could not retag
            if not self._mail.label_exists(label):
                log("label_missing", account=self.account, label=label,
                    detail="create it in Gmail, and make sure the label is set to 'Show in IMAP' (Gmail → Settings → Labels)")
                return False
        return True

    async def list_threads(self):
        return self._mail.todo_threads()

    async def swap(self, t):
        self._mail.swap(t["uids"])


def backend_name():
    return os.environ.get("MAIL_BACKEND", "imap").strip().lower()


def make_backend(account):
    name = backend_name()
    if name == "imap":
        return ImapBackend(account)
    if name == "superhuman":
        return SuperhumanBackend(account)
    raise RuntimeError(f"unknown MAIL_BACKEND {name!r} (use imap or superhuman)")


def accounts():
    """GMAIL_ACCOUNTS=a@gmail.com,b@gmail.com (order kept, duplicates dropped); defaults to ACCOUNT_EMAIL."""
    raw = os.environ.get("GMAIL_ACCOUNTS", "")
    seen = []
    for a in (x.strip() for x in raw.split(",")):
        if a and a not in seen:
            seen.append(a)
    return seen or [ACCOUNT_EMAIL]


def printed_key(account, thread_id):
    """Thread ids are only unique per account. The original account keeps bare ids so the existing
    printed-ids.json stays valid; every other account is namespaced."""
    return thread_id if account == ACCOUNT_EMAIL else f"{account}:{thread_id}"


# ---- Main --------------------------------------------------------------------------------------

def unwrap(e):
    while isinstance(e, BaseExceptionGroup) and e.exceptions:   # anyio task groups wrap the real cause
        e = e.exceptions[0]
    return e


PRINTER_STATE = {"down": False}   # set when a job times out: don't spend a minute per label on the rest of the run


async def run_account(account, dry_run, printed, failures):
    counts = {"matched": 0, "printed": 0, "skipped_already_printed": 0, "failed": 0}
    async with make_backend(account) as backend:
        if not await backend.labels_ok():
            return counts
        threads = await backend.list_threads()
        counts["matched"] = len(threads)
        for t in threads:
            if PRINTER_STATE["down"]:
                break
            key = printed_key(account, t["thread_id"])
            info = {"account": account, "thread_id": t["thread_id"], "subject": t["subject"], "sender": t["sender"], "date": t["date"]}
            if key in printed:
                counts["skipped_already_printed"] += 1   # counted in run_summary only; seeded backlog would be noisy
                continue
            img = render(info["subject"], info["sender"], info["date"])
            if dry_run:
                out = HERE / "logs" / f"dry-run-{t['thread_id']}.png"
                img.save(out)
                log("would_print", png=str(out), **info)
                continue
            try:
                via = print_label(img)
                printed.add(key)
                PRINTED_FILE.write_text(json.dumps(sorted(printed)))
                await backend.swap(t)
                counts["printed"] += 1
                log("printed", via=via, **info)
            except Exception as e:  # noqa: BLE001
                counts["failed"] += 1
                failures.append(f"{info['subject'][:40]}: {e}")
                log("failed", error=str(e), **info)
                if isinstance(e, PrinterUnavailable):
                    PRINTER_STATE["down"] = True
                    break
    return counts


async def run(dry_run):
    printed = set(json.loads(PRINTED_FILE.read_text())) if PRINTED_FILE.exists() else set()
    failures, errors = [], []
    PRINTER_STATE["down"] = False
    for account in accounts():   # one account failing must never stop the others
        try:
            counts = await run_account(account, dry_run, printed, failures)
        except Exception as e:  # noqa: BLE001
            e = unwrap(e)
            errors.append(f"{account}: {e}"[:200])
            log("fatal", account=account, error=repr(e))
            continue
        log("run_summary", account=account, dry_run=dry_run, backend=backend_name(), **counts)
    if errors:
        alert("fatal", "🔴 Board label check crashed", " | ".join(errors)[:300])
    if failures:
        alert("print-failed", "🔴 Board label print failed", f"{len(failures)} label(s) failed: " + " | ".join(failures[:3])[:300])
    if not errors:
        clear_alert("fatal")
    if not failures:
        clear_alert("print-failed")
    if errors or failures:
        sys.exit(1)


def imap_auth(emails):
    """One-time per account: store a Gmail app password in the Keychain after proving it logs in."""
    ok = True
    for email in emails or accounts():
        print(f"\n{email}\nCreate an app password at https://myaccount.google.com/apppasswords (needs 2-Step Verification).")
        password = getpass.getpass("App password (input hidden): ").replace(" ", "").strip()
        if not password:
            print("  skipped (empty)")
            ok = False
            continue
        mail = ImapMail(email, password, LABEL_TODO, LABEL_DONE)
        try:
            mail.connect()
            missing = [l for l in (LABEL_TODO, LABEL_DONE) if not mail.label_exists(l)]
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {e}")
            ok = False
            continue
        finally:
            mail.close()
        kc_set(imap_account(email), password)
        print(f"  OK: logged in, app password saved to the login Keychain.")
        if missing:
            print(f"  WARNING: label(s) not visible over IMAP: {', '.join(missing)} — create them / set 'Show in IMAP' in Gmail → Settings → Labels.")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--imap-auth", nargs="*", metavar="EMAIL", help="one-time: store a Gmail app password per account (all of GMAIL_ACCOUNTS if none given)")
    p.add_argument("--auth", action="store_true", help="(superhuman backend only) one-time browser sign-in; stores tokens in the Keychain")
    p.add_argument("--dry-run", action="store_true", help="render each not-yet-printed label to logs/dry-run-<thread>.png and print nothing; labels are left as they are")
    a = p.parse_args()
    load_env()
    if a.imap_auth is not None:
        return sys.exit(imap_auth(a.imap_auth))
    if a.auth:
        return asyncio.run(authorize())
    try:
        asyncio.run(run(a.dry_run))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        e = unwrap(e)
        log("fatal", error=repr(e))
        alert("fatal", "🔴 Board label check crashed", str(e)[:300])
        sys.exit(1)


if __name__ == "__main__":
    main()
