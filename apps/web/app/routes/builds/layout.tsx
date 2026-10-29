/** Copyright (c) 2023-present Plane Software, Inc. and contributors. SPDX-License-Identifier: AGPL-3.0-only */
import { Outlet, Link, useParams } from "react-router";
import { Header } from "@plane/ui";
import { AppHeader } from "@/components/core/app-header";
import { ContentWrapper } from "@/components/core/content-wrapper";

export default function BuildsLayout() {
  const { workspaceSlug, projectId } = useParams();
  return (
    <>
      <AppHeader
        header={
          <Header>
            <Header.LeftItem>
              <Link className="text-13 font-medium" to={`/${workspaceSlug}/projects/${projectId}/builds`}>
                Builds
              </Link>
            </Header.LeftItem>
          </Header>
        }
      />
      <ContentWrapper>
        <Outlet />
      </ContentWrapper>
    </>
  );
}
