# CE management API extension

This extension adds API-key access to Plane CE 1.4.2 operations that previously
required a browser session. It reuses the existing app serializers, permissions,
models, and background tasks. No new database or daemon is introduced.

All routes are below `/api/v1/`. Send `X-Api-Key`; a browser cookie is not a
substitute. The new management views confine workspace-scoped keys to their
workspace, including workspace discovery. Existing CE views, including the newly
wired estimates views, retain upstream API-key scoping. Normal user/project
membership and private-page permissions still apply.

`GET workspaces/{slug}/management-capabilities/` identifies the extension and
supported features. Clients must check it before using extension-only routes.
Native releases are not present in CE and are not advertised.

## Routes

| Prefix | Suffix | Methods |
| --- | --- | --- |
| none | `workspaces/` | GET, POST |
| workspace | empty | GET, PATCH, DELETE |
| workspace | `workspace-members/` | GET, POST |
| workspace | `workspace-members/{membership_id}/` | GET, PATCH, DELETE |
| workspace | `webhooks/` | GET, POST |
| workspace | `webhooks/{id}/` | GET, PATCH, DELETE |
| workspace | `webhooks/{id}/regenerate/` | POST |
| workspace | `webhook-logs/{id}/` | GET |
| project | `pages/` | GET, POST |
| project | `pages-summary/` | GET |
| project | `pages/{id}/` | GET, PATCH, DELETE |
| project | `pages/{id}/description/` | GET, PATCH |
| project | `pages/{id}/archive/` | POST, DELETE |
| project | `pages/{id}/lock/` | POST, DELETE |
| project | `pages/{id}/access/` | POST |
| project | `pages/{id}/duplicate/` | POST |
| project | `pages/{id}/versions/` | GET |
| project | `pages/{id}/versions/{version_id}/` | GET |
| project | `favorite-pages/{id}/` | POST, DELETE |
| project | `archived-work-items/` | GET |
| project | `work-items/{id}/archive/` | GET, POST, DELETE |
| project | `work-items/{id}/relations/{relation_id}/` | DELETE |

Workspace prefix: `workspaces/{slug}/`.
Project prefix: `workspaces/{slug}/projects/{project_id}/`.
Existing estimate and estimate-point routes are now included in the public URL
configuration. Existing project, work-item, module, cycle, label, state, asset,
invitation, and project-member APIs remain available.

Workspace membership creation accepts `member_id` (an existing active user UUID)
and `role` (5, 15, or 20). It does not provision user accounts. Duplicate membership
creation returns 409 and the existing membership ID. Existing app restrictions,
including bot-account membership editing restrictions, are preserved.

## Work-item relation identifiers

Create responses retain `id` as the related work-item UUID and additionally
return `relation_id` as the relationship row UUID. Grouped list responses retain
`project_id` and `issue_id`, and additionally return the same `relation_id` in
each reference. Pass `relation_id` to the relation DELETE endpoint; either end
of the relation may be used as the work item in the URL. This also works for
reverse and cross-project relations without guessing an internal database ID.

## Page content and concurrency

Create a page with `{"name":"Build status","description_html":"<p>Ready</p>"}`.
The live service converts HTML with Plane's editor schema before any Page row is
created. The canonical HTML, JSON and Yjs binary are stored together. The default
body is an empty paragraph.

Read the page or its `description/` endpoint to obtain HTML, JSON, base64 Yjs
binary, `name`, `updated_at`, and an `ETag`. Update body content by PATCHing
`description/` with `description_html`. PATCH the page itself with `name` to
rename it. A rename must be a separate request from other metadata changes; it
updates the Yjs title while preserving the current document body.

Send the last `ETag` in `If-Match` for optimistic concurrency. A stale database
revision returns 412. A conflicting open editor document returns 409. Metadata
and lifecycle operations check the revision while holding a row lock. Content
operations release all database locks before contacting live, then use a
revision-checked callback transaction to persist the document. Page version and
reference tasks are dispatched only after the callback transaction commits.

The live service uses its existing Hocuspocus document and editor codec, and
persists all formats together through this API. API content writes therefore
require the live service and its shared `LIVE_SERVER_SECRET_KEY`. The shared
secret alone cannot write: callbacks must also authenticate with the original
API key and satisfy normal page permissions.

A failed or timed-out live request can return `outcome: "unknown"`. Read the page
and reconcile before retrying; a network failure does not prove that no write
occurred. Codec failure during creation occurs before the page is saved and
returns `outcome: "not_applied"` for malformed/network responses.

## Deployment and verification

Deploy the matching API and live changes together. The API's `LIVE_BASE_URL` and
`LIVE_BASE_PATH` must resolve the live service; live's `API_BASE_URL` must resolve
the API. Both require the same `LIVE_SERVER_SECRET_KEY`. Existing Redis and
Celery infrastructure is reused. There are no new migrations. Run one live
replica: page serialization in this first version uses a process-local lock;
distributed API/editor transactions across live replicas are not implemented.

Backend contract tests use PostgreSQL and real ORM/auth/routes, mocking only the
live HTTP transport and async dispatch except where real version generation is
explicitly tested. Run `plane/tests/contract/api/test_management.py` from
`apps/api` with the repository's pytest settings and an isolated PostgreSQL
`DATABASE_URL`. Live tests separately exercise actual Yjs documents. A deployment
acceptance test must additionally run the complete client → Django → live →
PostgreSQL flow with an active editor and a server restart; unit/contract tests
alone do not establish that a deployment is ready.

For a running isolated local stack, the opt-in WebSocket acceptance script is:

```sh
node apps/live/tests/integration/content-stack-smoke.mjs /path/to/management-fixture.json
```

The ignored fixture must contain `base_url`, `workspace_slug`, `project_id`,
`user_id`, `api_key`, and an authenticated `session_cookie`. The script rejects
non-local hosts, creates a disposable page, checks propagation into an open
editor, compares persisted Yjs, tests rename and an unsaved-editor conflict, then
cleans up. Do not commit fixture credentials. HTML is normalized by Plane's
existing editor schema; it is not preserved byte-for-byte from the caller.
