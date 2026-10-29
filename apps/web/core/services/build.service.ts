/** Copyright (c) 2023-present Plane Software, Inc. and contributors. SPDX-License-Identifier: AGPL-3.0-only */
import { API_BASE_URL } from "@plane/constants";
import type { TBuild, TBuildList, TBuildSummary } from "@plane/types";
import { APIService } from "@/services/api.service";

export type TBuildFilters = {
  module_id?: string;
  ref?: string;
  status?: string;
  cursor?: string;
  repository?: string;
  workflow?: string;
};

export class BuildService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  async list(workspace: string, project: string, filters: TBuildFilters = {}): Promise<TBuildList> {
    return (await this.get(`/api/workspaces/${workspace}/projects/${project}/builds/`, { params: filters })).data;
  }

  async getBuild(workspace: string, project: string, id: string): Promise<TBuild> {
    return (await this.get(`/api/workspaces/${workspace}/projects/${project}/builds/${id}/`)).data;
  }

  async summary(workspace: string, project: string, filters: TBuildFilters = {}): Promise<TBuildSummary> {
    return (await this.get(`/api/workspaces/${workspace}/projects/${project}/builds-summary/`, { params: filters }))
      .data;
  }
}
