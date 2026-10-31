"""Orchestration tests (multi-account, dedupe, failure isolation, alert throttle) with a fake mail backend.
Run: .venv/bin/python -m unittest -v"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import board_labels as bl

A, B = "lee.greenwood@gmail.com", "second@gmail.com"
REAL_MAKE_BACKEND = bl.make_backend   # setUp swaps the module attribute for a fake


class FakeBackend:
    """Class-level script so each account gets its own threads / failure."""
    threads = {}
    enter_error = {}
    swapped = []

    def __init__(self, account):
        self.account = account

    async def __aenter__(self):
        if self.account in self.enter_error:
            raise RuntimeError(self.enter_error[self.account])
        return self

    async def __aexit__(self, *exc):
        return None

    async def labels_ok(self):
        return True

    async def list_threads(self):
        return [dict(t) for t in self.threads.get(self.account, [])]

    async def swap(self, t):
        self.swapped.append((self.account, t["thread_id"]))


def thread(tid, subject="Subject"):
    return {"thread_id": tid, "subject": subject, "sender": "Someone", "date": "2026-10-30"}


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        FakeBackend.threads, FakeBackend.enter_error, FakeBackend.swapped = {}, {}, []
        self.printed_calls, self.alerts = [], []
        patches = [
            mock.patch.object(bl, "PRINTED_FILE", self.tmp / "printed.json"),
            mock.patch.object(bl, "ALERT_STATE_FILE", self.tmp / "alert-state.json"),
            mock.patch.object(bl, "LOG_FILE", self.tmp / "log"),
            mock.patch.object(bl, "HERE", self.tmp),
            mock.patch.object(bl, "make_backend", lambda account: FakeBackend(account)),
            mock.patch.object(bl, "print_label", lambda img: self.printed_calls.append(img) or "fake"),
            mock.patch.object(bl, "notify_moshi", lambda title, msg: self.alerts.append((title, msg))),
            mock.patch.dict(os.environ, {"GMAIL_ACCOUNTS": f"{A},{B}"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        (self.tmp / "logs").mkdir()

    def run_check(self, dry_run=False):
        try:
            asyncio.run(bl.run(dry_run))
            return 0
        except SystemExit as e:
            return e.code

    def printed_file(self):
        return set(json.loads(bl.PRINTED_FILE.read_text())) if bl.PRINTED_FILE.exists() else set()

    def test_accounts_parse_dedupes_and_defaults(self):
        with mock.patch.dict(os.environ, {"GMAIL_ACCOUNTS": f" {B}, {A},{B},, "}):
            self.assertEqual(bl.accounts(), [B, A])
        with mock.patch.dict(os.environ, {"GMAIL_ACCOUNTS": ""}):
            self.assertEqual(bl.accounts(), [A])

    def test_both_accounts_print_and_swap_with_namespaced_keys(self):
        FakeBackend.threads = {A: [thread("aaa")], B: [thread("bbb")]}
        self.assertEqual(self.run_check(), 0)
        self.assertEqual(len(self.printed_calls), 2)
        self.assertEqual(sorted(FakeBackend.swapped), sorted([(A, "aaa"), (B, "bbb")]))
        self.assertEqual(self.printed_file(), {"aaa", f"{B}:bbb"})   # original account keeps bare ids

    def test_same_thread_id_in_two_accounts_is_not_confused(self):
        bl.PRINTED_FILE.write_text(json.dumps(["same"]))   # printed for the original account only
        FakeBackend.threads = {A: [thread("same")], B: [thread("same")]}
        self.assertEqual(self.run_check(), 0)
        self.assertEqual(FakeBackend.swapped, [(B, "same")])
        self.assertEqual(len(self.printed_calls), 1)

    def test_legacy_ids_still_block_reprints(self):
        bl.PRINTED_FILE.write_text(json.dumps(["1987a98e911f1685"]))
        FakeBackend.threads = {A: [thread("1987a98e911f1685")]}
        self.assertEqual(self.run_check(), 0)
        self.assertEqual(self.printed_calls, [])
        self.assertEqual(FakeBackend.swapped, [])

    def test_one_account_failing_does_not_stop_the_other(self):
        FakeBackend.enter_error = {A: "login failed"}
        FakeBackend.threads = {B: [thread("bbb")]}
        self.assertEqual(self.run_check(), 1)
        self.assertEqual(FakeBackend.swapped, [(B, "bbb")])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn(A, self.alerts[0][1])
        self.assertIn("login failed", self.alerts[0][1])

    def test_print_failure_leaves_labels_and_is_retried(self):
        FakeBackend.threads = {A: [thread("aaa")]}
        with mock.patch.object(bl, "print_label", side_effect=RuntimeError("printer offline")):
            self.assertEqual(self.run_check(), 1)
        self.assertEqual(FakeBackend.swapped, [])
        self.assertEqual(self.printed_file(), set())   # not recorded, so the next run prints it
        self.assertEqual(self.alerts[0][0], "🔴 Board label print failed")

    def test_dry_run_prints_and_swaps_nothing(self):
        FakeBackend.threads = {A: [thread("aaa")]}
        self.assertEqual(self.run_check(dry_run=True), 0)
        self.assertEqual(self.printed_calls, [])
        self.assertEqual(FakeBackend.swapped, [])
        self.assertEqual(self.printed_file(), set())

    def test_repeated_failure_alerts_once_then_again_after_recovery(self):
        FakeBackend.enter_error = {A: "down"}
        self.run_check()
        self.run_check()
        self.run_check()
        self.assertEqual(len(self.alerts), 1, "same fault must not page every run")
        FakeBackend.enter_error = {}
        self.assertEqual(self.run_check(), 0)   # recovery clears the throttle
        FakeBackend.enter_error = {A: "down again"}
        self.run_check()
        self.assertEqual(len(self.alerts), 2)

    def test_unknown_backend_is_rejected(self):
        with mock.patch.dict(os.environ, {"MAIL_BACKEND": "pigeon"}):
            with self.assertRaisesRegex(RuntimeError, "unknown MAIL_BACKEND"):
                REAL_MAKE_BACKEND(A)
        for name, cls in (("imap", bl.ImapBackend), ("superhuman", bl.SuperhumanBackend)):
            with mock.patch.dict(os.environ, {"MAIL_BACKEND": name}):
                self.assertIsInstance(REAL_MAKE_BACKEND(A), cls)


if __name__ == "__main__":
    unittest.main()
