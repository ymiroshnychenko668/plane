import type { TTeamActor, TTeamContact, TTeamMember, TTeamTarget } from "@plane/types";

export const TEAM_ROLES = [
  { value: 20, label: "Admin" },
  { value: 15, label: "Member" },
  { value: 5, label: "Guest" },
];
export const teamRoleName = (role: number) => TEAM_ROLES.find((item) => item.value === role)?.label ?? `Role ${role}`;

export const legacyBlocksAddresses = (status?: string) => status === "pending" || status === "conflict";

export function targetFromActor(actor?: TTeamActor): TTeamTarget {
  if (!actor) return {};
  const binding = actor.binding;
  const keys =
    actor.runtime === "codex" ? (["thread_id", "host_id"] as const) : (["agent_id", "host_id", "profile"] as const);
  const target: TTeamTarget = {};
  for (const key of keys) if (binding[key]) target[key] = binding[key];
  if (actor.runtime === "hermes" && binding.session_id) target.conversation_id = binding.session_id;
  return target;
}

export function matchesTeamSearch(item: TTeamActor | TTeamContact | TTeamMember, query: string): boolean {
  const words = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  const values = [
    item.display_name,
    typeof item.role === "number" ? teamRoleName(item.role) : (item.role ?? ""),
    "agent_key" in item ? item.agent_key : "",
    "address" in item ? item.address : "",
    "runtime" in item ? `${item.runtime} ${item.runtime === "hermes" ? "server agent" : "codex"}` : "",
  ]
    .join(" ")
    .toLocaleLowerCase();
  return words.every((word) => values.includes(word));
}

export function teamErrorMessage(error: unknown): string {
  const response = (error as { response?: { status?: number; data?: unknown } })?.response;
  if (response?.status === 412 || response?.status === 409)
    return "This change conflicts with the current project data. Refresh the team and review the latest data before saving again.";
  if (response?.status === 403) return "You do not have permission to make this change.";
  if (response?.status === 404) return "This team record is unavailable. Refresh the team to check its current state.";
  const data = response?.data;
  if (response?.status === 400 && data && typeof data === "object") {
    const messages = Object.entries(data).flatMap(([key, value]) => {
      const values = Array.isArray(value) ? value : [value];
      return values.filter((v): v is string => typeof v === "string").map((v) => `${key}: ${v}`);
    });
    if (messages.length) return messages.join(" ");
  }
  return "The request could not be completed. Refresh to check the current state before retrying.";
}

export function contactStatus(contact: TTeamContact): string {
  return !contact.active ? "Inactive" : contact.available ? "Available" : "Binding unavailable";
}
