#!/usr/bin/env node
// Starts Hyphae's daily note workflow, waits for it, then pulls the repo so the
// Mac has today's note. See README.md in this folder.
//
// Usage: node pull-hyphae.js

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";

const run = promisify(execFile);
const HERE = dirname(fileURLToPath(import.meta.url));
try { process.loadEnvFile(join(HERE, ".env")); } catch { /* .env is optional */ }

const REPO = process.env.HYPHAE_REPO || join(homedir(), "Code", "hyphae");
const GITHUB_REPO = process.env.HYPHAE_GITHUB_REPO || "leegreenwood/hyphae";
const WORKFLOW = "daily-note.yml";
const LOG_FILE = process.env.LOG_FILE || join(HERE, "logs", "pull-hyphae.log");
const MOSHI_TOKEN = process.env.MOSHI_WEBHOOK_TOKEN;
const RETRY_DELAYS_MS = [60_000, 120_000]; // the network can lag behind a wake from sleep
const WORKFLOW_POLL_MS = 5_000;
const WORKFLOW_TIMEOUT_MS = 5 * 60_000;
const CLOCK_SKEW_MS = 30_000; // this Mac's clock and GitHub's can disagree slightly

mkdirSync(dirname(LOG_FILE), { recursive: true });
function log(event, fields = {}) {
  const line = JSON.stringify({ ts: new Date().toISOString(), event, ...fields });
  appendFileSync(LOG_FILE, line + "\n");
  console.log(line);
}

const errorText = (err) => String(err.stderr || err.message).trim().slice(0, 300);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Prompts are off: nobody is there to answer one, and it would hang the job.
async function git(...args) {
  const env = { ...process.env, GIT_TERMINAL_PROMPT: "0", GCM_INTERACTIVE: "never" };
  const { stdout } = await run("git", ["-C", REPO, ...args], { env, timeout: 120_000 });
  return stdout.trim();
}

async function gh(...args) {
  const env = { ...process.env, GH_PROMPT_DISABLED: "1" };
  const { stdout } = await run("gh", [...args, "-R", GITHUB_REPO], { env, timeout: 60_000 });
  return stdout.trim();
}

async function notifyMoshi(title, message) {
  if (!MOSHI_TOKEN) return log("alert_not_sent", { reason: "MOSHI_WEBHOOK_TOKEN not set", title });
  try {
    const res = await fetch("https://api.getmoshi.app/api/webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: MOSHI_TOKEN, title, message, unified: true }),
    });
    if (!res.ok) log("alert_failed", { status: res.status });
  } catch (err) {
    log("alert_failed", { error: String(err) });
  }
}

async function withRetries(name, fn) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await fn();
    } catch (err) {
      if (attempt === RETRY_DELAYS_MS.length) throw err;
      log(`${name}_retry`, { attempt: attempt + 1, error: errorText(err) });
      await sleep(RETRY_DELAYS_MS[attempt]);
    }
  }
}

// The workflow is idempotent: it skips if today's note already exists, so
// starting it when the note is already there changes nothing.
async function createDailyNote() {
  const startedAt = Date.now();
  await withRetries("workflow_start", () => gh("workflow", "run", WORKFLOW, "--ref", "main"));
  while (Date.now() - startedAt < WORKFLOW_TIMEOUT_MS) {
    await sleep(WORKFLOW_POLL_MS);
    let runs;
    try {
      runs = JSON.parse(await gh("run", "list", "--workflow", WORKFLOW, "--event", "workflow_dispatch", "--limit", "5", "--json", "createdAt,status,conclusion"));
    } catch (err) {
      log("workflow_poll_failed", { error: errorText(err) }); // keep polling until the timeout
      continue;
    }
    const current = runs.find((r) => Date.parse(r.createdAt) >= startedAt - CLOCK_SKEW_MS);
    if (current?.status !== "completed") continue;
    if (current.conclusion !== "success") throw new Error(`daily note workflow finished: ${current.conclusion}`);
    return;
  }
  throw new Error(`daily note workflow not finished after ${WORKFLOW_TIMEOUT_MS / 60_000} minutes`);
}

// --ff-only: never creates a merge commit or leaves conflict markers in the vault.
// If local work blocks the pull, git refuses and changes nothing.
const pull = () => withRetries("pull", () => git("pull", "--ff-only"));

// A workflow failure doesn't stop the pull: anything already on GitHub is still worth having.
const errors = [];
try {
  await createDailyNote();
} catch (err) {
  errors.push({ title: "🔴 Daily note workflow failed", error: errorText(err) });
}

let commits = 0;
let head;
try {
  const before = await git("rev-parse", "HEAD");
  await pull();
  head = await git("rev-parse", "HEAD");
  commits = before === head ? 0 : Number(await git("rev-list", "--count", `${before}..${head}`));
} catch (err) {
  errors.push({ title: "🔴 Hyphae pull failed", error: errorText(err) });
}

log("run_summary", { ok: errors.length === 0, commits, head: head?.slice(0, 7), errors: errors.map((e) => e.error) });
for (const { title, error } of errors) await notifyMoshi(title, error);
if (errors.length) process.exitCode = 1;
