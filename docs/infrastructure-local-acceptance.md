# Infrastructure v1 — local source acceptance

Date: 2026-10-30. Scope: `dependencies/plane` only. The existing checkout and
submodule structure were retained. No branch change, staging, commit, push, PR,
deployment, working-server connection, live Plane/OpenBao action, real access
grant, job activation or backup was performed. Existing assignment/dispatch work
was preserved.

## Delivered

- Native project Infrastructure section in both navigation modes; independent of
  task Modules. Resource cards, filters, list/detail routes, forms, relationships,
  project sharing, loading/error/empty states and revision conflict feedback.
- Workspace-unique stable resource keys/UUIDs for physical hosts, VM/LXC,
  services, databases, storage, network resources and stacks. Explicit ownership,
  lifecycle, provenance, evidence, last verification and scoped access metadata.
- One shared resource record with explicit `serves_project` links. Project-local
  `runs_on`/`depends_on` graphs. ACL filtering applies to resource, endpoint and
  project labels; hidden projects and relations are not enumerated.
- Migration `0129_infrastructure`, following the existing local `0128` migration.
- Eleven native/session and API-key operations, discovery `infrastructure.version=1`,
  strict bounded schemas, signed forward cursors, required ETags and transactional
  compare-and-set/graph validation. No retry of unknown mutation outcomes.
- Credential-shaped metadata rejection, logical OpenBao refs only, and redacted
  infrastructure API bodies/query strings in logging, including rejected input
  and DEBUG dispatch. The UI never executes an access method or dereferences a ref.

The normative routes, schemas, ACLs, errors, pagination and concurrency semantics
are in [infrastructure-contract-v1.md](infrastructure-contract-v1.md). That contract
was published before parallel client implementation and additive label fields
were communicated to the client workstream.

The checked [monitoring example](infrastructure-monitoring-example-v1.json) has
Grafana and Prometheus API methods separately from monitoring VM SSH/service/config
operations. It contains five synthetic resources and six relationships. The API
test creates it through real routes; it is not an importer or a live inventory.
`planned` lifecycle and null `last_verified` deliberately avoid claiming readiness.

## Verification

| Check                                            | Observed result                                                                     |
| ------------------------------------------------ | ----------------------------------------------------------------------------------- |
| Django migration/model drift                     | No changes detected                                                                 |
| PostgreSQL migration roundtrip                   | Full forward, `0129 → 0128 → 0129` passed                                           |
| Infrastructure API contracts                     | 73 passed, including real cookie-session CRUD and the documented monitoring example |
| Management, Builds, Teams and logging regression | 108 passed                                                                          |
| Real cookie-session CRUD/readback                | Focused test passed, without `force_authenticate`                                   |
| Documented monitoring example through API        | Focused test passed                                                                 |
| Infrastructure frontend tests                    | 32 passed                                                                           |
| Infrastructure + Builds/Teams frontend tests     | 57 passed                                                                           |
| Web route generation and TypeScript              | Passed                                                                              |
| Shared types build and typecheck                 | Passed                                                                              |
| Real-component Chrome fixture                    | 15 scenarios passed; 0 runtime/duplicate-key errors, 0 external requests            |
| Responsive list/detail/editor                    | 320, 390 and 1440 px, no horizontal document overflow                               |
| Targeted Python/TypeScript lint and formatting   | Passed; 0 targeted lint warnings/errors                                             |
| Git whitespace check                             | Passed                                                                              |
| Superproject `python3 scripts/check-project.py`  | Passed after the other workstream's canonical profile sync                          |

PostgreSQL tests cover actual API-key identity/workspace binding; active project
and workspace membership; guest restrictions; owner/consumer write authority;
hidden-project labels and key collisions; shared attach/detach and incident-edge
cleanup; CRUD/readback; invalid input without value echo; signed cursor binding
and membership revocation; concurrent creates, stale writers and graph cycles.

Browser checks execute the actual Infrastructure components, native buttons,
dialogs, router and HTTP service with current compiled Tailwind CSS. Only shell
store hooks and API responses are synthetic. They cover create/edit/delete,
named relationships, attach/detach, draft preservation after 412, unknown outcome
without retry, permission errors versus empty results, reader controls and inert
access information. This is component/browser acceptance, not a full deployed
Plane-shell/backend/session end-to-end run. Both navigation integrations are
typechecked; guest navigation behavior also has a rendering test.

The browser run found and fixed duplicate sibling React keys that repeated
relationship/project sections after an edit, and implicit labels that included
select options. Mobile native buttons have 44 px minimum touch targets. Editors
and confirmations freeze the revision observed when opened, so SWR refreshes
cannot silently replace the concurrency precondition under an existing draft.

## Reproduction

From this Plane checkout, using the existing `.venv` and PostgreSQL binaries:

```sh
python3 tools/check-infrastructure-local.py --migration-check
python3 tools/check-infrastructure-local.py \
  plane/tests/contract/api/test_management.py \
  plane/tests/contract/api/test_builds.py \
  plane/tests/contract/api/test_teams.py \
  plane/tests/unit/middleware/test_infrastructure_logger.py \
  plane/tests/unit/middleware/test_logger.py
pnpm --filter web test:infrastructure
pnpm --filter web check:types
```

The PostgreSQL harness creates a private temporary database with a Unix socket,
TCP disabled, a minimal environment, memory cache/broker and a Python network
guard. It loads no credential store or `.env`, starts no worker, and stops/deletes
its temporary database in `finally`. `--pg-bin` selects an installed PostgreSQL
directory; the macOS default is Homebrew PostgreSQL 17.

Browser reproduction uses an already installed Playwright module and Chrome:

```sh
PLAYWRIGHT_MODULE=/absolute/path/to/playwright/index.mjs \
  node tools/check-infrastructure-ui-local.mjs
```

No server is started. All browser requests are fulfilled by the fixture; unexpected
origins are aborted and fail the report. Default evidence output:
`/tmp/plane-infrastructure-ui-report/report.json`, plus list/detail/editor PNGs
at all three widths. `INFRASTRUCTURE_UI_OUTPUT` can select another local output
directory. The browser is closed after each run.

## Limits and pending acceptance

- No live inventory or connectivity was read. Source tests do not establish that
  DevOps has working Grafana/Prometheus or VM access. Deployment and operational
  readiness remain a separately authorized stage.
- Shared-resource ownership transfer is not implemented. Deleting an owning Plane
  project hides its resources from consumers; eventual project hard deletion
  cascades them. Sharing does not outlive the owning project. Key reservation
  applies to inventory deletion while its owning project/workspace exists.
- Access methods and evidence collections use validated advanced JSON fields;
  ordinary resource, owner, source and relationship fields have structured forms.
  There is no topology canvas, bulk importer, live probe or background sync.
- Browser authentication retains Plane's existing `BaseSessionAuthentication`
  behavior, which exempts DRF CSRF-token checks. `If-Match` is required on every
  mutation; trusted browser-origin/CORS configuration remains inherited. No
  stronger session-origin isolation or deployment configuration change is claimed.
- Credential-pattern rejection is defense in depth, not a universal secret
  detector. Operators must never put secret values into any inventory text.
- Existing test-environment warnings: missing collected-static directory; Node
  module/deprecation notices and Vite's tsconfig-path plugin notice. No production
  frontend bundle/deployment was built or activated as part of this acceptance.
