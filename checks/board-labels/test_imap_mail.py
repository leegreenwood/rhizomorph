"""Unit tests for the IMAP mail backend. Run: .venv/bin/python -m unittest -v"""

import imaplib
import unittest

import imap_mail as im

HEADERS_1 = b"Subject: =?UTF-8?B?Q2Fmw6kgcGxhbg==?=\r\nFrom: =?UTF-8?Q?J=C3=BCrgen_M=C3=BCller?= <j@example.com>\r\nDate: Tue, 06 Oct 2026 09:15:00 +0000\r\n\r\n"
HEADERS_2 = b"Subject:  Re:   Budget   review \r\nFrom: ops@example.com\r\nDate: Wed, 07 Oct 2026 10:00:00 +0000\r\n\r\n"


def fetch_item(uid, thrid, headers, trailing=b")"):
    meta = f"1 (UID {uid} X-GM-THRID {thrid} BODY[HEADER.FIELDS (SUBJECT FROM DATE)] {{{len(headers)}}}".encode()
    return [(meta, headers), trailing]


class FakeImap:
    """Just enough of imaplib.IMAP4_SSL for ImapMail; records every command."""

    def __init__(self, search=b"", fetches=None, login_error=None, list_lines=None, fail_store=False):
        self.search, self.fetches = search, fetches or {}
        self.login_error, self.fail_store = login_error, fail_store
        self.list_lines = list_lines if list_lines is not None else [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            b'(\\HasNoChildren) "/" "To Do"',
            b'(\\HasNoChildren) "/" "@Triaged"',
        ]
        self.calls = []

    def login(self, user, password):
        self.calls.append(("login", user))
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        return "OK", [b"ok"]

    def list(self):
        return "OK", self.list_lines

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            return "OK", [self.search]
        if command == "FETCH":
            data = []
            for u in args[0].split(","):
                data += self.fetches[int(u)]
            return "OK", data
        if command == "STORE":
            return ("NO", [b"nope"]) if self.fail_store else ("OK", [b"done"])
        raise AssertionError(command)

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]


class Helpers(unittest.TestCase):
    def test_thread_hex_matches_gmail_api_ids(self):
        # a real id from printed-ids.json: IMAP reports it in decimal, the dedupe file stores hex
        self.assertEqual(im.thread_hex("1839625402589320837"), "1987a98e911f1685")

    def test_sender_prefers_display_name_and_decodes(self):
        self.assertEqual(im.sender_display("=?UTF-8?Q?J=C3=BCrgen_M=C3=BCller?= <j@example.com>"), "Jürgen Müller")
        self.assertEqual(im.sender_display("ops@example.com"), "ops@example.com")
        self.assertEqual(im.sender_display(""), "?")

    def test_date_display(self):
        self.assertRegex(im.date_display("Tue, 06 Oct 2026 09:15:00 +0000"), r"^2026-10-0[67]$")
        self.assertEqual(im.date_display("garbage"), "")

    def test_quote_escapes(self):
        self.assertEqual(im.quote('A "b" \\c'), '"A \\"b\\" \\\\c"')

    def test_parse_list_finds_labels_and_all_mail(self):
        names, all_mail = im.parse_list(FakeImap().list()[1])
        self.assertIn("To Do", names)
        self.assertIn("@Triaged", names)
        self.assertEqual(all_mail, "[Gmail]/All Mail")

    def test_parse_fetch_decodes_headers_and_ids(self):
        msgs = im.parse_fetch(fetch_item(41, 1839625402589320837, HEADERS_1) + fetch_item(42, 1839625402589320837, HEADERS_2))
        self.assertEqual([m["uid"] for m in msgs], [41, 42])
        self.assertEqual(msgs[0]["subject"], "Café plan")
        self.assertEqual(msgs[0]["sender"], "Jürgen Müller")
        self.assertEqual(msgs[1]["subject"], "Re: Budget review")   # whitespace collapsed
        self.assertEqual(msgs[0]["thread"], "1987a98e911f1685")

    def test_parse_fetch_tolerates_attributes_after_the_literal(self):
        meta = b"1 (BODY[HEADER.FIELDS (SUBJECT FROM DATE)] {%d}" % len(HEADERS_2)
        data = [(meta, HEADERS_2), b" UID 7 X-GM-THRID 255)"]
        self.assertEqual(im.parse_fetch(data)[0]["thread"], "ff")

    def test_parse_fetch_skips_rows_without_ids(self):
        self.assertEqual(im.parse_fetch([(b"1 (FLAGS ())", HEADERS_2)]), [])

    def test_group_threads_uses_newest_message_and_orders_oldest_first(self):
        msgs = [
            {"uid": 5, "thread": "a", "subject": "old", "sender": "x", "date": "d1"},
            {"uid": 9, "thread": "a", "subject": "new", "sender": "y", "date": "d2"},
            {"uid": 7, "thread": "b", "subject": "other", "sender": "z", "date": "d3"},
        ]
        t = im.group_threads(msgs)
        self.assertEqual([x["thread_id"] for x in t], ["b", "a"])
        self.assertEqual(t[1]["subject"], "new")
        self.assertEqual(t[1]["uids"], [5, 9])


class Client(unittest.TestCase):
    def make(self, **kw):
        fake = FakeImap(**kw)
        c = im.ImapMail("me@gmail.com", "pw", "To Do", "@Triaged", factory=lambda: fake)
        return c, fake

    def test_connect_requires_login_and_finds_labels(self):
        c, _ = self.make()
        c.connect()
        self.assertTrue(c.label_exists("@Triaged"))
        self.assertFalse(c.label_exists("Nope"))

    def test_login_failure_names_the_fix(self):
        c, _ = self.make(login_error="[AUTHENTICATIONFAILED] Invalid credentials")
        with self.assertRaisesRegex(RuntimeError, "--imap-auth"):
            c.connect()

    def test_missing_all_mail_is_an_error(self):
        c, _ = self.make(list_lines=[b'(\\HasNoChildren) "/" "INBOX"'])
        with self.assertRaisesRegex(RuntimeError, "All Mail"):
            c.connect()

    def test_todo_threads_reads_only_and_groups(self):
        fetches = {1: fetch_item(1, 100, HEADERS_1), 2: fetch_item(2, 100, HEADERS_2), 3: fetch_item(3, 200, HEADERS_2)}
        c, fake = self.make(search=b"1 2 3", fetches=fetches)
        c.connect()
        threads = c.todo_threads()
        self.assertEqual([t["thread_id"] for t in threads], ["64", "c8"])
        self.assertEqual(threads[0]["uids"], [1, 2])
        self.assertTrue(all(call[2] for call in fake.calls if call[0] == "select"), "listing must open All Mail read-only")
        self.assertIn(("uid", "SEARCH", "X-GM-LABELS", '"To Do"'), fake.calls)
        self.assertFalse([c for c in fake.calls if c[:2] == ("uid", "STORE")], "listing must not write")

    def test_no_matches(self):
        c, _ = self.make(search=b"")
        c.connect()
        self.assertEqual(c.todo_threads(), [])

    def test_swap_adds_done_before_removing_todo(self):
        c, fake = self.make()
        c.connect()
        c.swap([4, 8])
        stores = [call for call in fake.calls if call[:2] == ("uid", "STORE")]
        self.assertEqual(stores[0], ("uid", "STORE", "4,8", "+X-GM-LABELS", '("@Triaged")'))
        self.assertEqual(stores[1], ("uid", "STORE", "4,8", "-X-GM-LABELS", '("To Do")'))
        self.assertFalse([call for call in fake.calls if call[0] == "select"][-1][2], "swap needs a writable select")

    def test_failed_store_raises_and_never_removes_todo(self):
        c, fake = self.make(fail_store=True)
        c.connect()
        with self.assertRaisesRegex(RuntimeError, "STORE add"):
            c.swap([4])
        self.assertEqual(len([call for call in fake.calls if call[:2] == ("uid", "STORE")]), 1)

    def test_large_searches_fetch_in_batches(self):
        n = im.FETCH_BATCH + 5
        fetches = {i: fetch_item(i, i, HEADERS_2) for i in range(1, n + 1)}
        c, fake = self.make(search=" ".join(map(str, range(1, n + 1))).encode(), fetches=fetches)
        c.connect()
        self.assertEqual(len(c.todo_threads()), n)
        self.assertEqual(len([call for call in fake.calls if call[:2] == ("uid", "FETCH")]), 2)


if __name__ == "__main__":
    unittest.main()
