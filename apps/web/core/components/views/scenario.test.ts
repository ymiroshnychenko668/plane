import { describe, expect, it } from "vitest";
import type { IProjectView } from "@plane/types";
import { ACTIVITY_POLICY, createScenarioDefinition, isScenarioView, readScenario, viewErrorMessage } from "./scenario";

describe("project View scenario definitions", () => {
  it("uses relative meaningful activity with explicit open, non-archived, non-draft scope", () => {
    const definition = createScenarioDefinition("inactive", 7);
    expect(definition).toEqual({
      filter_meta: { version: 1, policy_version: ACTIVITY_POLICY, source_schema_version: "task_text_v1" },
      rich_filters: {
        and: [
          { state_group__in: "backlog,unstarted,started" },
          { is_archived__exact: false },
          { is_draft__exact: false },
          { meaningful_activity__older_than_days: 7 },
        ],
      },
    });
    expect(readScenario(definition)).toEqual({ scenario: "inactive", days: 7 });
  });
  it("keeps plain unassigned independent of activity history, age and policy", () => {
    const definition = createScenarioDefinition("unassigned", Number.NaN);
    expect(definition.filter_meta).toEqual({ version: 1 });
    expect(JSON.stringify(definition)).not.toContain("meaningful_activity");
    expect(JSON.stringify(definition)).not.toContain("older_than");
    expect(readScenario(definition)?.scenario).toBe("unassigned");
    expect(readScenario(createScenarioDefinition("inactive_unassigned", 30))).toEqual({
      scenario: "inactive_unassigned",
      days: 30,
    });
  });
  it.each([0, -1, 0.5, 36501, Number.NaN, Number.POSITIVE_INFINITY])("rejects invalid day count %s", (days) => {
    expect(() => createScenarioDefinition("inactive", days)).toThrow();
  });
  it("preserves unknown expressions without offering a destructive preset conversion", () => {
    const original = createScenarioDefinition("inactive", 7);
    const future = {
      ...original,
      rich_filters: { and: [...(original.rich_filters as { and: unknown[] }).and, { future_condition__exact: true }] },
    } as Partial<IProjectView>;
    const snapshot = JSON.stringify(future);
    expect(isScenarioView(future)).toBe(true);
    expect(readScenario(future)).toBeNull();
    expect(JSON.stringify(future)).toBe(snapshot);
    expect(
      readScenario({ ...original, filter_meta: { ...original.filter_meta, policy_version: "future_v2" } })
    ).toBeNull();
    expect(readScenario({ ...original, rich_filters: { or: [] } as IProjectView["rich_filters"] })).toBeNull();
    expect(
      readScenario({
        ...original,
        rich_filters: { and: [{ meaningful_activity__older_than_days: 7 }] } as IProjectView["rich_filters"],
      })
    ).toBeNull();
  });
  it("does not capture legacy custom Views or silently accept missing metadata", () => {
    expect(isScenarioView({ rich_filters: { priority__in: "high" } })).toBe(false);
    expect(isScenarioView({ rich_filters: {} })).toBe(false);
    const invalid = { ...createScenarioDefinition("inactive", 7), filter_meta: undefined };
    expect(isScenarioView(invalid)).toBe(true);
    expect(readScenario(invalid)).toBeNull();
  });
  it("tells a user to reload a conflicting revision instead of retrying it blindly", () => {
    expect(viewErrorMessage({ response: { status: 412 } })).toContain("Reload");
    expect(viewErrorMessage({ response: { status: 403 } })).toContain("permission");
    expect(
      viewErrorMessage({
        response: { status: 400, data: { error: { code: "validation_error", detail: "Unsupported layout" } } },
      })
    ).toBe("Unsupported layout");
  });
});
