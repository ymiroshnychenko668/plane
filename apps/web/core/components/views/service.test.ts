import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TActivityCandidate } from "@/services/activity-assessment.service";
import { ActivityAssessmentService } from "@/services/activity-assessment.service";
import { ViewService } from "@/services/view.service";
import { ProjectViewStore } from "@/store/project-view.store";
import type { IProjectView } from "@plane/types";
import { createScenarioDefinition } from "./scenario";

const calls = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/services/api.service", () => ({
  APIService: class {
    get = calls.get;
    post = calls.post;
    patch = calls.patch;
    delete = calls.delete;
  },
}));
const service = new ViewService();
const assessments = new ActivityAssessmentService();
const base = "/api/workspaces/work/projects/project/views/";
beforeEach(() => {
  for (const call of Object.values(calls)) call.mockReset().mockResolvedValue({ data: {} });
});

describe("native project View browser contract", () => {
  it("evaluates saved Views through the View endpoint with the review partition and cursor", async () => {
    await service.evaluateView("work", "project", "view", { partition: "needs_review", cursor: "next" });
    expect(calls.get).toHaveBeenCalledWith(`${base}view/work-items/`, {
      params: { partition: "needs_review", cursor: "next", per_page: 50 },
      signal: undefined,
    });
  });
  it("previews an unsaved definition with metadata and list layout", async () => {
    const definition = createScenarioDefinition("inactive_unassigned", 14);
    await service.previewView("work", "project", definition, { partition: "matches" });
    expect(calls.post).toHaveBeenCalledWith(
      `${base}preview/`,
      { ...definition, display_filters: { layout: "list" }, partition: "matches", per_page: 50 },
      { signal: undefined }
    );
  });
  it("requires the observed revision for metadata-only mixed edits and delete", async () => {
    await service.patchView("work", "project", "view", { name: "Renamed", revision: 8 }, 8);
    expect(calls.patch).toHaveBeenCalledWith(`${base}view/`, { name: "Renamed" }, { headers: { "If-Match": '"8"' } });
    await service.deleteView("work", "project", "view", 9);
    expect(calls.delete).toHaveBeenCalledWith(`${base}view/`, undefined, { headers: { "If-Match": '"9"' } });
  });
  it("does not retry or erase status when CAS rejects a stale update", async () => {
    const error = { response: { status: 412 } };
    calls.patch.mockRejectedValue(error);
    await expect(service.patchView("work", "project", "view", { name: "Stale" }, 1)).rejects.toBe(error);
    expect(calls.patch).toHaveBeenCalledTimes(1);
  });
  it("retains the observed data when a mixed edit conflicts and stores the returned revision on success", async () => {
    const store = new ProjectViewStore({} as never);
    store.viewMap.view = { id: "view", name: "Current", revision: 4 } as IProjectView;
    calls.patch.mockRejectedValueOnce({ response: { status: 412 } });
    await expect(
      store.updateView("work", "project", "view", { name: "Stale draft", revision: 3 })
    ).rejects.toBeDefined();
    expect(store.getViewById("view").name).toBe("Current");
    expect(calls.patch).toHaveBeenLastCalledWith(
      `${base}view/`,
      { name: "Stale draft" },
      { headers: { "If-Match": '"3"' } }
    );
    calls.patch.mockResolvedValueOnce({ data: { id: "view", name: "Saved", revision: 5 } });
    await store.updateView("work", "project", "view", { name: "Saved" });
    expect(store.getViewById("view").revision).toBe(5);
  });
  it("retracts only the observed assessment with a reason and request identity", async () => {
    const candidate = { assessment: { id: "assessment", revision: 8 } } as TActivityCandidate;
    await assessments.retract("work", "project", "issue", candidate, {
      rationale: "Evidence was corrected",
      request_id: "request",
    });
    expect(calls.post).toHaveBeenCalledWith(
      "/api/workspaces/work/projects/project/work-items/issue/activity-assessments/assessment/retract/",
      { reason: "Evidence was corrected", request_id: "request" },
      { headers: { "If-Match": '"8"' } }
    );
  });
  it("reads candidates without creating assessments and writes the observed source/assessment revision", async () => {
    await assessments.summary("work", "project", "issue");
    expect(calls.get).toHaveBeenCalledWith(
      "/api/workspaces/work/projects/project/work-items/issue/activity-assessments/summary/",
      { params: { policy_version: "meaningful_activity_v1" } }
    );
    await assessments.candidates("work", "project", "issue", "next");
    expect(calls.post).not.toHaveBeenCalled();
    expect(calls.get).toHaveBeenCalledWith(
      "/api/workspaces/work/projects/project/work-items/issue/activity-assessments/candidates/",
      { params: { cursor: "next", per_page: 20, policy_version: "meaningful_activity_v1" } }
    );
    const candidate = { event_id: "event", source_revision: 3, assessment: { revision: 7 } } as TActivityCandidate;
    const data = {
      decision: "meaningful" as const,
      category: "investigation" as const,
      rationale: "A root cause was identified",
      request_id: "request",
    };
    await assessments.assess("work", "project", "issue", candidate, data);
    expect(calls.post).toHaveBeenCalledWith(
      "/api/workspaces/work/projects/project/work-items/issue/activity-assessments/assess/",
      { ...data, event_id: "event", source_revision: 3, policy_version: "meaningful_activity_v1" },
      { headers: { "If-Match": '"7"' } }
    );
    await assessments.assess("work", "project", "issue", { ...candidate, assessment: null }, data);
    expect(calls.post).toHaveBeenLastCalledWith(expect.any(String), expect.any(Object), {
      headers: { "If-Match": '"0"' },
    });
  });
});
