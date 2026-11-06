#!/usr/bin/env node
// launchd runs this every minute. If spores are flagged and no worker is running, it starts process.js
// in the Spores pane of the Rhizomorph Herdr workspace. It changes nothing itself. See README.md.

import { join } from "node:path";
import { HERE, alertOnce, log, run, spores, workerPid } from "./runtime.js";

const WORKSPACE = process.env.RHIZOMORPH_WORKSPACE_BIN || join(HERE, "../../bin/rhizomorph-workspace");
const HERDR = process.env.HERDR_BIN || "/opt/homebrew/bin/herdr";
const SHELLS = new Set(["zsh", "-zsh", "bash", "-bash", "sh", "fish"]);

async function paneIsIdle(pane) {
  const { stdout } = await run(HERDR, ["pane", "process-info", "--pane", pane]);
  const { process_info: info } = JSON.parse(stdout).result;
  return (info.foreground_processes ?? []).every((p) => SHELLS.has(p.name));
}

try {
  const flagged = await spores(["list", "--flagged", "--limit", "1"]);
  if (flagged.length && !workerPid()) {
    const { stdout } = await run(WORKSPACE, ["--spores-pane"], { timeout: 180_000 });
    const pane = stdout.trim();
    if (!pane) throw new Error("rhizomorph-workspace didn't name a Spores pane");
    if (await paneIsIdle(pane)) {
      await run(HERDR, ["pane", "run", pane, `${process.execPath} ${join(HERE, "process.js")}`]);
      log("worker_started", { pane });
    } else {
      // Something typed into the pane could be running: sending keystrokes to it would be unsafe.
      log("pane_busy", { pane });
      await alertOnce("pane_busy", "Spores check is waiting", `Spores are flagged, but Herdr pane ${pane} is busy. Free it and they'll be picked up within a minute.`);
    }
  }
} catch (err) {
  log("tick_failed", { error: String(err) });
  await alertOnce("tick", "Spores check can't start", String(err.message).slice(0, 300));
  process.exitCode = 1;
}
