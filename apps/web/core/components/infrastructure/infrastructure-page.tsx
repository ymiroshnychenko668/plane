/* oxlint-disable react/no-array-index-key -- Immutable evidence/reference arrays have no item IDs and no local item state. */
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { observer } from "mobx-react";
import useSWR from "swr";
import { ArrowLeft, Plus, RefreshCw } from "lucide-react";
import { Badge } from "@plane/propel/badge";
import { Button } from "./button";
import type {
  TInfrastructureEvidence,
  TInfrastructurePage,
  TInfrastructureRelation,
  TInfrastructureResource,
  TInfrastructureSource,
} from "@plane/types";
import { PageHead } from "@/components/core/page-title";
import { useUserPermissions } from "@/hooks/store/user";
import {
  AttachEditor,
  ConfirmEditor,
  Field,
  fieldClass,
  infrastructureService as service,
  RelationEditor,
  ResourceEditor,
} from "./editors";
import type { InfrastructureScope } from "./editors";
import {
  changeFilter,
  formatTimestamp,
  infrastructureError,
  isProjectAdmin,
  labelFor,
  LIFECYCLES,
  nextPage,
  RESOURCE_KINDS,
  resourceFilters,
} from "./helpers";

function useScope() {
  const { workspaceSlug = "", projectId = "", resourceId = "" } = useParams();
  const permissions = useUserPermissions();
  return {
    scope: { workspace: workspaceSlug, project: projectId },
    resourceId,
    base: `/${workspaceSlug}/projects/${projectId}/infrastructure`,
    admin: isProjectAdmin(permissions.getProjectRoleByWorkspaceSlugAndProjectId(workspaceSlug, projectId)),
  };
}
export function InfrastructureError({ error, refresh }: { error: unknown; refresh: () => void }) {
  return (
    <div role="alert" className="space-y-3 rounded-lg border border-subtle bg-surface-1 p-4">
      <h2 className="text-16 font-medium">Infrastructure is unavailable</h2>
      <p className="text-13 break-words text-secondary">{infrastructureError(error)}</p>
      <Button variant="secondary" size="sm" onClick={refresh}>
        Retry
      </Button>
    </div>
  );
}
export function Pagination({
  data,
  cursor,
  first,
  next,
  label,
  loading,
}: {
  data?: TInfrastructurePage<unknown>;
  cursor?: string | null;
  first: () => void;
  next: () => void;
  label: string;
  loading?: boolean;
}) {
  return (
    <nav aria-label={`${label} pages`} className="mt-3 flex flex-wrap justify-end gap-2">
      <Button variant="secondary" size="sm" disabled={!cursor || loading} onClick={first}>
        First page
      </Button>
      <Button variant="secondary" size="sm" disabled={!data?.has_more || !data.next_cursor || loading} onClick={next}>
        Next page
      </Button>
    </nav>
  );
}
export function ResourceCard({ resource, base }: { resource: TInfrastructureResource; base: string }) {
  return (
    <article
      className="min-w-0 space-y-3 rounded-lg border border-subtle bg-surface-1 p-4"
      data-testid="infrastructure-resource-card"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <Link
            className="text-16 font-medium break-words text-accent-primary hover:underline"
            to={`${base}/${resource.id}`}
          >
            {resource.name}
          </Link>
          <p className="font-mono mt-1 text-11 break-all text-tertiary">{resource.key}</p>
        </div>
        <Badge variant="neutral">{resource.lifecycle}</Badge>
      </div>
      <p className="text-13 text-secondary">
        {labelFor(resource.kind)} · {resource.owner.name}
      </p>
      {resource.description && <p className="text-13 break-words text-secondary">{resource.description}</p>}
      <p className="text-11 text-tertiary">Last verified: {formatTimestamp(resource.last_verified)}</p>
      <div className="flex flex-wrap gap-2 text-11 text-tertiary">
        {resource.tags.map((tag) => (
          <span key={tag} className="max-w-full rounded bg-surface-2 px-2 py-1 break-all">
            {tag}
          </span>
        ))}
      </div>
    </article>
  );
}
function Evidence({
  source,
  evidence,
  verified,
}: {
  source: TInfrastructureSource;
  evidence: TInfrastructureEvidence[];
  verified: string | null;
}) {
  return (
    <div className="min-w-0 space-y-3 text-13">
      <dl className="grid min-w-0 gap-3 sm:grid-cols-2">
        <Meta label="Source kind" value={source.kind} />
        <Meta label="Source reference" value={source.reference} />
        <Meta label="Source revision" value={source.revision} />
        <Meta label="Last verified" value={formatTimestamp(verified)} />
      </dl>
      {evidence.length ? (
        <ul className="space-y-2" aria-label="Evidence entries">
          {evidence.map((entry, index) => (
            <li key={`${entry.reference}:${index}`} className="rounded border border-subtle p-3 break-all">
              <p>
                {entry.kind} · {entry.reference}
              </p>
              <p className="mt-1 text-11 text-tertiary">Observed: {formatTimestamp(entry.observed_at)}</p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-secondary">No evidence recorded.</p>
      )}
    </div>
  );
}
function Meta({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="min-w-0">
      <dt className="text-11 text-tertiary">{label}</dt>
      <dd className="mt-1 text-13 break-all">{value}</dd>
    </div>
  );
}
export function Relations({
  scope,
  base,
  admin,
  resource,
}: {
  scope: InfrastructureScope;
  base: string;
  admin: boolean;
  resource?: TInfrastructureResource;
}) {
  const [cursor, setCursor] = useState<string>();
  const [kind, setKind] = useState<TInfrastructureRelation["kind"] | "">("");
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<TInfrastructureRelation>();
  const filters = {
    ...(resource ? { resource_id: resource.id } : {}),
    ...(kind ? { kind } : {}),
    ...(cursor ? { cursor } : {}),
  };
  const list = useSWR(
    ["infrastructure-relations", scope.workspace, scope.project, filters],
    () => service.listRelations(scope.workspace, scope.project, filters),
    { shouldRetryOnError: false }
  );
  const saved = async () => {
    setCursor(undefined);
    await list.mutate();
  };
  return (
    <section className="min-w-0 space-y-3" aria-label="Relationships">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-18 font-semibold">Relationships</h2>
          <p className="mt-1 text-13 text-secondary">Directed links recorded in this project.</p>
        </div>
        {admin && (
          <Button variant="secondary" size="sm" onClick={() => setAdding(true)}>
            Add relationship
          </Button>
        )}
      </header>
      <div className="max-w-xs">
        <Field label="Relationship filter">
          <select
            className={fieldClass}
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as typeof kind);
              setCursor(undefined);
            }}
          >
            <option value="">All relationships</option>
            <option value="depends_on">depends on</option>
            <option value="runs_on">runs on</option>
          </select>
        </Field>
      </div>
      {list.error ? (
        <InfrastructureError
          error={list.error}
          refresh={() => {
            void list.mutate();
          }}
        />
      ) : !list.data ? (
        <p role="status" className="text-13 text-secondary">
          Loading relationships…
        </p>
      ) : !list.data.results.length ? (
        <p className="rounded-lg border border-subtle p-4 text-13 text-secondary">
          No relationships recorded for these filters. Add resources and describe how they depend on each other.
        </p>
      ) : (
        <ul className="space-y-3">
          {list.data.results.map((relation) => (
            <li key={relation.id} className="min-w-0 rounded-lg border border-subtle bg-surface-1 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="flex min-w-0 flex-wrap gap-2 text-13">
                  <Link className="break-all text-accent-primary hover:underline" to={`${base}/${relation.source_id}`}>
                    {relation.source_name}
                    <span className="ml-1 text-11 text-tertiary">({relation.source_key})</span>
                  </Link>
                  <span className="text-secondary">{labelFor(relation.kind)} →</span>
                  <Link className="break-all text-accent-primary hover:underline" to={`${base}/${relation.target_id}`}>
                    {relation.target_name}
                    <span className="ml-1 text-11 text-tertiary">({relation.target_key})</span>
                  </Link>
                </p>
                {relation.can_manage && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => setRemoving(relation)}
                    aria-label={`Remove relationship ${relation.id}`}
                  >
                    Remove
                  </Button>
                )}
              </div>
              <details className="mt-3">
                <summary className="cursor-pointer text-11 text-secondary">
                  Relationship evidence · revision {relation.revision}
                </summary>
                <div className="mt-3">
                  <Evidence source={relation.source} evidence={relation.evidence} verified={relation.last_verified} />
                </div>
              </details>
            </li>
          ))}
        </ul>
      )}
      <Pagination
        data={list.data}
        cursor={cursor}
        loading={list.isLoading}
        label="Relationship"
        first={() => setCursor(undefined)}
        next={() => setCursor(list.data?.next_cursor ?? undefined)}
      />
      {adding && <RelationEditor scope={scope} resource={resource} saved={saved} close={() => setAdding(false)} />}
      {removing && (
        <ConfirmEditor
          title="Remove relationship"
          description="Remove this project's inventory relationship. Resource records and real infrastructure are unaffected."
          close={() => setRemoving(undefined)}
          action={async () => {
            await service.deleteRelation(scope.workspace, scope.project, removing.id, removing.revision);
            await saved();
          }}
        />
      )}
    </section>
  );
}
export const InfrastructurePage = observer(function InfrastructurePage() {
  const { scope, base, admin } = useScope();
  const [search, setSearch] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const navigate = useNavigate();
  const filters = resourceFilters(search);
  const list = useSWR(
    ["infrastructure-resources", scope.workspace, scope.project, filters],
    () => service.listResources(scope.workspace, scope.project, filters),
    { shouldRetryOnError: false }
  );
  function filter(key: string, value: string) {
    setSearch((previous) => changeFilter(previous, key, value));
  }
  return (
    <div className="h-full min-w-0 overflow-y-auto bg-surface-2 p-3 sm:p-6" data-testid="infrastructure-page">
      <PageHead title="Infrastructure" />
      <div className="mx-auto max-w-7xl min-w-0 space-y-6">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-11 text-tertiary">Project / Infrastructure</p>
            <h1 className="mt-1 text-24 font-semibold">Infrastructure</h1>
            <p className="mt-1 max-w-2xl text-13 text-secondary">
              Hosts, virtual machines, services, databases, storage and networks serving this project. Inventory
              evidence does not imply current connectivity or permission.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                void list.mutate();
              }}
            >
              <RefreshCw className="mr-1.5 size-3.5" />
              Refresh
            </Button>
            {admin && !list.error && (
              <Button size="sm" onClick={() => setAdding(true)}>
                <Plus className="mr-1.5 size-3.5" />
                Add resource
              </Button>
            )}
          </div>
        </header>
        <section className="space-y-4" aria-label="Resources">
          <div className="grid min-w-0 gap-3 sm:grid-cols-2 lg:grid-cols-4" aria-label="Resource filters">
            <Field label="Search resources">
              <input
                className={fieldClass}
                maxLength={200}
                value={search.get("search") ?? ""}
                placeholder="Name or stable key"
                onChange={(e) => filter("search", e.target.value)}
              />
            </Field>
            <Field label="Kind filter">
              <select
                className={fieldClass}
                value={search.get("kind") ?? ""}
                onChange={(e) => filter("kind", e.target.value)}
              >
                <option value="">All kinds</option>
                {RESOURCE_KINDS.map((kind) => (
                  <option key={kind} value={kind}>
                    {labelFor(kind)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Lifecycle filter">
              <select
                className={fieldClass}
                value={search.get("lifecycle") ?? ""}
                onChange={(e) => filter("lifecycle", e.target.value)}
              >
                <option value="">All lifecycles</option>
                {LIFECYCLES.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
            <Field label="Tag filters">
              <input
                className={fieldClass}
                value={search.get("tags") ?? ""}
                placeholder="monitoring, shared"
                onChange={(e) => filter("tags", e.target.value)}
              />
            </Field>
          </div>
          {search.get("key") && (
            <p className="text-13 break-all text-secondary">
              Exact key: {search.get("key")}{" "}
              <button type="button" className="ml-2 text-accent-primary underline" onClick={() => filter("key", "")}>
                Clear exact key
              </button>
            </p>
          )}
          {list.error ? (
            <InfrastructureError
              error={list.error}
              refresh={() => {
                void list.mutate();
              }}
            />
          ) : !list.data ? (
            <p role="status" className="text-13 text-secondary">
              Loading infrastructure…
            </p>
          ) : !list.data.results.length ? (
            <div className="space-y-2 rounded-lg border border-subtle bg-surface-1 p-6">
              <h2 className="text-16 font-medium">No resources found</h2>
              <p className="text-13 text-secondary">
                Adjust the filters or ask a project administrator to add resources or share an existing inventory
                record.
              </p>
            </div>
          ) : (
            <div className="grid min-w-0 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {list.data.results.map((resource) => (
                <ResourceCard key={resource.id} resource={resource} base={base} />
              ))}
            </div>
          )}
          <Pagination
            data={list.data}
            cursor={search.get("cursor")}
            loading={list.isLoading}
            label="Resource"
            first={() => setSearch((previous) => nextPage(previous, null))}
            next={() => setSearch((previous) => nextPage(previous, list.data?.next_cursor ?? null))}
          />
        </section>
        {!list.error && (
          <Relations key={`${scope.workspace}:${scope.project}`} scope={scope} base={base} admin={admin} />
        )}
        {adding && (
          <ResourceEditor
            scope={scope}
            close={() => setAdding(false)}
            saved={(resource) => {
              navigate(`${base}/${resource.id}`);
            }}
          />
        )}
      </div>
    </div>
  );
});
function ProjectLinks({
  scope,
  resource,
  updated,
  removed,
}: {
  scope: InfrastructureScope;
  resource: TInfrastructureResource;
  updated: (resource?: TInfrastructureResource) => Promise<unknown>;
  removed: () => void;
}) {
  const [cursor, setCursor] = useState<string>();
  const [attaching, setAttaching] = useState(false);
  const [detach, setDetach] = useState<string>();
  const links = useSWR(
    ["infrastructure-links", scope.workspace, scope.project, resource.id, resource.revision, cursor],
    () => service.listLinks(scope.workspace, scope.project, resource.id, { cursor }),
    { shouldRetryOnError: false }
  );
  return (
    <section className="space-y-3" aria-label="Project links">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-18 font-semibold">Serves projects</h2>
        {resource.can_manage && (
          <Button variant="secondary" size="sm" onClick={() => setAttaching(true)}>
            Share with a project
          </Button>
        )}
        {resource.can_detach && (
          <Button variant="secondary" size="sm" onClick={() => setDetach(scope.project)}>
            Remove from this project
          </Button>
        )}
      </header>
      <p className="text-13 text-secondary">
        Only project links you can read are shown. Shared resources retain one stable ID; project-specific relationships
        are separate.
      </p>
      {links.error ? (
        <InfrastructureError
          error={links.error}
          refresh={() => {
            void links.mutate();
          }}
        />
      ) : !links.data ? (
        <p role="status" className="text-13 text-secondary">
          Loading project links…
        </p>
      ) : !links.data.results.length ? (
        <p className="text-13 text-secondary">No readable project links on this page.</p>
      ) : (
        <ul className="space-y-2">
          {links.data.results.map((link) => (
            <li
              key={link.id}
              className="flex min-w-0 flex-wrap items-center justify-between gap-3 rounded border border-subtle bg-surface-1 p-3"
            >
              <div className="min-w-0">
                <Link
                  className="text-13 break-all text-accent-primary hover:underline"
                  to={`/${scope.workspace}/projects/${link.project_id}/infrastructure/${resource.id}`}
                >
                  {link.project_name}
                  {link.project_id === scope.project ? " (this project)" : ""}
                </Link>
                <p className="text-11 text-tertiary">
                  {link.is_owner ? "Ownership project" : "Consumer project"} · serves project
                </p>
              </div>
              {!link.is_owner && resource.can_manage && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setDetach(link.project_id)}
                  aria-label={`Remove project link ${link.project_id}`}
                >
                  Remove link
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      <Pagination
        data={links.data}
        cursor={cursor}
        loading={links.isLoading}
        label="Project link"
        first={() => setCursor(undefined)}
        next={() => setCursor(links.data?.next_cursor ?? undefined)}
      />
      {attaching && (
        <AttachEditor
          scope={scope}
          resource={resource}
          close={() => setAttaching(false)}
          saved={async (result) => {
            setCursor(undefined);
            await updated(result);
          }}
        />
      )}
      {detach && (
        <ConfirmEditor
          title="Remove project link"
          description="Remove this project's access to this inventory record and its incident relationships. This changes inventory visibility, not infrastructure permissions. Removing another project's link requires active admin membership in that project."
          close={() => setDetach(undefined)}
          action={async () => {
            await service.detachProject(scope.workspace, scope.project, resource.id, detach, resource.revision);
            if (detach === scope.project) removed();
            else {
              setCursor(undefined);
              await updated();
            }
          }}
        />
      )}
    </section>
  );
}
export const InfrastructureDetailPage = observer(function InfrastructureDetailPage() {
  const { scope, base, admin, resourceId } = useScope();
  const [editing, setEditing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const navigate = useNavigate();
  const detail = useSWR(
    ["infrastructure-resource", scope.workspace, scope.project, resourceId],
    () => service.getResource(scope.workspace, scope.project, resourceId),
    { shouldRetryOnError: false }
  );
  const resource = detail.data;
  const updated = async (result?: TInfrastructureResource) =>
    result ? detail.mutate(result, { revalidate: false }) : detail.mutate();
  return (
    <div className="h-full min-w-0 overflow-y-auto bg-surface-2 p-3 sm:p-6" data-testid="infrastructure-detail-page">
      <PageHead title={resource?.name ?? "Infrastructure resource"} />
      <div className="mx-auto max-w-5xl min-w-0 space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Link className="inline-flex items-center gap-2 text-13 text-accent-primary" to={base}>
            <ArrowLeft className="size-4" />
            Infrastructure
          </Link>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              void detail.mutate();
            }}
          >
            Refresh resource
          </Button>
        </div>
        {detail.error ? (
          <InfrastructureError
            error={detail.error}
            refresh={() => {
              void detail.mutate();
            }}
          />
        ) : !resource ? (
          <p role="status">Loading resource…</p>
        ) : (
          <>
            <header className="flex min-w-0 flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <h1 className="text-24 font-semibold break-words">{resource.name}</h1>
                <p className="font-mono mt-1 text-13 break-all text-secondary">{resource.key}</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Badge variant="neutral">{labelFor(resource.kind)}</Badge>
                  <Badge variant="neutral">{resource.lifecycle}</Badge>
                </div>
              </div>
              {resource.can_manage && (
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" onClick={() => setEditing(true)}>
                    Edit resource
                  </Button>
                  <Button variant="secondary" size="sm" onClick={() => setRemoving(true)}>
                    Delete resource
                  </Button>
                </div>
              )}
            </header>
            <p className="text-13 break-words text-secondary">{resource.description || "No description recorded."}</p>
            <dl className="grid min-w-0 gap-4 rounded-lg border border-subtle bg-surface-1 p-4 sm:grid-cols-2">
              <Meta label="Stable resource ID" value={resource.id} />
              <Meta label="Record revision" value={resource.revision} />
              <Meta label="Owner" value={resource.owner.name} />
              <Meta label="Owner reference" value={resource.owner.reference} />
              <Meta label="Recovery contact" value={resource.owner.recovery_contact} />
              <Meta
                label="Ownership project"
                value={resource.ownership_project_id ?? "Not visible in your project scope"}
              />
              <Meta label="Created" value={formatTimestamp(resource.created_at)} />
              <Meta label="Updated" value={formatTimestamp(resource.updated_at)} />
              <Meta label="Tags" value={resource.tags.join(", ") || "None"} />
            </dl>
            {!resource.can_manage && (
              <p className="text-13 text-secondary">
                Shared metadata can be edited only by an administrator through the ownership project.
              </p>
            )}
            <section
              className="space-y-3 rounded-lg border border-subtle bg-surface-1 p-4"
              aria-label="Source and evidence"
            >
              <h2 className="text-18 font-semibold">Source and evidence</h2>
              <Evidence source={resource.source} evidence={resource.evidence} verified={resource.last_verified} />
            </section>
            <section className="space-y-3" aria-label="Access methods">
              <h2 className="text-18 font-semibold">Access methods</h2>
              <p className="text-13 text-secondary">
                Recorded metadata only. Use your assigned identity and permissions. Credential references do not reveal
                secret values or grant access.
              </p>
              {!resource.access_methods.length ? (
                <p className="rounded-lg border border-subtle p-4 text-13 text-secondary">
                  No access methods recorded. Contact the resource owner for the assigned access route.
                </p>
              ) : (
                resource.access_methods.map((method) => (
                  <article
                    key={method.id}
                    className="min-w-0 space-y-4 rounded-lg border border-subtle bg-surface-1 p-4"
                  >
                    <h3 className="text-16 font-medium break-all">
                      {method.id} · {method.kind}
                    </h3>
                    <dl className="grid min-w-0 gap-3 sm:grid-cols-2">
                      <Meta label="Endpoint" value={method.endpoint} />
                      <Meta label="Capability" value={method.capability} />
                      <Meta label="Operations" value={method.operations.join(", ")} />
                      <Meta label="Scope" value={method.scope} />
                      <Meta label="Identity selector" value={method.identity} />
                    </dl>
                    {method.notes && <p className="text-13 break-words text-secondary">{method.notes}</p>}
                    <h4 className="text-13 font-medium">Logical OpenBao references</h4>
                    {method.credential_refs?.length ? (
                      <ul className="space-y-3">
                        {method.credential_refs.map((ref, index) => (
                          <li
                            key={`${ref.path}:${ref.field}:${index}`}
                            className="min-w-0 rounded border border-subtle p-3"
                          >
                            <dl className="grid min-w-0 gap-2 sm:grid-cols-2">
                              <Meta label="Provider" value={ref.provider} />
                              <Meta label="Logical path" value={ref.path} />
                              <Meta label="Field" value={ref.field} />
                              <Meta label="Purpose" value={ref.purpose} />
                            </dl>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-13 text-secondary">No credential references recorded.</p>
                    )}
                  </article>
                ))
              )}
            </section>
            <Relations
              key={`relations:${scope.workspace}:${scope.project}:${resource.id}`}
              scope={scope}
              base={base}
              admin={admin}
              resource={resource}
            />
            <ProjectLinks
              key={`links:${scope.workspace}:${scope.project}:${resource.id}`}
              scope={scope}
              resource={resource}
              updated={updated}
              removed={() => navigate(base)}
            />
            {editing && (
              <ResourceEditor
                key={`editor:${scope.workspace}:${scope.project}:${resource.id}`}
                scope={scope}
                resource={resource}
                close={() => setEditing(false)}
                saved={updated}
              />
            )}
            {removing && (
              <ConfirmEditor
                title="Delete resource record"
                description="Delete this inventory record and its owner-project relationships. Resources still linked to other projects cannot be deleted. Real infrastructure is not changed and the stable key remains reserved."
                close={() => setRemoving(false)}
                action={async () => {
                  await service.deleteResource(scope.workspace, scope.project, resource.id, resource.revision);
                  navigate(base);
                }}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
});
