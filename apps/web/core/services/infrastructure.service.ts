import { API_BASE_URL } from "@plane/constants";
import type {
  TInfrastructurePage,
  TInfrastructurePagination,
  TInfrastructureProjectLink,
  TInfrastructureRelation,
  TInfrastructureRelationFilters,
  TInfrastructureRelationWrite,
  TInfrastructureResource,
  TInfrastructureResourceFilters,
  TInfrastructureResourceWrite,
} from "@plane/types";
import { APIService } from "@/services/api.service";

export class InfrastructureService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }
  private base(workspace: string, project: string) {
    return `/api/workspaces/${encodeURIComponent(workspace)}/projects/${encodeURIComponent(project)}/infrastructure/`;
  }
  private revision(revision: number) {
    return { headers: { "If-Match": `"${revision}"` } };
  }
  async listResources(
    workspace: string,
    project: string,
    filters: TInfrastructureResourceFilters = {}
  ): Promise<TInfrastructurePage<TInfrastructureResource>> {
    return (await this.get(`${this.base(workspace, project)}resources/`, { params: filters })).data;
  }
  async getResource(workspace: string, project: string, id: string): Promise<TInfrastructureResource> {
    return (await this.get(`${this.base(workspace, project)}resources/${encodeURIComponent(id)}/`)).data;
  }
  async createResource(
    workspace: string,
    project: string,
    data: TInfrastructureResourceWrite
  ): Promise<TInfrastructureResource> {
    return (await this.post(`${this.base(workspace, project)}resources/`, data, this.revision(0))).data;
  }
  async updateResource(
    workspace: string,
    project: string,
    id: string,
    data: Partial<TInfrastructureResourceWrite>,
    revision: number
  ): Promise<TInfrastructureResource> {
    return (
      await this.patch(
        `${this.base(workspace, project)}resources/${encodeURIComponent(id)}/`,
        data,
        this.revision(revision)
      )
    ).data;
  }
  async deleteResource(workspace: string, project: string, id: string, revision: number): Promise<void> {
    await this.delete(
      `${this.base(workspace, project)}resources/${encodeURIComponent(id)}/`,
      undefined,
      this.revision(revision)
    );
  }
  async listRelations(
    workspace: string,
    project: string,
    filters: TInfrastructureRelationFilters = {}
  ): Promise<TInfrastructurePage<TInfrastructureRelation>> {
    return (await this.get(`${this.base(workspace, project)}relations/`, { params: filters })).data;
  }
  async createRelation(
    workspace: string,
    project: string,
    data: TInfrastructureRelationWrite
  ): Promise<TInfrastructureRelation> {
    return (await this.post(`${this.base(workspace, project)}relations/`, data, this.revision(0))).data;
  }
  async deleteRelation(workspace: string, project: string, id: string, revision: number): Promise<void> {
    await this.delete(
      `${this.base(workspace, project)}relations/${encodeURIComponent(id)}/`,
      undefined,
      this.revision(revision)
    );
  }
  async listLinks(
    workspace: string,
    project: string,
    id: string,
    pagination: TInfrastructurePagination = {}
  ): Promise<TInfrastructurePage<TInfrastructureProjectLink>> {
    return (
      await this.get(`${this.base(workspace, project)}resources/${encodeURIComponent(id)}/project-links/`, {
        params: pagination,
      })
    ).data;
  }
  async attachProject(
    workspace: string,
    project: string,
    id: string,
    target: string,
    revision: number
  ): Promise<TInfrastructureResource> {
    return (
      await this.post(
        `${this.base(workspace, project)}resources/${encodeURIComponent(id)}/project-links/`,
        { project_id: target },
        this.revision(revision)
      )
    ).data;
  }
  async detachProject(workspace: string, project: string, id: string, target: string, revision: number): Promise<void> {
    await this.delete(
      `${this.base(workspace, project)}resources/${encodeURIComponent(id)}/project-links/${encodeURIComponent(target)}/`,
      undefined,
      this.revision(revision)
    );
  }
}
