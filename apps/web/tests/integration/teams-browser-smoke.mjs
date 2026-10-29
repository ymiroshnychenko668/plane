// Authorized local synthetic fixture only. Never prints session or API-key values.
// node teams-browser-smoke.mjs /private/fixture.json /output/directory
import assert from "node:assert/strict";
import { readFile, writeFile, mkdir, stat } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
const fixturePath = process.argv[2];
assert.ok(fixturePath, "Pass an isolated development fixture");
assert.equal((await stat(fixturePath)).mode & 0o077, 0, "Fixture must be private");
const fixture = JSON.parse(await readFile(fixturePath, "utf8"));
assert.match(fixture.base_url, /^http:\/\/127\.0\.0\.1:/, "Only isolated localhost preview is allowed");
const output = resolve(process.argv[3] || ".test-state/teams/browser");
await mkdir(output, { recursive: true });
const { chromium } = await import(
  process.env.PLAYWRIGHT_MODULE ? pathToFileURL(resolve(process.env.PLAYWRIGHT_MODULE)).href : "playwright"
);
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({
  viewport: { width: 1500, height: 1040 },
  locale: "en-US",
  timezoneId: "Europe/Kiev",
});
await context.addCookies(
  fixture.session_cookie.split(";").map((entry) => {
    const at = entry.indexOf("=");
    return {
      name: entry.slice(0, at).trim(),
      value: entry.slice(at + 1).trim(),
      url: fixture.base_url,
      httpOnly: true,
      sameSite: "Lax",
    };
  })
);
const root = `${fixture.base_url}/${fixture.workspace_slug}/projects/${fixture.project_id}`;
const api = `${fixture.api_url}/api/workspaces/${fixture.workspace_slug}/projects/${fixture.project_id}/team/`;
const report = { success: false, synthetic_fixture: true, checks: [], errors: [], baseline_errors: [] };
const page = await context.newPage();
let phase = "baseline";
page.on("pageerror", (error) => (phase === "baseline" ? report.baseline_errors : report.errors).push(error.message));
const check = (name) => report.checks.push(name);
async function roster() {
  const r = await context.request.get(api);
  assert.equal(r.status(), 200);
  return r.json();
}
async function saved() {
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await page.getByRole("button", { name: "Close editor", exact: true }).waitFor({ state: "hidden" });
}
async function tab(name) {
  await page.getByRole("tab", { name: new RegExp("^" + name) }).click();
}
async function resolveAddress(address, status = 200) {
  const r = await context.request.post(api + "resolve/", { data: { address } });
  assert.equal(r.status(), status);
  return r;
}
try {
  await page.goto(root + "/issues");
  await page.getByRole("link", { name: "Teams", exact: true }).first().waitFor();
  await page.waitForLoadState("networkidle");
  phase = "teams";
  await page.getByRole("link", { name: "Teams", exact: true }).first().click();
  await page.getByRole("heading", { name: "Teams", exact: true }).waitFor();
  await page.getByRole("button", { name: "Add member", exact: true }).waitFor();
  check("native-project-navigation");
  if ((await roster()).legacy.status === "pending") {
    await page.getByRole("button", { name: "Import addresses", exact: true }).click();
    await page.getByText("Existing addresses imported.", { exact: true }).waitFor();
  }
  let team = await roster();
  assert.equal(team.legacy.status, "imported");
  assert.equal(team.contacts.filter((c) => ["review", "quality", "release"].includes(c.address)).length, 3);
  check("legacy-import-to-canonical-registry");
  await page.screenshot({ path: output + "/teams-agents.png", fullPage: true });
  await page.getByLabel("Search team").fill("regression");
  await page.getByRole("row").filter({ hasText: "Quality engineer" }).waitFor();
  assert.equal(await page.getByRole("row").filter({ hasText: "Release coordinator" }).count(), 0);
  await page.getByLabel("Search team").fill("");
  check("search-responsibility");
  const reviewer = team.actors.find((a) => a.agent_key === "review");
  await page.getByRole("button", { name: "Edit agent " + reviewer.display_name, exact: true }).click();
  await page.getByLabel("Responsibility", { exact: true }).fill("Reviews API and UI changes with measured evidence");
  await saved();
  let updated = (await roster()).actors.find((a) => a.registration_id === reviewer.registration_id);
  assert.equal(updated.role, "Reviews API and UI changes with measured evidence");
  const stale = await context.request.patch(api + "agents/" + reviewer.registration_id + "/", {
    headers: { "If-Match": `"${reviewer.revision}"` },
    data: { role: "stale overwrite" },
  });
  assert.equal(stale.status(), 412);
  check("agent-edit-and-stale-write-rejection");
  await page.getByRole("button", { name: "Add member", exact: true }).click();
  await page.getByLabel("Workspace member", { exact: true }).selectOption(fixture.add_user_id);
  await saved();
  assert.ok((await roster()).members.some((m) => m.id === fixture.add_user_id && m.is_active));
  check("add-existing-workspace-member");
  await page.getByRole("button", { name: "Add agent", exact: true }).click();
  await page.getByLabel("Agent key", { exact: true }).fill("ui-reviewer");
  await page.getByLabel("Project member", { exact: true }).selectOption(fixture.add_user_id);
  await page.getByLabel("Chat ID", { exact: true }).fill("11111111-2222-4333-8444-555555555555");
  await page.getByLabel("Host", { exact: true }).fill("local");
  await page.getByLabel("Display name", { exact: true }).fill("UI reviewer");
  await page.getByLabel("Responsibility", { exact: true }).fill("Validates interface behavior and accessible controls");
  await saved();
  team = await roster();
  const actor = team.actors.find((a) => a.agent_key === "ui-reviewer");
  assert.ok(actor && actor.user_id === fixture.add_user_id);
  check("create-agent-linked-to-project-member");
  await page.getByRole("button", { name: "Add address", exact: true }).click();
  await page.getByLabel("Address", { exact: true }).fill("ui-reviewer");
  await page.getByLabel("Agent", { exact: true }).selectOption(actor.registration_id);
  await saved();
  await resolveAddress("ui-reviewer");
  check("create-address-and-resolve");
  await tab("Addresses");
  await page.getByRole("button", { name: "Edit address ui-reviewer", exact: true }).click();
  await page.getByLabel("Address active", { exact: true }).uncheck();
  await saved();
  await resolveAddress("ui-reviewer", 404);
  await page.getByRole("button", { name: "Edit address ui-reviewer", exact: true }).click();
  await page.getByLabel("Address active", { exact: true }).check();
  await saved();
  await resolveAddress("ui-reviewer");
  check("deactivate-reactivate-address");
  await tab("Agents");
  await page.getByRole("button", { name: "Edit agent UI reviewer", exact: true }).click();
  await page.getByLabel("Agent active", { exact: true }).uncheck();
  await saved();
  await resolveAddress("ui-reviewer", 404);
  await page.getByRole("button", { name: "Edit agent UI reviewer", exact: true }).click();
  await page.getByLabel("Agent active", { exact: true }).check();
  await saved();
  await resolveAddress("ui-reviewer");
  check("agent-deactivation-invalidates-resolution");
  await tab("Members");
  await page.getByRole("button", { name: "Manage member Sam Lee", exact: true }).click();
  await page.getByLabel("Project role", { exact: true }).selectOption("5");
  await saved();
  assert.equal((await roster()).members.find((m) => m.id === fixture.add_user_id).role, 5);
  await page.getByRole("button", { name: "Manage member Sam Lee", exact: true }).click();
  await page.getByLabel("Project role", { exact: true }).selectOption("15");
  await saved();
  check("member-role-update");
  await page.getByRole("button", { name: "Manage member Sam Lee", exact: true }).click();
  await page.getByRole("button", { name: "Remove member", exact: true }).click();
  await page.getByRole("button", { name: "Confirm removal", exact: true }).click();
  await page.getByRole("button", { name: "Close editor", exact: true }).waitFor({ state: "hidden" });
  await resolveAddress("ui-reviewer", 404);
  await page.getByRole("button", { name: "Add member", exact: true }).click();
  await page.getByLabel("Workspace member", { exact: true }).selectOption(fixture.add_user_id);
  await saved();
  await resolveAddress("ui-reviewer");
  check("membership-deactivation-and-reactivation");
  await tab("Addresses");
  await page.screenshot({ path: output + "/teams-addresses.png", fullPage: true });
  await page.reload();
  await page.getByRole("button", { name: "Add member", exact: true }).waitFor();
  await tab("Members");
  await page.screenshot({ path: output + "/teams-members.png", fullPage: true });
  check("deep-link-reload-persisted-data");
  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await mobile.addCookies(await context.cookies());
  const mobilePage = await mobile.newPage();
  mobilePage.on("pageerror", (error) => report.errors.push(error.message));
  await mobilePage.goto(root + "/teams");
  await mobilePage.getByRole("button", { name: "Add member", exact: true }).waitFor();
  // Use the existing native sidebar toggle before measuring the narrow content area.
  if ((await mobilePage.getByTestId("teams-page").evaluate((element) => element.clientWidth)) < 300)
    await mobilePage.locator("button:has(svg.lucide-panel-left):visible").first().click();
  // Focusing a tab during the native sidebar transition can scroll a narrowing
  // ancestor and capture a clipped page even though its final layout is valid.
  await mobilePage.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
        .map((animation) => animation.finished.catch(() => {}))
    )
  );
  await mobilePage.waitForFunction(() => {
    const bounds = document.querySelector('[data-testid="teams-page"]')?.getBoundingClientRect();
    return bounds && bounds.width >= window.innerWidth - 32 && bounds.x >= 0 && bounds.right <= window.innerWidth;
  });
  await mobilePage.getByRole("tab", { name: /^Addresses/ }).click();
  await mobilePage.screenshot({ path: output + "/teams-mobile.png", fullPage: true });
  assert.ok(await mobilePage.getByRole("heading", { name: "Teams", exact: true }).isVisible());
  await mobile.close();
  check("mobile-layout");
  const warnings = new Set(report.baseline_errors);
  report.feature_errors = report.errors.filter((e) => !warnings.has(e));
  assert.deepEqual(report.feature_errors, []);
  check("no-new-feature-runtime-errors");
  report.success = true;
} catch (error) {
  report.failure = String(error);
  await page.screenshot({ path: output + "/failure.png", fullPage: true }).catch(() => {});
  throw error;
} finally {
  await writeFile(output + "/browser-report.json", JSON.stringify(report, null, 2) + "\n");
  await browser.close();
  console.log(
    JSON.stringify({
      success: report.success,
      checks: report.checks,
      baseline_error_count: report.baseline_errors.length,
      feature_errors: report.feature_errors || report.errors,
    })
  );
}
