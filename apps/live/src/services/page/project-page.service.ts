/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { AppError } from "@/lib/errors";
import { PageService } from "./extended.service";
import { env } from "@/env";

interface ProjectPageServiceParams {
  workspaceSlug: string | null;
  projectId: string | null;
  cookie: string | null;
  apiKey?: string;
  [key: string]: unknown;
}

export class ProjectPageService extends PageService {
  protected basePath: string;

  constructor(params: ProjectPageServiceParams) {
    super();
    const { workspaceSlug, projectId } = params;
    if (!workspaceSlug || !projectId) throw new AppError("Missing required fields.");
    // validate cookie
    if (!params.cookie && !params.apiKey) throw new AppError("Cookie or backend API key is required.");
    // set cookie
    if (params.apiKey) {
      this.setHeader("X-Api-Key", params.apiKey);
      this.setHeader("live-server-secret-key", env.LIVE_SERVER_SECRET_KEY);
    } else {
      this.setHeader("Cookie", params.cookie!);
    }
    // set base path
    this.basePath = `${params.apiKey ? "/api/v1" : "/api"}/workspaces/${workspaceSlug}/projects/${projectId}`;
  }
}
