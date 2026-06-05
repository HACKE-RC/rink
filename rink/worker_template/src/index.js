import { DurableObject } from "cloudflare:workers";

const JSON_HEADERS = { "Content-Type": "application/json; charset=utf-8" };
const HTML_HEADERS = {
  "Cache-Control": "no-store, max-age=0",
  "Content-Type": "text/html; charset=utf-8",
};

export class RinkLinks extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.ctx.storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS drops (
          id TEXT PRIMARY KEY,
          prefix TEXT NOT NULL,
          label TEXT,
          created_at INTEGER NOT NULL,
          expires_at INTEGER NOT NULL,
          max_uploads INTEGER NOT NULL,
          max_bytes INTEGER NOT NULL,
          max_download_views INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reservations (
          id TEXT PRIMARY KEY,
          file_id TEXT NOT NULL,
          key TEXT NOT NULL,
          filename TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
          id TEXT PRIMARY KEY,
          key TEXT NOT NULL,
          filename TEXT NOT NULL,
          size INTEGER NOT NULL,
          content_type TEXT,
          uploaded_at INTEGER NOT NULL,
          download_token_hash TEXT NOT NULL,
          views INTEGER NOT NULL DEFAULT 0,
          max_views INTEGER NOT NULL
        );
      `);
    });
  }

  async createDrop(drop) {
    const existing = this.getDropRow();
    if (existing) {
      return this.publicDrop(existing);
    }
    this.ctx.storage.sql.exec(
      `INSERT INTO drops
       (id, prefix, label, created_at, expires_at, max_uploads, max_bytes, max_download_views)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      drop.id,
      drop.prefix,
      drop.label,
      drop.createdAt,
      drop.expiresAt,
      drop.maxUploads,
      drop.maxBytes,
      drop.maxDownloadViews,
    );
    return this.publicDrop(this.getDropRow());
  }

  async getDrop() {
    const drop = this.getDropRow();
    if (!drop) {
      return null;
    }
    return {
      ...this.publicDrop(drop),
      files: this.listFiles(),
      active: Date.now() <= drop.expires_at && this.uploadSlotsUsed() < drop.max_uploads,
    };
  }

  async reserveUpload(input) {
    const drop = this.getDropRow();
    if (!drop) {
      return { ok: false, status: 404, error: "receive link not found" };
    }
    const now = Date.now();
    if (now > drop.expires_at) {
      return { ok: false, status: 410, error: "receive link expired" };
    }
    if (input.size > drop.max_bytes) {
      return { ok: false, status: 413, error: "file is larger than this link allows" };
    }
    this.clearExpiredReservations(now);
    if (this.uploadSlotsUsed() >= drop.max_uploads) {
      return { ok: false, status: 409, error: "receive link already used" };
    }

    const fileId = randomToken(16);
    const reservationId = randomToken(16);
    const token = randomToken(32);
    const safeName = safeFilename(input.filename);
    const key = `${drop.prefix}/${drop.id}/${fileId}-${safeName}`;
    const tokenHash = await sha256Hex(token);

    this.ctx.storage.sql.exec(
      `INSERT INTO reservations (id, file_id, key, filename, created_at)
       VALUES (?, ?, ?, ?, ?)`,
      reservationId,
      fileId,
      key,
      safeName,
      now,
    );
    return {
      ok: true,
      reservationId,
      fileId,
      key,
      filename: safeName,
      downloadToken: token,
      downloadTokenHash: tokenHash,
      maxViews: drop.max_download_views,
    };
  }

  async completeUpload(input) {
    const reservation = this.ctx.storage.sql
      .exec("SELECT * FROM reservations WHERE id = ?", input.reservationId)
      .toArray()[0];
    if (!reservation) {
      return { ok: false, status: 404, error: "upload reservation not found" };
    }
    this.ctx.storage.sql.exec(
      `INSERT INTO files
       (id, key, filename, size, content_type, uploaded_at, download_token_hash, max_views)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      input.fileId,
      reservation.key,
      reservation.filename,
      input.size,
      input.contentType,
      Date.now(),
      input.downloadTokenHash,
      input.maxViews,
    );
    this.ctx.storage.sql.exec("DELETE FROM reservations WHERE id = ?", input.reservationId);
    return {
      ok: true,
      file: this.publicFile(
        this.ctx.storage.sql.exec("SELECT * FROM files WHERE id = ?", input.fileId).toArray()[0],
      ),
    };
  }

  async failUpload(reservationId) {
    this.ctx.storage.sql.exec("DELETE FROM reservations WHERE id = ?", reservationId);
  }

  async claimDownload(fileId, token) {
    const file = this.ctx.storage.sql
      .exec("SELECT * FROM files WHERE id = ?", fileId)
      .toArray()[0];
    if (!file) {
      return { ok: false, status: 404, error: "file not found" };
    }
    if (!(await verifyToken(token, file.download_token_hash))) {
      return { ok: false, status: 404, error: "file not found" };
    }
    if (file.max_views > 0 && file.views >= file.max_views) {
      return { ok: false, status: 410, error: "download link already used" };
    }
    const views = file.views + 1;
    this.ctx.storage.sql.exec("UPDATE files SET views = ? WHERE id = ?", views, file.id);
    return {
      ok: true,
      key: file.key,
      filename: file.filename,
      contentType: file.content_type,
      views,
      maxViews: file.max_views,
    };
  }

  getDropRow() {
    return this.ctx.storage.sql.exec("SELECT * FROM drops LIMIT 1").toArray()[0] ?? null;
  }

  uploadSlotsUsed() {
    const files = this.ctx.storage.sql.exec("SELECT COUNT(*) AS count FROM files").one().count;
    const reservations = this.ctx.storage.sql
      .exec("SELECT COUNT(*) AS count FROM reservations")
      .one().count;
    return files + reservations;
  }

  clearExpiredReservations(now) {
    this.ctx.storage.sql.exec(
      "DELETE FROM reservations WHERE created_at < ?",
      now - 15 * 60 * 1000,
    );
  }

  listFiles() {
    return this.ctx.storage.sql
      .exec("SELECT * FROM files ORDER BY uploaded_at DESC")
      .toArray()
      .map((row) => this.publicFile(row));
  }

  publicDrop(drop) {
    return {
      id: drop.id,
      prefix: drop.prefix,
      label: drop.label,
      createdAt: drop.created_at,
      expiresAt: drop.expires_at,
      maxUploads: drop.max_uploads,
      maxBytes: drop.max_bytes,
      maxDownloadViews: drop.max_download_views,
    };
  }

  publicFile(file) {
    return {
      id: file.id,
      key: file.key,
      filename: file.filename,
      size: file.size,
      contentType: file.content_type,
      uploadedAt: file.uploaded_at,
      views: file.views,
      maxViews: file.max_views,
    };
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (request.method === "OPTIONS") {
        return new Response(null, { headers: corsHeaders() });
      }
      const route = routeRequest(request, url);
      if (route.kind === "home") {
        return text("rink serve is running");
      }
      if (route.kind === "create-drop") {
        return await createDrop(request, env, url);
      }
      if (route.kind === "drop-status") {
        return await dropStatus(request, env, route.dropId);
      }
      if (route.kind === "receive-page") {
        return await receivePage(env, url, route.dropId);
      }
      if (route.kind === "upload") {
        return await uploadFile(request, env, url, route.dropId, route.filename);
      }
      if (route.kind === "download") {
        return await downloadFile(env, route.dropId, route.fileId, route.token);
      }
      return json({ error: "not found" }, 404);
    } catch (error) {
      console.error(JSON.stringify({
        message: "request failed",
        path: url.pathname,
        error: error instanceof Error ? error.message : String(error),
      }));
      return json({ error: "internal server error" }, 500);
    }
  },
};

function routeRequest(request, url) {
  const parts = url.pathname.split("/").filter(Boolean);
  if (parts.length === 0) {
    return { kind: "home" };
  }
  if (
    request.method === "POST" &&
    parts[0] === "api" &&
    parts[1] === "drops" &&
    parts.length === 2
  ) {
    return { kind: "create-drop" };
  }
  if (request.method === "GET" && parts[0] === "api" && parts[1] === "drops" && parts[2]) {
    return { kind: "drop-status", dropId: parts[2] };
  }
  if (request.method === "GET" && parts[0] === "r" && parts[1] && parts.length === 2) {
    return { kind: "receive-page", dropId: parts[1] };
  }
  if (request.method === "PUT" && parts[0] === "r" && parts[1] && parts.length >= 3) {
    return {
      kind: "upload",
      dropId: parts[1],
      filename: decodeURIComponent(parts.slice(2).join("-")),
    };
  }
  if (request.method === "GET" && parts[0] === "d" && parts[1] && parts[2] && parts[3]) {
    return { kind: "download", dropId: parts[1], fileId: parts[2], token: parts[3] };
  }
  return { kind: "not-found" };
}

async function createDrop(request, env, url) {
  if (!(await isAdmin(request, env))) {
    return json({ error: "unauthorized" }, 401);
  }
  const body = await readJson(request);
  const id = randomToken(16);
  const ttlSeconds = numberSetting(body.ttlSeconds, env.DEFAULT_TTL_SECONDS, 86400);
  const drop = {
    id,
    prefix: cleanPrefix(body.prefix ?? env.OBJECT_PREFIX ?? "rink-inbox"),
    label: typeof body.label === "string" ? body.label.slice(0, 120) : null,
    createdAt: Date.now(),
    expiresAt: Date.now() + ttlSeconds * 1000,
    maxUploads: numberSetting(body.maxUploads, env.DEFAULT_MAX_UPLOADS, 1),
    maxBytes: numberSetting(body.maxBytes, env.DEFAULT_MAX_UPLOAD_BYTES, 512 * 1024 * 1024),
    maxDownloadViews: numberSetting(
      body.maxDownloadViews,
      env.DEFAULT_MAX_DOWNLOAD_VIEWS,
      1,
    ),
  };
  const stub = env.RINK_LINKS.getByName(id);
  const created = await stub.createDrop(drop);
  return json({
    ...created,
    uploadUrl: `${url.origin}/r/${id}`,
  });
}

async function dropStatus(request, env, dropId) {
  if (!(await isAdmin(request, env))) {
    return json({ error: "unauthorized" }, 401);
  }
  const drop = await env.RINK_LINKS.getByName(dropId).getDrop();
  return drop ? json(drop) : json({ error: "not found" }, 404);
}

async function receivePage(env, url, dropId) {
  const drop = await env.RINK_LINKS.getByName(dropId).getDrop();
  if (!drop) {
    return html(
      renderMessagePage("Receive link not found", "This upload link does not exist."),
      404,
    );
  }
  if (!drop.active) {
    return html(
      renderMessagePage("Receive link closed", "This upload link is expired or used."),
      410,
    );
  }
  return html(renderUploadPage(drop, `${url.origin}/r/${dropId}`));
}

async function uploadFile(request, env, url, dropId, filename) {
  if (!request.body) {
    return json({ error: "missing request body" }, 400);
  }
  const contentLength = request.headers.get("Content-Length");
  if (!contentLength || !/^\d+$/.test(contentLength)) {
    return json({ error: "Content-Length is required" }, 411);
  }
  const size = Number(contentLength);
  const contentType = request.headers.get("Content-Type") ?? "application/octet-stream";
  const stub = env.RINK_LINKS.getByName(dropId);
  const reservation = await stub.reserveUpload({ filename, size, contentType });
  if (!reservation.ok) {
    return json({ error: reservation.error }, reservation.status);
  }
  try {
    const object = await env.BUCKET.put(reservation.key, request.body, {
      httpMetadata: { contentType },
      customMetadata: { rinkDropId: dropId, rinkFileId: reservation.fileId },
    });
    await stub.completeUpload({
      reservationId: reservation.reservationId,
      fileId: reservation.fileId,
      size: object?.size ?? size,
      contentType,
      downloadTokenHash: reservation.downloadTokenHash,
      maxViews: reservation.maxViews,
    });
    return json({
      ok: true,
      key: reservation.key,
      filename: reservation.filename,
      size: object?.size ?? size,
      downloadUrl: `${url.origin}/d/${dropId}/${reservation.fileId}/${reservation.downloadToken}`,
    });
  } catch (error) {
    await stub.failUpload(reservation.reservationId);
    throw error;
  }
}

async function downloadFile(env, dropId, fileId, token) {
  const claimed = await env.RINK_LINKS.getByName(dropId).claimDownload(fileId, token);
  if (!claimed.ok) {
    return json({ error: claimed.error }, claimed.status);
  }
  const object = await env.BUCKET.get(claimed.key);
  if (!object?.body) {
    return json({ error: "object missing from bucket" }, 404);
  }
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("Content-Type", claimed.contentType ?? "application/octet-stream");
  headers.set("Content-Disposition", `attachment; filename="${quotedFilename(claimed.filename)}"`);
  headers.set("ETag", object.httpEtag);
  headers.set("X-Rink-View-Count", String(claimed.views));
  headers.set("X-Rink-Max-Views", String(claimed.maxViews));
  return new Response(object.body, { headers });
}

async function isAdmin(request, env) {
  const expected = env.RINK_SERVE_ADMIN_TOKEN;
  if (!expected) {
    return false;
  }
  const auth = request.headers.get("Authorization") ?? "";
  const provided = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  return verifySecret(provided, expected);
}

async function readJson(request) {
  const textBody = await request.text();
  if (!textBody) {
    return {};
  }
  const parsed = JSON.parse(textBody);
  return parsed && typeof parsed === "object" ? parsed : {};
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { ...JSON_HEADERS, ...corsHeaders() },
  });
}

function html(body, status = 200) {
  return new Response(body, { status, headers: HTML_HEADERS });
}

function text(body, status = 200) {
  return new Response(body, { status, headers: { "Content-Type": "text/plain" } });
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
    "Access-Control-Allow-Origin": "*",
  };
}

function numberSetting(value, fallback, defaultValue) {
  const raw = value ?? fallback ?? defaultValue;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : defaultValue;
}

function cleanPrefix(value) {
  return String(value)
    .split("/")
    .map(safeFilename)
    .filter(Boolean)
    .join("/") || "rink-inbox";
}

function safeFilename(value) {
  return String(value || "upload.bin")
    .replace(/[\\/\0]/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 160) || "upload.bin";
}

function quotedFilename(value) {
  return safeFilename(value).replace(/["\\]/g, "_");
}

function formatBytes(value) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value) || 0;
  for (const unit of units) {
    if (size < 1024 || unit === units[units.length - 1]) {
      return unit === "B" ? String(size) + unit : size.toFixed(1) + unit;
    }
    size /= 1024;
  }
  return String(value) + "B";
}

function randomToken(bytes) {
  const data = new Uint8Array(bytes);
  crypto.getRandomValues(data);
  return [...data].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function verifyToken(token, expectedHash) {
  return verifySecret(await sha256Hex(token), expectedHash);
}

async function verifySecret(provided, expected) {
  const encoder = new TextEncoder();
  const [providedHash, expectedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(provided)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  if (crypto.subtle.timingSafeEqual) {
    return crypto.subtle.timingSafeEqual(providedHash, expectedHash);
  }
  return constantTimeEqual(new Uint8Array(providedHash), new Uint8Array(expectedHash));
}

function constantTimeEqual(a, b) {
  if (a.length !== b.length) {
    return false;
  }
  let result = 0;
  for (let i = 0; i < a.length; i += 1) {
    result |= a[i] ^ b[i];
  }
  return result === 0;
}

function renderMessagePage(title, message) {
  return `<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<body><main><h1>${escapeHtml(title)}</h1><p>${escapeHtml(message)}</p></main></body>
</html>`;
}

function renderUploadPage(drop, endpoint) {
  const usedUploads = Array.isArray(drop.files) ? drop.files.length : 0;
  const remainingUploads = Math.max(0, drop.maxUploads - usedUploads);
  return `<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(drop.label ?? "Send a file")}</title>
<style>
  :root {
    --paper: #fbfbf8;
    --panel: #ffffff;
    --ink: #171717;
    --muted: #62645f;
    --line: rgba(23, 23, 23, .14);
    --line-strong: rgba(23, 23, 23, .28);
    --accent: #0f6fbd;
    --accent-soft: #e8f3ff;
    --success: #0c7a43;
    --error: #b42318;
  }
  * { box-sizing: border-box; }
  body {
    min-height: 100vh;
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font: 16px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  main { width: min(680px, calc(100% - 32px)); margin: 9vh auto; }
  h1 { margin: 0 0 8px; font-size: clamp(1.9rem, 4vw, 2.5rem); line-height: 1.05; }
  form, .result {
    display: grid;
    gap: 16px;
    margin-top: 28px;
    padding: 20px;
    border: 1px solid var(--line);
    background: var(--panel);
    box-shadow: 0 12px 32px rgba(0, 0, 0, .06);
  }
  input, button { font: inherit; }
  .meta, .hint, .status, .file-meta { color: var(--muted); }
  .meta { margin: 0; }
  .dropzone {
    display: grid;
    gap: 10px;
    min-height: 150px;
    padding: 22px;
    align-content: center;
    border: 1px dashed var(--line-strong);
    background: #fcfcfb;
    cursor: pointer;
    transition: border-color .15s ease, background .15s ease;
  }
  .dropzone:hover, .dropzone.dragging { border-color: var(--accent); background: var(--accent-soft); }
  .dropzone strong { font-size: 1.05rem; }
  input[type=file] { position: absolute; inline-size: 1px; block-size: 1px; opacity: 0; pointer-events: none; }
  button, .button-link {
    min-height: 44px;
    padding: 11px 15px;
    border: 1px solid #111;
    background: #111;
    color: white;
    cursor: pointer;
    text-align: center;
    text-decoration: none;
  }
  button.secondary, .button-link.secondary { border-color: var(--line-strong); background: white; color: var(--ink); }
  button:disabled { opacity: .58; cursor: wait; }
  .actions, .result-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .progress-row { display: grid; gap: 8px; }
  progress {
    inline-size: 100%;
    block-size: 12px;
    overflow: hidden;
    border: 0;
    background: #ecefeb;
  }
  progress::-webkit-progress-bar { background: #ecefeb; }
  progress::-webkit-progress-value { background: var(--accent); }
  progress::-moz-progress-bar { background: var(--accent); }
  .status { min-height: 1.4em; margin: 0; }
  .status.success { color: var(--success); }
  .status.error { color: var(--error); }
  .result-heading {
    display: flex;
    gap: 12px;
    align-items: flex-start;
  }
  .result-mark {
    display: inline-grid;
    place-items: center;
    inline-size: 28px;
    block-size: 28px;
    border-radius: 50%;
    background: rgba(12, 122, 67, .12);
    color: var(--success);
    font-weight: 700;
  }
  .result-title { display: grid; gap: 2px; }
  .result-title span, .result-status { color: var(--muted); }
  .download-link {
    display: flex;
    gap: 12px;
    align-items: center;
    justify-content: space-between;
    inline-size: 100%;
    min-height: 0;
    padding: 13px 14px;
    border: 1px solid var(--line);
    background: #f6f8fa;
    color: #0645ad;
    text-align: left;
  }
  .download-link:hover { border-color: var(--accent); background: var(--accent-soft); }
  .download-url {
    min-width: 0;
    overflow-wrap: anywhere;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: .9rem;
  }
  .copy-pill {
    flex: 0 0 auto;
    padding: 4px 8px;
    border: 1px solid var(--line);
    background: white;
    color: var(--ink);
    font-size: .78rem;
  }
  .result-status {
    min-height: 1.4em;
    margin: 0;
  }
  .result-status.success { color: var(--success); }
  .result-status.error { color: var(--error); }
  [hidden] { display: none !important; }
</style>
<main>
  <h1>${escapeHtml(drop.label ?? "Send a file")}</h1>
  <p class="meta">Uploads close ${new Date(drop.expiresAt).toLocaleString()} · ${remainingUploads} of ${drop.maxUploads} upload slot(s) left · ${formatBytes(drop.maxBytes)} max.</p>
  <form id="upload-form">
    <label id="dropzone" class="dropzone" for="file">
      <strong>Choose a file or drop it here</strong>
      <span class="hint">The upload starts when you press Upload. Keep this tab open until it finishes.</span>
      <span id="file-meta" class="file-meta">No file selected.</span>
    </label>
    <input id="file" type="file" required>
    <div class="progress-row" aria-live="polite">
      <progress id="progress" value="0" max="100"></progress>
      <p id="status" class="status">Waiting for a file.</p>
    </div>
    <div class="actions">
      <button id="submit" type="submit">Upload</button>
    </div>
  </form>
  <section id="result" class="result" hidden>
    <div class="result-heading">
      <span class="result-mark">✓</span>
      <div class="result-title">
        <strong>Upload complete</strong>
        <span>Click the link to copy it.</span>
      </div>
    </div>
    <button id="download-link" class="download-link" type="button" aria-label="Copy download link">
      <span id="download-url" class="download-url"></span>
      <span class="copy-pill">Copy</span>
    </button>
    <p id="copy-status" class="result-status" aria-live="polite"></p>
    <div class="result-actions">
      <button id="copy-link" class="secondary" type="button">Copy link</button>
      <a id="open-link" class="button-link secondary" href="" rel="noopener">Open link</a>
    </div>
  </section>
</main>
<script>
const form = document.getElementById("upload-form");
const fileInput = document.getElementById("file");
const button = document.getElementById("submit");
const dropzone = document.getElementById("dropzone");
const fileMeta = document.getElementById("file-meta");
const progress = document.getElementById("progress");
const status = document.getElementById("status");
const resultPanel = document.getElementById("result");
const downloadLink = document.getElementById("download-link");
const downloadUrl = document.getElementById("download-url");
const openLink = document.getElementById("open-link");
const copyButton = document.getElementById("copy-link");
const copyStatus = document.getElementById("copy-status");
const endpoint = "${endpoint}";
const maxBytes = ${drop.maxBytes};
let selectedFile = null;
let currentDownloadUrl = "";

fileInput.addEventListener("change", () => {
  selectedFile = fileInput.files[0] || null;
  renderSelectedFile();
});

dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("dragging");
});

dropzone.addEventListener("dragleave", () => {
  dropzone.classList.remove("dragging");
});

dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
  selectedFile = event.dataTransfer.files[0] || null;
  renderSelectedFile();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = selectedFile || fileInput.files[0];
  if (!file) return;
  if (file.size > maxBytes) {
    setStatus("This file is larger than this receive link allows.", "error");
    return;
  }
  button.disabled = true;
  resultPanel.hidden = true;
  progress.value = 0;
  setStatus("Uploading 0%...", "");
  try {
    const result = await uploadFile(file);
    await showDownloadLink(result.downloadUrl);
  } catch (error) {
    setStatus(error.message || String(error), "error");
  } finally {
    button.disabled = false;
  }
});

copyButton.addEventListener("click", async () => {
  const copied = await copyDownloadLink();
  setCopyStatus(copied ? "Copied to clipboard." : "Copy blocked by this browser. Long-press or select the URL text.", copied ? "success" : "error");
});

downloadLink.addEventListener("click", async () => {
  const copied = await copyDownloadLink();
  setCopyStatus(copied ? "Copied to clipboard." : "Copy blocked by this browser. Long-press or select the URL text.", copied ? "success" : "error");
});

function uploadFile(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", endpoint + "/" + encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) {
        setStatus("Uploading " + formatBytes(event.loaded) + "...", "");
        return;
      }
      const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
      progress.value = percent;
      setStatus("Uploading " + percent + "% · " + formatBytes(event.loaded) + " of " + formatBytes(event.total), "");
    };
    xhr.onload = () => {
      const data = parseJson(xhr.responseText);
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error((data && data.error) || "upload failed"));
        return;
      }
      resolve(data);
    };
    xhr.onerror = () => reject(new Error("network error while uploading"));
    xhr.onabort = () => reject(new Error("upload cancelled"));
    xhr.send(file);
  });
}

async function showDownloadLink(url) {
  progress.value = 100;
  currentDownloadUrl = url;
  downloadUrl.textContent = url;
  openLink.href = url;
  resultPanel.hidden = false;
  setStatus("Uploaded.", "success");
  setCopyStatus("Click the link to copy it.", "");
}

async function copyDownloadLink() {
  const value = currentDownloadUrl;
  if (!value) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
    return fallbackCopy(value);
  } catch {
    return fallbackCopy(value);
  }
}

function fallbackCopy(value) {
  const field = document.createElement("textarea");
  field.value = value;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.inset = "0 auto auto 0";
  field.style.inlineSize = "1px";
  field.style.blockSize = "1px";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.focus();
  field.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  field.remove();
  return copied;
}

function renderSelectedFile() {
  if (!selectedFile) {
    fileMeta.textContent = "No file selected.";
    return;
  }
  fileMeta.textContent = selectedFile.name + " · " + formatBytes(selectedFile.size);
  if (selectedFile.size > maxBytes) {
    setStatus("This file is larger than this receive link allows.", "error");
  } else {
    setStatus("Ready to upload.", "");
  }
}

function setStatus(message, state) {
  status.textContent = message;
  status.className = "status" + (state ? " " + state : "");
}

function setCopyStatus(message, state) {
  copyStatus.textContent = message;
  copyStatus.className = "result-status" + (state ? " " + state : "");
}

function parseJson(value) {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function formatBytes(value) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value) || 0;
  for (const unit of units) {
    if (size < 1024 || unit === units[units.length - 1]) {
      return unit === "B" ? String(size) + unit : size.toFixed(1) + unit;
    }
    size /= 1024;
  }
  return String(value) + "B";
}
</script>
</html>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}
