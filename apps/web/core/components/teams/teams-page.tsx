import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useParams } from "react-router";
import { Dialog } from "@headlessui/react";
import { Bot, Check, Copy, Hash, Plus, RefreshCw, Search, Users, X } from "lucide-react";
import useSWR from "swr";
import { Badge } from "@plane/propel/badge";
import { Button } from "@plane/propel/button";
import type { TProjectTeam, TTeamActor, TTeamContact, TTeamMember, TTeamRuntime, TTeamTarget } from "@plane/types";
import { PageHead } from "@/components/core/page-title";
import { TeamService } from "@/services/team.service";
import {
  contactStatus,
  legacyBlocksAddresses,
  matchesTeamSearch,
  targetFromActor,
  TEAM_ROLES,
  teamErrorMessage,
  teamRoleName,
} from "./helpers";

const service = new TeamService();
const fieldClass =
  "w-full rounded-md border border-subtle bg-surface-1 px-3 py-2 text-13 text-primary outline-none focus:border-accent-primary";
type Editor =
  | { kind: "contact"; contact?: TTeamContact }
  | { kind: "agent"; actor?: TTeamActor }
  | { kind: "member"; member?: TTeamMember };
type Tab = "agents" | "members" | "addresses";
type Scope = { workspace: string; project: string };

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5 text-13 text-secondary">
      <span>{label}</span>
      {children}
    </label>
  );
}

function Status({ active, children }: { active: boolean; children?: ReactNode }) {
  return (
    <Badge variant="neutral">
      <span className={`mr-1.5 inline-block size-1.5 rounded-full ${active ? "bg-green-500" : "bg-gray-400"}`} />
      {children ?? (active ? "Active" : "Inactive")}
    </Badge>
  );
}

function EditorDialog({
  editor,
  close,
  team,
  scope,
  saved,
}: {
  editor: Editor;
  close: () => void;
  team: TProjectTeam;
  scope: Scope;
  saved: () => Promise<unknown>;
}) {
  const title =
    editor.kind === "contact"
      ? editor.contact
        ? "Edit address"
        : "Add address"
      : editor.kind === "agent"
        ? editor.actor
          ? "Edit agent"
          : "Add agent"
        : editor.member
          ? "Manage member"
          : "Add member";
  return (
    <Dialog open onClose={close} className="relative z-50">
      <div className="fixed inset-0 bg-black/30" aria-hidden="true" />
      <div className="fixed inset-0 flex items-center justify-center overflow-y-auto p-4">
        <Dialog.Panel className="shadow-xl max-h-full w-full max-w-lg overflow-y-auto rounded-xl border border-subtle bg-surface-1 p-6">
          <div className="mb-5 flex items-center justify-between gap-4">
            <Dialog.Title className="text-18 font-semibold">{title}</Dialog.Title>
            <button
              aria-label="Close editor"
              type="button"
              onClick={close}
              className="rounded p-1 text-tertiary hover:bg-surface-2"
            >
              <X className="size-4" />
            </button>
          </div>
          {editor.kind === "contact" ? (
            <ContactEditor contact={editor.contact} team={team} scope={scope} saved={saved} close={close} />
          ) : editor.kind === "agent" ? (
            <AgentEditor actor={editor.actor} team={team} scope={scope} saved={saved} close={close} />
          ) : (
            <MemberEditor member={editor.member} team={team} scope={scope} saved={saved} close={close} />
          )}
        </Dialog.Panel>
      </div>
    </Dialog>
  );
}

function useEditorSave(saved: () => Promise<unknown>, close: () => void) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(operation: () => Promise<unknown>) {
    setError("");
    setBusy(true);
    try {
      await operation();
      await saved();
      close();
    } catch (cause) {
      setError(teamErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return { error, busy, save };
}

function EditorActions({
  error,
  busy,
  close,
  children,
}: {
  error: string;
  busy: boolean;
  close: () => void;
  children?: ReactNode;
}) {
  return (
    <>
      {error && (
        <p role="alert" className="rounded-md border border-subtle bg-surface-2 p-3 text-13 text-primary">
          {error}
        </p>
      )}
      <div className="flex flex-wrap items-center justify-end gap-2 border-t border-subtle pt-4">
        {children}
        <Button variant="secondary" size="sm" onClick={close} disabled={busy}>
          Cancel
        </Button>
        <Button type="submit" size="sm" disabled={busy}>
          {busy ? "Saving…" : "Save changes"}
        </Button>
      </div>
    </>
  );
}

function ContactEditor({
  contact,
  team,
  scope,
  saved,
  close,
}: {
  contact?: TTeamContact;
  team: TProjectTeam;
  scope: Scope;
  saved: () => Promise<unknown>;
  close: () => void;
}) {
  const candidates = team.actors.filter((actor) => actor.can_manage && actor.active && actor.member && actor.user_id);
  const [registrationId, setRegistrationId] = useState(
    contact?.registration_id ?? candidates[0]?.registration_id ?? ""
  );
  const actor = team.actors.find((item) => item.registration_id === registrationId);
  const [address, setAddress] = useState(contact?.address ?? "");
  const [target, setTarget] = useState<TTeamTarget>(contact?.target ?? targetFromActor(candidates[0]));
  const [active, setActive] = useState(contact?.active ?? true);
  const { error, busy, save } = useEditorSave(saved, close);
  const targetFields: { key: keyof TTeamTarget; label: string; required?: boolean }[] =
    actor?.runtime === "hermes"
      ? [
          { key: "agent_id", label: "Agent receiver", required: true },
          { key: "host_id", label: "Host" },
          { key: "profile", label: "Profile" },
          { key: "conversation_id", label: "Conversation ID" },
        ]
      : [
          { key: "thread_id", label: "Chat ID", required: true },
          { key: "host_id", label: "Host" },
        ];
  function submit(event: FormEvent) {
    event.preventDefault();
    const cleanTarget = Object.fromEntries(
      Object.entries(target)
        .filter(([, value]) => value?.trim())
        .map(([key, value]) => [key, value?.trim()])
    );
    void save(() =>
      service.saveContact(
        scope.workspace,
        scope.project,
        address.trim().replace(/^@/, ""),
        { registration_id: registrationId, target: cleanTarget, active },
        contact?.revision ?? 0
      )
    );
  }
  return (
    <form onSubmit={submit} className="space-y-4">
      <p className="text-13 text-secondary">
        Give an existing project agent a stable address. The receiver must match its registered identity.
      </p>
      <Field label="Address">
        <input
          className={fieldClass}
          aria-label="Address"
          value={address}
          onChange={(event) => setAddress(event.target.value)}
          placeholder="project-reviewer"
          required
          maxLength={128}
          pattern="@?[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
          disabled={!!contact}
        />
      </Field>
      <Field label="Agent">
        <select
          className={fieldClass}
          aria-label="Agent"
          value={registrationId}
          onChange={(event) => {
            const selected = team.actors.find((item) => item.registration_id === event.target.value);
            setRegistrationId(event.target.value);
            setTarget(targetFromActor(selected));
          }}
          required
          disabled={!!contact}
        >
          <option value="">Select an agent</option>
          {team.actors
            .filter((item) => candidates.includes(item) || item.registration_id === contact?.registration_id)
            .map((item) => (
              <option key={item.registration_id} value={item.registration_id}>
                {item.display_name} · {item.agent_key}
              </option>
            ))}
        </select>
      </Field>
      {targetFields.map(({ key, label, required }) => (
        <Field key={key} label={label}>
          <input
            aria-label={label}
            className={fieldClass}
            value={target[key] ?? ""}
            onChange={(event) => setTarget({ ...target, [key]: event.target.value })}
            required={required}
            maxLength={255}
          />
        </Field>
      ))}
      <label className="flex items-center gap-2 text-13">
        <input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} />
        Address active
      </label>
      <EditorActions error={error} busy={busy || !actor} close={close} />
    </form>
  );
}

function AgentEditor({
  actor,
  team,
  scope,
  saved,
  close,
}: {
  actor?: TTeamActor;
  team: TProjectTeam;
  scope: Scope;
  saved: () => Promise<unknown>;
  close: () => void;
}) {
  const [name, setName] = useState(actor?.display_name ?? "");
  const [role, setRole] = useState(actor?.role ?? "");
  const [active, setActive] = useState(actor?.active ?? true);
  const [runtime, setRuntime] = useState<TTeamRuntime>("codex");
  const [agentKey, setAgentKey] = useState("");
  const [userId, setUserId] = useState("");
  const [bindingId] = useState(() => crypto.randomUUID());
  const [binding, setBinding] = useState<TTeamActor["binding"]>({});
  const { error, busy, save } = useEditorSave(saved, close);
  const bindingFields: { key: keyof TTeamActor["binding"]; label: string; required?: boolean }[] =
    runtime === "hermes"
      ? [
          { key: "agent_id", label: "Agent receiver", required: true },
          { key: "host_id", label: "Host" },
          { key: "profile", label: "Profile" },
          { key: "session_id", label: "Session ID" },
        ]
      : [
          { key: "thread_id", label: "Chat ID", required: true },
          { key: "host_id", label: "Host" },
        ];
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void save(() =>
          actor
            ? service.updateActor(scope.workspace, scope.project, actor, {
                display_name: name.trim(),
                role: role.trim(),
                active,
              })
            : service.createActor(scope.workspace, scope.project, {
                runtime,
                agent_key: agentKey.trim(),
                user_id: userId,
                display_name: name.trim(),
                role: role.trim(),
                binding: {
                  ...Object.fromEntries(
                    Object.entries(binding)
                      .filter(([, value]) => value?.trim())
                      .map(([key, value]) => [key, value?.trim()])
                  ),
                  binding_id: bindingId,
                },
              })
        );
      }}
      className="space-y-4"
    >
      {actor ? (
        <p className="text-13 text-secondary">
          {actor.agent_key} · {actor.runtime === "codex" ? "Codex" : "Server agent"}
        </p>
      ) : (
        <>
          <p className="text-13 text-secondary">
            Register an existing agent receiver and link it to an active project member. To connect the current Codex
            chat automatically, use Join project from that chat.
          </p>
          <Field label="Agent type">
            <select
              aria-label="Agent type"
              className={fieldClass}
              value={runtime}
              onChange={(event) => {
                setRuntime(event.target.value as TTeamRuntime);
                setBinding({});
              }}
            >
              <option value="codex">Codex chat receiver</option>
              <option value="hermes">Codex server</option>
            </select>
          </Field>
          <Field label="Agent key">
            <input
              aria-label="Agent key"
              className={fieldClass}
              value={agentKey}
              onChange={(event) => setAgentKey(event.target.value)}
              required
              pattern="[a-z][a-z0-9_-]{0,63}"
              maxLength={64}
              placeholder="release-reviewer"
            />
          </Field>
          <Field label="Project member">
            <select
              aria-label="Project member"
              className={fieldClass}
              value={userId}
              onChange={(event) => setUserId(event.target.value)}
              required
            >
              <option value="">Select an existing member</option>
              {team.members
                .filter((member) => member.is_active)
                .map((member) => (
                  <option key={member.id} value={member.id}>
                    {member.display_name}
                  </option>
                ))}
            </select>
          </Field>
          {bindingFields.map(({ key, label, required }) => (
            <Field key={key} label={label}>
              <input
                aria-label={label}
                className={fieldClass}
                value={binding[key] ?? ""}
                onChange={(event) => setBinding({ ...binding, [key]: event.target.value })}
                required={required}
                maxLength={255}
              />
            </Field>
          ))}
        </>
      )}
      <Field label="Display name">
        <input
          aria-label="Display name"
          className={fieldClass}
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          maxLength={255}
        />
      </Field>
      <Field label="Responsibility">
        <textarea
          aria-label="Responsibility"
          className={fieldClass}
          value={role}
          onChange={(event) => setRole(event.target.value)}
          rows={3}
          required
          maxLength={4096}
          placeholder="What this agent owns in the project"
        />
      </Field>
      {actor && (
        <>
          <label className="flex items-center gap-2 text-13">
            <input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} />
            Agent active
          </label>
          <p className="text-11 text-tertiary">
            Inactive agents remain in the registry. Their addresses cannot resolve until the agent and its project
            membership are active.
          </p>
        </>
      )}
      <EditorActions error={error} busy={busy} close={close} />
    </form>
  );
}

function MemberEditor({
  member,
  team,
  scope,
  saved,
  close,
}: {
  member?: TTeamMember;
  team: TProjectTeam;
  scope: Scope;
  saved: () => Promise<unknown>;
  close: () => void;
}) {
  const [userId, setUserId] = useState(member?.id ?? "");
  const [role, setRole] = useState(member?.role ?? 15);
  const [removing, setRemoving] = useState(false);
  const { error, busy, save } = useEditorSave(saved, close);
  const candidates = useSWR(!member ? ["teams-workspace-members", scope.workspace] : null, () =>
    service.workspaceMembers(scope.workspace)
  );
  const available = candidates.data?.filter(
    (item) =>
      item.is_active !== false && !team.members.some((existing) => existing.id === item.member.id && existing.is_active)
  );
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void save(() => service.saveMember(scope.workspace, scope.project, userId, role, !!member));
      }}
      className="space-y-4"
    >
      {member ? (
        <p className="font-medium">{member.display_name}</p>
      ) : (
        <Field label="Workspace member">
          <select
            aria-label="Workspace member"
            className={fieldClass}
            value={userId}
            onChange={(event) => setUserId(event.target.value)}
            required
          >
            <option value="">
              {candidates.isLoading ? "Loading workspace members…" : "Select a workspace member"}
            </option>
            {available?.map((item) => (
              <option key={item.member.id} value={item.member.id}>
                {item.member.display_name}
              </option>
            ))}
          </select>
        </Field>
      )}
      {candidates.error && (
        <p role="alert" className="text-13">
          Workspace members could not be loaded. Close and retry.
        </p>
      )}
      <Field label="Project role">
        <select
          aria-label="Project role"
          className={fieldClass}
          value={role}
          onChange={(event) => setRole(Number(event.target.value))}
        >
          {TEAM_ROLES.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </Field>
      {removing && (
        <div className="rounded-md border border-subtle bg-surface-2 p-3 text-13">
          <p>Remove {member?.display_name} from this project? Their agents will no longer have available addresses.</p>
          <Button
            variant="secondary"
            size="sm"
            className="mt-3"
            disabled={busy}
            onClick={() => void save(() => service.removeMember(scope.workspace, scope.project, userId))}
          >
            Confirm removal
          </Button>
        </div>
      )}
      <EditorActions error={error} busy={busy || !userId || !!candidates.error} close={close}>
        {member?.is_active && (
          <Button variant="secondary" size="sm" onClick={() => setRemoving(!removing)} disabled={busy}>
            Remove member
          </Button>
        )}
      </EditorActions>
    </form>
  );
}

export function TeamsPage() {
  const { workspaceSlug = "", projectId = "" } = useParams();
  return (
    <ProjectTeams key={`${workspaceSlug}/${projectId}`} scope={{ workspace: workspaceSlug, project: projectId }} />
  );
}

function ProjectTeams({ scope }: { scope: Scope }) {
  const roster = useSWR(
    ["project-team", scope.workspace, scope.project],
    () => service.roster(scope.workspace, scope.project),
    { refreshInterval: 30000, refreshWhenHidden: false }
  );
  const [tab, setTab] = useState<Tab>("agents");
  const [query, setQuery] = useState("");
  const [state, setState] = useState("all");
  const [editor, setEditor] = useState<Editor | null>(null);
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);
  const team = roster.data;
  const agents =
    team?.actors.filter(
      (item) => matchesTeamSearch(item, query) && (state === "all" || item.active === (state === "active"))
    ) ?? [];
  const members =
    team?.members.filter(
      (item) => matchesTeamSearch(item, query) && (state === "all" || item.is_active === (state === "active"))
    ) ?? [];
  const contacts =
    team?.contacts.filter(
      (item) => matchesTeamSearch(item, query) && (state === "all" || item.active === (state === "active"))
    ) ?? [];
  const canAddAddress = team?.actors.some((actor) => actor.can_manage && actor.active && actor.member && actor.user_id);
  async function runAction(operation: () => Promise<unknown>, success: string) {
    setBusy(true);
    setMessage("");
    setActionError("");
    try {
      await operation();
      await roster.mutate();
      setMessage(success);
    } catch (error) {
      setActionError(teamErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }
  const tabs = [
    { id: "agents" as const, label: "Agents", icon: Bot, count: team?.actors.length },
    { id: "members" as const, label: "Members", icon: Users, count: team?.members.length },
    { id: "addresses" as const, label: "Addresses", icon: Hash, count: team?.contacts.length },
  ];
  return (
    <div className="h-full overflow-y-auto bg-surface-2 p-4 md:p-6" data-testid="teams-page">
      <PageHead title="Teams" />
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <p className="text-11 text-tertiary">{team?.project.name ?? "Project"} / Teams</p>
            <h1 className="mt-1 text-24 font-semibold">Teams</h1>
            <p className="mt-1 text-13 text-secondary">The people and agents working together on this project.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" disabled={roster.isValidating} onClick={() => void roster.mutate()}>
              <RefreshCw className="mr-1.5 size-3.5" />
              Refresh
            </Button>
            {team?.can_manage_members && (
              <Button variant="secondary" size="sm" onClick={() => setEditor({ kind: "member" })}>
                <Plus className="mr-1.5 size-3.5" />
                Add member
              </Button>
            )}
            {team?.can_manage_members && (
              <Button variant="secondary" size="sm" onClick={() => setEditor({ kind: "agent" })}>
                <Plus className="mr-1.5 size-3.5" />
                Add agent
              </Button>
            )}
            {canAddAddress && (
              <Button
                size="sm"
                disabled={legacyBlocksAddresses(team?.legacy.status)}
                onClick={() => setEditor({ kind: "contact" })}
              >
                <Plus className="mr-1.5 size-3.5" />
                Add address
              </Button>
            )}
          </div>
        </header>
        {roster.error ? (
          <div role="alert" className="rounded-lg border border-subtle bg-surface-1 p-6">
            <h2 className="font-medium">Teams could not be loaded</h2>
            <p className="mt-2 text-13 text-secondary">{teamErrorMessage(roster.error)}</p>
          </div>
        ) : !team ? (
          <p role="status" className="text-13 text-secondary">
            Loading team…
          </p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              {[
                {
                  title: "Project members",
                  count: team.members.filter((item) => item.is_active).length,
                  detail: "People and assignable agent identities",
                  icon: Users,
                },
                {
                  title: "Active agents",
                  count: team.actors.filter((item) => item.active).length,
                  detail: "Chats and server agents with a project role",
                  icon: Bot,
                },
                {
                  title: "Available addresses",
                  count: team.contacts.filter((item) => item.available).length,
                  detail: "Validated receivers for project messages",
                  icon: Hash,
                },
              ].map(({ title, count, detail, icon: Icon }) => (
                <div key={title} className="rounded-lg border border-subtle bg-surface-1 p-4">
                  <div className="flex items-center justify-between text-13 text-secondary">
                    <span>{title}</span>
                    <Icon className="size-4 text-tertiary" />
                  </div>
                  <p className="mt-2 text-24 font-semibold tabular-nums">{count}</p>
                  <p className="mt-1 text-11 text-tertiary">{detail}</p>
                </div>
              ))}
            </div>
            {legacyBlocksAddresses(team.legacy.status) && (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-subtle bg-surface-1 p-4">
                <div>
                  <h2 className="text-13 font-medium">
                    {team.legacy.status === "conflict" ? "Address pages need review" : "Existing address page"}
                  </h2>
                  <p className="mt-1 text-13 text-secondary">
                    {team.legacy.status === "conflict"
                      ? "Multiple address pages exist. A project admin must reconcile them before addresses can be imported or changed."
                      : "Import its addresses into Teams before adding addresses. Existing page content is preserved."}
                  </p>
                </div>
                {team.can_manage_members && team.legacy.status === "pending" ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={busy}
                    onClick={() =>
                      void runAction(
                        () => service.importLegacy(scope.workspace, scope.project),
                        "Existing addresses imported."
                      )
                    }
                  >
                    Import addresses
                  </Button>
                ) : !team.can_manage_members ? (
                  <p className="text-11 text-tertiary">A project admin can import these addresses.</p>
                ) : null}
              </div>
            )}
            {message && (
              <p role="status" className="flex items-center gap-2 text-13">
                <Check className="size-4" />
                {message}
              </p>
            )}
            {actionError && (
              <p role="alert" className="rounded-lg border border-subtle p-3 text-13">
                {actionError}
              </p>
            )}
            <section className="rounded-lg border border-subtle bg-surface-1">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-subtle px-4 pt-3">
                <div role="tablist" aria-label="Team registry" className="flex gap-4">
                  {tabs.map(({ id, label, icon: Icon, count }) => (
                    <button
                      key={id}
                      type="button"
                      role="tab"
                      id={`team-tab-${id}`}
                      aria-controls="team-panel"
                      aria-selected={tab === id}
                      onClick={() => setTab(id)}
                      className={`inline-flex items-center gap-2 border-b-2 pb-3 text-13 ${tab === id ? "border-accent-primary font-medium text-accent-primary" : "border-transparent text-secondary"}`}
                    >
                      <Icon className="size-4" />
                      {label}
                      <span className="rounded bg-surface-2 px-1.5 text-11 text-tertiary">{count}</span>
                    </button>
                  ))}
                </div>
                <div className="mb-3 flex w-full gap-2 sm:w-auto">
                  <label className="relative grow">
                    <span className="sr-only">Search team</span>
                    <Search className="absolute top-2.5 left-2.5 size-3.5 text-tertiary" />
                    <input
                      aria-label="Search team"
                      placeholder="Search name, role, address…"
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      className={`${fieldClass} pl-8`}
                    />
                  </label>
                  <select
                    aria-label="Team status"
                    value={state}
                    onChange={(event) => setState(event.target.value)}
                    className={`${fieldClass} w-auto`}
                  >
                    <option value="all">All statuses</option>
                    <option value="active">Active</option>
                    <option value="inactive">Inactive</option>
                  </select>
                </div>
              </div>
              <div role="tabpanel" id="team-panel" aria-labelledby={`team-tab-${tab}`} className="overflow-x-auto">
                <table className="w-full min-w-[680px] text-left text-13">
                  <thead className="border-b border-subtle text-11 text-tertiary">
                    <tr>
                      {(tab === "agents"
                        ? ["Agent", "Responsibility", "Addresses", "Status", ""]
                        : tab === "members"
                          ? ["Member", "Project role", "Agents", "Status", ""]
                          : ["Address", "Agent", "Receiver", "Status", ""]
                      ).map((label) => (
                        <th key={label || "actions"} className="px-4 py-3 font-medium">
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {tab === "agents" &&
                      agents.map((actor) => (
                        <tr key={actor.registration_id} className="border-b border-subtle last:border-0">
                          <td className="px-4 py-4">
                            <div className="font-medium">{actor.display_name}</div>
                            <p className="mt-1 text-11 text-tertiary">
                              {actor.runtime === "codex" ? "Codex" : "Server agent"} · {actor.agent_key}
                            </p>
                          </td>
                          <td className="max-w-xs px-4 py-4">
                            <p className="line-clamp-3 whitespace-pre-wrap text-secondary">
                              {actor.role || "No responsibility assigned"}
                            </p>
                          </td>
                          <td className="px-4 py-4">
                            {team.contacts
                              .filter((contact) => contact.registration_id === actor.registration_id)
                              .map((contact) => (
                                <p
                                  key={contact.id}
                                  className={`text-11 ${contact.available ? "text-accent-primary" : "text-tertiary"}`}
                                >
                                  @{contact.address}
                                </p>
                              ))}
                            {!team.contacts.some((contact) => contact.registration_id === actor.registration_id) && (
                              <span className="text-tertiary">No address</span>
                            )}
                          </td>
                          <td className="px-4 py-4">
                            <Status active={actor.active && actor.member}>
                              {!actor.active ? "Inactive" : !actor.member ? "No membership" : "Active"}
                            </Status>
                          </td>
                          <td className="px-4 py-4 text-right">
                            {actor.can_manage && (
                              <Button
                                size="sm"
                                variant="secondary"
                                onClick={() => setEditor({ kind: "agent", actor })}
                                aria-label={`Edit agent ${actor.display_name}`}
                              >
                                Edit
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                    {tab === "members" &&
                      members.map((member) => (
                        <tr key={member.id} className="border-b border-subtle last:border-0">
                          <td className="px-4 py-4">
                            <div className="font-medium">{member.display_name}</div>
                            <p className="mt-1 text-11 text-tertiary">{member.is_bot ? "Agent identity" : "Person"}</p>
                          </td>
                          <td className="px-4 py-4">{teamRoleName(member.role)}</td>
                          <td className="px-4 py-4 text-secondary">
                            {team.actors.filter((actor) => actor.user_id === member.id).length}
                          </td>
                          <td className="px-4 py-4">
                            <Status active={member.is_active} />
                          </td>
                          <td className="px-4 py-4 text-right">
                            {team.can_manage_members && (
                              <Button
                                size="sm"
                                variant="secondary"
                                onClick={() => setEditor({ kind: "member", member })}
                                aria-label={`Manage member ${member.display_name}`}
                              >
                                Manage
                              </Button>
                            )}
                          </td>
                        </tr>
                      ))}
                    {tab === "addresses" &&
                      contacts.map((contact) => (
                        <tr key={contact.id} className="border-b border-subtle last:border-0">
                          <td className="px-4 py-4">
                            <div className="flex items-center gap-2">
                              <span className="font-medium">@{contact.address}</span>
                              <button
                                type="button"
                                aria-label={`Copy address ${contact.address}`}
                                className="text-tertiary"
                                onClick={() => {
                                  void navigator.clipboard
                                    .writeText(`@${contact.address}`)
                                    .then(() => setMessage("Address copied."))
                                    .catch(() => setActionError("Clipboard unavailable. Select and copy the address."));
                                }}
                              >
                                <Copy className="size-3.5" />
                              </button>
                            </div>
                          </td>
                          <td className="px-4 py-4">
                            <p>{contact.display_name}</p>
                            <p className="mt-1 text-11 text-tertiary">{contact.agent_key}</p>
                          </td>
                          <td className="max-w-[220px] px-4 py-4">
                            <p
                              className="truncate text-11 text-secondary"
                              title={contact.target.thread_id ?? contact.target.agent_id}
                            >
                              {contact.target.thread_id ?? contact.target.agent_id}
                            </p>
                            <p className="mt-1 text-11 text-tertiary">{contact.target.host_id ?? "Registered host"}</p>
                          </td>
                          <td className="px-4 py-4">
                            <Status active={contact.available}>{contactStatus(contact)}</Status>
                          </td>
                          <td className="px-4 py-4">
                            <div className="flex justify-end gap-2">
                              <Button
                                variant="secondary"
                                size="sm"
                                disabled={busy || !contact.available}
                                onClick={() =>
                                  void runAction(
                                    () => service.resolve(scope.workspace, scope.project, contact.address),
                                    `@${contact.address} resolves to its registered receiver.`
                                  )
                                }
                              >
                                Check
                              </Button>
                              {contact.can_manage && (
                                <Button
                                  variant="secondary"
                                  size="sm"
                                  onClick={() => setEditor({ kind: "contact", contact })}
                                  aria-label={`Edit address ${contact.address}`}
                                >
                                  Edit
                                </Button>
                              )}
                            </div>
                          </td>
                        </tr>
                      ))}
                    {!(tab === "agents" ? agents : tab === "members" ? members : contacts).length && (
                      <tr>
                        <td colSpan={5} className="px-4 py-12 text-center text-secondary">
                          <p className="font-medium">
                            {query || state !== "all"
                              ? "No matches"
                              : tab === "agents"
                                ? "No agents registered yet"
                                : tab === "members"
                                  ? "No project members"
                                  : "No addresses yet"}
                          </p>
                          <p className="mt-2 text-13">
                            {query || state !== "all"
                              ? "Try another name, role or status."
                              : tab === "agents"
                                ? "Join the project from an agent chat to register its identity and responsibility."
                                : tab === "addresses"
                                  ? "Add an address for a registered agent, or import the existing address page."
                                  : "Add an existing workspace member to the project."}
                          </p>
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </section>
            <p className="text-11 text-tertiary">
              Available addresses have an active project member, agent identity and matching receiver. Availability does
              not indicate whether an agent is online.
            </p>
          </>
        )}
      </div>
      {editor && team && (
        <EditorDialog
          editor={editor}
          close={() => setEditor(null)}
          team={team}
          scope={scope}
          saved={() => roster.mutate()}
        />
      )}
    </div>
  );
}
