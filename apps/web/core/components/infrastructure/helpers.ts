import type {
  TInfrastructureAccess,
  TInfrastructureEvidence,
  TInfrastructureResource,
  TInfrastructureResourceFilters,
} from "@plane/types";

export const RESOURCE_KINDS = [
  "physical_host",
  "vm",
  "lxc",
  "service",
  "database",
  "storage",
  "network",
  "stack",
] as const;
export const LIFECYCLES = ["planned", "active", "maintenance", "retired", "unknown"] as const;
export const SOURCE_KINDS = ["git", "manual", "import", "api"] as const;
export const labelFor = (value: string) => value.replaceAll("_", " ");
export const isProjectAdmin = (role: unknown) => role === 20 || role === "20";

export function resourceFilters(search: URLSearchParams): TInfrastructureResourceFilters {
  const filters: Record<string, string> = {};
  for (const key of ["kind", "key", "lifecycle", "tags", "search", "cursor"]) {
    const value = search.get(key);
    if (value) filters[key] = value;
  }
  return filters as TInfrastructureResourceFilters;
}

export function changeFilter(search: URLSearchParams, key: string, value: string): URLSearchParams {
  const next = new URLSearchParams(search);
  if (value) next.set(key, value);
  else next.delete(key);
  next.delete("cursor");
  return next;
}

export function nextPage(search: URLSearchParams, cursor: string | null): URLSearchParams {
  const next = new URLSearchParams(search);
  if (cursor) next.set("cursor", cursor);
  else next.delete("cursor");
  return next;
}

export function infrastructureError(error: unknown, mutation = false): string {
  if (error instanceof InfrastructureFormError) return error.message;
  const status = (error as { response?: { status?: number } })?.response?.status;
  if (status === 412)
    return "The record changed. Your draft is preserved. Close this editor, refresh, and review the latest revision before saving again.";
  if (status === 409)
    return "This conflicts with existing inventory or project links. Refresh and review the current record before trying again.";
  if (status === 428) return "A current revision is required. Refresh the record before making this change.";
  if (status === 403) return "You do not have permission for this project or operation.";
  if (status === 401) return "Your session has expired. Sign in again.";
  if (status === 404)
    return "This inventory, resource or project is unavailable. It may have been removed or your access may have changed.";
  if (status === 429) return "Too many requests. Wait briefly, then refresh to check the current state.";
  if (status === 400) {
    const data = (error as { response?: { data?: { error?: { code?: string; fields?: unknown } } } }).response?.data
      ?.error;
    if (data?.code === "invalid_cursor") return "This page cursor is no longer valid. Return to the first page.";
    const fields =
      data?.fields && typeof data.fields === "object"
        ? Object.keys(data.fields)
            .filter((key) => /^[a-z_][a-z0-9_.]*$/.test(key))
            .slice(0, 8)
        : [];
    return `The supplied inventory data is invalid${fields.length ? ` (${fields.join(", ")})` : ""}. Check the required fields, logical references and timestamps. Do not include secret values.`;
  }
  return mutation
    ? "The outcome is unknown. Your draft is preserved. Refresh and compare the resource key, relationship or project link before retrying; this request is never retried automatically."
    : "Infrastructure could not be loaded. Refresh to retry; a failed request is not an empty inventory.";
}

export class InfrastructureFormError extends Error {}
const fail = (message: string): never => {
  throw new InfrastructureFormError(message);
};
const unsafe =
  /\bbearer\s+\S+|-----BEGIN[^\n]*PRIVATE KEY|\b(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+/i;
function text(value: unknown, label: string, max = 1024): asserts value is string {
  if (
    typeof value !== "string" ||
    !value.trim() ||
    value.length > max ||
    unsafe.test(value) ||
    [...value].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
  )
    fail(`${label} must be nonempty metadata within its length limit, without credentials or control characters.`);
}
function shape(value: unknown, required: string[], optional: string[], label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${label} must be an object.`);
  const object = value as Record<string, unknown>;
  if (
    required.some((key) => !(key in object)) ||
    Object.keys(object).some((key) => !required.includes(key) && !optional.includes(key))
  )
    fail(`${label} has missing or unsupported fields.`);
  return object;
}
function array(textValue: string, label: string, max: number): unknown[] {
  let value: unknown;
  try {
    value = JSON.parse(textValue);
  } catch {
    return fail(`${label} must be valid JSON. Rejected input is not echoed.`);
  }
  if (!Array.isArray(value) || value.length > max) fail(`${label} must be an array with at most ${max} entries.`);
  return value as unknown[];
}
function timestamp(value: unknown, label: string): asserts value is string {
  text(value, label, 64);
  const parsed = Date.parse(value);
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value) || Number.isNaN(parsed) || parsed > Date.now())
    fail(`${label} must be a timezone-aware timestamp in the past or present.`);
}
export function parseEvidence(value: string): TInfrastructureEvidence[] {
  return array(value, "Evidence", 50).map((entry) => {
    const item = shape(entry, ["kind", "reference", "observed_at"], [], "Evidence entry");
    if (!["review", "observation", "document"].includes(String(item.kind)))
      fail("Evidence kind must be review, observation or document.");
    text(item.reference, "Evidence reference");
    timestamp(item.observed_at, "Evidence observation time");
    return item as TInfrastructureEvidence;
  });
}
export function parseLastVerified(value: string, evidence: TInfrastructureEvidence[]): string | null {
  if (!value.trim()) return null;
  timestamp(value.trim(), "Last verified");
  if (!evidence.length) fail("Last verified requires at least one evidence entry.");
  return new Date(value).toISOString();
}
export function parseAccessMethods(value: string): TInfrastructureAccess[] {
  const ids = new Set<string>();
  return array(value, "Access methods", 20).map((entry) => {
    const item = shape(
      entry,
      ["id", "kind", "endpoint", "capability", "operations", "scope", "identity"],
      ["credential_refs", "notes"],
      "Access method"
    );
    text(item.id, "Access method ID", 64);
    if (!/^[a-z0-9][a-z0-9._:-]*$/.test(item.id) || ids.has(item.id))
      fail("Access method IDs must be unique lowercase slugs.");
    ids.add(item.id);
    if (
      !["ssh", "http", "https", "postgresql", "mysql", "s3", "smb", "nfs", "console", "other"].includes(
        String(item.kind)
      )
    )
      fail("Access method kind is invalid.");
    text(item.endpoint, "Access endpoint");
    if (/[\s\\?#]/.test(item.endpoint))
      fail("Access endpoints must not contain whitespace, backslashes, queries or fragments.");
    let endpoint: URL;
    try {
      endpoint = new URL(item.endpoint);
    } catch {
      return fail("Access endpoints must be absolute URIs.");
    }
    const scheme = ["console", "other"].includes(String(item.kind)) ? "https" : item.kind;
    if (
      endpoint.protocol !== `${scheme}:` ||
      !endpoint.hostname ||
      endpoint.username ||
      endpoint.password ||
      endpoint.search ||
      endpoint.hash
    )
      fail("Access endpoint must use its declared scheme and contain no userinfo, query or fragment.");
    text(item.capability, "Capability", 128);
    if (!/^[a-z0-9][a-z0-9._:-]*$/.test(item.capability)) fail("Capability must be a lowercase slug.");
    text(item.scope, "Access scope");
    text(item.identity, "Identity selector", 255);
    if (
      !Array.isArray(item.operations) ||
      !item.operations.length ||
      item.operations.length > 30 ||
      new Set(item.operations).size !== item.operations.length
    )
      fail("Operations must contain 1–30 unique slugs.");
    for (const operation of item.operations as unknown[]) {
      text(operation, "Operation", 128);
      if (!/^[a-z0-9][a-z0-9._:-]*$/.test(operation)) fail("Operations must be lowercase slugs.");
    }
    if (item.notes !== undefined && item.notes !== "") text(item.notes, "Notes", 1000);
    if (item.credential_refs !== undefined) {
      if (!Array.isArray(item.credential_refs) || item.credential_refs.length > 10)
        fail("Credential references must be an array with at most 10 entries.");
      for (const reference of item.credential_refs as unknown[]) {
        const ref = shape(reference, ["provider", "path", "field", "purpose"], [], "Credential reference");
        if (ref.provider !== "openbao") fail("Credential references must use the openbao provider.");
        text(ref.path, "Logical OpenBao path", 512);
        if (
          !/^[a-zA-Z0-9._-]+(?:\/[a-zA-Z0-9._-]+)*$/.test(ref.path) ||
          ref.path.split("/").some((segment) => [".", ".."].includes(segment))
        )
          fail("Use a logical OpenBao path without empty or dot segments.");
        text(ref.field, "Credential field", 128);
        if (!/^[a-z0-9][a-z0-9._:-]*$/.test(ref.field)) fail("Credential field must be a lowercase slug.");
        text(ref.purpose, "Credential purpose", 512);
      }
    }
    return item as TInfrastructureAccess;
  });
}
export function exactResource(resources: TInfrastructureResource[], key: string): TInfrastructureResource {
  if (resources.length !== 1 || resources[0].key !== key)
    fail("The exact resource key must resolve to one visible resource in this project.");
  return resources[0];
}
export const formatTimestamp = (value: string | null) => (value ? new Date(value).toLocaleString() : "Not verified");
