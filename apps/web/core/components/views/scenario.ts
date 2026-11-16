import type { IProjectView, TWorkItemFilterExpression } from "@plane/types";

export type TViewScenario = "inactive" | "unassigned" | "inactive_unassigned";
export type TScenarioDefinition = Pick<IProjectView, "rich_filters" | "filter_meta">;
export const ACTIVITY_POLICY = "meaningful_activity_v1";
export const SOURCE_SCHEMA = "task_text_v1";

export const isScenarioView = (view: Partial<IProjectView>) =>
  (!!view.filter_meta && Object.keys(view.filter_meta).length > 0) ||
  /meaningful_activity__|assignee_id__is_empty|is_archived__exact|is_draft__exact/.test(
    JSON.stringify(view.rich_filters)
  );

export function createScenarioDefinition(scenario: TViewScenario, days: number): TScenarioDefinition {
  if (scenario !== "unassigned" && (!Number.isInteger(days) || days < 1 || days > 36500)) {
    throw new Error("Enter a whole number of days between 1 and 36500.");
  }
  const conditions: Record<string, string | boolean | number>[] = [
    { state_group__in: "backlog,unstarted,started" },
    { is_archived__exact: false },
    { is_draft__exact: false },
  ];
  if (scenario !== "unassigned") conditions.push({ meaningful_activity__older_than_days: days });
  if (scenario !== "inactive") conditions.push({ assignee_id__is_empty: true });
  return {
    rich_filters: { and: conditions } as TWorkItemFilterExpression,
    filter_meta:
      scenario === "unassigned"
        ? { version: 1 }
        : {
            version: 1,
            policy_version: ACTIVITY_POLICY,
            source_schema_version: SOURCE_SCHEMA,
          },
  };
}

/** Strict parsing prevents an unknown expression being silently replaced by a preset. */
export function readScenario(view: Partial<IProjectView>): { scenario: TViewScenario; days: number } | null {
  const expression = view.rich_filters as Record<string, unknown> | undefined;
  const meta = view.filter_meta;
  if (
    !meta ||
    meta.version !== 1 ||
    !expression ||
    Object.keys(expression).length !== 1 ||
    !Array.isArray(expression.and)
  )
    return null;
  const conditions: Record<string, unknown> = {};
  for (const condition of expression.and) {
    if (!condition || typeof condition !== "object" || Array.isArray(condition) || Object.keys(condition).length !== 1)
      return null;
    const [key, value] = Object.entries(condition)[0];
    if (Object.hasOwn(conditions, key)) return null;
    conditions[key] = value;
  }
  const days = conditions.meaningful_activity__older_than_days;
  const unassigned = conditions.assignee_id__is_empty;
  const scenario: TViewScenario =
    days !== undefined ? (unassigned === true ? "inactive_unassigned" : "inactive") : "unassigned";
  if (days === undefined && unassigned !== true) return null;
  if (days !== undefined && (typeof days !== "number" || !Number.isInteger(days) || days < 1 || days > 36500))
    return null;
  const canonical = createScenarioDefinition(scenario, typeof days === "number" ? days : 7);
  const expected = Object.assign({}, ...(canonical.rich_filters as { and: Record<string, unknown>[] }).and);
  if (
    Object.keys(expected).length !== Object.keys(conditions).length ||
    Object.entries(expected).some(([key, value]) => conditions[key] !== value)
  )
    return null;
  if (
    Object.keys(meta).length !== Object.keys(canonical.filter_meta ?? {}).length ||
    Object.entries(canonical.filter_meta ?? {}).some(([key, value]) => meta[key] !== value)
  )
    return null;
  return { scenario, days: typeof days === "number" ? days : 7 };
}

export function viewErrorMessage(error: unknown): string {
  const response = (
    error as { response?: { status?: number; data?: { detail?: string; error?: string | { detail?: string } } } }
  )?.response;
  if (response?.status === 409 || response?.status === 412)
    return "This View or assessment changed. Reload it and review the latest version before saving again.";
  if (response?.status === 403) return "You do not have permission to access these View results or assessments.";
  const serverError = response?.data?.error;
  return (
    response?.data?.detail ??
    (typeof serverError === "string" ? serverError : serverError?.detail) ??
    (error instanceof Error ? error.message : "Could not complete the request. Try again.")
  );
}

export function activityReason(reason: string): string {
  const labels: Record<string, string> = {
    insufficient_coverage: "History does not cover the full period",
    incomplete_coverage: "History does not cover the full period",
    pending_assessment: "Source events need review",
    pending_review: "Source events need review",
    uncertain_time: "Source time needs review",
    no_meaningful_activity: "No confirmed meaningful activity during the period",
    older_than_cutoff: "Last meaningful activity is older than the threshold",
    recent_activity: "Recent meaningful activity",
    recent_meaningful_activity: "Recent meaningful activity",
    recent_creation: "Work item was created within the period",
    capture_unavailable: "Activity capture is unavailable",
    capture_missing: "Activity history has not been captured",
    observation_window_incomplete: "History does not cover the full period",
    existence_window_incomplete: "The work item's age is not yet established for the full period",
    assessment_incomplete: "Source events need review",
    no_meaningful_activity_in_window: "No confirmed meaningful activity during the period",
  };
  return labels[reason] ?? reason.replaceAll("_", " ");
}
