// Real Infrastructure components/services in Chrome, with synthetic intercepted HTTP.
// No web server, credentials, working services or external network are required.
// PLAYWRIGHT_MODULE=/installed/playwright/index.mjs node tools/check-infrastructure-ui-local.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readdir, mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const web = resolve(root, "apps/web");
const require = createRequire(resolve(web, "package.json"));
const packages = await readdir(resolve(root, "node_modules/.pnpm"));
const esbuildPath = packages.find((entry) => entry.startsWith("esbuild@"));
assert.ok(esbuildPath, "Use the existing installed esbuild dependency");
const { build } = await import(
  pathToFileURL(resolve(root, "node_modules/.pnpm", esbuildPath, "node_modules/esbuild/lib/main.js"))
);
const { chromium } = await import(
  process.env.PLAYWRIGHT_MODULE
    ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href
    : require.resolve("playwright")
);
const tailwindPath = require.resolve("@tailwindcss/postcss");
const twRequire = createRequire(tailwindPath);
const postcss = twRequire("postcss");
const tailwind = require("@tailwindcss/postcss");
const css = (
  await postcss([tailwind({ base: web })]).process(
    `@import "${resolve(root, "packages/tailwind-config/index.css")}"; @source "${web}/core/components/infrastructure";`,
    { from: resolve(web, "infrastructure-fixture.css") }
  )
).css;
const owner = "11111111-1111-4111-8111-111111111111";
const consumer = "22222222-2222-4222-8222-222222222222";
const origin = "http://infrastructure.invalid";
const projectPath = `/example/projects/${owner}/infrastructure`;
const compiled = await build({
  stdin: {
    contents: `
    import React from 'react';
    import { createRoot } from 'react-dom/client';
    import { BrowserRouter, Routes, Route } from 'react-router';
    import { SWRConfig } from 'swr';
    import { InfrastructurePage, InfrastructureDetailPage } from './core/components/infrastructure/infrastructure-page';
    createRoot(document.getElementById('root')).render(<SWRConfig value={{dedupingInterval:0,revalidateOnFocus:false}}>
      <BrowserRouter><Routes>
      <Route path='/:workspaceSlug/projects/:projectId/infrastructure' element={<InfrastructurePage/>}/>
      <Route path='/:workspaceSlug/projects/:projectId/infrastructure/:resourceId' element={<InfrastructureDetailPage/>}/>
      </Routes></BrowserRouter></SWRConfig>);`,
    resolveDir: web,
    loader: "tsx",
  },
  bundle: true,
  write: false,
  format: "iife",
  platform: "browser",
  jsx: "automatic",
  define: { "process.env": JSON.stringify({ NODE_ENV: "development", VITE_API_BASE_URL: origin }) },
  alias: { "@": resolve(web, "core") },
  plugins: [
    {
      name: "synthetic-shell-only",
      setup(builder) {
        builder.onResolve({ filter: /^@\/hooks\/store\/(user|use-project)$/ }, (args) => ({
          path: args.path,
          namespace: "shell",
        }));
        builder.onResolve({ filter: /^@\/components\/core\/page-title$/ }, (args) => ({
          path: args.path,
          namespace: "shell",
        }));
        builder.onLoad({ filter: /.*/, namespace: "shell" }, () => ({
          contents: `
      export const PageHead = () => null;
      export const useUserPermissions = () => ({getProjectRoleByWorkspaceSlugAndProjectId:()=>window.fixtureRole ?? 20});
      export const useProject = () => ({workspaceProjectIds:['${owner}','${consumer}'],
        getProjectById:(id)=>({id,name:id==='${owner}'?'Owner project':'Consumer project',workspace_slug:'example'})});`,
          loader: "js",
        }));
      },
    },
  ],
});
const javascript = compiled.outputFiles[0].text;
const html =
  '<!doctype html><html data-theme="light"><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script src="/fixture.js"></script></body></html>';
const output = resolve(process.env.INFRASTRUCTURE_UI_OUTPUT || "/tmp/plane-infrastructure-ui-report");
await mkdir(output, { recursive: true });
const report = {
  scope: "native Infrastructure components; synthetic shell and intercepted API",
  checks: [],
  errors: [],
  blocked_network: [],
};
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
let counter = 10;
const id = () => `00000000-0000-4000-8000-${String(counter++).padStart(12, "0")}`;
const source = { kind: "manual", reference: "doc:synthetic-monitoring", revision: "fixture-v1" };
const resource = (key, name, kind) => ({
  id: id(),
  key,
  name,
  kind,
  lifecycle: "active",
  description: "Synthetic local inventory",
  tags: ["monitoring"],
  owner: { name: "Operations", reference: "team:operations", recovery_contact: "role:supervisor" },
  source,
  evidence: [],
  last_verified: null,
  access_methods: [],
  revision: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  ownership_project_id: owner,
  can_manage: true,
  can_detach: false,
});
const grafana = resource("monitoring/grafana", "Grafana", "service");
grafana.access_methods = [
  {
    id: "grafana-api",
    kind: "https",
    endpoint: "https://grafana.example.invalid",
    capability: "grafana",
    operations: ["dashboards.read"],
    scope: "monitoring organization",
    identity: "runtime-own-identity",
    notes: "",
    credential_refs: [
      {
        provider: "openbao",
        path: "projects/example/monitoring/grafana",
        field: "api_token",
        purpose: "Read dashboards",
      },
    ],
  },
];
const vm = resource("monitoring/vm", "Monitoring VM", "vm");
const resources = new Map([grafana, vm].map((row) => [row.id, row]));
const relations = [];
const links = new Map(
  [...resources].map(([key]) => [
    key,
    [
      {
        id: id(),
        resource_id: key,
        project_id: owner,
        project_name: "Owner project",
        kind: "serves_project",
        is_owner: true,
        revision: 1,
      },
    ],
  ])
);
let failReads = false,
  conflictNext = false,
  unknownNext = false;
const requests = [];
const envelope = (rows) => ({ results: rows, next_cursor: null, has_more: false, per_page: 50 });
await context.route("**/*", async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  if (url.origin !== origin) {
    report.blocked_network.push(request.url());
    return route.abort();
  }
  const send = (data, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: status === 204 ? "" : JSON.stringify(data) });
  if (url.pathname === "/fixture.js") return route.fulfill({ contentType: "application/javascript", body: javascript });
  if (url.pathname === "/fixture.css") return route.fulfill({ contentType: "text/css", body: css });
  if (!url.pathname.startsWith("/api/")) return route.fulfill({ contentType: "text/html", body: html });
  const match = url.pathname.match(/\/infrastructure\/(.*)/);
  assert.ok(match, `Unexpected API: ${url.pathname}`);
  const path = match[1],
    method = request.method();
  requests.push({ path, method, revision: request.headers()["if-match"] });
  if (method === "GET" && failReads) return send({ error: { code: "forbidden" } }, 403);
  if (method !== "GET" && unknownNext) {
    unknownNext = false;
    return route.abort("connectionfailed");
  }
  if (method !== "GET" && conflictNext) {
    conflictNext = false;
    return send({ error: { code: "revision_conflict" } }, 412);
  }
  const parts = path.split("/").filter(Boolean);
  const payload = method === "POST" || method === "PATCH" ? request.postDataJSON() : null;
  if (parts[0] === "resources" && parts.length === 1) {
    if (method === "GET")
      return send(
        envelope(
          [...resources.values()].filter(
            (row) =>
              (!url.searchParams.get("key") || row.key === url.searchParams.get("key")) &&
              (!url.searchParams.get("kind") || row.kind === url.searchParams.get("kind")) &&
              (!url.searchParams.get("search") ||
                `${row.name} ${row.key}`.toLowerCase().includes(url.searchParams.get("search").toLowerCase()))
          )
        )
      );
    assert.equal(request.headers()["if-match"], '"0"');
    const row = { ...resource(payload.key, payload.name, payload.kind), ...payload };
    resources.set(row.id, row);
    links.set(row.id, [
      {
        id: id(),
        resource_id: row.id,
        project_id: owner,
        project_name: "Owner project",
        kind: "serves_project",
        is_owner: true,
        revision: 1,
      },
    ]);
    return send(row, 201);
  }
  if (parts[0] === "relations") {
    if (method === "GET")
      return send(
        envelope(
          relations.filter(
            (row) =>
              !url.searchParams.get("resource_id") ||
              [row.source_id, row.target_id].includes(url.searchParams.get("resource_id"))
          )
        )
      );
    if (method === "DELETE") {
      const index = relations.findIndex((row) => row.id === parts[1]);
      assert.ok(index >= 0);
      relations.splice(index, 1);
      return send(null, 204);
    }
    assert.equal(request.headers()["if-match"], '"0"');
    const from = resources.get(payload.source_id),
      to = resources.get(payload.target_id);
    const row = {
      ...payload,
      id: id(),
      project_id: owner,
      source_name: from.name,
      source_key: from.key,
      target_name: to.name,
      target_key: to.key,
      revision: 1,
      can_manage: true,
      created_at: "2026-01-01T00:00:00Z",
    };
    relations.push(row);
    return send(row, 201);
  }
  const row = resources.get(parts[1]);
  if (!row) return send({ error: { code: "not_found" } }, 404);
  if (parts[2] === "project-links") {
    if (method === "GET")
      return send(envelope(links.get(row.id).map((link) => Object.assign({}, link, { revision: row.revision }))));
    assert.equal(request.headers()["if-match"], `"${row.revision}"`);
    row.revision++;
    if (method === "POST") {
      links.get(row.id).push({
        id: id(),
        resource_id: row.id,
        project_id: payload.project_id,
        project_name: "Consumer project",
        kind: "serves_project",
        is_owner: false,
      });
      return send(row);
    }
    links.set(
      row.id,
      links.get(row.id).filter((link) => link.project_id !== parts[3])
    );
    return send(null, 204);
  }
  if (method === "GET") return send(row);
  assert.equal(request.headers()["if-match"], `"${row.revision}"`);
  if (method === "PATCH") {
    Object.assign(row, payload, { revision: row.revision + 1 });
    return send(row);
  }
  resources.delete(row.id);
  return send(null, 204);
});
const page = await context.newPage();
page.on("pageerror", (error) => report.errors.push(error.message));
page.on("console", (message) => {
  if (/Encountered two children|unique .key. prop/.test(message.text())) report.errors.push(message.text());
});
page.setDefaultTimeout(10000);
const check = (name) => report.checks.push(name);
async function closeEditor() {
  await page.getByRole("button", { name: "Close editor", exact: true }).click();
}
async function saved() {
  await page.getByRole("dialog").waitFor({ state: "hidden" });
}
async function fillSource() {
  await page.getByLabel("Source reference", { exact: true }).fill(source.reference);
  await page.getByLabel("Source revision").fill(source.revision);
}
try {
  await page.goto(origin + projectPath);
  await page.getByRole("heading", { name: "Infrastructure", exact: true }).waitFor();
  await page.getByRole("link", { name: "Grafana", exact: true }).waitFor();
  await page.getByLabel("Search resources").fill("does-not-exist");
  await page.getByRole("heading", { name: "No resources found" }).waitFor();
  await page.getByLabel("Search resources").fill("");
  check("list-filters-empty-state");
  await page.getByRole("button", { name: "Add resource", exact: true }).click();
  await page.getByLabel("Stable resource key").fill("monitoring/prometheus");
  await page.getByLabel("Name", { exact: true }).fill("Prometheus");
  await page.getByLabel("Owner name", { exact: true }).fill("Operations");
  await page.getByLabel("Owner reference", { exact: true }).fill("team:operations");
  await page.getByLabel("Recovery contact", { exact: true }).fill("role:supervisor");
  await fillSource();
  await page.getByRole("button", { name: "Create resource", exact: true }).click();
  await page.getByRole("heading", { name: "Prometheus", exact: true }).waitFor();
  check("create-and-detail-readback");
  await page.getByRole("button", { name: "Edit resource", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("Prometheus draft");
  conflictNext = true;
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "record changed" }).waitFor();
  assert.equal(await page.getByLabel("Name", { exact: true }).inputValue(), "Prometheus draft");
  await closeEditor();
  check("stale-write-retains-draft");
  await page.getByRole("button", { name: "Edit resource", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("Prometheus metrics");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await saved();
  await page.getByRole("heading", { name: "Prometheus metrics", exact: true }).waitFor();
  check("edit-readback");
  await page.getByRole("button", { name: "Add relationship", exact: true }).click();
  await page.getByLabel("Target resource key", { exact: true }).fill(vm.key);
  await page.getByLabel("Relationship kind", { exact: true }).selectOption("runs_on");
  await fillSource();
  await page.getByRole("button", { name: "Create relationship", exact: true }).click();
  await saved();
  await page.getByRole("link", { name: /Monitoring VM/ }).waitFor();
  assert.equal(relations.length, 1);
  check("relationship-create-names-and-bound-key-resolution");
  await page.getByRole("button", { name: "Share with a project", exact: true }).click();
  const selector = page.getByLabel("Administered project");
  if (await selector.count()) await selector.selectOption(consumer);
  else await page.getByLabel("Target project ID", { exact: true }).fill(consumer);
  await page.getByRole("button", { name: "Share resource", exact: true }).click();
  await saved();
  await page.getByRole("link", { name: /Consumer project/ }).waitFor();
  check("project-sharing-stable-resource");
  await page.getByRole("button", { name: `Remove project link ${consumer}`, exact: true }).click();
  await page.getByRole("button", { name: "Confirm removal", exact: true }).click();
  await saved();
  check("project-link-detach");
  await page.getByRole("button", { name: /Remove relationship/ }).click();
  await page.getByRole("button", { name: "Confirm removal", exact: true }).click();
  await saved();
  assert.equal(relations.length, 0);
  check("relationship-delete");
  await page.getByRole("button", { name: "Edit resource", exact: true }).click();
  unknownNext = true;
  const before = requests.filter((row) => row.method === "PATCH").length;
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "outcome is unknown" }).waitFor();
  assert.equal(requests.filter((row) => row.method === "PATCH").length, before + 1);
  await closeEditor();
  check("unknown-outcome-visible-no-automatic-retry");
  await page.getByRole("button", { name: "Delete resource", exact: true }).click();
  await page.getByRole("button", { name: "Confirm removal", exact: true }).click();
  await page.getByRole("heading", { name: "Infrastructure", exact: true }).waitFor();
  check("resource-delete");
  failReads = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await page.getByRole("heading", { name: "Infrastructure is unavailable", exact: true }).waitFor();
  assert.equal(await page.getByRole("heading", { name: "No resources found" }).count(), 0);
  failReads = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await page.getByRole("link", { name: "Grafana", exact: true }).waitFor();
  check("permission-error-not-empty-retry");
  async function verifyViewport(width) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(origin + projectPath);
    await page.getByRole("link", { name: "Grafana", exact: true }).waitFor();
    assert.ok(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      `List overflow at ${width}`
    );
    await page.screenshot({ path: resolve(output, `list-${width}.png`), fullPage: true });
    await page.getByRole("link", { name: "Grafana", exact: true }).click();
    await page.getByRole("heading", { name: "Grafana", exact: true }).waitFor();
    assert.equal(await page.locator('a[href^="https://grafana"]').count(), 0);
    assert.ok(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
      `Detail overflow at ${width}`
    );
    await page.screenshot({ path: resolve(output, `detail-${width}.png`), fullPage: true });
    await page.getByRole("button", { name: "Edit resource", exact: true }).click();
    assert.ok(
      await page.getByRole("dialog").evaluate((element) => element.scrollWidth <= innerWidth + 1),
      `Editor overflow at ${width}`
    );
    await page.screenshot({ path: resolve(output, `editor-${width}.png`), fullPage: true });
    await closeEditor();
    check(`list-detail-editor-${width}px-inert-access`);
  }
  for (const width of [320, 390, 1440]) {
    // A single browser page has dependent viewport and navigation state.
    // eslint-disable-next-line no-await-in-loop
    await verifyViewport(width);
  }
  await page.addInitScript(() => {
    window.fixtureRole = 5;
  });
  await page.goto(origin + projectPath);
  await page.getByRole("link", { name: "Grafana", exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Add resource", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Add relationship", exact: true }).count(), 0);
  check("reader-management-controls-hidden");
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.blocked_network, []);
  report.success = true;
} catch (error) {
  report.failure = String(error);
  await page.screenshot({ path: resolve(output, "failure.png"), fullPage: true }).catch(() => {});
  throw error;
} finally {
  await writeFile(resolve(output, "report.json"), JSON.stringify(report, null, 2) + "\n");
  await browser.close();
  console.log(JSON.stringify(report));
}
