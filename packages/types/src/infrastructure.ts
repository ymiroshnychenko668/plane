/** Infrastructure v1: inventory metadata only, never credentials or grants. */
export type TInfrastructureKind =
  | "physical_host"
  | "vm"
  | "lxc"
  | "service"
  | "database"
  | "storage"
  | "network"
  | "stack";
export type TInfrastructureLifecycle = "planned" | "active" | "maintenance" | "retired" | "unknown";
export type TInfrastructureSource = { kind: "git" | "manual" | "import" | "api"; reference: string; revision: string };
export type TInfrastructureEvidence = {
  kind: "review" | "observation" | "document";
  reference: string;
  observed_at: string;
};
export type TInfrastructureOwner = { name: string; reference: string; recovery_contact: string };
export type TInfrastructureCredentialRef = { provider: "openbao"; path: string; field: string; purpose: string };
export type TInfrastructureAccess = {
  id: string;
  kind: "ssh" | "https" | "postgresql" | "mysql" | "s3" | "smb" | "nfs" | "console" | "other";
  endpoint: string;
  capability: string;
  operations: string[];
  scope: string;
  identity: string;
  credential_refs?: TInfrastructureCredentialRef[];
  notes?: string;
};
export type TInfrastructureResourceWrite = {
  key: string;
  name: string;
  kind: TInfrastructureKind;
  owner: TInfrastructureOwner;
  source: TInfrastructureSource;
  lifecycle?: TInfrastructureLifecycle;
  description?: string;
  tags?: string[];
  evidence?: TInfrastructureEvidence[];
  last_verified?: string | null;
  access_methods?: TInfrastructureAccess[];
};
export type TInfrastructureResource = Required<TInfrastructureResourceWrite> & {
  id: string;
  revision: number;
  created_at: string;
  updated_at: string;
  ownership_project_id: string | null;
  can_manage: boolean;
  can_detach: boolean;
};
export type TInfrastructureRelationWrite = {
  source_id: string;
  target_id: string;
  kind: "runs_on" | "depends_on";
  source: TInfrastructureSource;
  evidence?: TInfrastructureEvidence[];
  last_verified?: string | null;
};
export type TInfrastructureRelation = Required<TInfrastructureRelationWrite> & {
  id: string;
  project_id: string;
  source_name: string;
  source_key: string;
  target_name: string;
  target_key: string;
  revision: number;
  created_at: string;
  can_manage: boolean;
};
export type TInfrastructureProjectLink = {
  id: string;
  resource_id: string;
  project_id: string;
  kind: "serves_project";
  project_name: string;
  is_owner: boolean;
  revision: number;
};
export type TInfrastructurePage<T> = { results: T[]; next_cursor: string | null; has_more: boolean; per_page: number };
export type TInfrastructurePagination = { cursor?: string; per_page?: number };
export type TInfrastructureResourceFilters = TInfrastructurePagination & {
  kind?: TInfrastructureKind;
  key?: string;
  lifecycle?: TInfrastructureLifecycle;
  tags?: string;
  search?: string;
};
export type TInfrastructureRelationFilters = TInfrastructurePagination & {
  kind?: TInfrastructureRelation["kind"];
  source_id?: string;
  target_id?: string;
  resource_id?: string;
};
