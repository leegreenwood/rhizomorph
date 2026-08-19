import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import express from "express";
import jwt from "jsonwebtoken";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PORT = process.env.PORT || 8788;
const WEBHOOK_SECRET = process.env.NETLIFY_WEBHOOK_SECRET;
const MOSHI_TOKEN = process.env.MOSHI_WEBHOOK_TOKEN;

if (!WEBHOOK_SECRET) {
  console.error("NETLIFY_WEBHOOK_SECRET is required — set it to the JWS secret configured on each Netlify outgoing webhook.");
  process.exit(1);
}
if (!MOSHI_TOKEN) {
  console.error("MOSHI_WEBHOOK_TOKEN is required — Moshi webhook token from Settings.");
  process.exit(1);
}

const sitesPath = path.join(__dirname, "sites.json");
function loadSites() {
  try {
    return JSON.parse(fs.readFileSync(sitesPath, "utf8"));
  } catch {
    return { by_site_id: {}, by_name: {} };
  }
}

function friendlyName(payload) {
  const sites = loadSites();
  return (
    sites.by_site_id?.[payload.site_id] ||
    sites.by_name?.[payload.name] ||
    payload.name ||
    payload.site_id ||
    "unknown site"
  );
}

function validSignature(token, rawBody) {
  if (!token) return false;
  try {
    const decoded = jwt.verify(token, WEBHOOK_SECRET, {
      issuer: "netlify",
      algorithms: ["HS256"],
    });
    const hashedBody = crypto.createHash("sha256").update(rawBody).digest("hex");
    return decoded.sha256 === hashedBody;
  } catch {
    return false;
  }
}

async function notifyMoshi({ title, message }) {
  const res = await fetch("https://api.getmoshi.app/api/webhook", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: MOSHI_TOKEN, title, message, unified: true }),
  });
  if (!res.ok) {
    console.error("Moshi notify failed:", res.status, await res.text().catch(() => ""));
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

app.post("/netlify/webhook", async (req, res) => {
  const signature = req.headers["x-webhook-signature"];
  if (!validSignature(signature, req.rawBody)) {
    console.warn(`${new Date().toISOString()} Rejected webhook with invalid/missing signature`);
    return res.status(403).send("invalid signature");
  }

  const payload = req.body;
  const site = friendlyName(payload);
  const branch = payload.branch ? ` (${payload.branch})` : "";

  // Ack Netlify immediately; this receiver only notifies, it never mutates anything.
  res.status(200).send("ok");

  if (payload.state === "ready") {
    console.log(`${new Date().toISOString()} deploy_succeeded for ${site}`);
    await notifyMoshi({
      title: `✅ ${site} deployed`,
      message: `${payload.title || payload.commit_message || "Deploy"}${branch}`.trim(),
    });
  } else if (payload.state === "error") {
    console.log(`${new Date().toISOString()} deploy_failed for ${site}`);
    await notifyMoshi({
      title: `🔴 ${site} deploy failed`,
      message: `${payload.error_message || "No error detail in payload"}${branch}`.trim(),
    });
  } else {
    console.log(`${new Date().toISOString()} Ignoring deploy state "${payload.state}" for ${site}`);
  }
});

app.listen(PORT, () => {
  console.log(`Netlify receiver listening on :${PORT}`);
});
