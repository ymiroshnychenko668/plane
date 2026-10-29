// Read-only acceptance against an explicitly configured development Plane instance.
// node tests/integration/builds-browser-smoke.mjs /mode-600/fixture.json /evidence/directory
// Fixture: base_url, api_url(optional), workspace_slug, project_id, session_cookie,
// build_id(optional), module_id(optional). No fixture values are printed.
import assert from "node:assert/strict";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const fixturePath = process.argv[2];
assert.ok(fixturePath, "Pass a private development fixture JSON");
assert.equal((await stat(fixturePath)).mode & 0o077, 0, "Fixture must be private");
const fixture = JSON.parse(await readFile(fixturePath, "utf8"));
for (const key of ["base_url", "workspace_slug", "project_id", "session_cookie"])
  assert.equal(typeof fixture[key], "string", `Missing field ${key}`);
const output = resolve(process.argv[3] || ".test-state/builds-browser-smoke");
await mkdir(output, { recursive: true, mode: 0o700 });
const playwright = await import(
  process.env.PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : "playwright"
);
const browser = await playwright.chromium.launch({
  headless: true,
  ...(process.env.PLANE_BROWSER_EXECUTABLE
    ? { executablePath: process.env.PLANE_BROWSER_EXECUTABLE }
    : { channel: "chrome" }),
});
const origin = new URL(fixture.base_url).origin;
const apiOrigin = new URL(fixture.api_url || fixture.base_url).origin;
const path = `/${fixture.workspace_slug}/projects/${fixture.project_id}`;
const apiPath = `/api/workspaces/${fixture.workspace_slug}/projects/${fixture.project_id}`;
const report = { success: false, checks: [], browser: browser.version(), errors: [], error_details: [] };
let context;
let phase = "baseline";

try {
  context = await browser.newContext({
    viewport: { width: 1440, height: 1080 },
    locale: "en-US",
    timezoneId: fixture.timezone || "Europe/Kiev",
  });
  await context.addCookies(
    fixture.session_cookie.split(";").map((entry) => {
      const at = entry.indexOf("=");
      assert.ok(at > 0, "Invalid cookie fixture");
      return {
        name: entry.slice(0, at).trim(),
        value: entry.slice(at + 1).trim(),
        url: origin,
        httpOnly: true,
        sameSite: "Lax",
      };
    })
  );
  const listResponse = await context.request.get(`${apiOrigin}${apiPath}/builds/`);
  assert.equal(listResponse.status(), 200, "Session build list must be readable");
  const list = await listResponse.json();
  assert.ok(list.results.length, "Seed at least one build before UI acceptance");
  const buildId = fixture.build_id || list.results[0].id;
  const build =
    list.results.find((entry) => entry.id === buildId) ||
    (await (await context.request.get(`${apiOrigin}${apiPath}/builds/${buildId}/`)).json());
  const moduleId = fixture.module_id || build.results.find((entry) => entry.module_id)?.module_id;
  const page = await context.newPage();
  page.on("pageerror", (error) => {
    report.errors.push(error.message);
    report.error_details.push({
      message: error.message,
      stack: error.stack,
      route: page.url(),
      stage: phase,
      checks_completed: report.checks.length,
    });
  });

  // Compare with the existing Work items route; retain known root hydration warnings in the report.
  await page.goto(`${origin}${path}/issues`);
  await page.getByRole("link", { name: "Builds", exact: true }).first().waitFor();
  // Let the existing route finish its bootstrap requests before capturing its health separately.
  await page.waitForLoadState("networkidle");
  const recognizedWarnings = new Set([
    "Hydration failed because the initial UI does not match what was rendered on the server.",
    "There was an error while hydrating. Because the error happened outside of a Suspense boundary, the entire root will switch to client rendering.",
  ]);
  const baselineErrors = new Set(report.errors.filter((error) => recognizedWarnings.has(error)));
  report.baseline_warnings = {
    route: "existing-work-items",
    messages: [...baselineErrors],
    count: report.errors.filter((error) => baselineErrors.has(error)).length,
  };
  report.baseline_errors = report.error_details
    .filter((error) => error.stage === "baseline" && !recognizedWarnings.has(error.message))
    .map((error) => ({ route: error.route, stage: error.stage, full_message: error.message, stack: error.stack }));
  report.baseline_health = { success: report.baseline_errors.length === 0 && baselineErrors.size === 0 };
  report.baseline_note = report.baseline_errors.length
    ? "The existing Work items route produced an unhandled error before Builds checks started. It is reported separately; the same error during Builds checks still fails feature acceptance."
    : baselineErrors.size
      ? "The existing Work items route produced known root hydration recovery warnings."
      : "No baseline errors observed.";
  report.errors = [];
  phase = "builds";

  await page.goto(`${origin}${path}/builds`);
  await page.getByRole("heading", { name: "Build history", exact: true }).waitFor();
  await page
    .getByRole("link", { name: `${build.workflow} #${build.run_id}`, exact: true })
    .last()
    .waitFor();
  await page.screenshot({ path: resolve(output, "project-builds.png"), fullPage: true });
  report.checks.push("native-project-overview-and-history");
  await page.getByRole("heading", { name: "Build history", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(output, "build-history.png"), fullPage: true });

  await page.getByLabel("Repository", { exact: true }).fill(build.repository);
  await page.getByLabel("Workflow", { exact: true }).fill(build.workflow);
  const matchesFilters = (response, suffix) => {
    const url = new URL(response.url());
    return (
      url.pathname === `${apiPath}/${suffix}/` &&
      url.searchParams.get("repository") === build.repository &&
      url.searchParams.get("workflow") === build.workflow &&
      url.searchParams.get("status") === "completed" &&
      response.status() === 200
    );
  };
  await Promise.all([
    page.waitForResponse((response) => matchesFilters(response, "builds")),
    page.waitForResponse((response) => matchesFilters(response, "builds-summary")),
    page.getByLabel("Status", { exact: true }).selectOption("completed"),
  ]);
  report.checks.push("repository-workflow-and-status-filter-both-history-and-summary");
  await page.goto(`${origin}${path}/builds`);
  await page.reload();
  await page.getByRole("heading", { name: "Build history", exact: true }).waitFor();
  report.checks.push("project-deep-link-and-reload");

  await page.goto(`${origin}${path}/builds/${buildId}`);
  await page.getByRole("heading", { name: `${build.workflow} #${build.run_id}`, exact: true }).waitFor();
  const actions = page.getByRole("link", { name: "GitHub Actions", exact: true });
  assert.equal(await actions.getAttribute("href"), build.pipeline_url);
  assert.equal(await actions.getAttribute("rel"), "noopener noreferrer");
  await page.getByText(build.revision, { exact: true }).first().waitFor();
  const coverageChecks = [];
  for (const result of build.results) {
    for (const kind of ["line", "branch", "function", "statement"]) {
      const metric = result.coverage[kind];
      const expected =
        metric?.total === 0 && metric?.covered === 0
          ? "No executable items (0/0)"
          : metric?.percent != null
            ? `${metric.percent.toFixed(1)}%`
            : "Not measured";
      coverageChecks.push(page.getByText(expected, { exact: true }).first().waitFor());
      for (const state of metric?.evidence_states || [])
        if (state !== "measured")
          coverageChecks.push(page.getByText(state.replaceAll("_", " "), { exact: true }).first().waitFor());
    }
  }
  await Promise.all(coverageChecks);
  await page.screenshot({ path: resolve(output, "build-detail.png"), fullPage: true });
  report.checks.push("build-detail-revision-coverage-and-actions-link");

  if (moduleId) {
    await page.goto(`${origin}${path}/modules/${moduleId}/builds`);
    await page.getByRole("heading", { name: "Build history", exact: true }).waitFor();
    assert.equal(await page.getByLabel("Module", { exact: true }).count(), 0);
    await page.getByRole("link", { name: "Module work items", exact: true }).waitFor();
    report.checks.push("native-module-builds-deep-link");
  }

  // Exercise both navigation render modes without changing persisted user preferences.
  const checkNavigationMode = async (mode) => {
    await page.route(`**/api/workspaces/${fixture.workspace_slug}/user-properties/`, async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      const response = await route.fetch();
      const data = await response.json();
      await route.fulfill({ response, json: { ...data, navigation_control_preference: mode } });
    });
    await page.goto(`${origin}${path}/builds`);
    await page.getByRole("heading", { name: "Build history", exact: true }).waitFor();
    await page.getByRole("link", { name: "Builds", exact: true }).first().waitFor();
    await page.screenshot({ path: resolve(output, `navigation-${mode.toLowerCase()}.png`), fullPage: true });
    await page.unroute(`**/api/workspaces/${fixture.workspace_slug}/user-properties/`);
    report.checks.push(`navigation-${mode.toLowerCase()}`);
  };
  await checkNavigationMode("ACCORDION");
  await checkNavigationMode("TABBED");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${origin}${path}/builds`);
  await page.getByRole("heading", { name: "Build history", exact: true }).waitFor();
  await page.screenshot({ path: resolve(output, "mobile-builds.png"), fullPage: true });
  report.checks.push("mobile-builds-view");

  await page.route(`**${apiPath}/builds-summary/**`, (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: "{}" })
  );
  await page.goto(`${origin}${path}/builds`);
  await page.getByRole("heading", { name: "Builds are unavailable on this Plane instance", exact: true }).waitFor();
  report.checks.push("missing-capability-message");
  report.baseline_warning_count = report.errors.filter((error) => baselineErrors.has(error)).length;
  report.errors = report.errors.filter((error) => !baselineErrors.has(error));
  assert.equal(report.errors.length, 0, "New browser page errors occurred; inspect private report");
  report.checks.push("no-new-browser-page-errors");
  report.feature_success = true;
  report.runtime_clean = report.baseline_health.success && report.baseline_warning_count === 0;
  report.success = true;
} catch (error) {
  report.failure = error.message;
  const failedPage = context?.pages()[0];
  if (failedPage) {
    report.page_url = failedPage.url();
    report.page_text = (await failedPage.locator("body").innerText()).slice(0, 2000);
    await failedPage.screenshot({ path: resolve(output, "failure.png"), fullPage: true });
  }
  throw error;
} finally {
  await writeFile(resolve(output, "report.json"), `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
  await context?.close();
  await browser.close();
}
console.log(
  JSON.stringify({
    success: report.success,
    feature_success: report.feature_success,
    baseline_health: report.baseline_health,
    baseline_note: report.baseline_note,
    checks: report.checks,
  })
);
