import { describe, expect, it } from "vitest";
import {
  changeFilter,
  exactResource,
  infrastructureError,
  isProjectAdmin,
  nextPage,
  parseAccessMethods,
  parseEvidence,
  parseLastVerified,
  resourceFilters,
} from "./helpers";
import { resourceFixture } from "./fixtures";

const access = resourceFixture.access_methods[0];
describe("Infrastructure project-scoped filters and pagination", () => {
  it("preserves only contract filters and opaque cursors", () => {
    expect(resourceFilters(new URLSearchParams("search=grafana&kind=service&cursor=opaque&role=admin"))).toEqual({
      search: "grafana",
      kind: "service",
      cursor: "opaque",
    });
  });
  it("resets cursor when changing filters, retaining unrelated filters", () => {
    const search = new URLSearchParams("search=grafana&tags=monitoring&cursor=old");
    expect(changeFilter(search, "search", "prometheus").toString()).toBe("search=prometheus&tags=monitoring");
    expect(search.get("cursor")).toBe("old");
    expect(nextPage(search, "signed/+cursor").get("cursor")).toBe("signed/+cursor");
    expect(nextPage(search, null).toString()).toBe("search=grafana&tags=monitoring");
  });
  it("requires an exact unambiguous selector result", () => {
    expect(exactResource([resourceFixture], resourceFixture.key)).toBe(resourceFixture);
    for (const results of [[], [resourceFixture, resourceFixture]])
      expect(() => exactResource(results, resourceFixture.key)).toThrow(/one visible resource/);
    expect(() => exactResource([resourceFixture], "other")).toThrow();
  });
  it("uses only project admin role for authoring", () => {
    for (const role of [20, "20"]) expect(isProjectAdmin(role)).toBe(true);
    for (const role of [5, 15, undefined, "admin", { workspace_role: 20 }]) expect(isProjectAdmin(role)).toBe(false);
  });
});
describe("Infrastructure access and evidence form validation", () => {
  it("accepts inert access metadata and logical OpenBao refs", () => {
    expect(parseAccessMethods(JSON.stringify([access]))).toEqual([access]);
    expect(
      parseAccessMethods(
        JSON.stringify([
          { ...access, credential_refs: [{ ...access.credential_refs![0], path: "projects/data/grafana" }] },
        ])
      )
    ).toHaveLength(1);
  });
  it.each([
    "https://user:pass@example.invalid",
    "https://example.invalid/?",
    "https://example.invalid/#",
    "https://example.invalid?x=1",
    "https://example.invalid/#x",
    "https://example.invalid\\path",
    "https://example.invalid/path value",
    "ssh://example.invalid",
  ])("rejects unsafe or mismatched endpoint %s", (endpoint) => {
    expect(() => parseAccessMethods(JSON.stringify([{ ...access, endpoint }]))).toThrow();
  });
  it("rejects unknown and secret-value fields without echoing input", () => {
    for (const data of [
      { ...access, token: "sensitive-value" },
      { ...access, notes: "password=sensitive-value" },
      { ...access, credential_refs: [{ ...access.credential_refs![0], value: "sensitive-value" }] },
    ]) {
      let message = "";
      try {
        parseAccessMethods(JSON.stringify([data]));
      } catch (error) {
        message = String(error);
      }
      expect(message).toBeTruthy();
      expect(message).not.toContain("sensitive-value");
    }
  });
  it("rejects duplicate access IDs, operations, missing required metadata and invalid refs", () => {
    expect(() => parseAccessMethods(JSON.stringify([access, access]))).toThrow(/unique/);
    expect(() => parseAccessMethods(JSON.stringify([{ ...access, operations: ["read", "read"] }]))).toThrow();
    expect(() => parseAccessMethods(JSON.stringify([{ id: "one" }]))).toThrow();
    for (const path of ["/leading", "a//b", "a/../b", "a/./b"])
      expect(() =>
        parseAccessMethods(JSON.stringify([{ ...access, credential_refs: [{ ...access.credential_refs![0], path }] }]))
      ).toThrow();
  });
  it("validates evidence and timezone-aware verification without false freshness", () => {
    expect(parseEvidence(JSON.stringify(resourceFixture.evidence))).toEqual(resourceFixture.evidence);
    expect(parseLastVerified("", [])).toBeNull();
    expect(() => parseLastVerified("2026-01-01T00:00:00Z", [])).toThrow(/requires/);
    for (const observed_at of ["2099-01-01T00:00:00Z", "2026-01-01T00:00:00", "not-a-date"])
      expect(() => parseEvidence(JSON.stringify([{ kind: "review", reference: "doc:review", observed_at }]))).toThrow();
    expect(() => parseEvidence('{"token":"sensitive-value"}')).toThrow(/array/);
    expect(() => parseEvidence("not json sensitive-value")).toThrow(
      "Evidence must be valid JSON. Rejected input is not echoed."
    );
  });
});
describe("Infrastructure failure semantics", () => {
  it("distinguishes conflicts, permission errors, invalid cursor and unknown outcomes", () => {
    expect(infrastructureError({ response: { status: 412 } }, true)).toMatch(/draft is preserved/);
    expect(infrastructureError({ response: { status: 403 } })).toMatch(/permission/);
    expect(infrastructureError({ response: { status: 400, data: { error: { code: "invalid_cursor" } } } })).toMatch(
      /first page/
    );
    expect(infrastructureError({ response: { status: 500 } }, true)).toMatch(/outcome is unknown/);
    expect(infrastructureError(new Error("sensitive-value"), true)).not.toContain("sensitive-value");
  });
  it("does not render rejected values or raw server error details", () => {
    expect(
      infrastructureError({
        response: {
          status: 400,
          data: { error: { detail: "sensitive-value", fields: { access_methods: ["sensitive-value"] } } },
        },
      })
    ).toMatch(/access_methods/);
    expect(
      infrastructureError({
        response: {
          status: 400,
          data: { error: { detail: "sensitive-value", fields: { access_methods: ["sensitive-value"] } } },
        },
      })
    ).not.toContain("sensitive-value");
  });
});

describe("Explicit HTTP access metadata", () => {
  it("preserves HTTP endpoints without a fabricated HTTPS scheme", () => {
    const method = { ...access, kind: "http", endpoint: "http://monitoring.example.invalid:3000" };
    expect(parseAccessMethods(JSON.stringify([method]))).toEqual([method]);
  });
  it.each([
    "http://user:pass@example.invalid",
    "http://example.invalid/?token=value",
    "http://example.invalid/#part",
    "http://example.invalid/path value",
    "http://example.invalid\\path",
    "https://example.invalid",
  ])("rejects unsafe or mismatched HTTP endpoint %s", (endpoint) => {
    expect(() => parseAccessMethods(JSON.stringify([{ ...access, kind: "http", endpoint }]))).toThrow();
  });
});
