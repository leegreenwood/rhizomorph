#!/usr/bin/env node
// Pulls the Hyphae repo so the Mac has the latest daily notes. See README.md in this folder.
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
const LOG_FILE = process.env.LOG_FILE || join(HERE, "logs", "pull-hyphae.log");
const MOSHI_TOKEN = process.env.MOSHI_WEBHOOK_TOKEN;
const RETRY_DELAYS_MS = [60_000, 120_000]; // the network can lag behind a wake from sleep

mkdirSync(dirname(LOG_FILE), { recursive: true });
function log(event, fields = {}) {
  const line = JSON.stringify({ ts: new Date().toISOString(), event, ...fields });
  appendFileSync(LOG_FILE, line + "\n");
  console.log(line);
}

async function git(...args) {
  // Prompts are off: nobody is there to answer one, and it would hang the job.
  const env = { ...process.env, GIT_TERMINAL_PROMPT: "0", GCM_INTERACTIVE: "never" };
  const { stdout } = await run("git", ["-C", REPO, ...args], { env, timeout: 120_000 });
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

// --ff-only: never creates a merge commit or leaves conflict markers in the vault.
// If local work blocks the pull, git refuses and changes nothing.
async function pull() {
  for (let attempt = 0; ; attempt++) {
    try {
      return await git("pull", "--ff-only");
    } catch (err) {
      if (attempt === RETRY_DELAYS_MS.length) throw err;
      log("pull_retry", { attempt: attempt + 1, error: String(err.stderr || err.message).trim().slice(0, 300) });
      await new Promise((resolve) => setTimeout(resolve, RETRY_DELAYS_MS[attempt]));
    }
  }
}

try {
  const before = await git("rev-parse", "HEAD");
  await pull();
  const after = await git("rev-parse", "HEAD");
  const commits = before === after ? 0 : Number(await git("rev-list", "--count", `${before}..${after}`));
  log("run_summary", { ok: true, commits, head: after.slice(0, 7) });
} catch (err) {
  const error = String(err.stderr || err.message).trim().slice(0, 300);
  log("run_summary", { ok: false, commits: 0, error });
  await notifyMoshi("🔴 Hyphae pull failed", error);
  process.exitCode = 1;
}
