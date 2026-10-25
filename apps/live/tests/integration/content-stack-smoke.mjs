// Opt-in integration smoke test against an isolated API/live/PostgreSQL stack.
// node tests/integration/content-stack-smoke.mjs /path/to/management-fixture.json
// Add --restart-checkpoint to pause for a real live restart, then press Enter.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";

const liveRequire = createRequire(new URL("../../package.json", import.meta.url));
const editorRequire = createRequire(new URL("../../../../packages/editor/package.json", import.meta.url));
const { HocuspocusProvider } = editorRequire("@hocuspocus/provider");
const Y = liveRequire("yjs");
const WebSocket = liveRequire("ws");
const fixture = JSON.parse(await readFile(process.argv[2], "utf8"));
const apiURL = new URL(fixture.base_url);
assert.ok(["localhost", "127.0.0.1"].includes(apiURL.hostname), "Use an isolated local fixture only");
const liveURL = new URL(process.env.PLANE_LIVE_SMOKE_URL || "ws://127.0.0.1:3101/live/collaboration/");
assert.ok(["localhost", "127.0.0.1"].includes(liveURL.hostname), "Use an isolated local live server only");
liveURL.searchParams.set("workspaceSlug", fixture.workspace_slug);
liveURL.searchParams.set("projectId", fixture.project_id);
liveURL.searchParams.set("documentType", "project_page");
const pages = `/api/v1/workspaces/${fixture.workspace_slug}/projects/${fixture.project_id}/pages/`;

async function request(method, path, body, etag) {
  const response = await fetch(new URL(path, fixture.base_url), {
    method,
    headers: {
      "X-Api-Key": fixture.api_key,
      "Content-Type": "application/json",
      ...(etag ? { "If-Match": etag } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const text = await response.text();
  return { status: response.status, etag: response.headers.get("etag"), data: text ? JSON.parse(text) : null };
}

async function until(check, label, timeout = 12000) {
  const end = Date.now() + timeout;
  // Poll sequentially: later checks depend on observing the preceding attempt.
  /* eslint-disable no-await-in-loop */
  while (Date.now() < end) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  /* eslint-enable no-await-in-loop */
  throw new Error(`Timed out: ${label}`);
}

let pageId;
let provider;
let document = new Y.Doc();
const checks = [];
function connect() {
  return new HocuspocusProvider({
    url: liveURL.toString(),
    name: pageId,
    document,
    token: JSON.stringify({ id: fixture.user_id, cookie: fixture.session_cookie }),
    WebSocketPolyfill: WebSocket,
  });
}

try {
  const created = await request("POST", pages, {
    name: "Disposable live content smoke",
    description_html: "<p>Original content</p>",
  });
  assert.equal(created.status, 201, JSON.stringify(created.data));
  pageId = created.data.id;
  const path = `${pages}${pageId}/`;
  provider = connect();
  await until(
    () => provider.isSynced && document.getXmlFragment("default").toJSON().includes("Original content"),
    "initial WebSocket sync"
  );
  checks.push("authenticated WebSocket loaded the created page");

  let current = await request("GET", `${path}description/`);
  const updated = await request(
    "PATCH",
    `${path}description/`,
    { description_html: "<h2>Опубликовано</h2><p>Published by API</p>" },
    current.etag
  );
  assert.equal(updated.status, 200, JSON.stringify(updated.data));
  await until(
    () => document.getXmlFragment("default").toJSON().includes("Published by API"),
    "API change reaches WebSocket"
  );
  current = await request("GET", `${path}description/`);
  const persisted = new Y.Doc();
  Y.applyUpdate(persisted, Buffer.from(current.data.description_binary, "base64"));
  assert.equal(persisted.getXmlFragment("default").toJSON(), document.getXmlFragment("default").toJSON());
  persisted.destroy();
  checks.push("API content persisted and reached the open editor");

  const renamed = await request("PATCH", path, { name: "API rename visible in editor" }, current.etag);
  assert.equal(renamed.status, 200, JSON.stringify(renamed.data));
  await until(
    () => document.getXmlFragment("title").toJSON().includes("API rename visible in editor"),
    "API rename reaches WebSocket"
  );
  checks.push("API rename updated the open editor title");

  if (process.argv.includes("--restart-checkpoint")) {
    provider.destroy();
    provider.configuration.websocketProvider.destroy();
    provider = undefined;
    document.destroy();
    document = new Y.Doc();
    console.log("RESTART_READY: restart the local live process, then press Enter to continue.");
    await new Promise((resolve) => process.stdin.once("data", resolve));
    process.stdin.pause();
    current = await request("GET", `${path}description/`);
    assert.equal(current.data.name, "API rename visible in editor");
    assert.ok(current.data.description_html.includes("Published by API"));
    provider = connect();
    await until(
      () => provider.isSynced && document.getXmlFragment("default").toJSON().includes("Published by API"),
      "fresh WebSocket loads persisted content after restart"
    );
    assert.ok(document.getXmlFragment("title").toJSON().includes("API rename visible in editor"));
    checks.push("a fresh Yjs client restored persisted content and title after the live process restarted");
    const afterRestart = await request(
      "PATCH",
      `${path}description/`,
      { description_html: "<p>Updated after live restart</p>" },
      current.etag
    );
    assert.equal(afterRestart.status, 200, JSON.stringify(afterRestart.data));
    await until(
      () => document.getXmlFragment("default").toJSON().includes("Updated after live restart"),
      "CAS update reaches reopened editor"
    );
    current = await request("GET", `${path}description/`);
    assert.ok(current.data.description_html.includes("Updated after live restart"));
    assert.equal(current.data.name, "API rename visible in editor");
    checks.push("the next CAS update persisted and reached the reopened editor after restart");
  }

  current = await request("GET", `${path}description/`);
  const paragraph = new Y.XmlElement("paragraph");
  const text = new Y.XmlText();
  text.insert(0, "Unsaved editor change");
  paragraph.insert(0, [text]);
  document.getXmlFragment("default").insert(0, [paragraph]);
  await until(() => !provider.hasUnsyncedChanges, "editor update acknowledged by live server");
  const conflict = await request(
    "PATCH",
    `${path}description/`,
    { description_html: "<p>Must not overwrite</p>" },
    current.etag
  );
  assert.equal(conflict.status, 409, JSON.stringify(conflict.data));
  assert.ok(document.getXmlFragment("default").toJSON().includes("Unsaved editor change"));
  assert.ok(!document.getXmlFragment("default").toJSON().includes("Must not overwrite"));
  checks.push("unsaved editor change rejects conflicting API update with 409");

  provider.destroy();
  provider.configuration.websocketProvider.destroy();
  provider = undefined;
  await until(async () => {
    const saved = await request("GET", `${path}description/`);
    return saved.data.description_html.includes("Unsaved editor change");
  }, "editor change persists after disconnect");
  checks.push("editor change persisted after disconnect without stale replay");
} finally {
  if (provider) {
    provider.destroy();
    provider.configuration.websocketProvider.destroy();
  }
  document.destroy();
  if (pageId) {
    const path = `${pages}${pageId}/`;
    let current = await request("GET", path);
    const archived = await request("POST", `${path}archive/`, {}, current.etag);
    assert.equal(archived.status, 200, JSON.stringify(archived.data));
    current = await request("GET", path);
    const deleted = await request("DELETE", path, undefined, current.etag);
    assert.equal(deleted.status, 204, "Disposable page cleanup failed");
  }
}
console.log(JSON.stringify({ success: true, checks }, null, 2));
