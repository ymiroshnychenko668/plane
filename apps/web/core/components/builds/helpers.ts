/** Copyright (c) 2023-present Plane Software, Inc. and contributors. SPDX-License-Identifier: AGPL-3.0-only */
import type { TBuild, TBuildCoverage, TBuildList, TBuildResult, TCoverageKind } from "@plane/types";
import type { TBuildFilters } from "@/services/build.service";

export const COVERAGE_KINDS: TCoverageKind[] = ["line", "branch", "function", "statement"];

export function buildFiltersFromSearch(search: URLSearchParams, moduleId?: string): TBuildFilters {
  return {
    module_id: moduleId || search.get("module_id") || undefined,
    repository: search.get("repository") || undefined,
    workflow: search.get("workflow") || undefined,
    ref: search.get("ref") || undefined,
    status: search.get("status") || undefined,
    cursor: search.get("cursor") || undefined,
  };
}

export function summaryBuildFilters(filters: TBuildFilters): TBuildFilters {
  return { repository: filters.repository, workflow: filters.workflow, ref: filters.ref, status: filters.status };
}

export function buildPageSearch(
  search: URLSearchParams,
  page: TBuildList | undefined,
  direction: "previous" | "next"
): URLSearchParams | null {
  const available = direction === "previous" ? page?.prev_page_results : page?.next_page_results;
  const cursor = direction === "previous" ? page?.prev_cursor : page?.next_cursor;
  if (!available || !cursor) return null;
  const next = new URLSearchParams(search);
  next.set("cursor", cursor);
  return next;
}

function sortedExclusions(values: string[]): string[] {
  // oxlint-disable-next-line unicorn/no-array-sort -- The ES2022 target lacks toSorted; only mutate a fresh copy.
  return [...values].sort();
}

export function safeExternalUrl(value: string | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch {
    return undefined;
  }
}

export function coveragePercent(metric: TBuildCoverage | undefined): number | null {
  if (!metric || metric.evidence_states?.some((state) => ["not_measured", "unknown", "invalid"].includes(state)))
    return null;
  const value = metric.percent;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

export function formatCoverage(metric: TBuildCoverage | undefined): string {
  if (metric?.total === 0 && metric.covered === 0) return "No executable items (0/0)";
  const value = coveragePercent(metric);
  return value === null ? "Not measured" : `${value.toFixed(1)}%`;
}

export function buildPollingInterval(builds: TBuild[] | undefined, visible: boolean): number {
  return visible && builds?.some((build) => ["queued", "running"].includes(build.status)) ? 15_000 : 0;
}

export function isSameResult(a: TBuildResult, b: TBuildResult): boolean {
  return (
    a.module_id === b.module_id &&
    a.suite === b.suite &&
    a.test_type === b.test_type &&
    a.scope === b.scope &&
    JSON.stringify(sortedExclusions(a.exclusions)) === JSON.stringify(sortedExclusions(b.exclusions))
  );
}

export function resultKey(result: TBuildResult): string {
  return JSON.stringify([
    result.module_id,
    result.suite,
    result.test_type,
    result.scope,
    sortedExclusions(result.exclusions),
  ]);
}

function buildRunTime(value: TBuild): number {
  return Date.parse(value.started_at ?? value.observed_at);
}

/** Only measurements with matching provenance are comparable. Never average independent suites. */
export function coverageDelta(
  build: TBuild,
  result: TBuildResult,
  kind: TCoverageKind,
  history: TBuild[]
): number | null {
  const current = result.coverage[kind];
  const currentPercent = coveragePercent(current);
  if (build.status !== "completed" || currentPercent === null || current?.revision !== build.revision) return null;
  const earlier = history
    .filter(
      (candidate) =>
        candidate.id !== build.id &&
        candidate.repository === build.repository &&
        candidate.status === "completed" &&
        candidate.ref === build.ref &&
        candidate.workflow === build.workflow &&
        buildRunTime(candidate) < buildRunTime(build)
    )
    // oxlint-disable-next-line unicorn/no-array-sort -- The ES2022 target lacks toSorted; filter created a fresh array.
    .sort((a, b) => buildRunTime(b) - buildRunTime(a));
  for (const candidate of earlier) {
    const previous = candidate.results.find((value) => isSameResult(result, value));
    const metric = previous?.coverage[kind];
    const value = coveragePercent(metric);
    if (value !== null && metric?.tool === current.tool && metric?.revision === candidate.revision)
      return currentPercent - value;
  }
  return null;
}

export function resultsForModule(build: TBuild, moduleId: string | null): TBuildResult[] {
  return build.results.filter((result) => result.module_id === moduleId);
}

export function formatBuildTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}
