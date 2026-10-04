#!/usr/bin/env node
// Repeat-after-completion for Apple Reminders. See README.md in this folder.
// Shells out to RemCTL only — no private APIs or database access of its own.
//
// Usage: node repeat.js [--dry-run] [--only <id>]

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";
import { planReminder, sameDue } from "./lib.js";

const run = promisify(execFile);
const HERE = dirname(fileURLToPath(import.meta.url));
try { process.loadEnvFile(join(HERE, ".env")); } catch { /* .env is optional */ }

const REMCTL = process.env.REMCTL_PATH || join(homedir(), "bin", "remctl");
const LOG_FILE = process.env.LOG_FILE || join(HERE, "logs", "repeat-after-completion.log");
const MOSHI_TOKEN = process.env.MOSHI_WEBHOOK_TOKEN;
const MOSHI_RETRY_DELAYS_MS = [5000, 15000, 30000];
const RECURRENCE_CLEAR = process.env.RECURRENCE_CLEAR_VALUE || "none"; // value remctl edit --recurrence takes to remove a rule

const args = process.argv.slice(2);
const DRY_RUN = args.includes("--dry-run");
const onlyIdx = args.indexOf("--only");
const ONLY_ID = onlyIdx >= 0 ? Number(args[onlyIdx + 1]) : null;
if (onlyIdx >= 0 && !Number.isInteger(ONLY_ID)) {
  console.error("--only needs a numeric reminder id");
  process.exit(2);
}

mkdirSync(dirname(LOG_FILE), { recursive: true });
function log(event, fields = {}) {
  const line = JSON.stringify({ ts: new Date().toISOString(), event, dry_run: DRY_RUN, ...fields });
  appendFileSync(LOG_FILE, line + "\n");
  console.log(line);
}

async function remctl(argv) {
  const { stdout } = await run(REMCTL, argv, { maxBuffer: 64 * 1024 * 1024, timeout: 120_000 });
  return stdout ? JSON.parse(stdout) : null;
}

async function notifyMoshi({ title, message }) {
  if (!MOSHI_TOKEN) return log("alert_not_sent", { reason: "MOSHI_WEBHOOK_TOKEN not set", title });
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await fetch("https://api.getmoshi.app/api/webhook", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: MOSHI_TOKEN, title, message, unified: true }),
      });
      if (res.ok) return;
      if (res.status !== 429 || attempt === MOSHI_RETRY_DELAYS_MS.length) return log("alert_failed", { status: res.status });
    } catch (err) {
      return log("alert_failed", { error: String(err) });
    }
    await new Promise((resolve) => setTimeout(resolve, MOSHI_RETRY_DELAYS_MS[attempt]));
  }
}

const label = (r) => ({ id: r.id, title: r.title, list: r.list });

/** Re-reads one reminder and throws unless `check` passes — every write is confirmed, never assumed. */
async function confirm(id, what, check) {
  const fresh = await remctl(["info", String(id), "--json"]);
  if (!check(fresh)) throw new Error(`verification failed after ${what}: ${JSON.stringify({ completed: fresh.completed, dueDate: fresh.dueDate, recurrence: fresh.recurrence })}`);
  return fresh;
}

async function stripRecurrence(r) {
  await remctl(["edit", String(r.id), "--recurrence", RECURRENCE_CLEAR, "--json"]);
  await confirm(r.id, "clearing recurrence", (f) => !f.recurrence);
}

/**
 * Write order matters for interruption safety: set the new due date while the reminder is still
 * completed, and only then reopen it. A crash in between leaves it completed with an unchanged
 * completion date, so the next run recomputes the identical due date and finishes the job.
 */
async function reschedule(r, plan) {
  if (plan.stripRecurrence) await stripRecurrence(r);
  await remctl(["edit", String(r.id), "-d", plan.due, "--json"]);
  await confirm(r.id, "setting due date", (f) => sameDue(f.dueDate, plan.due, r.allDay));
  const res = await remctl(["undone", String(r.id), "--json"]);
  if (res?.results?.failed?.length || res?.results?.uncertain?.length) log("undone_not_clean", { ...label(r), result: res });
  await confirm(r.id, "reopening", (f) => f.completed === false && sameDue(f.dueDate, plan.due, r.allDay));
}

async function main() {
  const now = new Date();
  const all = await remctl(["export", "--json"]);
  const reminders = ONLY_ID ? all.filter((r) => r.id === ONLY_ID) : all;
  const counts = { exported: all.length, tagged: 0, completed_tagged: 0, rescheduled: 0, recurrence_stripped: 0, skipped: 0, failed: 0 };
  const failures = [];

  for (const r of reminders) {
    const plan = planReminder(r, now);
    if (plan.action === "ignore") continue;
    counts.tagged++;
    if (r.completed) counts.completed_tagged++;

    if (plan.action === "skip") {
      counts.skipped++;
      log("skipped", { ...label(r), reason: plan.reason });
      continue;
    }
    if (plan.action === "strip-recurrence") {
      if (DRY_RUN) { log("would_strip_recurrence", label(r)); continue; }
      try {
        await stripRecurrence(r);
        counts.recurrence_stripped++;
        log("recurrence_stripped", label(r));
      } catch (err) {
        counts.failed++;
        failures.push(`${r.title}: ${err.message}`);
        log("failed", { ...label(r), step: "strip-recurrence", error: err.message });
      }
      continue;
    }

    if (plan.completionSource === "run-time-fallback") log("completion_date_unreadable", { ...label(r), using: now.toISOString() });
    const change = { ...label(r), interval: plan.interval, old_due: r.dueDate, new_due: plan.due, all_day: Boolean(r.allDay), completed_at: r.completionDate ?? null, strip_recurrence: plan.stripRecurrence };
    if (DRY_RUN) { log("would_reschedule", change); counts.rescheduled++; continue; }
    try {
      await reschedule(r, plan);
      counts.rescheduled++;
      log("rescheduled", change);
    } catch (err) {
      counts.failed++;
      failures.push(`${r.title}: ${err.message}`);
      log("failed", { ...change, step: "reschedule", error: err.message });
    }
  }

  log("run_summary", counts);
  if (failures.length) {
    await notifyMoshi({ title: "🔴 Reminders repeat check failed", message: `${failures.length} reminder(s) failed: ${failures.slice(0, 3).join(" | ")}` });
    process.exitCode = 1;
  }
}

main().catch(async (err) => {
  log("fatal", { error: String(err?.stderr || err?.message || err) });
  await notifyMoshi({ title: "🔴 Reminders repeat check crashed", message: String(err?.message || err).slice(0, 300) });
  process.exit(1);
});
