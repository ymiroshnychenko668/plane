import { describe, expect, it } from "vitest";
import type { TBuild, TBuildCoverage, TBuildResult } from "@plane/types";
import {
  buildFiltersFromSearch,
  buildPageSearch,
  summaryBuildFilters,
  buildPollingInterval,
  coverageDelta,
  formatCoverage,
  safeExternalUrl,
} from "./helpers";

const metric = (percent: number, revision = "abc"): TBuildCoverage => ({
  covered: percent,
  total: 100,
  percent,
  revision,
  tool: "coverage.py",
  measured_at: "2026-10-29T10:00:00Z",
  source: "coverage.json",
  evidence_states: ["measured"],
});
const result: TBuildResult = {
  module_id: "module",
  suite: "api",
  test_type: "unit",
  scope: "apps/api",
  exclusions: ["generated", "vendor"],
  tests: { passed: 10, failed: 0, skipped: 1 },
  coverage: { line: metric(0) },
};
const build = (id: string, observed_at: string, results: TBuildResult[] = [result]): TBuild => ({
  id,
  observed_at,
  repository: "org/repo",
  workflow: "CI",
  run_id: id,
  attempt: 1,
  revision: "abc",
  ref: "main",
  pipeline_url: "https://github.com/org/repo/actions/runs/1",
  status: "completed",
  conclusion: "success",
  started_at: null,
  finished_at: null,
  links: [],
  results,
});

describe("build evidence presentation", () => {
  it("uses server previous-page metadata after a deep link/reload and keeps all filters", () => {
    const search = new URLSearchParams("cursor=2%3A2%3A0&repository=org%2Frepo&module_id=module&status=completed");
    const page = {
      results: [],
      next_page_results: true,
      next_cursor: "2:3:0",
      prev_page_results: true,
      prev_cursor: "2:1:1",
    };
    const previous = buildPageSearch(search, page, "previous");
    expect(previous?.get("cursor")).toBe("2:1:1");
    expect(previous?.get("repository")).toBe("org/repo");
    expect(previous?.get("module_id")).toBe("module");
    expect(previous?.get("status")).toBe("completed");
    expect(buildPageSearch(search, { ...page, prev_page_results: false }, "previous")).toBeNull();
    expect(buildPageSearch(search, page, "next")?.get("cursor")).toBe("2:3:0");
    expect(search.get("cursor")).toBe("2:2:0");
  });
  it("applies repository/workflow/ref/status to both history and summary while module routes override query selection", () => {
    const filters = buildFiltersFromSearch(
      new URLSearchParams(
        "repository=org%2Frepo&workflow=CI&ref=refs%2Fheads%2Fmain&status=running&module_id=query-module&cursor=next"
      ),
      "route-module"
    );
    expect(filters).toEqual({
      repository: "org/repo",
      workflow: "CI",
      ref: "refs/heads/main",
      status: "running",
      module_id: "route-module",
      cursor: "next",
    });
    expect(summaryBuildFilters(filters)).toEqual({
      repository: "org/repo",
      workflow: "CI",
      ref: "refs/heads/main",
      status: "running",
    });
    expect(buildFiltersFromSearch(new URLSearchParams())).toEqual({
      repository: undefined,
      workflow: undefined,
      ref: undefined,
      status: undefined,
      module_id: undefined,
      cursor: undefined,
    });
  });
  it("distinguishes measured zero from missing, non-finite and explicit unmeasured coverage", () => {
    expect(formatCoverage(metric(0))).toBe("0.0%");
    expect(formatCoverage(undefined)).toBe("Not measured");
    expect(formatCoverage({ ...metric(50), evidence_states: ["not_measured"] })).toBe("Not measured");
    expect(formatCoverage(metric(NaN))).toBe("Not measured");
    expect(formatCoverage({ ...metric(0), covered: 0, total: 0, percent: null })).toBe("No executable items (0/0)");
  });
  it("only polls active builds while visible; unknown state never becomes a failure", () => {
    const current = build("1", "2026-10-29T10:00:00Z");
    expect(buildPollingInterval([{ ...current, status: "running" }], true)).toBe(15000);
    expect(buildPollingInterval([{ ...current, status: "queued" }], false)).toBe(0);
    expect(buildPollingInterval([{ ...current, status: "unknown" }], true)).toBe(0);
    expect(buildPollingInterval([current], true)).toBe(0);
  });
  it("compares matching provenance regardless of exclusion ordering", () => {
    const previous = build("1", "2026-10-28T10:00:00Z", [
      { ...result, exclusions: ["vendor", "generated"], coverage: { line: metric(20) } },
    ]);
    expect(coverageDelta(build("2", "2026-10-29T10:00:00Z"), result, "line", [previous])).toBe(-20);
  });
  it.each(["repository", "ref", "workflow"] as const)("does not compare different %s", (key) => {
    const previous = { ...build("1", "2026-10-28T10:00:00Z"), [key]: "different" };
    expect(coverageDelta(build("2", "2026-10-29T10:00:00Z"), result, "line", [previous])).toBeNull();
  });
  it("does not compare a different module, suite, tool, scope or exclusions", () => {
    for (const changed of [
      { module_id: "other" },
      { suite: "other" },
      { scope: "other" },
      { exclusions: [] },
      { coverage: { line: { ...metric(30), tool: "other" } } },
    ]) {
      const previous = build("1", "2026-10-28T10:00:00Z", [{ ...result, ...changed }]);
      expect(coverageDelta(build("2", "2026-10-29T10:00:00Z"), result, "line", [previous])).toBeNull();
    }
  });
  it("does not display a trend for mismatched source revision", () => {
    const current = { ...result, coverage: { line: metric(70, "wrong") } };
    expect(
      coverageDelta(build("2", "2026-10-29T10:00:00Z"), current, "line", [build("1", "2026-10-28T10:00:00Z")])
    ).toBeNull();
  });
  it("compares completed runs in execution order even when an older run was observed later", () => {
    const current = { ...build("3", "2026-10-29T10:00:00Z"), started_at: "2026-10-29T09:00:00Z" };
    const previous = {
      ...build("1", "2026-10-30T10:00:00Z", [{ ...result, coverage: { line: metric(20) } }]),
      started_at: "2026-10-28T09:00:00Z",
    };
    const running = {
      ...build("2", "2026-10-29T08:00:00Z", [{ ...result, coverage: { line: metric(60) } }]),
      status: "running" as const,
    };
    expect(coverageDelta(current, result, "line", [running, previous])).toBe(-20);
    expect(coverageDelta({ ...current, status: "running" }, result, "line", [previous])).toBeNull();
  });
  it("uses observation time rather than reverse ingestion order when start time is missing", () => {
    const current = { ...build("2", "2026-10-29T09:00:00Z"), created_at: "2026-10-29T10:00:00Z" };
    const olderImportedLater = {
      ...build("1", "2026-10-28T09:00:00Z", [{ ...result, coverage: { line: metric(20) } }]),
      created_at: "2026-10-30T10:00:00Z",
    };
    expect(coverageDelta(current, result, "line", [olderImportedLater])).toBe(-20);
  });
  it("rejects executable and credential-bearing links", () => {
    for (const value of ["javascript:alert(1)", "data:text/html,test", "https://user:password@example.com", "invalid"])
      expect(safeExternalUrl(value)).toBeUndefined();
    expect(safeExternalUrl("https://github.com/org/repo/actions/runs/1")).toBe(
      "https://github.com/org/repo/actions/runs/1"
    );
  });
});
