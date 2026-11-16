import type { TInfrastructureResource } from "@plane/types";
export const resourceFixture: TInfrastructureResource = {
  id: "11111111-1111-4111-8111-111111111111",
  key: "monitoring/grafana",
  name: "Grafana",
  kind: "service",
  lifecycle: "active",
  description: "Monitoring dashboards",
  tags: ["monitoring"],
  owner: { name: "Operations", reference: "team:operations", recovery_contact: "role:supervisor" },
  source: { kind: "git", reference: "repo:monitoring", revision: "synthetic-v1" },
  evidence: [{ kind: "review", reference: "doc:review", observed_at: "2026-01-01T00:00:00Z" }],
  last_verified: "2026-01-01T00:00:00Z",
  access_methods: [
    {
      id: "grafana-api",
      kind: "https",
      endpoint: "https://grafana.example.invalid",
      capability: "grafana",
      operations: ["dashboards.read"],
      scope: "monitoring organization",
      identity: "runtime-own-identity",
      credential_refs: [
        { provider: "openbao", path: "projects/example/grafana", field: "api_token", purpose: "Read dashboards" },
      ],
    },
  ],
  revision: 3,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  ownership_project_id: "owner-project",
  can_manage: true,
  can_detach: false,
};
