import { describe, expect, it } from "vitest";
import type { TTeamActor, TTeamContact, TTeamMember } from "@plane/types";
import {
  contactStatus,
  legacyBlocksAddresses,
  matchesTeamSearch,
  targetFromActor,
  teamErrorMessage,
  teamRoleName,
} from "./helpers";

const actor: TTeamActor = {
  id: "registration",
  registration_id: "registration",
  runtime: "codex",
  agent_key: "chat-review",
  user_id: "user",
  display_name: "Release reviewer",
  role: "Review frontend changes",
  active: true,
  member: true,
  revision: 1,
  can_manage: true,
  binding: { thread_id: "chat", host_id: "local", profile: "should-not-copy", agent_id: "should-not-copy" },
};

describe("Teams search and form defaults", () => {
  it("blocks address edits until an existing page is imported or reconciled", () => {
    expect(legacyBlocksAddresses("pending")).toBe(true);
    expect(legacyBlocksAddresses("conflict")).toBe(true);
    expect(legacyBlocksAddresses("imported")).toBe(false);
    expect(legacyBlocksAddresses("none")).toBe(false);
  });
  it("finds combined name and responsibility terms without case sensitivity", () => {
    expect(matchesTeamSearch(actor, " RELEASE frontend ")).toBe(true);
    expect(matchesTeamSearch(actor, "release backend")).toBe(false);
    expect(matchesTeamSearch(actor, " ")).toBe(true);
  });
  it("finds member roles by their display labels", () => {
    const member: TTeamMember = {
      id: "user",
      membership_id: "membership",
      display_name: "Yuri",
      role: 20,
      is_active: true,
      is_bot: false,
    };
    expect(matchesTeamSearch(member, "admin")).toBe(true);
    expect(teamRoleName(15)).toBe("Member");
    expect(teamRoleName(42)).toBe("Role 42");
  });
  it("copies only fields belonging to the selected receiver type", () => {
    expect(targetFromActor(actor)).toEqual({ thread_id: "chat", host_id: "local" });
    expect(
      targetFromActor({
        ...actor,
        runtime: "hermes",
        binding: {
          agent_id: "receiver",
          host_id: "host",
          profile: "review",
          session_id: "session",
          thread_id: "wrong",
        },
      })
    ).toEqual({ agent_id: "receiver", host_id: "host", profile: "review", conversation_id: "session" });
    expect(targetFromActor()).toEqual({});
  });
  it("does not treat active but stale bindings as available", () => {
    const contact = { active: true, available: false } as TTeamContact;
    expect(contactStatus(contact)).toBe("Binding unavailable");
    expect(contactStatus({ ...contact, available: true })).toBe("Available");
    expect(contactStatus({ ...contact, active: false })).toBe("Inactive");
  });
  it("explains CAS conflicts without suggesting a blind retry", () => {
    for (const status of [409, 412])
      expect(teamErrorMessage({ response: { status } })).toMatch(/review the latest data/);
    expect(teamErrorMessage({ response: { status: 403 } })).toMatch(/permission/);
  });
  it("shows safe validation strings and never serializes unknown errors", () => {
    expect(teamErrorMessage({ response: { status: 400, data: { address: ["Already registered."] } } })).toBe(
      "address: Already registered."
    );
    expect(teamErrorMessage({ request: { token: "not-for-ui" } })).not.toContain("not-for-ui");
  });
});
