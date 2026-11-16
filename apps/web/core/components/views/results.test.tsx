import { createElement } from "react";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { IProjectView, TProjectViewEvaluation } from "@plane/types";
import { ScenarioViewResults } from "./scenario-results";
import { createScenarioDefinition } from "./scenario";

const state = vi.hoisted(() => ({
  data: undefined as TProjectViewEvaluation | undefined,
  error: undefined as unknown,
  keys: [] as unknown[],
}));
vi.mock("swr", () => ({
  default: (key: unknown) => {
    state.keys.push(key);
    return { data: state.data, error: state.error, isLoading: !state.data && !state.error, mutate: vi.fn() };
  },
}));
vi.mock("@/hooks/store/use-project", () => ({
  useProject: () => ({ getProjectById: () => ({ identifier: "DEMO" }) }),
}));
vi.mock("./activity-review", () => ({ ActivityReview: () => null }));
vi.mock("react-router", () => ({
  Link: ({ to, children }: { to: string; children: ReactNode }) => createElement("a", { href: to }, children),
}));
vi.mock("@/services/api.service", () => ({
  APIService: class {
    baseURL = "";
  },
}));
vi.mock("@plane/propel/button", () => ({
  Button: ({
    children,
    variant: _variant,
    size: _size,
    ...props
  }: {
    children: ReactNode;
    variant?: string;
    size?: string;
  }) => createElement("button", props, children),
}));

const view = {
  id: "view",
  name: "Inactive work",
  revision: 4,
  ...createScenarioDefinition("inactive", 7),
} as IProjectView;
const render = (current = view) =>
  renderToStaticMarkup(
    createElement(ScenarioViewResults, { workspaceSlug: "work", projectId: "project", view: current })
  );
beforeEach(() => {
  state.keys = [];
  state.error = undefined;
  state.data = {
    results: [],
    next_page_results: false,
    next_cursor: null,
    counts: { matches: 0, needs_review: 3 },
    assessment_complete: false,
    evaluated_at: "2026-10-30T12:00:00Z",
    cutoff_at: "2026-10-23T12:00:00Z",
    policy_version: "meaningful_activity_v1",
    view_revision: 4,
    consistency: "live",
    counts_observed_at: "2026-10-30T12:00:00Z",
  };
});

describe("project View result states", () => {
  it("shows pending review explicitly even when there are no confirmed matches", () => {
    const html = render();
    expect(html).toContain("Needs review (3)");
    expect(html).toContain("does not mean every work item has recent meaningful activity");
    expect(html).toContain("No confirmed matches in this partition.");
    expect(html).toContain("Results are live");
  });
  it("keeps unrecognized definitions read-only and surfaces evaluator errors without widening filters", () => {
    state.data = undefined;
    state.error = { response: { status: 400, data: { error: { detail: "Unsupported policy version" } } } };
    const html = render({ ...view, filter_meta: { version: 2 } });
    expect(html).toContain("original definition is preserved");
    expect(state.keys[0]).toEqual(expect.arrayContaining(["project-view-evaluation", "view", 4]));
    expect(html).toContain("Unsupported policy version");
    expect(html).not.toContain("No confirmed matches");
  });
  it("does not turn a forbidden or loading response into a zero count", () => {
    state.data = undefined;
    state.error = { response: { status: 403 } };
    expect(render()).toContain("do not have permission");
    expect(render()).not.toContain("Matches (0)");
    state.error = undefined;
    expect(render()).toContain("Evaluating work items");
  });
  it("keeps plain unassigned independent of activity warnings and review controls", () => {
    const html = render({ ...view, ...createScenarioDefinition("unassigned", 7) });
    expect(html).toContain("No assignee");
    expect(html).not.toContain("Needs review");
    expect(html).not.toContain("Review activity");
  });
  it("escapes issue text and shows the activity reason/date with its native issue link", () => {
    state.data!.results = [
      {
        id: "issue",
        name: "<script>unsafe</script>",
        project_id: "project",
        state_id: "state",
        sequence_id: 7,
        priority: "high",
        assignee_ids: [],
        activity: { match: null, reason: "pending_assessment", last_meaningful_at: null },
      },
    ];
    const html = render();
    expect(html).toContain("/work/projects/project/issues/issue");
    expect(html).toContain("&lt;script&gt;unsafe&lt;/script&gt;");
    expect(html).toContain("Source events need review");
    expect(html).toContain("No confirmed meaningful activity date");
  });
});
