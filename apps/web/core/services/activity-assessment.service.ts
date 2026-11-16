import { API_BASE_URL } from "@plane/constants";
import { APIService } from "@/services/api.service";

export type TActivityDecision = "meaningful" | "routine" | "needs_review";
export type TActivityCategory =
  | "implementation"
  | "investigation"
  | "verification"
  | "decision"
  | "blocker"
  | "routine"
  | "unknown";
export type TActivityCandidate = {
  event_id: string;
  source_kind: string;
  source_revision: number;
  source_content: Record<string, unknown>;
  before_content: Record<string, unknown>;
  occurred_at: string | null;
  observed_at: string;
  time_quality: "native" | "import" | "unattributed";
  is_baseline: boolean;
  assessment: {
    id: string;
    revision: number;
    decision: TActivityDecision;
    category: TActivityCategory;
    rationale: string;
    policy_version: string;
  } | null;
};

export class ActivityAssessmentService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  private base(workspace: string, project: string, issue: string) {
    return `/api/workspaces/${encodeURIComponent(workspace)}/projects/${encodeURIComponent(project)}/work-items/${encodeURIComponent(issue)}/activity-assessments/`;
  }

  async summary(
    workspace: string,
    project: string,
    issue: string
  ): Promise<{
    last_meaningful_at: string | null;
    capture_started_at: string | null;
    pending_count: number;
    policy_version: string;
    source_schema_version: string;
  }> {
    return (
      await this.get(`${this.base(workspace, project, issue)}summary/`, {
        params: { policy_version: "meaningful_activity_v1" },
      })
    ).data;
  }

  async candidates(
    workspace: string,
    project: string,
    issue: string,
    cursor?: string
  ): Promise<{
    results: TActivityCandidate[];
    next_page_results: boolean;
    next_cursor: string | null;
  }> {
    return (
      await this.get(`${this.base(workspace, project, issue)}candidates/`, {
        params: { cursor, per_page: 20, policy_version: "meaningful_activity_v1" },
      })
    ).data;
  }

  async assess(
    workspace: string,
    project: string,
    issue: string,
    candidate: TActivityCandidate,
    data: { decision: TActivityDecision; category: TActivityCategory; rationale: string; request_id: string }
  ) {
    return (
      await this.post(
        `${this.base(workspace, project, issue)}assess/`,
        {
          event_id: candidate.event_id,
          source_revision: candidate.source_revision,
          policy_version: "meaningful_activity_v1",
          ...data,
        },
        { headers: { "If-Match": `"${candidate.assessment?.revision ?? 0}"` } }
      )
    ).data;
  }

  async retract(
    workspace: string,
    project: string,
    issue: string,
    candidate: TActivityCandidate,
    data: { rationale: string; request_id: string }
  ) {
    if (!candidate.assessment) throw new Error("Reload the event before retracting its assessment.");
    return (
      await this.post(
        `${this.base(workspace, project, issue)}${encodeURIComponent(candidate.assessment.id)}/retract/`,
        { request_id: data.request_id, reason: data.rationale },
        { headers: { "If-Match": `"${candidate.assessment.revision}"` } }
      )
    ).data;
  }
}
