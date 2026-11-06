#!/usr/bin/env node
// The Spores worker: runs in the Rhizomorph Herdr workspace until no flagged spores are left. It asks
// a tool-free Claude what each one calls for, asks Lee through Moshi, and only then acts. See README.md.
//
// Usage: node process.js [--dry-run]

import { spawn } from "node:child_process";
import { closeSync, openSync, readFileSync, renameSync, rmSync, writeFileSync, writeSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import {
  CLAUDE, LOCK_FILE, MOSHI, REMCTL, STATE_FILE,
  alertOnce, log, notify, readJson, run, spores, workerPid,
} from "./runtime.js";
import {
  TAG_DECLINED, TAG_DONE, TAG_FAILED, TAG_UNSURE,
  approvalQuestion, askOutcome, outcomeArgs, proposalSchema, quote, reminderArgs, systemPrompt, textHash, userMessage, validateProposal,
} from "./lib.js";

const DRY_RUN = process.argv.includes("--dry-run");
const MODEL = process.env.SPORES_CHECK_MODEL || "claude-haiku-5-5";
const ASK_TIMEOUT = process.env.SPORES_CHECK_ASK_TIMEOUT || "2h";
const POLL_MS = Number(process.env.SPORES_CHECK_POLL_MS || 30_000);
const MOSHI_RETRY_MS = Number(process.env.SPORES_CHECK_MOSHI_RETRY_MS || 5 * 60_000);
const MAX_CLASSIFY_ATTEMPTS = 3;
const REMINDERS_NOTE = process.env.REMINDERS_NOTE || join(homedir(), "Code/hyphae/systems/Reminders.md");

const state = readJson(STATE_FILE, { spores: {} }); // Survives restarts: cached proposals, and any add not yet recorded on its spore.
const asks = new Map(); // spore id -> { child, hash, settling }: approvals out on Lee's phone.
const counts = { approved: 0, declined: 0, unsure: 0, failed: 0 };
let moshiDownUntil = 0;

function say(line) {
  const time = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  console.log(`${time}  ${line}`);
}

function saveState() {
  if (DRY_RUN) return;
  writeFileSync(`${STATE_FILE}.tmp`, JSON.stringify(state, null, 2));
  renameSync(`${STATE_FILE}.tmp`, STATE_FILE);
}

function acquireLock() {
  const holder = workerPid();
  if (holder) return false;
  rmSync(LOCK_FILE, { force: true });
  try {
    const fd = openSync(LOCK_FILE, "wx");
    writeSync(fd, JSON.stringify({ pid: process.pid }));
    closeSync(fd);
    return true;
  } catch {
    return false; // Another worker got there between the check and the open.
  }
}

async function classify(spore) {
  const lists = await run(REMCTL, ["lists", "--json"], { maxBuffer: 16 * 1024 * 1024, timeout: 60_000 })
    .then(({ stdout }) => JSON.parse(stdout).filter((l) => !l.isGroup));
  let remindersNote = "";
  try { remindersNote = readFileSync(REMINDERS_NOTE, "utf8"); } catch { /* the prompt copes without it */ }

  const now = new Date();
  const listNames = lists.map((l) => l.title);
  // No tools, settings or MCP servers: a note can talk this model into a bad proposal, but into nothing
  // else. Skipping settings also keeps this call's own hooks away from Moshi.
  const env = { ...process.env };
  delete env.CLAUDECODE;
  delete env.CLAUDE_CODE_PLUGIN_DIRS;
  const call = run(CLAUDE, [
    "-p", userMessage(spore),
    "--model", MODEL,
    "--tools", "",
    "--setting-sources", "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--system-prompt", systemPrompt({ now, listNames, remindersNote }),
    "--json-schema", JSON.stringify(proposalSchema(listNames)),
    "--output-format", "json",
  ], { cwd: tmpdir(), env, maxBuffer: 16 * 1024 * 1024, timeout: 120_000 });
  call.child.stdin.end();
  const { stdout } = await call;
  const result = JSON.parse(stdout);
  if (result.is_error) throw new Error(result.result || "the model returned an error");
  const checked = validateProposal(result.structured_output, lists, now);
  if (!checked.ok) throw new Error(checked.error);
  return checked.proposal;
}

async function finish(spore, updateArgs, event, fields = {}) {
  await spores(["update", String(spore.id), ...updateArgs]);
  delete state.spores[spore.id];
  saveState();
  log(event, { id: spore.id, ...fields });
}

async function markUnsure(spore, reason) {
  await finish(spore, ["--no-flagged", ...outcomeArgs(TAG_UNSURE)], "unsure", { reason });
  counts.unsure++;
  say(`#${spore.id} nothing to do: ${reason}`);
  await notify(`Spore ${spore.id} needs you`, `I don't know what to do with "${quote(spore.text)}". ${reason} The flag is cleared. Flag it again to retry.`);
}

async function markFailed(spore, what, err) {
  const detail = String(err?.stderr || err?.message || err).trim().slice(0, 300);
  await finish(spore, ["--no-flagged", ...outcomeArgs(TAG_FAILED)], "failed", { what, error: detail });
  counts.failed++;
  say(`#${spore.id} failed: ${what}: ${detail}`);
  await notify(`Spore ${spore.id} failed`, `${what}: "${quote(spore.text)}". ${detail} The flag is cleared. Flag it again to retry.`, { error: true });
}

async function recordDone(spore, entry) {
  await finish(spore, ["--status", "archived", "--no-flagged", ...outcomeArgs(TAG_DONE)], "reminder_added", {
    reminder_id: entry.reminder_id, title: entry.proposal.title, list: entry.proposal.list,
  });
  counts.approved++;
  say(`#${spore.id} added to ${entry.proposal.list}: ${entry.proposal.title}`);
  await notify("Reminder added", `${entry.proposal.title} (${entry.proposal.list})`, { silent: true });
}

async function addReminder(spore, entry) {
  let stdout;
  try {
    ({ stdout } = await run(REMCTL, reminderArgs(entry.proposal), { maxBuffer: 16 * 1024 * 1024, timeout: 120_000 }));
  } catch (err) {
    return markFailed(spore, "Couldn't add the reminder, so check it isn't there before retrying", err);
  }
  // Recorded before the spore is updated, so a crash here can't add it twice on the next run.
  let created = null;
  try { created = JSON.parse(stdout); } catch { log("reminder_output_unreadable", { id: spore.id, stdout: stdout.slice(0, 300) }); }
  entry.reminder_id = created?.id ?? "unknown";
  saveState();
  await recordDone(spore, entry);
}

async function settle(spore, entry, hash, code) {
  const outcome = askOutcome(code);
  log("answer", { id: spore.id, outcome, exit_code: code });
  if (outcome === "timed_out") {
    say(`#${spore.id} no answer yet, asking again`);
  } else if (outcome === "unreachable") {
    moshiDownUntil = Date.now() + MOSHI_RETRY_MS;
    say(`#${spore.id} Moshi unreachable (exit ${code}), trying again in ${Math.round(MOSHI_RETRY_MS / 60_000)} min`);
  } else if (outcome === "declined") {
    await finish(spore, ["--no-flagged", ...outcomeArgs(TAG_DECLINED)], "declined");
    counts.declined++;
    say(`#${spore.id} declined`);
  } else {
    // The answer was to this exact wording: if Lee has since changed or unflagged it, it no longer applies.
    const fresh = await spores(["get", String(spore.id)]);
    if (fresh.flagged_at && fresh.status === "new" && textHash(fresh.text) === hash) {
      await addReminder(fresh, entry);
    } else {
      delete state.spores[spore.id];
      saveState();
      log("approval_stale", { id: spore.id });
      say(`#${spore.id} changed after approval, so nothing was done`);
    }
  }
}

function ask(spore, entry, hash) {
  const question = approvalQuestion(spore, entry.proposal);
  // --require-remote: only the phone can approve, never whatever is typed into this pane.
  const child = spawn(MOSHI, [
    "ask", "--require-remote", "--source", "rhizomorph", "--label", "Rhizomorph", "--timeout", ASK_TIMEOUT, question,
  ], { stdio: ["ignore", "ignore", "pipe"] });
  const pending = { child, hash, settling: false };
  asks.set(spore.id, pending);
  log("asked", { id: spore.id, proposal: entry.proposal });
  say(`#${spore.id} waiting for approval: ${question.split("\n")[0]}`);

  const done = (code) => {
    if (asks.get(spore.id) !== pending) return; // Withdrawn: the spore changed while this was out.
    pending.settling = true;
    settle(spore, entry, hash, code)
      .catch((err) => {
        log("settle_failed", { id: spore.id, error: String(err) });
        say(`#${spore.id} error: ${err.message}`);
      })
      .finally(() => asks.delete(spore.id));
  };
  child.on("error", () => done(4));
  child.on("close", (code) => done(code ?? 4));
}

/** Withdraw approvals whose spore was unflagged or edited. The card stays, but answering it does nothing. */
function withdrawStale(flagged) {
  const current = new Map(flagged.map((s) => [s.id, textHash(s.text)]));
  for (const [id, pending] of asks) {
    if (pending.settling || current.get(id) === pending.hash) continue;
    asks.delete(id);
    pending.child.kill();
    delete state.spores[id];
    saveState();
    log("ask_withdrawn", { id });
    say(`#${id} changed or unflagged, so its approval no longer counts`);
  }
}

async function consider(spore) {
  const hash = textHash(spore.text);
  let entry = state.spores[spore.id];
  if (entry?.reminder_id) return recordDone(spore, entry); // A previous run added it, then stopped short.
  if (entry?.hash !== hash) entry = state.spores[spore.id] = { hash, attempts: 0 };

  // RSS text is written by third parties: it never reaches the model, and nothing is proposed from it.
  if (spore.source === "rss") return markUnsure(spore, "RSS posts aren't handled yet.");

  if (!entry.proposal) {
    try {
      entry.proposal = await classify(spore);
      log("classified", { id: spore.id, model: MODEL, proposal: entry.proposal });
      saveState();
    } catch (err) {
      entry.attempts++;
      saveState();
      log("classify_failed", { id: spore.id, attempt: entry.attempts, error: String(err) });
      say(`#${spore.id} couldn't classify (attempt ${entry.attempts}): ${err.message}`);
      if (entry.attempts >= MAX_CLASSIFY_ATTEMPTS) await markFailed(spore, "Couldn't work out what to do", err);
      return;
    }
  }
  if (entry.proposal.action === "none") return markUnsure(spore, entry.proposal.reason || "");
  if (Date.now() >= moshiDownUntil) ask(spore, entry, hash);
}

async function dryRun() {
  const flagged = await spores(["list", "--flagged", "--limit", "0"]);
  for (const spore of flagged) {
    if (spore.source === "rss") { console.log(`#${spore.id} rss: not classified`); continue; }
    try {
      const proposal = await classify(spore);
      console.log(proposal.action === "none"
        ? `#${spore.id} none: ${proposal.reason}\n    "${quote(spore.text)}"`
        : `#${spore.id} would ask:\n${approvalQuestion(spore, proposal).replace(/^/gm, "    ")}`);
    } catch (err) {
      console.log(`#${spore.id} couldn't classify: ${err.message}`);
    }
  }
  console.log(`${flagged.length} flagged. Nothing was changed.`);
}

async function main() {
  if (DRY_RUN) return dryRun();
  if (!acquireLock()) return say("Another worker is already running.");

  const stop = () => {
    for (const { child } of asks.values()) child.kill();
    rmSync(LOCK_FILE, { force: true });
    process.exit(130);
  };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);

  try {
    say("Handling flagged spores.");
    log("run_start", { pid: process.pid });
    for (;;) {
      const flagged = await spores(["list", "--flagged", "--limit", "0"]);
      withdrawStale(flagged);
      // One card at a time, oldest flag first: with two out at once, answering one lost the other
      // from Lee's phone while its ask carried on waiting.
      for (const spore of flagged) {
        if (asks.size) break;
        await consider(spore);
      }
      // Moshi being down, or a retry, leaves work behind that the next pass picks up.
      const remaining = flagged.length ? await spores(["list", "--flagged", "--limit", "0"]) : [];
      if (!asks.size && !remaining.length) break;
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
    log("run_summary", counts);
    say(`Done: ${counts.approved} added, ${counts.declined} declined, ${counts.unsure} unsure, ${counts.failed} failed.`);
  } catch (err) {
    log("run_failed", { error: String(err) });
    say(`Stopped: ${err.message}`);
    for (const { child } of asks.values()) child.kill();
    await alertOnce("worker", "Spores check stopped", String(err.message).slice(0, 300));
    process.exitCode = 1;
  } finally {
    rmSync(LOCK_FILE, { force: true });
  }
}

await main();
