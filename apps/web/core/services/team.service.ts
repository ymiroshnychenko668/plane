import { API_BASE_URL } from "@plane/constants";
import type {
  IWorkspaceMember,
  TProjectTeam,
  TTeamActor,
  TTeamActorCreate,
  TTeamContact,
  TTeamContactWrite,
} from "@plane/types";
import { APIService } from "@/services/api.service";

export class TeamService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  private base(workspace: string, project: string) {
    return `/api/workspaces/${encodeURIComponent(workspace)}/projects/${encodeURIComponent(project)}/team/`;
  }

  async roster(workspace: string, project: string): Promise<TProjectTeam> {
    return (await this.get(this.base(workspace, project))).data;
  }

  async saveContact(
    workspace: string,
    project: string,
    address: string,
    data: TTeamContactWrite,
    revision: number
  ): Promise<TTeamContact> {
    return (
      await this.put(`${this.base(workspace, project)}contacts/${encodeURIComponent(address)}/`, data, {
        headers: { "If-Match": `"${revision}"` },
      })
    ).data;
  }

  async deactivateContact(workspace: string, project: string, contact: TTeamContact): Promise<void> {
    await this.delete(`${this.base(workspace, project)}contacts/${encodeURIComponent(contact.address)}/`, undefined, {
      headers: { "If-Match": `"${contact.revision}"` },
    });
  }

  async updateActor(
    workspace: string,
    project: string,
    actor: TTeamActor,
    data: { display_name: string; role: string; active: boolean }
  ): Promise<TTeamActor> {
    return (
      await this.patch(`${this.base(workspace, project)}agents/${actor.registration_id}/`, data, {
        headers: { "If-Match": `"${actor.revision}"` },
      })
    ).data;
  }

  async createActor(workspace: string, project: string, data: TTeamActorCreate): Promise<TTeamActor> {
    return (await this.post(`${this.base(workspace, project)}agents/`, data, { headers: { "If-Match": '"0"' } })).data;
  }

  async resolve(workspace: string, project: string, address: string): Promise<TTeamContact> {
    return (await this.post(`${this.base(workspace, project)}resolve/`, { address })).data;
  }

  async importLegacy(
    workspace: string,
    project: string
  ): Promise<{ status: "imported"; page_id: string; changed: boolean; imported?: number }> {
    return (await this.post(`${this.base(workspace, project)}import/`)).data;
  }

  async workspaceMembers(workspace: string): Promise<IWorkspaceMember[]> {
    return (await this.get(`/api/workspaces/${encodeURIComponent(workspace)}/members/`)).data;
  }

  async saveMember(workspace: string, project: string, userId: string, role: number, exists: boolean): Promise<void> {
    const url = `${this.base(workspace, project)}members/${encodeURIComponent(userId)}/`;
    if (exists) await this.patch(url, { role });
    else await this.post(url, { role });
  }

  async removeMember(workspace: string, project: string, userId: string): Promise<void> {
    await this.delete(`${this.base(workspace, project)}members/${encodeURIComponent(userId)}/`);
  }
}
