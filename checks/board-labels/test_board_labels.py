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

    def test_printer_down_stops_the_run_and_retags_nothing(self):
        FakeBackend.threads = {A: [thread("a1"), thread("a2")], B: [thread("b1")]}
        attempts = []
        def offline(img):
            attempts.append(1)
            raise bl.PrinterUnavailable("printer did not take the job")
        with mock.patch.object(bl, "print_label", offline):
            self.assertEqual(self.run_check(), 1)
        self.assertEqual(len(attempts), 1, "after one timeout, no further labels are tried this run (or on other accounts)")
        self.assertEqual(FakeBackend.swapped, [])
        self.assertEqual(self.printed_file(), set())
        self.assertEqual(len(self.alerts), 1)
        # printer back: the next run prints everything that was left
        self.assertEqual(self.run_check(), 0)
        self.assertEqual(sorted(FakeBackend.swapped), sorted([(A, "a1"), (A, "a2"), (B, "b1")]))

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


class CupsPrintTests(unittest.TestCase):
    """print_label's USB path against a faked lp/lpstat/cancel."""

    def run_print(self, still_pending, lp_out="request id is Brother_QL_810W-12 (1 file(s))\n"):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd[0])
            return mock.Mock(stdout=lp_out if cmd[0] == "lp" else "", returncode=0)
        env = {k: v for k, v in os.environ.items() if k != "PRINTER_IP"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(bl.subprocess, "run", fake_run), \
                mock.patch.object(bl, "wait_for_cups_job", lambda q, j: not still_pending):
            try:
                return calls, bl.print_label(bl.render("Subject", "Sender", "2026-10-30")), None
            except Exception as e:  # noqa: BLE001
                return calls, None, e

    def test_job_that_leaves_the_queue_is_a_success(self):
        calls, via, err = self.run_print(still_pending=False)
        self.assertIsNone(err)
        self.assertTrue(via.startswith("usb/cups"))
        self.assertNotIn("cancel", calls)

    def test_job_stuck_in_queue_is_cancelled_and_reported(self):
        calls, via, err = self.run_print(still_pending=True)
        self.assertIsInstance(err, bl.PrinterUnavailable)
        self.assertIn("cancel", calls, "an unprinted job must be cancelled or it prints later as a duplicate")

    def test_unreadable_job_id_is_an_error_not_a_success(self):
        _, via, err = self.run_print(still_pending=False, lp_out="something unexpected")
        self.assertIsNone(via)
        self.assertIsInstance(err, RuntimeError)

    def test_wait_sees_a_pending_job_then_its_absence(self):
        outs = iter(["Brother_QL_810W-12  lee 20480 Fri Oct 30\n", ""])
        with mock.patch.object(bl.subprocess, "run", lambda *a, **k: mock.Mock(stdout=next(outs))), mock.patch.object(bl.time, "sleep", lambda s: None):
            self.assertTrue(bl.wait_for_cups_job("Brother_QL_810W", "Brother_QL_810W-12", timeout=30))

    def test_wait_gives_up_after_the_timeout(self):
        pending = "Brother_QL_810W-12  lee 20480 Fri Oct 30\n"
        with mock.patch.object(bl.subprocess, "run", lambda *a, **k: mock.Mock(stdout=pending)), mock.patch.object(bl.time, "sleep", lambda s: None):
            self.assertFalse(bl.wait_for_cups_job("Brother_QL_810W", "Brother_QL_810W-12", timeout=0))


if __name__ == "__main__":
    unittest.main()
