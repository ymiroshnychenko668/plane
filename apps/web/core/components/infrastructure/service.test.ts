import { beforeEach, describe, expect, it, vi } from "vitest";
import { InfrastructureService } from "@/services/infrastructure.service";
import { getTabUrl } from "../navigation/tab-navigation-utils";
import { resourceFixture } from "./fixtures";
const calls = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }));
vi.mock("@/services/api.service", () => ({
  APIService: class {
    get = calls.get;
    post = calls.post;
    patch = calls.patch;
    delete = calls.delete;
  },
}));
const service = new InfrastructureService();
const base = "/api/workspaces/work/projects/project/infrastructure/";
beforeEach(() => {
  for (const call of Object.values(calls)) call.mockReset().mockResolvedValue({ data: {} });
});
describe("Infrastructure native session contract", () => {
  it("reads resources, relations and links using scoped pagination", async () => {
    const filters = { key: "monitoring/grafana", cursor: "opaque", per_page: 25 };
    await service.listResources("work", "project", filters);
    expect(calls.get).toHaveBeenCalledWith(`${base}resources/`, { params: filters });
    await service.getResource("work", "project", "id");
    expect(calls.get).toHaveBeenCalledWith(`${base}resources/id/`);
    await service.listRelations("work", "project", { resource_id: "id" });
    expect(calls.get).toHaveBeenCalledWith(`${base}relations/`, { params: { resource_id: "id" } });
    await service.listLinks("work", "project", "id", { cursor: "next" });
    expect(calls.get).toHaveBeenCalledWith(`${base}resources/id/project-links/`, { params: { cursor: "next" } });
    expect(getTabUrl("work", "project", "infrastructure")).toBe("/work/projects/project/infrastructure");
  });
  it("encodes all path segments instead of changing project scope", async () => {
    await service.getResource("unsafe/work", "unsafe/project", "unsafe/id");
    expect(calls.get).toHaveBeenCalledWith(
      "/api/workspaces/unsafe%2Fwork/projects/unsafe%2Fproject/infrastructure/resources/unsafe%2Fid/"
    );
  });
  it("requires an absent revision on creates and observed revisions on updates/deletes", async () => {
    const { key, name, kind, owner, source } = resourceFixture;
    const data = { key, name, kind, owner, source };
    await service.createResource("work", "project", data);
    expect(calls.post).toHaveBeenCalledWith(`${base}resources/`, data, { headers: { "If-Match": '"0"' } });
    await service.updateResource("work", "project", "id", { name: "Updated" }, 3);
    expect(calls.patch).toHaveBeenCalledWith(
      `${base}resources/id/`,
      { name: "Updated" },
      { headers: { "If-Match": '"3"' } }
    );
    await service.deleteResource("work", "project", "id", 4);
    expect(calls.delete).toHaveBeenCalledWith(`${base}resources/id/`, undefined, { headers: { "If-Match": '"4"' } });
  });
  it("shares the same resource and never recreates it in the consumer project", async () => {
    await service.attachProject("work", "project", "id", "consumer", 5);
    expect(calls.post).toHaveBeenCalledWith(
      `${base}resources/id/project-links/`,
      { project_id: "consumer" },
      { headers: { "If-Match": '"5"' } }
    );
    await service.detachProject("work", "project", "id", "consumer/id", 6);
    expect(calls.delete).toHaveBeenCalledWith(`${base}resources/id/project-links/consumer%2Fid/`, undefined, {
      headers: { "If-Match": '"6"' },
    });
    expect(calls.post).toHaveBeenCalledTimes(1);
  });
  it("uses relation revisions for removal and source identity from resolved IDs", async () => {
    const data = { source_id: "one", target_id: "two", kind: "depends_on" as const, source: resourceFixture.source };
    await service.createRelation("work", "project", data);
    expect(calls.post).toHaveBeenCalledWith(`${base}relations/`, data, { headers: { "If-Match": '"0"' } });
    await service.deleteRelation("work", "project", "relation", 7);
    expect(calls.delete).toHaveBeenCalledWith(`${base}relations/relation/`, undefined, {
      headers: { "If-Match": '"7"' },
    });
  });
  it.each([412, 500])("surfaces mutation failure %s without blind retries", async (status) => {
    const error = { response: { status } };
    calls.patch.mockRejectedValue(error);
    await expect(service.updateResource("work", "project", "id", { name: "Updated" }, 1)).rejects.toBe(error);
    expect(calls.patch).toHaveBeenCalledTimes(1);
    expect(calls.post).not.toHaveBeenCalled();
    expect(calls.get).not.toHaveBeenCalled();
  });
});
