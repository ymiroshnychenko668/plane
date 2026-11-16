import { useRef, useState } from "react";
import useSWR from "swr";
import { EUserPermissions, EUserPermissionsLevel } from "@plane/constants";
import { Button } from "@plane/propel/button";
import { useUserPermissions } from "@/hooks/store/user";
import { ActivityAssessmentService } from "@/services/activity-assessment.service";
import type { TActivityCandidate, TActivityCategory, TActivityDecision } from "@/services/activity-assessment.service";
import { viewErrorMessage } from "./scenario";

const service = new ActivityAssessmentService();
const fieldClass = "w-full rounded-sm border border-subtle bg-surface-1 p-2 text-14";
const categories: TActivityCategory[] = [
  "implementation",
  "investigation",
  "verification",
  "decision",
  "blocker",
  "routine",
  "unknown",
];
type Props = { workspaceSlug: string; projectId: string; issueId: string; onAssessed: () => void };

function CandidateReview({
  candidate,
  workspaceSlug,
  projectId,
  issueId,
  onAssessed,
}: Props & { candidate: TActivityCandidate }) {
  const [decision, setDecision] = useState<TActivityDecision>(candidate.assessment?.decision ?? "needs_review");
  const [category, setCategory] = useState<TActivityCategory>(candidate.assessment?.category ?? "unknown");
  const [rationale, setRationale] = useState(candidate.assessment?.rationale ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lastRequest = useRef<{ payload: string; id: string } | null>(null);
  const { allowPermissions } = useUserPermissions();
  const canAssess = allowPermissions(
    [EUserPermissions.ADMIN, EUserPermissions.MEMBER],
    EUserPermissionsLevel.PROJECT,
    workspaceSlug,
    projectId
  );
  const validCategory =
    decision === "meaningful"
      ? !["routine", "unknown"].includes(category)
      : category === (decision === "routine" ? "routine" : "unknown");
  const save = async (retract = false) => {
    if (!rationale.trim() || !canAssess || (!retract && !validCategory)) return;
    setBusy(true);
    setError(null);
    const payload = JSON.stringify({ decision, category, rationale: rationale.trim(), retract });
    if (lastRequest.current?.payload !== payload) lastRequest.current = { payload, id: crypto.randomUUID() };
    try {
      const data = { decision, category, rationale: rationale.trim(), request_id: lastRequest.current.id };
      if (retract) await service.retract(workspaceSlug, projectId, issueId, candidate, data);
      else await service.assess(workspaceSlug, projectId, issueId, candidate, data);
      onAssessed();
    } catch (cause) {
      setError(viewErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  };
  const id = `activity-${candidate.event_id}`;
  return (
    <fieldset className="space-y-3 rounded-sm border border-subtle p-3" disabled={busy || !canAssess}>
      <legend className="px-1 font-medium">
        {candidate.source_kind} · source revision {candidate.source_revision}
      </legend>
      <p className="text-12 text-tertiary">
        {candidate.occurred_at ? new Date(candidate.occurred_at).toLocaleString() : "Occurrence time unknown"}
        {` · ${candidate.time_quality}`}
        {candidate.is_baseline ? " · baseline observation" : ""}
      </p>
      <details>
        <summary className="cursor-pointer">Previous source content</summary>
        <pre className="max-h-48 overflow-auto text-12 break-words whitespace-pre-wrap">
          {JSON.stringify(candidate.before_content, null, 2)}
        </pre>
      </details>
      <p className="font-medium">Current source content</p>
      <pre className="max-h-64 overflow-auto text-12 break-words whitespace-pre-wrap">
        {JSON.stringify(candidate.source_content, null, 2)}
      </pre>
      <div className="grid gap-3 sm:grid-cols-2">
        <label htmlFor={`${id}-decision`}>
          Assessment
          <select
            id={`${id}-decision`}
            className={fieldClass}
            value={decision}
            onChange={(event) => {
              const nextDecision = event.target.value as TActivityDecision;
              setDecision(nextDecision);
              if (nextDecision !== "meaningful") setCategory(nextDecision === "routine" ? "routine" : "unknown");
            }}
          >
            <option value="needs_review">Needs review</option>
            <option value="meaningful">Meaningful activity</option>
            <option value="routine">Routine activity</option>
          </select>
        </label>
        <label htmlFor={`${id}-category`}>
          Category
          <select
            id={`${id}-category`}
            className={fieldClass}
            value={category}
            onChange={(event) => setCategory(event.target.value as TActivityCategory)}
          >
            {categories.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="block" htmlFor={`${id}-rationale`}>
        Reason for this assessment
        <textarea
          id={`${id}-rationale`}
          className={fieldClass}
          value={rationale}
          onChange={(event) => setRationale(event.target.value)}
          rows={3}
          maxLength={4000}
          placeholder="Describe what changed and why it is meaningful, routine, or uncertain."
        />
      </label>
      {error && (
        <p role="alert" className="text-danger-primary">
          {error}
        </p>
      )}
      {!validCategory && (
        <p role="status" className="text-12">
          Choose a category that describes the meaningful work.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          disabled={!rationale.trim() || busy || !canAssess || !validCategory}
          onClick={() => void save()}
        >
          Save assessment
        </Button>
        {candidate.assessment && (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={!rationale.trim() || busy || !canAssess}
            onClick={() => void save(true)}
          >
            Retract assessment
          </Button>
        )}
      </div>
    </fieldset>
  );
}

export function ActivityReview(props: Props) {
  const { workspaceSlug, projectId, issueId, onAssessed } = props;
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const cursor = cursors[cursors.length - 1];
  const summary = useSWR(["activity-summary", workspaceSlug, projectId, issueId], () =>
    service.summary(workspaceSlug, projectId, issueId)
  );
  const candidates = useSWR(["activity-candidates", workspaceSlug, projectId, issueId, cursor], () =>
    service.candidates(workspaceSlug, projectId, issueId, cursor)
  );
  const refresh = () => {
    void summary.mutate();
    void candidates.mutate();
  };
  return (
    <div className="mt-3 space-y-3 rounded-sm bg-surface-2 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">Review meaningful activity</h3>
        <Button type="button" variant="secondary" size="sm" onClick={refresh}>
          Reload assessments
        </Button>
      </div>
      <p className="text-12 text-tertiary">
        Assess the work described in comments and task descriptions. Implementation, investigation, verification,
        decisions and explained blockers can count. Pings, assignments and status changes alone do not. Leave uncertain
        evidence as needs review. Imported or baseline text does not prove when the work happened.
      </p>
      {summary.data && (
        <p className="text-12">
          {summary.data.pending_count} event(s) need review · capture began{" "}
          {summary.data.capture_started_at
            ? new Date(summary.data.capture_started_at).toLocaleString()
            : "at an unknown time"}{" "}
          · {summary.data.policy_version}
        </p>
      )}
      {(summary.error || candidates.error) && (
        <p role="alert" className="text-danger-primary">
          {viewErrorMessage(summary.error || candidates.error)}
        </p>
      )}
      {candidates.isLoading && <p role="status">Loading source events…</p>}
      {candidates.data?.results.map((candidate) => (
        <CandidateReview
          key={`${candidate.event_id}-${candidate.source_revision}-${candidate.assessment?.revision ?? 0}`}
          {...props}
          candidate={candidate}
          onAssessed={() => {
            refresh();
            onAssessed();
          }}
        />
      ))}
      {candidates.data?.results.length === 0 && (
        <p>No source events on this page. This does not establish complete activity history.</p>
      )}
      {candidates.data && (
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={cursors.length === 1 || candidates.isValidating}
            onClick={() => setCursors(cursors.slice(0, -1))}
          >
            Previous events
          </Button>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={!candidates.data.next_page_results || !candidates.data.next_cursor || candidates.isValidating}
            onClick={() => {
              if (candidates.data?.next_cursor) setCursors([...cursors, candidates.data.next_cursor]);
            }}
          >
            Next events
          </Button>
        </div>
      )}
    </div>
  );
}
