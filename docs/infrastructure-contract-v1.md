# Infrastructure API contract v1

Status: implementation contract, 2026-10-30. This document is owned by the Plane
workstream. Infrastructure is a native project section, not a Plane task Module.
No discovery, scan, credential lookup, grant, probe or deployment is performed by
these endpoints. Values are user-supplied inventory evidence, not live acceptance.

## Discovery and transport

`GET /api/v1/workspaces/{slug}/management-capabilities/` adds `infrastructure` to
`capabilities` and the following object (existing capabilities remain intact):

```json
{
  "infrastructure": {
    "version": 1,
    "resource_kinds": ["physical_host", "vm", "lxc", "service", "database", "storage", "network", "stack"],
    "relation_kinds": ["runs_on", "depends_on"],
    "project_link_kind": "serves_project",
    "pagination": { "style": "cursor", "default_per_page": 50, "max_per_page": 100 },
    "revision_header": "If-Match",
    "secret_values": false
  }
}
```

API base `B=/api/v1/workspaces/{slug}/projects/{project_id}/infrastructure`.
Native browser base is the same with `/api/` instead of `/api/v1/`. Both use the
same service, validation and permissions. Trailing slashes are mandatory. API
uses the existing `X-API-Key` authentication and rate limits; browser uses the
existing `BaseSessionAuthentication` cookie session. That existing Plane class
does not enforce DRF CSRF tokens; this extension does not claim otherwise. Every
mutation requires the non-simple `If-Match` header. Browser-origin and credentialed
CORS policy remain the deployment's existing responsibility (the upstream default
allows all origins when no explicit list is configured). API identity is the key's actual user; a key
bound to another workspace is rejected. No impersonation/owner headers exist.
All responses use `Cache-Control: no-store`. Timestamps are UTC ISO 8601 strings.
UUIDs are strings. Unknown input properties are rejected, including nested ones.

## Operations and response shapes

| Operation        | Method/path below B                                                  | Input                                      | Success                       |
| ---------------- | -------------------------------------------------------------------- | ------------------------------------------ | ----------------------------- |
| resources.list   | GET `/resources/`                                                    | query below                                | 200 Page<Resource>            |
| resources.get    | GET `/resources/{resource_id}/`                                      | none                                       | 200 Resource + ETag           |
| resources.create | POST `/resources/`                                                   | ResourceWrite; `If-Match: "0"`             | 201 Resource + ETag           |
| resources.update | PATCH `/resources/{resource_id}/`                                    | partial ResourceWrite; current If-Match    | 200 Resource + ETag           |
| resources.delete | DELETE `/resources/{resource_id}/`                                   | current If-Match                           | 204 empty                     |
| relations.list   | GET `/relations/`                                                    | query below                                | 200 Page<Relation>            |
| relations.create | POST `/relations/`                                                   | RelationWrite; `If-Match: "0"`             | 201 Relation + ETag           |
| relations.delete | DELETE `/relations/{relation_id}/`                                   | relation If-Match                          | 204 empty                     |
| links.list       | GET `/resources/{resource_id}/project-links/`                        | pagination                                 | 200 Page<ProjectLink>         |
| links.attach     | POST `/resources/{resource_id}/project-links/`                       | `{"project_id":"UUID"}`; resource If-Match | 200 Resource + ETag           |
| links.detach     | DELETE `/resources/{resource_id}/project-links/{target_project_id}/` | resource If-Match                          | 204 empty + new resource ETag |

```json
{ "results": [], "next_cursor": null, "has_more": false, "per_page": 50 }
```

Every list uses this Page envelope. No total counts or invisible-resource counts
are returned. `per_page` is an integer 1..100, default 50. `cursor` is opaque,
signed and bound to caller, workspace, project, endpoint, normalized filters and
page size; invalid/mismatched cursors return 400 `invalid_cursor`. Stable ordering
is ascending UUID, forward only. Concurrent inserts before the cursor may require
a fresh traversal; this is not a snapshot. ACLs are checked again on every page.

Resource filters: `kind`, `key`, `lifecycle` (single exact values); `tags`
(comma-separated, all requested tags must match; max 20); `search` (case-insensitive
name/key substring, max 200). Relation filters: `kind`, `source_id`, `target_id`,
`resource_id` (either endpoint). Unknown/repeated query parameters and invalid
enums/UUIDs/page sizes return 400. Exact `key` lookup is project-scoped and returns
zero or one row. Clients must resolve selectors only in their bound project and
must treat multiple matches as ambiguous, never select the first arbitrarily.

## Resource schema

```json
{
  "id": "UUID",
  "key": "monitoring/grafana",
  "name": "Grafana",
  "kind": "service",
  "lifecycle": "active",
  "description": "Metrics dashboards",
  "tags": ["monitoring"],
  "owner": { "name": "Operations", "reference": "team:operations", "recovery_contact": "role:supervisor" },
  "source": { "kind": "git", "reference": "repo:infrastructure/monitoring", "revision": "example-revision" },
  "evidence": [{ "kind": "review", "reference": "doc:monitoring-review", "observed_at": "2026-10-30T08:00:00Z" }],
  "last_verified": "2026-10-30T08:00:00Z",
  "access_methods": [
    {
      "id": "grafana-api",
      "kind": "https",
      "endpoint": "https://grafana.example.invalid",
      "capability": "grafana",
      "operations": ["dashboards.read", "datasources.read"],
      "scope": "monitoring organization",
      "identity": "runtime-own-identity",
      "credential_refs": [
        {
          "provider": "openbao",
          "path": "projects/example/monitoring/grafana",
          "field": "api_token",
          "purpose": "Read monitoring metadata"
        }
      ],
      "notes": "Use assigned runtime credentials; no grant is implied."
    }
  ],
  "revision": 1,
  "created_at": "2026-10-30T08:00:00Z",
  "updated_at": "2026-10-30T08:00:00Z",
  "ownership_project_id": "UUID or null",
  "can_manage": true,
  "can_detach": false
}
```

`ResourceWrite` contains only `key,name,kind,lifecycle,description,tags,owner,source,
evidence,last_verified,access_methods`. Create requires `key,name,kind,owner,source`.
Defaults: lifecycle `planned`, description empty, tags/evidence/access_methods empty,
last_verified null. PATCH replaces supplied nested objects/arrays; omitted values
are preserved. `key` and `kind` are immutable; supplying their current value is
allowed; changing them returns 400 `validation_error`. IDs, revisions, project IDs, timestamps and capability booleans are server
owned and rejected as input. Resource kind is one of discovery's kinds. Lifecycle:
`planned`, `active`, `maintenance`, `retired`, `unknown`.

One immutable UUID and workspace-unique key identify one resource across projects.
Keys (1..160) match `[a-z0-9][a-z0-9._:/-]*`. Inventory-deleted keys remain reserved
while the owning project/workspace exists; collision
returns generic 409 `conflict` without revealing another project/resource.
Name 1..255; description <=4000. Tags are unique normalized lowercase slugs
`[a-z0-9][a-z0-9._:-]*`, 1..64, at most 20. Owner requires `name` (1..255),
`reference` (logical nonsecret reference, 1..512) and `recovery_contact` (1..512).
Source requires `kind` (`git`,`manual`,`import`,`api`), `reference` (1..1024),
`revision` (1..255). Evidence has at most 50 entries with required `kind`
(`review`,`observation`,`document`), `reference` (1..1024), aware `observed_at`.
`last_verified` is nullable, timezone-aware, cannot be in the future, and requires
at least one evidence entry when non-null. Evidence dates cannot be in the future.
Revisions and last_verified record supplied evidence, not a permission grant or
proof that the current actor can connect. Source revision differs from numeric
optimistic-concurrency `revision`.

Access methods (<=20) require unique slug `id` (1..64), `kind`
(`ssh`,`http`,`https`,`postgresql`,`mysql`,`s3`,`smb`,`nfs`,`console`,`other`), `endpoint`
(1..1024), `capability` (slug, 1..128), `operations` (1..30 unique slug strings,
1..128), `scope` (1..1024) and `identity` (1..255, an informational identity
selector, never a credential). `credential_refs` defaults [], <=10; each requires
exactly `provider:"openbao"`, logical `path` (1..512, segments of letters/digits/
`._-`, no leading slash, empty segment or dot/dotdot; never insert an OpenBao
KV API `/data/` component automatically; a literal logical segment `data` is valid),
`field` (slug, 1..128), `purpose` (1..512). `notes` defaults empty, <=1000.
There are no secret-value, headers, private-key, environment, executable probe or
command fields. Endpoints must be absolute URIs of the declared scheme (console/
other may use https). HTTP endpoints require explicit `kind=http`; they are not rewritten to HTTPS. No userinfo, query, fragment or embedded credentials.
All text rejects control characters and recognizable credential assignments,
Bearer tokens and PEM private keys. This is defense in depth, not a general secret
detector: users must never paste secrets into names/descriptions/evidence either.
Validation errors never echo rejected values. The API never dereferences any URL
or logical reference. UI renders access information as inert text, not actions.
Infrastructure request/response bodies and query parameters are redacted from
API-token logs; ordinary request logs omit the query string, even on rejection.

## Relationships and project links

```json
{
  "id": "UUID",
  "project_id": "UUID",
  "source_id": "UUID",
  "source_name": "Grafana",
  "source_key": "monitoring/grafana",
  "target_id": "UUID",
  "target_name": "Monitoring VM",
  "target_key": "monitoring/vm",
  "kind": "runs_on",
  "source": { "kind": "manual", "reference": "doc:monitoring-layout", "revision": "v1" },
  "evidence": [],
  "last_verified": null,
  "revision": 1,
  "created_at": "2026-10-30T08:00:00Z",
  "can_manage": true
}
```

RelationWrite requires `source_id,target_id,kind,source`; optional `evidence` and
`last_verified` follow Resource rules. Relations are immutable (delete/recreate
with current evidence). Both endpoints must be visible and linked to the route's
project, same workspace, different resources, and non-retired. Duplicate
`(project,source,target,kind)` is 409. Each kind forms a directed acyclic graph in
that project; a cycle is 400. `runs_on` sources: vm,lxc,service,database,storage;
targets: physical_host,vm,lxc,service,storage,network. A vm/lxc runs_on only a
physical_host or vm. `depends_on` accepts all kinds. Relations are project-local
assertions, not automatically copied to consumers of a shared resource. Listing
only returns relations whose two endpoints remain visible in the bound project.
Endpoint names and keys are read-only presentation fields from those visible
resources; they are not copies or independently writable relation metadata.

```json
{
  "id": "UUID",
  "resource_id": "UUID",
  "project_id": "UUID",
  "project_name": "Example project",
  "kind": "serves_project",
  "is_owner": true,
  "revision": 1
}
```

ProjectLink is the explicit resource-to-project `serves_project` edge; create
automatically adds the owner project's link. Resource metadata is shared in full
with deliberately linked projects. Link list includes only projects readable by
the caller, with no hidden IDs/counts. `ownership_project_id` is null when the
owner project is unreadable. Links use their resource's revision (returned in
each link), not an independently writable revision. Relation/link/resource IDs
are server-generated and stable for their lifetimes.

## Exact permission semantics

Every operation requires an active workspace membership and active membership of
the route's project, with workspace/project consistency and nondeleted project.
Workspace admin alone does not bypass missing project membership. Read allows
project role 15/20, or role 5 only when `guest_view_all_features` is enabled.
Role 5 is always read-only. Private projects are not discoverable through shared
resource metadata, link lists, relationship endpoints or filtering errors.

Resource create requires project admin (20); the route becomes ownership project.
Update/delete require route = ownership project AND active project admin. Consumer
project admin cannot modify the underlying shared record, even if their workspace
role is admin. `can_manage` states this rule for the current route/actor.
Deleting a resource with other active project links returns 409; detach them first.
Deletion removes owner-project relations and the owner link transactionally and
soft-deletes the resource; it does not delete real infrastructure.

Relation create/delete requires admin of the bound project, plus visibility of
both endpoints. This permits a consuming project to describe its own relationships
without modifying shared-resource metadata.

Attach requires the owner route, owner-project admin and active admin membership
in the target project in the same workspace. No workspace-only escalation exists.
An inaccessible target yields generic 404; a visible non-admin target yields 403.
Attaching an existing link with the current revision is a 200 no-op.
Detach requires either owner-route admin AND target-project admin, or the target
route itself with target-project admin (a consumer may remove its own link).
The owner link cannot be detached (409). Detach removes that project's incident
relations transactionally. `can_detach` is true only for a consumer-route admin.

## Concurrency, errors and unknown outcomes

ETag is the quoted decimal `revision` (e.g. `"3"`); clients may reconstruct it
from list bodies. PATCH/delete/attach/detach require the current resource ETag;
relation delete requires its ETag. Create requires `"0"`. Missing If-Match ->
428 `precondition_required`; malformed/wildcard/multiple tags -> 400
`validation_error`; stale -> 412 `revision_conflict`. Weak numeric ETags are
accepted to tolerate compression middleware. Actual mutations increment resource
revision by one; identical PATCH/attach is a no-op. Mutation checks and writes
are atomic under database locks, including sharing and graph-cycle checks.

Domain errors have `{"error":{"code":"validation_error","detail":"Safe message","fields":{}}}`.
Fields optionally contains field-to-safe-message/list validation details, never
rejected values. Codes: 400 `validation_error`/`invalid_cursor`, 403 `forbidden`,
404 `not_found`, 409 `conflict`, 412 `revision_conflict`, 428
`precondition_required`. Authentication/rate-limit/parser errors may retain the
standard Plane/DRF envelope; clients must primarily branch on HTTP status and
fall back to generic safe wording. Unauthenticated/invalid-key status follows
existing Plane authentication (401 or 403); rate limits are 429. 5xx must not be
interpreted as not-applied. Errors and revisions never reveal invisible resources.

No automatic mutation retry. On timeout/connection loss after POST, resolve the
exact stable resource key in the original project, or the exact relation tuple.
Compare the intended fields, not just existence. PATCH/attach: GET resource and
visible link list; delete/detach: GET/list absence (404 can also mean lost access).
An unresolved outcome remains unknown; never manufacture a success or a new key.
There is no separate client-request-ID deduplication in v1. Stable resource key
and unique relation/link tuples provide readback anchors.

## Local acceptance and limitations

Synthetic monitoring example: stack `monitoring` depends_on services
`monitoring/grafana` and `monitoring/prometheus`; both services run_on VM
`monitoring/vm`, which runs_on `lab/host`. Grafana also depends_on Prometheus.
Attach the same VM/services to a second project, preserving resource UUIDs, and
confirm that that project's reader sees only its own relation graph/project links.

Required checks: real PostgreSQL migrations; session and API-key CRUD/readback;
workspace/project/own-identity/guest/shared isolation; stale/missing ETags and
concurrent writers; cross-project/self/cyclic relations; secret-shaped payload
rejection; bounded pagination; native navigation/list/detail/forms/error/empty
states and responsive layout. Source-only tests grant no rights or operational
readiness. No inventory importer, live probes, secret retrieval, topology drawing,
bulk edits or background synchronization are included in v1.

Ownership transfer is not implemented in v1. Deleting the owning Plane project
follows Plane's existing project lifecycle: its resources become unavailable,
including to consuming projects, and eventual hard deletion cascades the records.
Inventory sharing does not extend the owning project's lifetime. Project teardown
must therefore be reviewed for shared infrastructure before it is performed.
