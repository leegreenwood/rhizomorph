"""Gmail over IMAP with an app password — the Superhuman-independent mail backend for board_labels.

Uses Gmail's IMAP extensions (X-GM-LABELS, X-GM-THRID) against "All Mail", so it works on labels
whether or not they are shown as folders. Thread ids are returned as lowercase hex, the same format
as the Gmail API / Superhuman ids already recorded in printed-ids.json.
"""

import imaplib
import re
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

HOST = "imap.gmail.com"
FETCH_BATCH = 100
FETCH_ITEMS = "(UID X-GM-THRID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
LIST_LINE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?:"(?P<delim>[^"]*)"|NIL)\s+(?P<name>.+)$')


def quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def decode_text(raw):
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 — a malformed header must never stop a print run
        return raw


def sender_display(from_header):
    """Display name if there is one, otherwise the address (matches the Superhuman backend's choice)."""
    name, addr = parseaddr(from_header or "")
    return decode_text(name).strip() or addr or "?"


def date_display(date_header):
    try:
        return parsedate_to_datetime(date_header).astimezone().strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""


def thread_hex(x_gm_thrid):
    """X-GM-THRID is the Gmail thread id in decimal; the API (and printed-ids.json) use hex."""
    return format(int(x_gm_thrid), "x")


def parse_list(lines):
    """-> (set of mailbox names, name of the \\All mailbox or None) from a LIST response."""
    names, all_mail = set(), None
    for line in lines:
        if not isinstance(line, bytes):
            continue
        m = LIST_LINE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        name = (name[1:-1] if name.startswith(b'"') and name.endswith(b'"') else name).decode("utf-8", "replace")
        names.add(name)
        if b"\\All" in m.group("flags"):
            all_mail = name
    return names, all_mail


def parse_fetch(data):
    """imaplib FETCH data -> [{uid, thread, subject, sender, date}]. Tolerates attributes after the literal."""
    chunks, cur = [], None
    for item in data:
        if isinstance(item, tuple):
            cur = [item[0].decode("utf-8", "replace"), item[1]]
            chunks.append(cur)
        elif isinstance(item, bytes) and cur is not None:
            cur[0] += " " + item.decode("utf-8", "replace")
    out = []
    for meta, raw in chunks:
        uid = re.search(r"\bUID (\d+)", meta)
        thr = re.search(r"\bX-GM-THRID (\d+)", meta)
        if not (uid and thr):
            continue
        h = message_from_bytes(raw)
        out.append({
            "uid": int(uid.group(1)),
            "thread": thread_hex(thr.group(1)),
            "subject": " ".join(decode_text(h.get("Subject", "")).split()),
            "sender": sender_display(h.get("From", "")),
            "date": date_display(h.get("Date", "")),
        })
    return out


def group_threads(messages):
    """Messages -> one entry per thread: the newest message (highest UID) supplies subject/sender/date."""
    by_thread = {}
    for m in messages:
        by_thread.setdefault(m["thread"], []).append(m)
    threads = []
    for thread_id, msgs in by_thread.items():
        newest = max(msgs, key=lambda m: m["uid"])
        threads.append({"thread_id": thread_id, "subject": newest["subject"], "sender": newest["sender"],
                        "date": newest["date"], "uids": sorted(m["uid"] for m in msgs)})
    return sorted(threads, key=lambda t: t["uids"][-1])


class ImapMail:
    def __init__(self, email_addr, password, label_todo, label_done, factory=None):
        self.email, self.password = email_addr, password
        self.todo, self.done = label_todo, label_done
        self._factory = factory or (lambda: imaplib.IMAP4_SSL(HOST, timeout=30))
        self._m = None
        self._all_mail = None
        self._names = set()

    def _check(self, result, what):
        typ, data = result
        if typ != "OK":
            raise RuntimeError(f"IMAP {what} failed: {typ} {data}")
        return data

    def connect(self):
        self._m = self._factory()
        try:
            self._m.login(self.email, self.password)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"Gmail IMAP login failed ({e}). Run: board_labels.py --imap-auth") from e
        self._names, self._all_mail = parse_list(self._check(self._m.list(), "LIST"))
        if not self._all_mail:
            raise RuntimeError("Gmail IMAP: could not find the All Mail mailbox (is IMAP enabled for the account?)")

    def close(self):
        if self._m is not None:
            try:
                self._m.logout()
            except Exception:  # noqa: BLE001
                pass
            self._m = None

    def label_exists(self, label):
        return label in self._names

    def _select(self, readonly):
        self._check(self._m.select(quote(self._all_mail), readonly=readonly), "SELECT")

    def todo_threads(self):
        """All threads carrying the To Do label, oldest first."""
        self._select(readonly=True)
        data = self._check(self._m.uid("SEARCH", "X-GM-LABELS", quote(self.todo)), "SEARCH")
        uids = data[0].split() if data and data[0] else []
        messages = []
        for i in range(0, len(uids), FETCH_BATCH):
            batch = b",".join(uids[i:i + FETCH_BATCH]).decode()
            messages += parse_fetch(self._check(self._m.uid("FETCH", batch, FETCH_ITEMS), "FETCH"))
        return group_threads(messages)

    def swap(self, uids):
        """Add the done label first, then remove To Do, so a thread is never left with neither."""
        self._select(readonly=False)
        uidset = ",".join(str(u) for u in uids)
        self._check(self._m.uid("STORE", uidset, "+X-GM-LABELS", f"({quote(self.done)})"), "STORE add")
        self._check(self._m.uid("STORE", uidset, "-X-GM-LABELS", f"({quote(self.todo)})"), "STORE remove")
