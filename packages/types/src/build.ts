/** Copyright (c) 2023-present Plane Software, Inc. and contributors. SPDX-License-Identifier: AGPL-3.0-only */

export type TBuildStatus = "queued" | "running" | "completed" | "unknown";
export type TCoverageKind = "line" | "branch" | "function" | "statement";
export type TBuildCoverage = {
  covered: number | null;
  total: number | null;
  percent: number | null;
  revision: string;
  tool: string;
  measured_at: string;
  source: string;
  evidence_states?: string[];
};
export type TBuildResult = {
  module_id: string | null;
  module_name?: string | null;
  suite: string;
  test_type: "unit" | "integration" | "e2e" | "other";
  scope: string;
  exclusions: string[];
  tests: { passed?: number; failed?: number; skipped?: number } | null;
  coverage: Partial<Record<TCoverageKind, TBuildCoverage>>;
  evidence_states?: string[];
};
export type TBuild = {
  id: string;
  repository: string;
  workflow: string;
  run_id: string;
  attempt: number;
  revision: string;
  ref: string;
  pipeline_url: string;
  status: TBuildStatus;
  conclusion: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds?: number | null;
  observed_at: string;
  created_at?: string;
  version?: number;
  freshness_hours?: number | null;
  links: { title: string; url: string }[];
  results: TBuildResult[];
};
export type TBuildList = {
  results: TBuild[];
  next_page_results: boolean;
  next_cursor?: string;
  prev_page_results: boolean;
  prev_cursor?: string;
};
export type TBuildSummaryEntry = {
  module_id: string | null;
  name: string;
  latest_build: TBuild | null;
  last_measured_build: TBuild | null;
};
export type TBuildSummary = {
  modules: TBuildSummaryEntry[];
  project: { latest_build: TBuild | null; last_measured_build: TBuild | null };
};
