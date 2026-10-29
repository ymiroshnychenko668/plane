import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TTeamActor, TTeamActorCreate, TTeamContact } from "@plane/types";
import { TeamService } from "@/services/team.service";
import { getTabUrl } from "../navigation/tab-navigation-utils";

const calls = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock("@/services/api.service", () => ({
  APIService: class {
    get = calls.get;
    post = calls.post;
    patch = calls.patch;
    put = calls.put;
    delete = calls.delete;
  },
}));

const service = new TeamService();
const base = "/api/workspaces/work/projects/project/team/";
beforeEach(() => {
  for (const call of Object.values(calls)) call.mockReset().mockResolvedValue({ data: {} });
});

describe("Teams browser API contract", () => {
  it("reads the same project roster and resolves addresses within its project", async () => {
    await service.roster("work", "project");
    expect(calls.get).toHaveBeenCalledWith(base);
    await service.resolve("work", "project", "reviewer");
    expect(calls.post).toHaveBeenCalledWith(`${base}resolve/`, { address: "reviewer" });
    expect(getTabUrl("work", "project", "teams")).toBe("/work/projects/project/teams");
  });
  it("sends observed contact revisions and cannot interpolate addresses as paths", async () => {
    const data = { registration_id: "agent", target: { thread_id: "chat" }, active: true };
    await service.saveContact("work", "project", "unsafe/path", data, 7);
    expect(calls.put).toHaveBeenCalledWith(`${base}contacts/unsafe%2Fpath/`, data, { headers: { "If-Match": '"7"' } });
    const contact = { address: "reviewer", revision: 9 } as TTeamContact;
    await service.deactivateContact("work", "project", contact);
    expect(calls.delete).toHaveBeenCalledWith(`${base}contacts/reviewer/`, undefined, {
      headers: { "If-Match": '"9"' },
    });
  });
  it("creates only an absent identity and preserves revisions for agent edits", async () => {
    const create = {
      runtime: "codex",
      agent_key: "review",
      user_id: "user",
      display_name: "Reviewer",
      role: "Review",
      binding: { thread_id: "chat", binding_id: "binding" },
    } satisfies TTeamActorCreate;
    await service.createActor("work", "project", create);
    expect(calls.post).toHaveBeenCalledWith(`${base}agents/`, create, { headers: { "If-Match": '"0"' } });
    const actor = { registration_id: "agent", revision: 4 } as TTeamActor;
    const update = { display_name: "Reviewer", role: "Review", active: false };
    await service.updateActor("work", "project", actor, update);
    expect(calls.patch).toHaveBeenCalledWith(`${base}agents/agent/`, update, { headers: { "If-Match": '"4"' } });
  });
  it("keeps membership identity distinct from registration and membership IDs", async () => {
    await service.saveMember("work", "project", "user", 15, false);
    expect(calls.post).toHaveBeenCalledWith(`${base}members/user/`, { role: 15 });
    await service.saveMember("work", "project", "user", 5, true);
    expect(calls.patch).toHaveBeenCalledWith(`${base}members/user/`, { role: 5 });
    await service.removeMember("work", "project", "user");
    expect(calls.delete).toHaveBeenCalledWith(`${base}members/user/`);
  });
  it("surfaces conflicts without retries or writes to the legacy page", async () => {
    const error = { response: { status: 412 } };
    calls.put.mockRejectedValue(error);
    await expect(
      service.saveContact("work", "project", "reviewer", { registration_id: "agent", target: {}, active: true }, 7)
    ).rejects.toBe(error);
    expect(calls.put).toHaveBeenCalledTimes(1);
    expect(calls.post).not.toHaveBeenCalled();
    await service.importLegacy("work", "project");
    expect(calls.post).toHaveBeenCalledWith(`${base}import/`);
  });
});
