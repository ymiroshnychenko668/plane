import { useState } from "react";
import { Link } from "react-router";
import useSWR from "swr";
import { Button } from "@plane/propel/button";
import type { IProjectView, TProjectViewPartition } from "@plane/types";
import { useProject } from "@/hooks/store/use-project";
import { ViewService } from "@/services/view.service";
import { ActivityReview } from "./activity-review";
import { activityReason, isScenarioView, readScenario, viewErrorMessage } from "./scenario";

const service = new ViewService();

type Props = {
  workspaceSlug: string;
  projectId: string;
  view?: IProjectView;
  definition?: Pick<IProjectView, "rich_filters" | "filter_meta">;
};

export function ScenarioViewResults({ workspaceSlug, projectId, view, definition }: Props) {
  const [partition, setPartition] = useState<TProjectViewPartition>("matches");
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [reviewing, setReviewing] = useState<string | null>(null);
  const { getProjectById } = useProject();
  const project = getProjectById(projectId);
  const configured = view ?? definition;
  const supported = configured && readScenario(configured);
  const cursor = cursors[cursors.length - 1];
  const { data, error, isLoading, isValidating, mutate } = useSWR(
    configured && isScenarioView(configured)
      ? ["project-view-evaluation", workspaceSlug, projectId, view?.id, view?.revision, definition, partition, cursor]
      : null,
    () =>
      view
        ? service.evaluateView(workspaceSlug, projectId, view.id, { partition, cursor })
        : service.previewView(workspaceSlug, projectId, definition!, { partition, cursor }),
    { revalidateOnFocus: false }
  );
  if (!configured || !isScenarioView(configured)) return null;
  const meaningful = supported ? supported.scenario !== "unassigned" : !!configured.filter_meta?.policy_version;

  const selectPartition = (next: TProjectViewPartition) => {
    setPartition(next);
    setCursors([undefined]);
    setReviewing(null);
  };

  return (
    <section
      aria-label={view ? "View work items" : "Unsaved View preview"}
      className="flex h-full min-w-0 flex-col overflow-auto p-4 text-14"
    >
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-medium">{view ? view.name : "Unsaved preview"}</h2>
          {supported ? (
            <>
              <p className="text-tertiary">Open work items · excludes archived items and drafts · list layout</p>
              <p className="text-tertiary">
                {meaningful ? `No meaningful activity for more than ${supported.days} days` : "No assignee"}
                {supported.scenario === "inactive_unassigned" ? " · no assignee" : ""}
              </p>
            </>
          ) : (
            <p className="text-tertiary">Custom saved conditions · list layout</p>
          )}
        </div>
        <Button type="button" variant="secondary" size="sm" disabled={isValidating} onClick={() => void mutate()}>
          Refresh results
        </Button>
      </div>
      {!supported && (
        <p role="status" className="mb-3 rounded-sm border border-subtle p-3">
          This editor cannot change these conditions. The original definition is preserved and evaluated by the server.
        </p>
      )}
      {error && (
        <p role="alert" className="mb-3 text-danger-primary">
          {viewErrorMessage(error)}
        </p>
      )}
      {isLoading && <p role="status">Evaluating work items…</p>}
      {data && !error && (
        <>
          <div className="mb-3 flex flex-wrap gap-2" role="group" aria-label="Result partition">
            <Button
              type="button"
              variant={partition === "matches" ? "primary" : "secondary"}
              size="sm"
              onClick={() => selectPartition("matches")}
            >
              Matches ({data.counts.matches})
            </Button>
            {meaningful && (
              <Button
                type="button"
                variant={partition === "needs_review" ? "primary" : "secondary"}
                size="sm"
                onClick={() => selectPartition("needs_review")}
              >
                Needs review ({data.counts.needs_review})
              </Button>
            )}
          </div>
          {meaningful && (!data.assessment_complete || data.counts.needs_review > 0) && (
            <p role="status" className="mb-3 rounded-sm border border-subtle bg-surface-2 p-3">
              {data.counts.needs_review} work item(s) still need review. A lack of confirmed matches does not mean every
              work item has recent meaningful activity.
            </p>
          )}
          <p className="mb-3 text-12 text-tertiary">
            Evaluated {new Date(data.evaluated_at).toLocaleString()}
            {data.cutoff_at ? ` · cutoff ${new Date(data.cutoff_at).toLocaleString()}` : ""}. Results are live and may
            change between pages.
          </p>
          {data.results.length === 0 ? (
            <p className="py-4">
              {partition === "needs_review"
                ? "No work items in this review partition."
                : "No confirmed matches in this partition."}
            </p>
          ) : (
            <ul className="divide-y divide-subtle border-y border-subtle">
              {data.results.map((item) => (
                <li key={item.id} className="py-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <Link
                      className="min-w-0 font-medium break-words text-primary hover:underline"
                      to={`/${workspaceSlug}/projects/${projectId}/issues/${item.id}`}
                    >
                      {project?.identifier ?? "#"}-{item.sequence_id} {item.name}
                    </Link>
                    {meaningful && view && (
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        aria-expanded={reviewing === item.id}
                        onClick={() => setReviewing(reviewing === item.id ? null : item.id)}
                      >
                        Review activity
                      </Button>
                    )}
                  </div>
                  {meaningful && (
                    <div className="mt-1 text-12 text-tertiary">
                      <p>
                        {item.activity?.last_meaningful_at
                          ? `Last confirmed meaningful activity: ${new Date(item.activity.last_meaningful_at).toLocaleString()}`
                          : "No confirmed meaningful activity date"}
                      </p>
                      <p>
                        {item.activity?.reason
                          ? activityReason(item.activity.reason)
                          : "Activity evidence is not available"}
                      </p>
                    </div>
                  )}
                  {reviewing === item.id && view && (
                    <ActivityReview
                      workspaceSlug={workspaceSlug}
                      projectId={projectId}
                      issueId={item.id}
                      onAssessed={() => void mutate()}
                    />
                  )}
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={cursors.length === 1 || isValidating}
              onClick={() => setCursors(cursors.slice(0, -1))}
            >
              Previous page
            </Button>
            <span>Page {cursors.length}</span>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              disabled={!data.next_page_results || !data.next_cursor || isValidating}
              onClick={() => {
                if (data.next_cursor) setCursors([...cursors, data.next_cursor]);
              }}
            >
              Next page
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
