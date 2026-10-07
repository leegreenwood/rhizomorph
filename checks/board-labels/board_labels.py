#!/usr/bin/env python3
"""Print a Brother QL label for every Gmail thread tagged "To Do", then retag it "@Triaged".

Mail comes from Superhuman's remote MCP server (Gmail labels are Superhuman labels).
One-time: `board_labels.py --auth` (browser sign-in, tokens kept in the login Keychain).
Then:     `board_labels.py [--dry-run]` — unattended, every few minutes via launchd.
See README.md in this folder.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
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

# ---- the few things you might change -------------------------------------------------------
LABEL_TODO = "To Do"          # Gmail/Superhuman label that means "print me"
LABEL_DONE = "@Triaged"      # swapped in after a successful print
ACCOUNT_EMAIL = "lee.greenwood@gmail.com"
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
        subprocess.run(["lp", "-d", queue, "-o", "raw", f.name], check=True, capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    return f"usb/cups:{queue}"


# ---- Main --------------------------------------------------------------------------------------

def sender_of(msg):
    name = (msg.get("from_name") or "").strip()
    return name or msg.get("from", "?")


def date_of(msg):
    try:
        return datetime.fromisoformat(msg["sent_at"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return (msg.get("sent_at") or "")[:10]


async def list_board_threads(client):
    threads, cursor = [], None
    while True:
        args = {"labels": [LABEL_TODO], "acting_email": ACCOUNT_EMAIL, "limit": 50}
        if cursor:
            args["cursor"] = cursor
        res = await client.call_tool("list_threads", args)
        if res.is_error:
            text = res.content[0].text if res.content else str(res)
            if "Unknown label" in text:   # label not created yet: nothing to do, don't alert every 5 minutes
                log("label_missing", label=LABEL_TODO, detail=text)
                return []
            raise RuntimeError(f"list_threads: {text}")
        body = json.loads(res.content[0].text)
        threads += body.get("threads", [])
        cursor = body.get("next_cursor")
        if not cursor:
            return threads


async def swap_labels(client, t):
    res = await client.call_tool("update_thread", {"thread_id": t["thread_id"], "last_message_id": t["last_message_id"],
                                                   "add_labels": [LABEL_DONE], "remove_labels": [LABEL_TODO], "acting_email": ACCOUNT_EMAIL})
    if res.is_error:
        raise RuntimeError(f"update_thread: {res.content[0].text if res.content else res}")


async def run(dry_run):
    printed = set(json.loads(PRINTED_FILE.read_text())) if PRINTED_FILE.exists() else set()
    counts = {"matched": 0, "printed": 0, "skipped_already_printed": 0, "failed": 0}
    failures = []
    http = create_mcp_http_client(auth=oauth(interactive=False))
    async with http, Client(streamable_http_client(MCP_URL, http_client=http, terminate_on_close=False)) as client:
        res = await client.call_tool("list_labels", {"acting_email": ACCOUNT_EMAIL})
        labels = json.loads(res.content[0].text).get("labels", []) if not res.is_error else []
        if LABEL_DONE not in labels:   # don't print anything we could not then retag
            log("label_missing", label=LABEL_DONE, detail="create it in Superhuman/Gmail first")
            return
        threads = await list_board_threads(client)
        counts["matched"] = len(threads)
        for t in threads:
            msg = (t.get("messages") or [{}])[-1]
            info = {"thread_id": t["thread_id"], "subject": t.get("subject", ""), "sender": sender_of(msg), "date": date_of(msg)}
            if t["thread_id"] in printed and not dry_run:
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
                printed.add(t["thread_id"])
                PRINTED_FILE.write_text(json.dumps(sorted(printed)))
                await swap_labels(client, t)
                counts["printed"] += 1
                log("printed", via=via, **info)
            except Exception as e:  # noqa: BLE001
                counts["failed"] += 1
                failures.append(f"{info['subject'][:40]}: {e}")
                log("failed", error=str(e), **info)
    log("run_summary", dry_run=dry_run, **counts)
    if failures:
        notify_moshi("🔴 Board label print failed", f"{len(failures)} label(s) failed: " + " | ".join(failures[:3])[:300])
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--auth", action="store_true", help="one-time browser sign-in to Superhuman; stores tokens in the Keychain")
    p.add_argument("--dry-run", action="store_true", help="render each label to logs/dry-run-<thread>.png and print nothing; labels are left as they are")
    a = p.parse_args()
    load_env()
    if a.auth:
        return asyncio.run(authorize())
    try:
        asyncio.run(run(a.dry_run))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        while isinstance(e, BaseExceptionGroup) and e.exceptions:   # unwrap anyio task groups to the real cause
            e = e.exceptions[0]
        log("fatal", error=repr(e))
        notify_moshi("🔴 Board label check crashed", str(e)[:300])
        sys.exit(1)


if __name__ == "__main__":
    main()
