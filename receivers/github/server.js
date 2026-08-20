import crypto from "node:crypto";
import express from "express";

const PORT = process.env.PORT || 8789;
const WEBHOOK_SECRET = process.env.GITHUB_WEBHOOK_SECRET;
const MOSHI_TOKEN = process.env.MOSHI_WEBHOOK_TOKEN;

if (!WEBHOOK_SECRET) {
  console.error("GITHUB_WEBHOOK_SECRET is required — set it to the secret configured on each repo's GitHub webhook.");
  process.exit(1);
}
if (!MOSHI_TOKEN) {
  console.error("MOSHI_WEBHOOK_TOKEN is required — Moshi webhook token from Settings.");
  process.exit(1);
}

function validSignature(header, rawBody) {
  if (!header || !header.startsWith("sha256=")) return false;
  const expected = crypto.createHmac("sha256", WEBHOOK_SECRET).update(rawBody).digest("hex");
  const expectedBuf = Buffer.from(`sha256=${expected}`);
  const actualBuf = Buffer.from(header);
  if (expectedBuf.length !== actualBuf.length) return false;
  return crypto.timingSafeEqual(expectedBuf, actualBuf);
}

const MOSHI_RETRY_DELAYS_MS = [5000, 15000, 30000]; // backoff on 429 — Moshi's free tier resets its quota every 60s

async function notifyMoshi({ title, message }) {
  for (let attempt = 0; ; attempt++) {
    const res = await fetch("https://api.getmoshi.app/api/webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: MOSHI_TOKEN, title, message, unified: true }),
    });
    if (res.ok) return;
    if (res.status !== 429 || attempt === MOSHI_RETRY_DELAYS_MS.length) {
      console.error("Moshi notify failed:", res.status, await res.text().catch(() => ""));
      return;
    }
    const delay = MOSHI_RETRY_DELAYS_MS[attempt];
    console.warn(`Moshi rate-limited, retrying in ${delay / 1000}s (attempt ${attempt + 1}/${MOSHI_RETRY_DELAYS_MS.length})`);
    await new Promise((resolve) => setTimeout(resolve, delay));
  }
}

const app = express();
app.use(
  express.json({
    verify: (req, _res, buffer) => {
      req.rawBody = buffer;
    },
  })
);

app.get("/healthz", (_req, res) => res.status(200).send("ok"));

app.post("/github/webhook", async (req, res) => {
  const signature = req.headers["x-hub-signature-256"];
  if (!validSignature(signature, req.rawBody)) {
    console.warn(`${new Date().toISOString()} Rejected webhook with invalid/missing signature`);
    return res.status(403).send("invalid signature");
  }

  const event = req.headers["x-github-event"];
  const payload = req.body;

  // Ack GitHub immediately; this receiver only notifies, it never mutates anything.
  res.status(200).send("ok");

  if (event !== "workflow_run") {
    console.log(`${new Date().toISOString()} Ignoring event "${event}"`);
    return;
  }

  const run = payload.workflow_run;
  if (payload.action !== "completed" || !run) {
    console.log(`${new Date().toISOString()} Ignoring workflow_run action "${payload.action}"`);
    return;
  }

  const repo = payload.repository?.full_name || "unknown repo";
  const branch = run.head_branch ? ` (${run.head_branch})` : "";
  const detail = `${run.name || "Workflow"}${branch}\n${run.html_url || ""}`.trim();

  if (run.conclusion === "success") {
    console.log(`${new Date().toISOString()} workflow_run succeeded for ${repo}`);
    await notifyMoshi({ title: `✅ ${repo} run succeeded`, message: detail });
  } else if (run.conclusion === "failure") {
    console.log(`${new Date().toISOString()} workflow_run failed for ${repo}`);
    await notifyMoshi({ title: `🔴 ${repo} run failed`, message: detail });
  } else {
    console.log(`${new Date().toISOString()} Ignoring workflow_run conclusion "${run.conclusion}" for ${repo}`);
  }
});

app.listen(PORT, () => {
  console.log(`GitHub receiver listening on :${PORT}`);
});
