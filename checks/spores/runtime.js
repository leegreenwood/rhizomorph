// What tick.js and process.js share: paths, logging, the worker lock and Moshi alerts.

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";

export const HERE = dirname(fileURLToPath(import.meta.url));
try { process.loadEnvFile(join(HERE, ".env")); } catch { /* .env is optional */ }

export const LOG_DIR = process.env.SPORES_CHECK_LOG_DIR || join(HERE, "logs");
export const LOCK_FILE = join(LOG_DIR, "worker.lock");
export const STATE_FILE = join(LOG_DIR, "state.json");
export const SPORES = process.env.SPORES_BIN || join(homedir(), "Code/spores-app/dist/bin/spores");
export const MOSHI = process.env.MOSHI_HOOK_BIN || "/opt/homebrew/bin/moshi-hook";
export const REMCTL = process.env.REMCTL_PATH || join(homedir(), "bin/remctl");
export const CLAUDE = process.env.CLAUDE_BIN || "/opt/homebrew/bin/claude";

const LOG_FILE = join(LOG_DIR, "spores.log");
const ALERT_STATE_FILE = join(LOG_DIR, "alert-state.json");
const ALERT_EVERY_MS = 60 * 60 * 1000;

mkdirSync(LOG_DIR, { recursive: true });

export const run = promisify(execFile);

export function log(event, fields = {}) {
  appendFileSync(LOG_FILE, JSON.stringify({ ts: new Date().toISOString(), event, ...fields }) + "\n");
}

export function readJson(path, fallback) {
  try { return JSON.parse(readFileSync(path, "utf8")); } catch { return fallback; }
}

export async function spores(argv) {
  const { stdout } = await run(SPORES, [...argv, "--json"], { maxBuffer: 64 * 1024 * 1024, timeout: 30_000 });
  return JSON.parse(stdout);
}

/** The pid holding the worker lock, or null if there is none or its process has gone. */
export function workerPid() {
  const pid = Number(readJson(LOCK_FILE, null)?.pid);
  if (!pid) return null;
  try { process.kill(pid, 0); return pid; } catch { return null; }
}

/** Best effort, like moshi-hook notify itself: a failed alert must never stop the job. */
export async function notify(title, message, { error = false, silent = false } = {}) {
  const args = ["notify", "--source", "rhizomorph", "--label", "Rhizomorph", "--title", title];
  if (error) args.push("--error");
  if (silent) args.push("--silent");
  try {
    await run(MOSHI, [...args, message], { timeout: 60_000 });
  } catch (err) {
    log("alert_failed", { title, error: String(err) });
  }
}

/** An error alert, at most hourly per key, as both jobs can hit the same fault every minute. */
export async function alertOnce(key, title, message) {
  const sent = readJson(ALERT_STATE_FILE, {});
  if (Date.now() - (sent[key] ?? 0) < ALERT_EVERY_MS) return;
  sent[key] = Date.now();
  writeFileSync(ALERT_STATE_FILE, JSON.stringify(sent));
  await notify(title, message, { error: true });
}
