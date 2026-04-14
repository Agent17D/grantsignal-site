/**
 * GrantSignal Preferences Worker
 * Route: grantsignal.news/api/preferences*
 *
 * Accepts POST with JSON body:
 *   { email, state, budget, org_type, mission_area, populations, grant_size, experience }
 *
 * Saves preferences to GitHub repo as data/preferences/{sha256(email)}.json
 *
 * Secrets required (set via Cloudflare dashboard → Workers → grantsignal-preferences → Settings → Variables):
 *   GITHUB_TOKEN — Personal access token with repo write access (contents: write)
 *
 * ─────────────────────────────────────────────────────────────────
 * MANUAL SETUP INSTRUCTIONS (one-time):
 *
 * 1. Go to https://dash.cloudflare.com → Workers & Pages
 * 2. Click "grantsignal-preferences"
 * 3. Go to "Settings" tab → "Variables and Secrets"
 * 4. Click "Add variable", set Type = Secret
 *    Name: GITHUB_TOKEN
 *    Value: your GitHub personal access token (needs repo / contents:write)
 * 5. Click "Deploy"
 *
 * The worker is already deployed at grantsignal.news/api/preferences*
 * ─────────────────────────────────────────────────────────────────
 *
 * Deployed as service-worker syntax (not ES module) for compatibility.
 */

addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request));
});

const GITHUB_REPO   = "Agent17D/grantcommand-site";
const GITHUB_BRANCH = "main";

async function handleRequest(request) {
  // CORS preflight
  if (request.method === "OPTIONS") {
    return corsResponse(null, 204);
  }

  if (request.method !== "POST") {
    return corsResponse({ success: false, error: "Method not allowed" }, 405);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return corsResponse({ success: false, error: "Invalid JSON body" }, 400);
  }

  const { email, state, budget, org_type, mission_area, populations, grant_size, experience } = body;

  if (!email || typeof email !== "string" || !email.includes("@")) {
    return corsResponse({ success: false, error: "Valid email is required" }, 400);
  }

  // SHA-256 hash of lowercase email (via Web Crypto API)
  const emailHash = await sha256(email.trim().toLowerCase());

  const preferences = {
    email_hash:   emailHash,
    email:        email.trim().toLowerCase(),
    state:        state        || null,
    budget:       budget       || null,
    org_type:     org_type     || null,
    mission_area: mission_area || null,
    populations:  Array.isArray(populations) ? populations : [],
    grant_size:   grant_size   || null,
    experience:   experience   || null,
    saved_at:     new Date().toISOString(),
  };

  const filePath    = "data/preferences/" + emailHash + ".json";
  const fileContent = JSON.stringify(preferences, null, 2);

  try {
    await upsertGitHubFile(GITHUB_TOKEN, filePath, fileContent, emailHash);
  } catch (err) {
    console.error("GitHub upsert failed:", err.message);
    return corsResponse({ success: false, error: "Failed to save preferences" }, 500);
  }

  return corsResponse({ success: true, hash: emailHash });
}

// ── Helpers ────────────────────────────────────────────────────────────────

async function sha256(message) {
  const msgBuffer  = new TextEncoder().encode(message);
  const hashBuffer = await crypto.subtle.digest("SHA-256", msgBuffer);
  const hashArray  = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map(b => b.toString(16).padStart(2, "0")).join("");
}

async function upsertGitHubFile(token, path, content, emailHash) {
  const url = "https://api.github.com/repos/" + GITHUB_REPO + "/contents/" + path;

  const headers = {
    "Authorization": "token " + token,
    "Content-Type":  "application/json",
    "User-Agent":    "GrantSignal-Worker/1.0",
    "Accept":        "application/vnd.github.v3+json",
  };

  // Try to fetch existing file SHA (needed for updates)
  let sha;
  const getResp = await fetch(url, { headers });
  if (getResp.ok) {
    const existing = await getResp.json();
    sha = existing.sha;
  } else if (getResp.status !== 404) {
    const errText = await getResp.text();
    throw new Error("GitHub GET failed (" + getResp.status + "): " + errText);
  }

  // Base64-encode content (btoa works in Cloudflare Workers)
  const encoded = btoa(unescape(encodeURIComponent(content)));

  const shortHash = emailHash.slice(0, 8);
  const putBody = {
    message: "Update preferences: " + shortHash + "…",
    content: encoded,
    branch:  GITHUB_BRANCH,
  };
  if (sha) putBody.sha = sha;

  const putResp = await fetch(url, {
    method:  "PUT",
    headers,
    body:    JSON.stringify(putBody),
  });

  if (!putResp.ok) {
    const errText = await putResp.text();
    throw new Error("GitHub PUT failed (" + putResp.status + "): " + errText);
  }
}

function corsResponse(body, status) {
  var s = status || 200;
  var headers = {
    "Content-Type":                 "application/json",
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
  return new Response(
    body !== null ? JSON.stringify(body) : null,
    { status: s, headers: headers }
  );
}
