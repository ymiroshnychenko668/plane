/** Copyright (c) 2023-present Plane Software, Inc. and contributors. SPDX-License-Identifier: AGPL-3.0-only */
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import useSWR from "swr";
import { ArrowLeft, ExternalLink, RefreshCw } from "lucide-react";
import { Badge } from "@plane/propel/badge";
import { Button } from "@plane/propel/button";
import type { TBuild, TBuildCoverage, TBuildResult, TBuildSummaryEntry, TCoverageKind } from "@plane/types";
import { PageHead } from "@/components/core/page-title";
import { BuildService } from "@/services/build.service";
import {
  COVERAGE_KINDS,
  buildPollingInterval,
  coverageDelta,
  coveragePercent,
  formatBuildTime,
  formatCoverage,
  resultsForModule,
  resultKey,
  buildFiltersFromSearch,
  summaryBuildFilters,
  buildPageSearch,
  safeExternalUrl,
} from "./helpers";

const service = new BuildService();
const fieldClass =
  "rounded-md border border-subtle bg-surface-1 px-3 py-2 text-13 text-primary outline-none focus:border-accent-primary";

function useDocumentVisible() {
  const [visible, setVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState === "visible"
  );
  useEffect(() => {
    const change = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", change);
    return () => document.removeEventListener("visibilitychange", change);
  }, []);
  return visible;
}

function BuildStatus({ build }: { build: TBuild }) {
  return (
    <Badge variant="neutral">
      {build.status}
      {build.conclusion ? ` · ${build.conclusion}` : ""}
    </Badge>
  );
}

function ExternalLinkButton({ url, children }: { url?: string; children: React.ReactNode }) {
  const href = safeExternalUrl(url);
  return href ? (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-accent-primary hover:underline"
    >
      {children}
      <ExternalLink className="size-3.5 shrink-0" />
    </a>
  ) : (
    <span className="text-tertiary">{children}: link unavailable</span>
  );
}

function CoverageValue({ metric, delta }: { metric?: TBuildCoverage; delta?: number | null }) {
  const states = metric?.evidence_states ?? [];
  return (
    <div className="space-y-1">
      <span className="font-medium tabular-nums">{formatCoverage(metric)}</span>
      {metric?.covered != null && metric.total != null && (
        <span className="ml-1 text-11 text-tertiary">
          {metric.covered}/{metric.total}
        </span>
      )}
      {delta != null && (
        <span className="ml-1 text-11 text-tertiary">
          ({delta > 0 ? "+" : ""}
          {delta.toFixed(1)} pp)
        </span>
      )}
      {states
        .filter((state) => state !== "measured")
        .map((state) => (
          <span key={state} className="block text-11 text-tertiary">
            {state.replaceAll("_", " ")}
          </span>
        ))}
    </div>
  );
}

function BuildError({ error, detail = false }: { error: { response?: { status?: number } }; detail?: boolean }) {
  const status = error.response?.status;
  return (
    <div role="alert" className="rounded-lg border border-subtle bg-surface-1 p-6">
      <h2 className="font-medium">
        {status === 404 && detail
          ? "Build not found"
          : [404, 501].includes(status ?? 0)
            ? "Builds are unavailable on this Plane instance"
            : status === 403
              ? "You do not have access to these builds"
              : "Builds could not be loaded"}
      </h2>
      <p className="mt-2 text-13 text-secondary">
        {[404, 501].includes(status ?? 0) && !detail
          ? "The Builds backend capability must be installed before reports can be displayed."
          : "Refresh to retry or return to the project. No missing report is treated as a successful build."}
      </p>
    </div>
  );
}

function ResultCoverage({ result }: { result: TBuildResult }) {
  return (
    <div className="space-y-2">
      <p className="text-11 text-tertiary">
        {result.suite} · {result.test_type} · {result.scope}
      </p>
      <div className="grid grid-cols-2 gap-3">
        {(["line", "branch"] as TCoverageKind[]).map((kind) => (
          <div key={kind}>
            <span className="text-11 text-tertiary capitalize">{kind}</span>
            <CoverageValue metric={result.coverage[kind]} />
          </div>
        ))}
      </div>
    </div>
  );
}

function SummaryCard({ entry, base }: { entry: TBuildSummaryEntry; base: string }) {
  const latest = entry.latest_build;
  const historical = entry.last_measured_build;
  const results = latest ? resultsForModule(latest, entry.module_id) : [];
  const measured = results.some((result) =>
    COVERAGE_KINDS.some((kind) => {
      const metric = result.coverage[kind];
      return (
        metric?.revision === latest?.revision &&
        !metric?.evidence_states?.some((state) => ["stale", "revision_mismatch"].includes(state)) &&
        (coveragePercent(metric) !== null || metric?.total === 0)
      );
    })
  );
  const hasRecordedCoverage = results.some((result) => COVERAGE_KINDS.some((kind) => !!result.coverage[kind]));
  return (
    <article className="rounded-lg border border-subtle bg-surface-1 p-4" data-testid="build-summary-card">
      <div className="flex items-center justify-between gap-2">
        {entry.module_id ? (
          <Link className="font-medium text-primary hover:underline" to={`${base}/modules/${entry.module_id}/builds`}>
            {entry.name}
          </Link>
        ) : (
          <h3 className="font-medium">{entry.name}</h3>
        )}
        {latest && <BuildStatus build={latest} />}
      </div>
      {latest ? (
        <>
          <Link to={`${base}/builds/${latest.id}`} className="mt-2 block truncate text-13 text-accent-primary">
            {latest.workflow} #{latest.run_id} · {latest.revision.slice(0, 12)}
          </Link>
          <p className="mt-1 text-11 text-tertiary">
            {latest.ref} · Last observed {formatBuildTime(latest.observed_at)}
          </p>
          {!measured && (
            <p className="mt-3 text-13 text-secondary">
              {hasRecordedCoverage
                ? "No current measurement · recorded evidence below"
                : "Current coverage unknown · Not measured"}
            </p>
          )}
          <div className="mt-3 space-y-3">
            {results.map((result) => (
              <ResultCoverage key={resultKey(result)} result={result} />
            ))}
          </div>
        </>
      ) : (
        <p className="mt-3 text-13 text-secondary">No builds reported · Not measured</p>
      )}
      {historical && historical.id !== latest?.id && (
        <div className="mt-4 border-t border-subtle pt-3">
          <p className="text-11 font-medium text-tertiary">Last measured coverage · historical</p>
          <Link to={`${base}/builds/${historical.id}`} className="mt-1 block text-11 text-accent-primary">
            {historical.revision.slice(0, 12)} · {formatBuildTime(historical.observed_at)}
          </Link>
          <div className="mt-2 space-y-3">
            {resultsForModule(historical, entry.module_id).map((result) => (
              <ResultCoverage key={resultKey(result)} result={result} />
            ))}
          </div>
        </div>
      )}
    </article>
  );
}

export function BuildsPage() {
  const { workspaceSlug = "", projectId = "", moduleId } = useParams();
  const [search, setSearch] = useSearchParams();
  const pendingSearch = useRef(search);
  useEffect(() => {
    pendingSearch.current = search;
  }, [search]);
  const selectedModule = moduleId ?? search.get("module_id") ?? "";
  const ref = search.get("ref") ?? "";
  const status = search.get("status") ?? "";
  const repository = search.get("repository") ?? "";
  const workflow = search.get("workflow") ?? "";
  const visible = useDocumentVisible();
  const base = `/${workspaceSlug}/projects/${projectId}`;
  const filters = buildFiltersFromSearch(search, moduleId);
  const summaryFilters = summaryBuildFilters(filters);
  const list = useSWR(
    ["builds", workspaceSlug, projectId, filters],
    () => service.list(workspaceSlug, projectId, filters),
    {
      refreshInterval: (data) => buildPollingInterval(data?.results, visible),
      refreshWhenHidden: false,
    }
  );
  const summary = useSWR(
    ["builds-summary", workspaceSlug, projectId, summaryFilters],
    () => service.summary(workspaceSlug, projectId, summaryFilters),
    {
      refreshInterval: (data) =>
        buildPollingInterval(
          [
            ...(data?.modules.map((entry) => entry.latest_build).filter((build): build is TBuild => !!build) ?? []),
            ...(data?.project.latest_build ? [data.project.latest_build] : []),
          ],
          visible
        ),
      refreshWhenHidden: false,
    }
  );
  const updateFilter = (key: string, value: string) => {
    // Preserve changes made while React Router is still committing the preceding filter update.
    const next = new URLSearchParams(pendingSearch.current);
    if (value) next.set(key, value);
    else next.delete(key);
    next.delete("cursor");
    pendingSearch.current = next;
    setSearch(next);
  };
  const entries = summary.data?.modules.filter((entry) => !selectedModule || entry.module_id === selectedModule) ?? [];
  const error = summary.error ?? list.error;

  return (
    <div className="h-full overflow-y-auto bg-surface-2 p-4 md:p-6" data-testid="builds-page">
      <PageHead title="Builds" />
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-11 text-tertiary">Project / {moduleId ? "Module / " : ""}Builds</p>
            <h1 className="mt-1 text-24 font-semibold">Builds</h1>
            <p className="mt-1 text-13 text-secondary">
              Agent reported test results and coverage for exact source revisions.
            </p>
          </div>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              void list.mutate();
              void summary.mutate();
            }}
          >
            <RefreshCw className="mr-1.5 size-3.5" />
            Refresh
          </Button>
        </header>
        <nav className="flex flex-wrap gap-4 text-13">
          <Link to={`${base}/issues`} className="text-accent-primary">
            Work items
          </Link>
          {moduleId && (
            <Link to={`${base}/modules/${moduleId}`} className="text-accent-primary">
              Module work items
            </Link>
          )}
          {moduleId && (
            <Link to={`${base}/builds`} className="text-accent-primary">
              All builds
            </Link>
          )}
        </nav>
        <div className="flex flex-wrap gap-3" aria-label="Build filters">
          <label className="flex flex-col gap-1 text-11 text-tertiary">
            Repository
            <input
              aria-label="Repository"
              placeholder="All repositories"
              className={fieldClass}
              value={repository}
              onChange={(event) => updateFilter("repository", event.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-11 text-tertiary">
            Workflow
            <input
              aria-label="Workflow"
              placeholder="All workflows"
              className={fieldClass}
              value={workflow}
              onChange={(event) => updateFilter("workflow", event.target.value)}
            />
          </label>
          {!moduleId && (
            <label className="flex flex-col gap-1 text-11 text-tertiary">
              Module
              <select
                aria-label="Module"
                value={selectedModule}
                onChange={(event) => updateFilter("module_id", event.target.value)}
                className={fieldClass}
              >
                <option value="">All modules</option>
                {summary.data?.modules.map((entry) => (
                  <option key={entry.module_id} value={entry.module_id ?? ""}>
                    {entry.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="flex flex-col gap-1 text-11 text-tertiary">
            Ref
            <input
              aria-label="Ref"
              placeholder="All refs"
              className={fieldClass}
              value={ref}
              onChange={(event) => updateFilter("ref", event.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1 text-11 text-tertiary">
            Status
            <select
              aria-label="Status"
              className={fieldClass}
              value={status}
              onChange={(event) => updateFilter("status", event.target.value)}
            >
              <option value="">All statuses</option>
              {["queued", "running", "completed", "unknown"].map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
        </div>
        {error ? (
          <BuildError error={error} />
        ) : (
          <>
            <section aria-label="Module build summary">
              <h2 className="mb-3 text-16 font-medium">
                {repository || workflow || ref || status
                  ? "Latest matching reports by module"
                  : "Latest reports by module"}
              </h2>
              {!summary.data ? (
                <p className="text-13 text-secondary">Loading reports…</p>
              ) : entries.length ? (
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                  {entries.map((entry) => (
                    <SummaryCard key={entry.module_id} entry={entry} base={base} />
                  ))}
                </div>
              ) : (
                <p className="text-13 text-secondary">
                  {moduleId ? "No report for this module." : "No modules in this project."}
                </p>
              )}
            </section>
            {!selectedModule && summary.data?.project && (
              <section aria-label="Project aggregate">
                <h2 className="mb-3 text-16 font-medium">Project aggregate</h2>
                <p className="mb-3 text-13 text-secondary">
                  Only explicit project reports appear here. Module percentages are never averaged.
                </p>
                <SummaryCard
                  entry={{ ...summary.data.project, module_id: null, name: "Project aggregate" }}
                  base={base}
                />
              </section>
            )}
            <section aria-label="Build history">
              <h2 className="mb-3 text-16 font-medium">Build history</h2>
              <div className="overflow-x-auto rounded-lg border border-subtle bg-surface-1">
                <table className="w-full min-w-[720px] text-left text-13">
                  <thead className="border-b border-subtle text-11 text-tertiary">
                    <tr>
                      {["Build / revision", "Ref", "Status", "Tests", "Line / branch", "Last observed", "Pipeline"].map(
                        (title) => (
                          <th key={title} className="px-4 py-3 font-medium">
                            {title}
                          </th>
                        )
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {list.data?.results.map((build) => {
                      const results = selectedModule ? resultsForModule(build, selectedModule) : build.results;
                      return (
                        <tr key={build.id} className="border-b border-subtle last:border-0">
                          <td className="px-4 py-3">
                            <Link to={`${base}/builds/${build.id}`} className="font-medium text-accent-primary">
                              {build.workflow} #{build.run_id}
                            </Link>
                            <p className="font-mono mt-1 text-11 text-tertiary">
                              {build.revision.slice(0, 12)} · attempt {build.attempt}
                            </p>
                          </td>
                          <td className="px-4 py-3">{build.ref}</td>
                          <td className="px-4 py-3">
                            <BuildStatus build={build} />
                          </td>
                          <td className="px-4 py-3">
                            {results.length
                              ? results.map((result) => (
                                  <p key={resultKey(result)} className="mb-1 text-11">
                                    <span className="text-tertiary">
                                      {result.module_name ?? result.module_id ?? "Project"} / {result.suite}:{" "}
                                    </span>
                                    {result.tests
                                      ? `${result.tests.passed ?? "—"} passed · ${result.tests.failed ?? "—"} failed · ${result.tests.skipped ?? "—"} skipped`
                                      : "Not measured"}
                                  </p>
                                ))
                              : "Not measured"}
                          </td>
                          <td className="px-4 py-3">
                            {results.length
                              ? results.map((result) => (
                                  <div key={resultKey(result)} className="mb-2 text-11">
                                    <p className="mb-1 text-tertiary">{result.suite}</p>
                                    <div className="flex gap-4">
                                      <div>
                                        <p className="text-tertiary">Line</p>
                                        <CoverageValue metric={result.coverage.line} />
                                      </div>
                                      <div>
                                        <p className="text-tertiary">Branch</p>
                                        <CoverageValue metric={result.coverage.branch} />
                                      </div>
                                    </div>
                                  </div>
                                ))
                              : "Not measured"}
                          </td>
                          <td className="px-4 py-3 text-11 text-secondary">{formatBuildTime(build.observed_at)}</td>
                          <td className="px-4 py-3">
                            <ExternalLinkButton url={build.pipeline_url}>Actions</ExternalLinkButton>
                          </td>
                        </tr>
                      );
                    })}
                    {!list.data?.results.length && (
                      <tr>
                        <td colSpan={7} className="px-4 py-8 text-center text-secondary">
                          {list.isLoading ? "Loading builds…" : "No builds reported for these filters."}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              <div className="mt-3 flex justify-end gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!buildPageSearch(search, list.data, "previous")}
                  onClick={() => {
                    const next = buildPageSearch(search, list.data, "previous");
                    if (next) setSearch(next);
                  }}
                >
                  Previous
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!buildPageSearch(search, list.data, "next")}
                  onClick={() => {
                    const next = buildPageSearch(search, list.data, "next");
                    if (next) setSearch(next);
                  }}
                >
                  Next
                </Button>
              </div>
            </section>
          </>
        )}
      </div>
    </div>
  );
}

export function BuildDetailPage() {
  const { workspaceSlug = "", projectId = "", buildId = "" } = useParams();
  const base = `/${workspaceSlug}/projects/${projectId}`;
  const visible = useDocumentVisible();
  const detail = useSWR(
    ["build", workspaceSlug, projectId, buildId],
    () => service.getBuild(workspaceSlug, projectId, buildId),
    { refreshInterval: (build) => buildPollingInterval(build ? [build] : [], visible), refreshWhenHidden: false }
  );
  const build = detail.data;
  const history = useSWR(
    build ? ["build-comparison", workspaceSlug, projectId, build.repository, build.ref, build.workflow] : null,
    () =>
      service.list(workspaceSlug, projectId, {
        repository: build!.repository,
        ref: build!.ref,
        workflow: build!.workflow,
      })
  );
  return (
    <div className="h-full overflow-y-auto bg-surface-2 p-4 md:p-6" data-testid="build-detail-page">
      <PageHead title={build ? `Build ${build.run_id}` : "Build"} />
      <div className="mx-auto max-w-6xl space-y-6">
        <Link to={`${base}/builds`} className="inline-flex items-center gap-2 text-13 text-accent-primary">
          <ArrowLeft className="size-4" />
          Builds
        </Link>
        {detail.error ? (
          <BuildError error={detail.error} detail />
        ) : !build ? (
          <p>Loading build…</p>
        ) : (
          <>
            <header className="flex flex-wrap justify-between gap-3">
              <div>
                <h1 className="text-24 font-semibold">
                  {build.workflow} #{build.run_id}
                </h1>
                <p className="mt-1 text-13 text-secondary">
                  {build.repository} · attempt {build.attempt}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <BuildStatus build={build} />
                <ExternalLinkButton url={build.pipeline_url}>GitHub Actions</ExternalLinkButton>
              </div>
            </header>
            <dl className="grid gap-4 rounded-lg border border-subtle bg-surface-1 p-4 text-13 sm:grid-cols-2 lg:grid-cols-3">
              {[
                ["Revision", build.revision],
                ["Ref", build.ref],
                ["Started", formatBuildTime(build.started_at)],
                ["Finished", formatBuildTime(build.finished_at)],
                ["Duration", build.duration_seconds == null ? "Unknown" : `${build.duration_seconds} seconds`],
                ["Last observed", formatBuildTime(build.observed_at)],
                [
                  "Freshness window",
                  build.freshness_hours == null ? "Not specified" : `${build.freshness_hours} hours`,
                ],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="text-11 text-tertiary">{label}</dt>
                  <dd className="font-mono mt-1 break-all">{value}</dd>
                </div>
              ))}
            </dl>
            <p className="text-13 text-secondary">
              Reported status remains unchanged until an agent supplies another observation. Unknown coverage stays
              unknown; older evidence is labelled separately.
            </p>
            {!!build.links.length && (
              <div className="flex flex-wrap gap-4 text-13">
                {build.links.map((link) => (
                  <ExternalLinkButton key={`${link.title}:${link.url}`} url={link.url}>
                    {link.title}
                  </ExternalLinkButton>
                ))}
              </div>
            )}
            <section className="space-y-4" aria-label="Test results">
              {build.results.map((result) => (
                <article key={resultKey(result)} className="rounded-lg border border-subtle bg-surface-1 p-4">
                  <div className="flex flex-wrap justify-between gap-3">
                    <div>
                      <h2 className="text-16 font-medium">
                        {result.module_id ? (
                          <Link to={`${base}/modules/${result.module_id}/builds`} className="text-accent-primary">
                            {result.module_name ?? result.module_id}
                          </Link>
                        ) : (
                          "Project aggregate"
                        )}{" "}
                        / {result.suite}
                      </h2>
                      <p className="mt-1 text-13 text-secondary">
                        {result.test_type} · scope: {result.scope}
                      </p>
                      <p className="mt-1 text-11 text-tertiary">
                        Exclusions: {result.exclusions.length ? result.exclusions.join(", ") : "None"}
                      </p>
                    </div>
                    <p className="text-13">
                      {result.tests
                        ? `${result.tests.passed ?? "—"} passed · ${result.tests.failed ?? "—"} failed · ${result.tests.skipped ?? "—"} skipped`
                        : "Tests: Not measured"}
                    </p>
                  </div>
                  <div className="mt-4 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                    {COVERAGE_KINDS.map((kind) => {
                      const metric = result.coverage[kind];
                      return (
                        <div key={kind} className="rounded-md border border-subtle p-3">
                          <h3 className="mb-2 text-11 font-medium text-tertiary capitalize">{kind} coverage</h3>
                          <CoverageValue
                            metric={metric}
                            delta={coverageDelta(build, result, kind, history.data?.results ?? [])}
                          />
                          {metric && (
                            <dl className="mt-3 space-y-1 text-11 text-tertiary">
                              <div>
                                <dt className="inline">Tool: </dt>
                                <dd className="inline">{metric.tool}</dd>
                              </div>
                              <div>
                                <dt className="inline">Measured: </dt>
                                <dd className="inline">{formatBuildTime(metric.measured_at)}</dd>
                              </div>
                              <div>
                                <dt className="inline">Revision: </dt>
                                <dd className="font-mono inline break-all">{metric.revision}</dd>
                              </div>
                              <div>
                                <dt className="inline">Evidence: </dt>
                                <dd className="inline break-all">
                                  {safeExternalUrl(metric.source) ? (
                                    <ExternalLinkButton url={metric.source}>Report</ExternalLinkButton>
                                  ) : (
                                    metric.source
                                  )}
                                </dd>
                              </div>
                            </dl>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </article>
              ))}
              {!build.results.length && <p className="text-secondary">No module results reported · Not measured</p>}
            </section>
            <p className="text-11 text-tertiary">
              Coverage deltas use only earlier reports with matching repository, ref, workflow, module, suite, test
              type, scope, exclusions and tool. Independent measurements are never averaged.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
