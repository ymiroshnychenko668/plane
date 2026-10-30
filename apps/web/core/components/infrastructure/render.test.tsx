import { createElement } from "react";
import type { ReactNode } from "react";
import type { IPartialProject } from "@plane/types";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InfrastructureDetailPage, InfrastructurePage } from "./infrastructure-page";
import { ResourceEditor, RelationEditor, AttachEditor } from "./editors";
import { useNavigationItems } from "../navigation/use-navigation-items";
import { resourceFixture } from "./fixtures";

const state = vi.hoisted(() => ({
  role: 20,
  data: {} as Record<string, unknown>,
  errors: {} as Record<string, unknown>,
}));
vi.mock("react-router", () => ({
  useParams: () => ({ workspaceSlug: "work", projectId: "owner-project", resourceId: "resource" }),
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
  Link: ({ to, children, ...props }: { to: string; children: ReactNode }) =>
    createElement("a", { ...props, href: to }, children),
}));
vi.mock("swr", () => ({
  default: (key: string[]) => ({
    data: state.data[key[0]],
    error: state.errors[key[0]],
    mutate: vi.fn(),
    isLoading: !state.data[key[0]],
  }),
}));
vi.mock("@/hooks/store/user", () => ({
  useUserPermissions: () => ({ getProjectRoleByWorkspaceSlugAndProjectId: () => state.role }),
}));
vi.mock("@/hooks/store/use-project", () => ({
  useProject: () => ({
    workspaceProjectIds: ["owner-project", "consumer"],
    getProjectById: (id: string) => ({ id, name: "Consumer project" }),
  }),
}));
vi.mock("@/components/core/page-title", () => ({ PageHead: () => null }));
vi.mock("@/services/api.service", () => ({
  APIService: class {
    baseURL = "";
  },
}));
vi.mock("@plane/propel/badge", () => ({
  Badge: ({ children }: { children: ReactNode }) => createElement("span", {}, children),
}));
vi.mock("@plane/propel/button", () => ({
  Button: ({
    children,
    variant: _variant,
    size: _size,
    ...props
  }: {
    children: ReactNode;
    variant?: string;
    size?: string;
  }) => createElement("button", props, children),
}));
vi.mock("@headlessui/react", () => ({
  Dialog: Object.assign(
    ({ children }: { children: ReactNode }) => createElement("section", { role: "dialog" }, children),
    {
      Panel: ({ children }: { children: ReactNode }) => createElement("div", {}, children),
      Title: ({ children }: { children: ReactNode }) => createElement("h2", {}, children),
    }
  ),
}));
const page = { results: [], next_cursor: null, has_more: false, per_page: 50 };
beforeEach(() => {
  state.role = 20;
  state.data = { "infrastructure-resources": page, "infrastructure-relations": page, "infrastructure-links": page };
  state.errors = {};
});
const render = (element: ReactNode) => renderToStaticMarkup(element);

describe("Infrastructure native rendering", () => {
  it("renders loading, empty and failure states distinctly", () => {
    delete state.data["infrastructure-resources"];
    expect(render(<InfrastructurePage />)).toContain("Loading infrastructure…");
    state.data["infrastructure-resources"] = page;
    expect(render(<InfrastructurePage />)).toContain("No resources found");
    state.errors["infrastructure-resources"] = { response: { status: 403 } };
    const html = render(<InfrastructurePage />);
    expect(html).toContain('role="alert"');
    expect(html).toContain("permission");
    expect(html).not.toContain("No resources found");
    expect(html).not.toContain("Add resource</button>");
  });
  it("renders resource cards and authoring only for project admins", () => {
    state.data["infrastructure-resources"] = { ...page, results: [resourceFixture] };
    let html = render(<InfrastructurePage />);
    expect(html).toContain("Grafana");
    expect(html).toContain("monitoring/grafana");
    expect(html).toContain("Add resource</button>");
    expect(html).toContain("Add relationship");
    state.role = 5;
    html = render(<InfrastructurePage />);
    expect(html).toContain("Grafana");
    expect(html).not.toContain("Add resource</button>");
    expect(html).not.toContain("Add relationship");
  });
  it("renders shared detail, logical refs and endpoints as inert text", () => {
    state.role = 15;
    state.data["infrastructure-resource"] = { ...resourceFixture, can_manage: false, ownership_project_id: null };
    const html = render(<InfrastructureDetailPage />);
    expect(html).toContain("https://grafana.example.invalid");
    expect(html).not.toContain('href="https://grafana.example.invalid');
    expect(html).toContain("projects/example/grafana");
    expect(html).toContain("runtime-own-identity");
    expect(html).toContain("Recovery contact");
    expect(html).toContain("Not visible in your project scope");
    expect(html).not.toContain("Edit resource");
    expect(html).not.toContain("Delete resource");
    expect(html).not.toContain("Share with a project");
  });
  it("shows consumer detach separately from shared metadata edit authority", () => {
    state.data["infrastructure-resource"] = { ...resourceFixture, can_manage: false, can_detach: true };
    const html = render(<InfrastructureDetailPage />);
    expect(html).toContain("Remove from this project");
    expect(html).not.toContain("Edit resource");
  });
  it("renders named directed relationships with evidence and safe internal links", () => {
    state.data["infrastructure-relations"] = {
      ...page,
      results: [
        {
          id: "relation",
          project_id: "owner-project",
          source_id: resourceFixture.id,
          source_name: "Grafana",
          source_key: "monitoring/grafana",
          target_id: "vm-id",
          target_name: "Monitoring VM",
          target_key: "monitoring/vm",
          kind: "runs_on",
          source: resourceFixture.source,
          evidence: [],
          last_verified: null,
          revision: 1,
          can_manage: false,
        },
      ],
    };
    const html = render(<InfrastructurePage />);
    expect(html).toContain("Grafana");
    expect(html).toContain("Monitoring VM");
    expect(html).toContain("runs on");
    expect(html).toContain("Relationship evidence");
    expect(html).toContain('href="/work/projects/owner-project/infrastructure/vm-id"');
    expect(html).not.toContain("Remove relationship");
  });
  it("provides complete labelled metadata and relationship forms", () => {
    const props = { scope: { workspace: "work", project: "owner-project" }, close: vi.fn(), saved: vi.fn() };
    const html = render(<ResourceEditor {...props} resource={resourceFixture} />);
    for (const label of [
      "Stable resource key",
      "Owner name",
      "Owner reference",
      "Recovery contact",
      "Source revision",
      "Evidence JSON",
      "Last verified",
      "Access methods JSON",
    ])
      expect(html).toContain(label);
    expect(html).toContain('disabled=""');
    expect(html).toContain("immutable after creation");
    const relation = render(<RelationEditor {...props} resource={resourceFixture} />);
    expect(relation).toContain("Source resource key");
    expect(relation).toContain("Target resource key");
    expect(relation).toContain("Create relationship");
    const share = render(<AttachEditor {...props} resource={resourceFixture} />);
    expect(share).toContain("Administered project");
    expect(share).toContain("Consumer project");
    expect(share).toContain("does not grant infrastructure access");
  });
  it("does not display Infrastructure navigation to restricted guests", () => {
    function Navigation({ guestAll }: { guestAll: boolean }) {
      const items = useNavigationItems({
        workspaceSlug: "work",
        projectId: "project",
        project: { id: "project", guest_view_all_features: guestAll } as IPartialProject,
        allowPermissions: (roles) => (roles as number[]).includes(state.role),
      });
      return (
        <>
          {items.map((item) => (
            <span key={item.key}>{item.name}</span>
          ))}
        </>
      );
    }
    state.role = 5;
    expect(render(<Navigation guestAll={false} />)).not.toContain("Infrastructure");
    expect(render(<Navigation guestAll />)).toContain("Infrastructure");
    state.role = 20;
    expect(render(<Navigation guestAll={false} />)).toContain("Infrastructure");
  });
});
