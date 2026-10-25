// Opt-in browser acceptance test against an explicitly supplied Plane instance.
// Requires Playwright or PLAYWRIGHT_MODULE=/absolute/path/to/playwright-core/index.mjs.
// Uses a temporary headless context; never attaches to an existing browser profile.
// node tests/integration/page-browser-smoke.mjs /path/to/mode-600-fixture.json /path/to/evidence
import assert from "node:assert/strict";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const fixturePath = process.argv[2];
assert.ok(fixturePath, "Pass a fixture JSON with base_url, workspace_slug, project_id, api_key and session_cookie");
assert.equal((await stat(fixturePath)).mode & 0o077, 0, "Fixture permissions must exclude group and other users");
const fixture = JSON.parse(await readFile(fixturePath, "utf8"));
for (const key of ["base_url", "workspace_slug", "project_id", "api_key", "session_cookie"]) {
  assert.equal(typeof fixture[key], "string", `Missing fixture field: ${key}`);
}
const origin = new URL(fixture.base_url).origin;
const output = resolve(process.argv[3] || ".test-state/browser-page-smoke");
await mkdir(output, { recursive: true, mode: 0o700 });
const playwright = await import(
  process.env.PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : "playwright"
);
const browser = await playwright.chromium.launch({
  headless: true,
  ...(process.env.PLANE_BROWSER_EXECUTABLE
    ? { executablePath: process.env.PLANE_BROWSER_EXECUTABLE }
    : { channel: process.env.PLANE_BROWSER_CHANNEL || "chrome" }),
});
const contextOptions = {
  viewport: { width: 1440, height: 1080 },
  locale: "en-US",
  ignoreHTTPSErrors: fixture.ignore_https_errors === true,
};
const cookies = fixture.session_cookie.split(";").map((entry) => {
  const at = entry.indexOf("=");
  assert.ok(at > 0, "session_cookie must contain cookie name=value pairs only");
  return {
    name: entry.slice(0, at).trim(),
    value: entry.slice(at + 1).trim(),
    url: origin,
    httpOnly: true,
    secure: origin.startsWith("https:"),
    sameSite: "Lax",
  };
});
const api = await playwright.request.newContext({
  baseURL: origin,
  ignoreHTTPSErrors: contextOptions.ignoreHTTPSErrors,
  extraHTTPHeaders: { "X-Api-Key": fixture.api_key },
});
const browserPagesPath = `/${fixture.workspace_slug}/projects/${fixture.project_id}/pages/`;
const sessionPagesPath = `/api/workspaces/${fixture.workspace_slug}/projects/${fixture.project_id}/pages/`;
const apiPagesPath = `/api/v1/workspaces/${fixture.workspace_slug}/projects/${fixture.project_id}/pages/`;
const marker = `browser-smoke-${Date.now()}`;
const initialTitle = `Temporary UI test ${marker}`;
const initialText = `Browser editor saved this text — проверка ${marker}`;
const apiTitle = `API rename ${marker}`;
const apiText = `API change visible in the editor ${marker}`;
const report = { success: false, checks: [], browser: browser.version(), page_id: null, cleanup: "not_needed" };
let context;
let page;
let pageId;
let failure;

async function request(method, path, data, etag) {
  const response = await api.fetch(path, {
    method,
    ...(data === undefined ? {} : { data }),
    ...(etag ? { headers: { "If-Match": etag } } : {}),
  });
  const text = await response.text();
  return { status: response.status(), etag: response.headers().etag, data: text ? JSON.parse(text) : null };
}

async function until(check, label, timeout = 45000) {
  const deadline = Date.now() + timeout;
  // Autosave is debounced; each observation must follow the preceding one.
  /* eslint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((done) => setTimeout(done, 500));
  }
  /* eslint-enable no-await-in-loop */
  throw new Error(`Timed out: ${label}`);
}

async function openContext() {
  context = await browser.newContext(contextOptions);
  await context.addCookies(cookies);
  page = await context.newPage();
  page.setDefaultTimeout(30000);
}

function editors() {
  return {
    title: page.locator(`[id="${pageId}-title"] .ProseMirror[contenteditable="true"]`),
    body: page.locator(`[id="${pageId}"] .ProseMirror[contenteditable="true"]`),
  };
}

async function typeInto(locator, text) {
  await locator.waitFor({ state: "visible" });
  await locator.click();
  await locator.press("ControlOrMeta+A");
  await page.keyboard.insertText(text);
}

async function cleanOwnPage() {
  if (!pageId) return;
  const path = `${apiPagesPath}${pageId}/`;
  // A final editor disconnect can finish its save before cleanup acquires the row.
  /* eslint-disable no-await-in-loop */
  for (const [method, suffix, expected] of [
    ["POST", "archive/", 200],
    ["DELETE", "", 204],
  ]) {
    let result;
    for (let attempt = 0; attempt < 5; attempt++) {
      const current = await request("GET", path);
      assert.equal(current.status, 200, "Unable to read the disposable page for cleanup");
      result = await request(method, `${path}${suffix}`, method === "POST" ? {} : undefined, current.etag);
      if (result.status !== 412) break;
      await new Promise((done) => setTimeout(done, 500));
    }
    assert.equal(result?.status, expected, `Disposable page cleanup failed: ${method} ${suffix}`);
  }
  /* eslint-enable no-await-in-loop */
  report.cleanup = "archived_and_deleted";
}

try {
  await openContext();
  await page.goto(`${origin}${browserPagesPath}`, { waitUntil: "domcontentloaded" });
  const [created] = await Promise.all([
    page.waitForResponse(
      (response) => new URL(response.url()).pathname === sessionPagesPath && response.request().method() === "POST"
    ),
    page.getByRole("button", { name: "Add page", exact: true }).click(),
  ]);
  assert.equal(created.status(), 201, `UI page creation failed: ${created.status()}`);
  pageId = (await created.json()).id;
  assert.match(pageId, /^[0-9a-f-]{36}$/i);
  report.page_id = pageId;
  report.cleanup = "pending";
  await page.waitForURL((url) => url.pathname.replace(/\/$/, "") === `${browserPagesPath}${pageId}`);
  report.checks.push("created a page with the real Add page UI");

  await typeInto(editors().title, initialTitle);
  await typeInto(editors().body, initialText);
  const contentPath = `${apiPagesPath}${pageId}/description/`;
  await until(async () => {
    const saved = await request("GET", contentPath);
    assert.equal(saved.status, 200);
    return (
      saved.data.name === initialTitle &&
      saved.data.description_html.includes(initialText) &&
      JSON.stringify(saved.data.description_json).includes(initialText) &&
      saved.data.description_binary.length > 0
    );
  }, "browser edits saved in all API content formats");
  report.checks.push("typed title and body in the real editor; API returned saved HTML, JSON and binary");
  await page.screenshot({ path: `${output}/browser-edit.png`, fullPage: false });

  let current = await request("GET", contentPath);
  const content = await request("PATCH", contentPath, { description_html: `<p>${apiText}</p>` }, current.etag);
  assert.equal(content.status, 200, `API content update failed: ${content.status}`);
  await until(
    async () => (await editors().body.innerText()).includes(apiText),
    "API content reaches open browser editor"
  );
  current = await request("GET", contentPath);
  const rename = await request("PATCH", `${apiPagesPath}${pageId}/`, { name: apiTitle }, current.etag);
  assert.equal(rename.status, 200, `API rename failed: ${rename.status}`);
  await until(
    async () => (await editors().title.innerText()).trim() === apiTitle,
    "API rename reaches open browser editor"
  );
  report.checks.push("API content and rename propagated into the already-open browser editor");

  await context.close();
  context = undefined;
  await openContext();
  await page.goto(`${origin}${browserPagesPath}${pageId}`, { waitUntil: "domcontentloaded" });
  await editors().body.waitFor({ state: "visible" });
  await until(
    async () =>
      (await editors().body.innerText()).includes(apiText) && (await editors().title.innerText()).trim() === apiTitle,
    "fresh browser context restores saved content and title"
  );
  await page.screenshot({ path: `${output}/fresh-context.png`, fullPage: false });
  report.checks.push("a fresh browser context restored the saved content and title without IndexedDB state");
  report.success = true;
} catch (error) {
  failure = error;
  report.error = error.message;
  await page?.screenshot({ path: `${output}/failure.png`, fullPage: false }).catch(() => undefined);
} finally {
  await context?.close();
  try {
    await cleanOwnPage();
  } catch (error) {
    report.success = false;
    report.cleanup = "failed";
    report.cleanup_error = error.message;
    failure ||= error;
  }
  await api.dispose();
  await browser.close();
  await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2), { mode: 0o600 });
}
console.log(JSON.stringify(report, null, 2));
if (failure) process.exitCode = 1;
