import { cloneElement, useId, useState } from "react";
import { observer } from "mobx-react";
import { useProject } from "@/hooks/store/use-project";
import { useUserPermissions } from "@/hooks/store/user";
import type { FormEvent, ReactElement, ReactNode } from "react";
import { Dialog } from "@headlessui/react";
import { X } from "lucide-react";
import { Button } from "./button";
import type {
  TInfrastructureResource,
  TInfrastructureResourceWrite,
  TInfrastructureRelationWrite,
  TInfrastructureSource,
} from "@plane/types";
import { InfrastructureService } from "@/services/infrastructure.service";
import {
  exactResource,
  isProjectAdmin,
  infrastructureError,
  labelFor,
  LIFECYCLES,
  parseAccessMethods,
  parseEvidence,
  parseLastVerified,
  RESOURCE_KINDS,
  SOURCE_KINDS,
} from "./helpers";

export const infrastructureService = new InfrastructureService();
export const fieldClass =
  "min-w-0 w-full rounded-md border border-subtle bg-surface-1 px-3 py-2 text-13 text-primary outline-none focus:border-accent-primary disabled:opacity-60";
export type InfrastructureScope = { workspace: string; project: string };
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactElement<{ id?: string; "aria-describedby"?: string }>;
  hint?: string;
}) {
  const generatedId = useId();
  const id = children.props.id ?? generatedId;
  return (
    <div className="flex min-w-0 flex-col gap-1.5 text-13 text-secondary">
      <label htmlFor={id}>{label}</label>
      {cloneElement(children, { id, "aria-describedby": hint ? `${id}-hint` : children.props["aria-describedby"] })}
      {hint && (
        <span id={`${id}-hint`} className="text-11 text-tertiary">
          {hint}
        </span>
      )}
    </div>
  );
}
export function EditorDialog({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  return (
    <Dialog open onClose={close} className="relative z-50">
      <div className="fixed inset-0 bg-black/30" aria-hidden="true" />
      <div className="fixed inset-0 flex items-center justify-center overflow-y-auto p-3 sm:p-6">
        <Dialog.Panel className="shadow-xl max-h-full w-full max-w-2xl min-w-0 overflow-y-auto rounded-xl border border-subtle bg-surface-1 p-4 sm:p-6">
          <div className="mb-5 flex items-center justify-between gap-3">
            <Dialog.Title className="text-18 font-semibold">{title}</Dialog.Title>
            <button
              type="button"
              aria-label="Close editor"
              onClick={close}
              className="rounded p-2 text-tertiary hover:bg-surface-2"
            >
              <X className="size-4" />
            </button>
          </div>
          {children}
        </Dialog.Panel>
      </div>
    </Dialog>
  );
}
function useSave(close: () => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async (operation: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await operation();
      close();
    } catch (cause) {
      setError(infrastructureError(cause, true));
    } finally {
      setBusy(false);
    }
  };
  return { save, busy, error };
}
function Actions({
  busy,
  error,
  close,
  label = "Save changes",
}: {
  busy: boolean;
  error: string;
  close: () => void;
  label?: string;
}) {
  return (
    <>
      {error && (
        <p role="alert" className="rounded border border-subtle bg-surface-2 p-3 text-13 break-words">
          {error}
        </p>
      )}
      <div className="flex flex-wrap justify-end gap-2 border-t border-subtle pt-4">
        <Button variant="secondary" size="sm" onClick={close} disabled={busy}>
          Cancel
        </Button>
        <Button type="submit" size="sm" disabled={busy}>
          {busy ? "Saving…" : label}
        </Button>
      </div>
    </>
  );
}
function SourceFields({
  source,
  setSource,
}: {
  source: TInfrastructureSource;
  setSource: (value: TInfrastructureSource) => void;
}) {
  return (
    <fieldset className="grid min-w-0 gap-3 rounded-lg border border-subtle p-3 sm:grid-cols-2">
      <legend className="px-1 text-13 font-medium">Source</legend>
      <Field label="Source kind">
        <select
          className={fieldClass}
          value={source.kind}
          onChange={(e) => setSource({ ...source, kind: e.target.value as TInfrastructureSource["kind"] })}
        >
          {SOURCE_KINDS.map((kind) => (
            <option key={kind}>{kind}</option>
          ))}
        </select>
      </Field>
      <Field label="Source revision" hint="Revision of the evidence source, separate from the record revision.">
        <input
          required
          maxLength={255}
          className={fieldClass}
          value={source.revision}
          onChange={(e) => setSource({ ...source, revision: e.target.value })}
        />
      </Field>
      <div className="sm:col-span-2">
        <Field label="Source reference">
          <input
            required
            maxLength={1024}
            className={fieldClass}
            placeholder="repo:infrastructure/monitoring"
            value={source.reference}
            onChange={(e) => setSource({ ...source, reference: e.target.value })}
          />
        </Field>
      </div>
    </fieldset>
  );
}
function EvidenceFields({
  evidence,
  setEvidence,
  verified,
  setVerified,
}: {
  evidence: string;
  setEvidence: (value: string) => void;
  verified: string;
  setVerified: (value: string) => void;
}) {
  return (
    <>
      <Field
        label="Evidence JSON"
        hint={
          'Array of {"kind":"review","reference":"doc:review","observed_at":"2026-01-01T00:00:00Z"}. No secret values.'
        }
      >
        <textarea
          rows={5}
          className={`${fieldClass} font-mono`}
          spellCheck={false}
          value={evidence}
          onChange={(e) => setEvidence(e.target.value)}
        />
      </Field>
      <Field
        label="Last verified"
        hint="Optional ISO 8601 timestamp with timezone; requires evidence. An observation does not grant access."
      >
        <input
          className={fieldClass}
          placeholder="2026-01-01T00:00:00Z"
          value={verified}
          onChange={(e) => setVerified(e.target.value)}
        />
      </Field>
    </>
  );
}
export function ResourceEditor({
  scope,
  resource: initialResource,
  close,
  saved,
}: {
  scope: InfrastructureScope;
  resource?: TInfrastructureResource;
  close: () => void;
  saved: (resource: TInfrastructureResource) => Promise<unknown> | void;
}) {
  // Freeze the observed revision with the draft; SWR revalidation must not rebase it.
  const [resource] = useState(initialResource);
  const [key, setKey] = useState(resource?.key ?? "");
  const [name, setName] = useState(resource?.name ?? "");
  const [kind, setKind] = useState<TInfrastructureResource["kind"]>(resource?.kind ?? "service");
  const [lifecycle, setLifecycle] = useState<TInfrastructureResource["lifecycle"]>(resource?.lifecycle ?? "planned");
  const [description, setDescription] = useState(resource?.description ?? "");
  const [tags, setTags] = useState(resource?.tags.join(", ") ?? "");
  const [owner, setOwner] = useState(resource?.owner ?? { name: "", reference: "", recovery_contact: "" });
  const [source, setSource] = useState<TInfrastructureSource>(
    resource?.source ?? { kind: "manual", reference: "", revision: "" }
  );
  const [evidence, setEvidence] = useState(JSON.stringify(resource?.evidence ?? [], null, 2));
  const [verified, setVerified] = useState(resource?.last_verified ?? "");
  const [access, setAccess] = useState(JSON.stringify(resource?.access_methods ?? [], null, 2));
  const { save, busy, error } = useSave(close);
  function submit(event: FormEvent) {
    event.preventDefault();
    void save(async () => {
      const parsedEvidence = parseEvidence(evidence);
      const data: TInfrastructureResourceWrite = {
        key,
        name,
        kind,
        lifecycle,
        description,
        tags: [
          ...new Set(
            tags
              .split(",")
              .map((tag) => tag.trim().toLowerCase())
              .filter(Boolean)
          ),
        ],
        owner,
        source,
        evidence: parsedEvidence,
        last_verified: parseLastVerified(verified, parsedEvidence),
        access_methods: parseAccessMethods(access),
      };
      const result = resource
        ? await infrastructureService.updateResource(
            scope.workspace,
            scope.project,
            resource.id,
            data,
            resource.revision
          )
        : await infrastructureService.createResource(scope.workspace, scope.project, data);
      await saved(result);
    });
  }
  return (
    <EditorDialog title={resource ? "Edit resource" : "Add resource"} close={close}>
      <form onSubmit={submit} className="space-y-4">
        <p className="text-13 text-secondary">
          Record infrastructure metadata and logical credential references only. Saving inventory does not connect to
          infrastructure or grant access.
        </p>
        <div className="grid min-w-0 gap-3 sm:grid-cols-2">
          <Field label="Stable resource key" hint="Unique in this workspace; immutable after creation.">
            <input
              required
              maxLength={160}
              className={fieldClass}
              value={key}
              disabled={!!resource}
              onChange={(e) => setKey(e.target.value)}
              placeholder="monitoring/grafana"
            />
          </Field>
          <Field label="Name">
            <input
              required
              maxLength={255}
              className={fieldClass}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <Field label="Resource kind">
            <select
              className={fieldClass}
              value={kind}
              disabled={!!resource}
              onChange={(e) => setKind(e.target.value as TInfrastructureResource["kind"])}
            >
              {RESOURCE_KINDS.map((value) => (
                <option key={value} value={value}>
                  {labelFor(value)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Lifecycle">
            <select
              className={fieldClass}
              value={lifecycle}
              onChange={(e) => setLifecycle(e.target.value as TInfrastructureResource["lifecycle"])}
            >
              {LIFECYCLES.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Description">
          <textarea
            maxLength={4000}
            rows={3}
            className={fieldClass}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field>
        <Field label="Tags" hint="Comma-separated lowercase slugs, up to 20.">
          <input className={fieldClass} value={tags} onChange={(e) => setTags(e.target.value)} />
        </Field>
        <fieldset className="grid min-w-0 gap-3 rounded-lg border border-subtle p-3">
          <legend className="px-1 text-13 font-medium">Owner and recovery</legend>
          <Field label="Owner name">
            <input
              required
              maxLength={255}
              className={fieldClass}
              value={owner.name}
              onChange={(e) => setOwner({ ...owner, name: e.target.value })}
            />
          </Field>
          <Field label="Owner reference">
            <input
              required
              maxLength={512}
              className={fieldClass}
              value={owner.reference}
              placeholder="team:operations"
              onChange={(e) => setOwner({ ...owner, reference: e.target.value })}
            />
          </Field>
          <Field label="Recovery contact">
            <input
              required
              maxLength={512}
              className={fieldClass}
              value={owner.recovery_contact}
              placeholder="role:supervisor"
              onChange={(e) => setOwner({ ...owner, recovery_contact: e.target.value })}
            />
          </Field>
        </fieldset>
        <SourceFields source={source} setSource={setSource} />
        <EvidenceFields evidence={evidence} setEvidence={setEvidence} verified={verified} setVerified={setVerified} />
        <Field
          label="Access methods JSON"
          hint="Structured metadata only. Required: id, kind, endpoint, capability, operations, scope, identity. Optional: credential_refs and notes."
        >
          <textarea
            rows={7}
            className={`${fieldClass} font-mono`}
            spellCheck={false}
            value={access}
            onChange={(e) => setAccess(e.target.value)}
          />
        </Field>
        <details className="min-w-0 text-11 text-secondary">
          <summary className="cursor-pointer">Access metadata example</summary>
          <pre className="mt-2 rounded border border-subtle p-3 break-all whitespace-pre-wrap">
            {JSON.stringify(
              [
                {
                  id: "grafana-api",
                  kind: "https",
                  endpoint: "https://grafana.example.invalid",
                  capability: "grafana",
                  operations: ["dashboards.read"],
                  scope: "monitoring organization",
                  identity: "runtime-own-identity",
                  credential_refs: [
                    {
                      provider: "openbao",
                      path: "projects/example/monitoring/grafana",
                      field: "api_token",
                      purpose: "Read monitoring metadata",
                    },
                  ],
                },
              ],
              null,
              2
            )}
          </pre>
        </details>
        <Actions busy={busy} error={error} close={close} label={resource ? "Save changes" : "Create resource"} />
      </form>
    </EditorDialog>
  );
}
export function RelationEditor({
  scope,
  resource,
  close,
  saved,
}: {
  scope: InfrastructureScope;
  resource?: TInfrastructureResource;
  close: () => void;
  saved: () => Promise<unknown> | void;
}) {
  const [sourceKey, setSourceKey] = useState(resource?.key ?? "");
  const [targetKey, setTargetKey] = useState("");
  const [kind, setKind] = useState<TInfrastructureRelationWrite["kind"]>("depends_on");
  const [source, setSource] = useState<TInfrastructureSource>({ kind: "manual", reference: "", revision: "" });
  const [evidence, setEvidence] = useState("[]");
  const [verified, setVerified] = useState("");
  const { save, busy, error } = useSave(close);
  function submit(event: FormEvent) {
    event.preventDefault();
    void save(async () => {
      const parsedEvidence = parseEvidence(evidence);
      const lastVerified = parseLastVerified(verified, parsedEvidence);
      const [sources, targets] = await Promise.all([
        infrastructureService.listResources(scope.workspace, scope.project, { key: sourceKey }),
        infrastructureService.listResources(scope.workspace, scope.project, { key: targetKey }),
      ]);
      await infrastructureService.createRelation(scope.workspace, scope.project, {
        source_id: exactResource(sources.results, sourceKey).id,
        target_id: exactResource(targets.results, targetKey).id,
        kind,
        source,
        evidence: parsedEvidence,
        last_verified: lastVerified,
      });
      await saved();
    });
  }
  return (
    <EditorDialog title="Add relationship" close={close}>
      <form className="space-y-4" onSubmit={submit}>
        <p className="text-13 text-secondary">
          Relationships belong to this project. Both resource keys must resolve here. Self-links, retired endpoints,
          duplicates and cycles are rejected.
        </p>
        <Field label="Source resource key">
          <input
            required
            maxLength={160}
            className={fieldClass}
            value={sourceKey}
            onChange={(e) => setSourceKey(e.target.value)}
            placeholder="monitoring/grafana"
          />
        </Field>
        <Field label="Relationship kind">
          <select
            className={fieldClass}
            value={kind}
            onChange={(e) => setKind(e.target.value as TInfrastructureRelationWrite["kind"])}
          >
            <option value="depends_on">depends on</option>
            <option value="runs_on">runs on</option>
          </select>
        </Field>
        <Field label="Target resource key">
          <input
            required
            maxLength={160}
            className={fieldClass}
            value={targetKey}
            onChange={(e) => setTargetKey(e.target.value)}
            placeholder="monitoring/vm"
          />
        </Field>
        <SourceFields source={source} setSource={setSource} />
        <EvidenceFields evidence={evidence} setEvidence={setEvidence} verified={verified} setVerified={setVerified} />
        <Actions busy={busy} error={error} close={close} label="Create relationship" />
      </form>
    </EditorDialog>
  );
}
export const AttachEditor = observer(function AttachEditor({
  scope,
  resource: initialResource,
  close,
  saved,
}: {
  scope: InfrastructureScope;
  resource: TInfrastructureResource;
  close: () => void;
  saved: (resource: TInfrastructureResource) => Promise<unknown> | void;
}) {
  const [resource] = useState(initialResource);
  const projects = useProject();
  const permissions = useUserPermissions();
  const candidates = (projects.workspaceProjectIds ?? [])
    .filter(
      (id) =>
        id !== scope.project &&
        isProjectAdmin(permissions.getProjectRoleByWorkspaceSlugAndProjectId(scope.workspace, id))
    )
    .map((id) => projects.getProjectById(id))
    .filter((project) => !!project);
  const [target, setTarget] = useState("");
  const { save, busy, error } = useSave(close);
  return (
    <EditorDialog title="Share with a project" close={close}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void save(async () => {
            const result = await infrastructureService.attachProject(
              scope.workspace,
              scope.project,
              resource.id,
              target,
              resource.revision
            );
            await saved(result);
          });
        }}
      >
        <p className="text-13 text-secondary">
          The target project will see this resource's full metadata and logical credential references. The resource
          keeps the same ID. This does not grant infrastructure access. You must be an active administrator in both
          projects.
        </p>
        <Field
          label="Administered project"
          hint="Only loaded projects where you are an administrator are offered. The server rechecks membership."
        >
          <select
            className={fieldClass}
            value={candidates.some((project) => project.id === target) ? target : ""}
            onChange={(event) => setTarget(event.target.value)}
          >
            <option value="">Select a project or enter its ID below</option>
            {candidates.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Target project ID">
          <input
            required
            className={fieldClass}
            pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
          />
        </Field>
        <Actions busy={busy} error={error} close={close} label="Share resource" />
      </form>
    </EditorDialog>
  );
});
export function ConfirmEditor({
  title,
  description,
  action: initialAction,
  close,
}: {
  title: string;
  description: string;
  action: () => Promise<unknown>;
  close: () => void;
}) {
  // Confirmation applies to the record revision the user saw when opening it.
  const [action] = useState(() => initialAction);
  const { save, busy, error } = useSave(close);
  return (
    <EditorDialog title={title} close={close}>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void save(action);
        }}
      >
        <p className="text-13 break-words text-secondary">{description}</p>
        <Actions busy={busy} error={error} close={close} label="Confirm removal" />
      </form>
    </EditorDialog>
  );
}
