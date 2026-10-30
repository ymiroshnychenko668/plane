/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { layout, route, type RouteConfigEntry } from "@react-router/dev/routes";

export const extendedRoutes: RouteConfigEntry[] = [
  layout("./(all)/layout.tsx", [
    layout("./(all)/[workspaceSlug]/layout.tsx", [
      layout("./(all)/[workspaceSlug]/(projects)/layout.tsx", [
        layout("./(all)/[workspaceSlug]/(projects)/projects/(detail)/[projectId]/layout.tsx", [
          layout("./routes/infrastructure/layout.tsx", [
            route(":workspaceSlug/projects/:projectId/infrastructure", "./routes/infrastructure/page.tsx"),
            route(
              ":workspaceSlug/projects/:projectId/infrastructure/:resourceId",
              "./routes/infrastructure/detail.tsx"
            ),
          ]),
          layout("./routes/teams/layout.tsx", [
            route(":workspaceSlug/projects/:projectId/teams", "./routes/teams/page.tsx"),
          ]),
          layout("./routes/builds/layout.tsx", [
            route(":workspaceSlug/projects/:projectId/builds", "./routes/builds/page.tsx"),
            route(":workspaceSlug/projects/:projectId/builds/:buildId", "./routes/builds/detail.tsx"),
            route(":workspaceSlug/projects/:projectId/modules/:moduleId/builds", "./routes/builds/module.tsx"),
          ]),
        ]),
      ]),
    ]),
  ]),
];
