/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";
import type { IProjectView, TProjectViewEvaluation, TProjectViewPartition } from "@plane/types";
import { APIService } from "@/services/api.service";
// types
// helpers

export class ViewService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  async createView(workspaceSlug: string, projectId: string, data: Partial<IProjectView>): Promise<any> {
    return (await this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/views/`, data)).data;
  }

  async patchView(
    workspaceSlug: string,
    projectId: string,
    viewId: string,
    data: Partial<IProjectView>,
    revision?: number
  ): Promise<IProjectView> {
    const { revision: _revision, request_id: _requestId, ...payload } = data;
    return (
      await this.patch(`/api/workspaces/${workspaceSlug}/projects/${projectId}/views/${viewId}/`, payload, {
        headers: revision === undefined ? {} : { "If-Match": `"${revision}"` },
      })
    ).data;
  }

  async deleteView(workspaceSlug: string, projectId: string, viewId: string, revision?: number): Promise<void> {
    await this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/views/${viewId}/`, undefined, {
      headers: revision === undefined ? {} : { "If-Match": `"${revision}"` },
    });
  }

  async evaluateView(
    workspaceSlug: string,
    projectId: string,
    viewId: string,
    params: { partition: TProjectViewPartition; cursor?: string },
    signal?: AbortSignal
  ): Promise<TProjectViewEvaluation> {
    return (
      await this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/views/${viewId}/work-items/`, {
        params: { ...params, per_page: 50 },
        signal,
      })
    ).data;
  }

  async previewView(
    workspaceSlug: string,
    projectId: string,
    definition: Pick<IProjectView, "rich_filters" | "filter_meta">,
    params: { partition: TProjectViewPartition; cursor?: string },
    signal?: AbortSignal
  ): Promise<TProjectViewEvaluation> {
    return (
      await this.post(
        `/api/workspaces/${workspaceSlug}/projects/${projectId}/views/preview/`,
        { ...definition, display_filters: { layout: "list" }, ...params, per_page: 50 },
        { signal }
      )
    ).data;
  }

  async getViews(workspaceSlug: string, projectId: string): Promise<IProjectView[]> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/views/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getViewDetails(workspaceSlug: string, projectId: string, viewId: string): Promise<IProjectView> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/views/${viewId}/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getViewIssues(workspaceSlug: string, projectId: string, viewId: string): Promise<any> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/views/${viewId}/issues/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async addViewToFavorites(
    workspaceSlug: string,
    projectId: string,
    data: {
      view: string;
    }
  ): Promise<any> {
    return this.post(`/api/workspaces/${workspaceSlug}/projects/${projectId}/user-favorite-views/`, data)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async removeViewFromFavorites(workspaceSlug: string, projectId: string, viewId: string): Promise<any> {
    return this.delete(`/api/workspaces/${workspaceSlug}/projects/${projectId}/user-favorite-views/${viewId}/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
