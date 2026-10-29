# Project Teams

Teams joins the existing `ProjectMember` and `ProjectAgentRegistration` records
with persistent `ProjectTeamContact` addresses. It does not create a second team
membership or user directory. The unused workspace `Team` model is unchanged.
Migration `0126_project_teams` creates contacts and a small legacy import receipt;
it never reads or changes a live address page automatically.

Browser routes start at `/api/workspaces/{slug}/projects/{project_id}/team/`.
API-key routes use `/api/v1/` instead. Both use the same service and SQL records.
Management discovery advertises `teams`.

| Method and suffix                 | Contract                                                                                                                                              |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /`                           | `project`, `members`, `actors`, `contacts`, `legacy`, `can_manage_members`; optional `search` matches names, roles, runtime, agent keys and addresses |
| `GET contacts/{address}/`         | Contact plus numeric revision ETag                                                                                                                    |
| `PUT contacts/{address}/`         | `{registration_id, target, active?}`; `If-Match: "0"` creates an absent address; current ETag updates                                                 |
| `DELETE contacts/{address}/`      | Current ETag required; deactivates and retains the address/revision tombstone                                                                         |
| `POST resolve/`                   | `{address}`; exact matching available contact, otherwise 404; an optional leading `@` is accepted                                                     |
| `POST agents/`                    | Admin adds a registration for an existing active project member: `{runtime, agent_key, display_name, role, user_id, binding}` and `If-Match: "0"`     |
| `PATCH agents/{registration_id}/` | Current ETag and partial `{display_name?, role?, active?}`                                                                                            |
| `POST members/{user_id}/`         | Admin adds/reactivates an existing workspace member with `{role}` with numeric value `5`, `15`, or `20`                                               |
| `PATCH members/{user_id}/`        | Admin changes the existing membership role                                                                                                            |
| `DELETE members/{user_id}/`       | Admin deactivates membership; user and registration records survive                                                                                   |
| `POST import/`                    | Admin explicitly imports the uniquely identified legacy address page; empty body; idempotent receipt                                                  |

Agent creation does not provision a user, credential or runtime. Binding fields
follow the existing registration schema: a UUID `binding_id`, `thread_id` for
Codex or `agent_id` for the legacy `hermes` wire value, and the existing optional
runtime-specific fields. `chat-*` is reserved for the existing join-chat flow.
Membership roles are numeric access levels; agent `role` is descriptive
responsibility. Multiple chat registrations may share one assignable user.

Reads and resolution require active project and workspace membership. Contact
and agent updates require the owning user (project member or admin) or a project
admin. Membership changes and import require project admin and preserve the
existing workspace-role bounds and admin protections. The API-key authentication
also enforces its workspace scope. Responses never include credentials or API
token identifiers.

`active` is stored intent; `available` is derived from the contact, active
registration/user/project/workspace membership and matching registered receiver.
Deactivated or mismatched entries stay visible for repair but cannot resolve.
Thread/host/profile/session changes invalidate resolution immediately. Availability
is not a claim that a runtime is online. Existing address names cannot be silently
assigned to another registration. CAS conflicts return 412, absent preconditions
428, identity/import conflicts 409. Retry after reading current state.

## Legacy page transition

`legacy.status` is `none`, `pending`, `conflict` or `imported`. When an active
`hcom team` page exists, native contact writes require import first. GET never
imports. Import locks the project and page, validates the schema-1 block and all
project/registration/user associations, and commits all contacts plus receipt
atomically. Duplicate pages, malformed blocks, unknown identity references and
conflicting existing contacts abort without changing any source. Inactive or
stale-but-recognized contacts are retained and reported unavailable. Imports are
idempotent and do not resurrect tombstones.

The old page's HTML, editor JSON/binary, ownership and archival state are
preserved. It becomes locked; app and API content/metadata mutations, including
live persistence callbacks, unlock and deletion, return an explicit Teams
conflict. A second `hcom team` page cannot be created after import.

For installed hcom clients, the old API `pages/{id}/description/` GET becomes a
read-only schema-1 compatibility projection of **available canonical contacts**
with current derived chat roles. Other HTML is preserved. Its ETag hashes the
derived HTML and changes when routing changes. It returns HTML and source metadata,
not stale editor JSON/binary. Native clients should use the Teams roster directly.
Old clients can continue syncing/resolving existing addresses; legacy clients
attempting changed binds receive the explicit conflict and must be updated.
There are never two writable address stores. This implementation does not deploy
the module, import production data or delete any live page.
