import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { Server } from "node:http";
import express from "express";
import { Hocuspocus } from "@hocuspocus/server";
import { registerController } from "@plane/decorators";
import {
  getBinaryDataFromDocumentEditorHTMLString,
  getAllDocumentFormatsFromDocumentEditorBinaryData,
} from "@plane/editor";
import * as Y from "yjs";

const environment = vi.hoisted(() => ({ backend: "" }));
vi.mock("@/env", () => ({
  env: {
    get API_BASE_URL() {
      return environment.backend;
    },
    LIVE_SERVER_SECRET_KEY: "fixture-secret",
  },
}));

import { Database } from "@/extensions/database";
import { TitleSyncExtension } from "@/extensions/title-sync";
import { DocumentContentController } from "@/controllers/document-content.controller";

const pageId = "2d665a71-4a77-4f77-8cb6-b8442ae4c755";
const projectId = "ab9ff4ab-d3e1-4ec0-813f-1834f6cfedec";
const userId = "54783fd0-c2a1-4a88-bef1-3421303cb819";
const context = {
  workspaceSlug: "test",
  projectId,
  userId,
  cookie: "",
  apiKey: "fixture-api-key",
  documentType: "project_page" as const,
};
let backend: Server;
let api: Server;
let apiURL: string;
let hocuspocus: Hocuspocus;
let openDocument: Awaited<ReturnType<Hocuspocus["openDirectConnection"]>>;
let page: {
  name: string;
  updated_at: string;
  description_binary: string;
  description_html: string;
  description_json: object;
};
let writes = 0;
let afterWrite: (() => void) | undefined;
let rejectNextWrite = false;
let metadataWrites = 0;

function formats(binary: Uint8Array) {
  const value = getAllDocumentFormatsFromDocumentEditorBinaryData(binary, false);
  return {
    description_binary: value.contentBinaryEncoded,
    description_html: value.contentHTML,
    description_json: value.contentJSON,
  };
}

async function listen(app: ReturnType<typeof express>) {
  return await new Promise<Server>((resolve) => {
    const server = app.listen(0, "127.0.0.1", () => resolve(server));
  });
}
function url(server: Server) {
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing fixture port");
  return `http://127.0.0.1:${address.port}`;
}

beforeAll(async () => {
  page = {
    name: "Тест",
    updated_at: "r1",
    ...formats(getBinaryDataFromDocumentEditorHTMLString("<p>Original</p>")),
  };
  const app = express();
  app.use(express.json());
  app.use((req, res, next) => {
    if (req.headers["x-api-key"] !== "fixture-api-key" || req.headers["live-server-secret-key"] !== "fixture-secret") {
      return res.status(403).json({ message: "Invalid backend credentials" });
    }
    next();
  });
  const base = `/api/v1/workspaces/test/projects/${projectId}/pages/${pageId}`;
  app.get(`${base}/`, (_req, res) => res.json(page));
  app.get(`${base}/description/`, (_req, res) => res.json(page));
  app.patch(`${base}/`, (_req, res) => {
    metadataWrites++;
    res.json(page);
  });
  app.patch(`${base}/description/`, (req, res) => {
    if (rejectNextWrite) {
      rejectNextWrite = false;
      return res.status(412).json({ message: "Concurrent metadata update" });
    }
    if (req.headers["if-match"] && req.headers["if-match"] !== `"${page.updated_at}"`) {
      return res.status(412).json({ message: "Revision changed" });
    }
    page = { ...page, ...req.body, updated_at: `r${++writes + 1}` };
    const callback = afterWrite;
    afterWrite = undefined;
    callback?.();
    res.json(page);
  });
  backend = await listen(app);
  environment.backend = url(backend);
  hocuspocus = new Hocuspocus({ extensions: [new Database(), new TitleSyncExtension()], debounce: 10_000 });
  openDocument = await hocuspocus.openDirectConnection(pageId, context);
  const live = express();
  live.use(express.json());
  const router = express.Router();
  registerController(router, DocumentContentController, [hocuspocus]);
  live.use("/live", router);
  api = await listen(live);
  apiURL = url(api);
});

afterAll(async () => {
  await openDocument?.disconnect();
  await hocuspocus?.destroy();
  await Promise.all([api, backend].map((server) => new Promise<void>((resolve) => server?.close(() => resolve()))));
});

describe("content HTTP endpoint with real codec and an open Hocuspocus document", () => {
  it("requires the existing live server secret", async () => {
    const result = await fetch(`${apiURL}/live/documents/${pageId}/content/`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    expect(result.status).toBe(401);
    expect(writes).toBe(0);
  });

  it("renames a page whose binary has not yet had its title migrated", async () => {
    const stored = new Y.Doc();
    Y.applyUpdate(stored, Buffer.from(page.description_binary, "base64"));
    expect(stored.getXmlFragment("title").length).toBe(0);
    stored.destroy();
    const result = await fetch(`${apiURL}/live/documents/${pageId}/content/`, {
      method: "POST",
      headers: { "content-type": "application/json", "live-server-secret-key": "fixture-secret" },
      body: JSON.stringify({
        workspace_slug: "test",
        project_id: projectId,
        user_id: userId,
        api_key: "fixture-api-key",
        expected_revision: page.updated_at,
        base_description_binary: page.description_binary,
        name: page.name,
        new_name: "First rename",
      }),
    });
    expect(result.status).toBe(200);
    expect(page.name).toBe("First rename");
    expect(openDocument.document!.getXmlFragment("default").toJSON()).toContain("Original");
  });

  it("round-trips Unicode, headings, tables and links and updates the already-open document", async () => {
    const body = {
      workspace_slug: "test",
      project_id: projectId,
      user_id: userId,
      api_key: "fixture-api-key",
      expected_revision: page.updated_at,
      base_description_binary: page.description_binary,
      description_html:
        '<h2>Сборка</h2><p><a href="https://example.test/build">Build 42</a></p><table><tbody><tr><th>Tests</th><th>Passed</th></tr><tr><td>Suite A</td><td>7</td></tr></tbody></table>',
    };
    const result = await fetch(`${apiURL}/live/documents/${pageId}/content/`, {
      method: "POST",
      headers: { "content-type": "application/json", "live-server-secret-key": "fixture-secret" },
      body: JSON.stringify(body),
    });
    expect(result.status).toBe(200);
    const saved = (await result.json()) as typeof page;
    expect(saved.description_html).toContain("Сборка");
    expect(saved.description_html).toContain("<table");
    expect(saved.description_html).toContain("https://example.test/build");
    expect(openDocument.document).not.toBeNull();
    const active = formats(Y.encodeStateAsUpdate(openDocument.document!));
    expect(active.description_json).toEqual(saved.description_json);
    expect(formats(Buffer.from(saved.description_binary, "base64")).description_json).toEqual(saved.description_json);
    expect(page.description_json).toEqual(saved.description_json);
  });

  it("rejects a stale persisted revision without changing the active document", async () => {
    const before = Y.encodeStateAsUpdate(openDocument.document!);
    const previousWrites = writes;
    const result = await fetch(`${apiURL}/live/documents/${pageId}/content/`, {
      method: "POST",
      headers: { "content-type": "application/json", "live-server-secret-key": "fixture-secret" },
      body: JSON.stringify({
        workspace_slug: "test",
        project_id: projectId,
        user_id: userId,
        api_key: "fixture-api-key",
        expected_revision: "r0",
        base_description_binary: page.description_binary,
        description_html: "<p>Must not replace</p>",
      }),
    });
    expect(result.status).toBe(412);
    expect(Y.encodeStateAsUpdate(openDocument.document!)).toEqual(before);
    // Releasing the API connection must not cause an unconditional DB save.
    await new Promise((resolve) => setImmediate(resolve));
    expect(writes).toBe(previousWrites);
  });

  it("renames the active Yjs title and stored page together without a recursive title save", async () => {
    const bodyBefore = openDocument.document!.getXmlFragment("default").toJSON();
    const result = await fetch(`${apiURL}/live/documents/${pageId}/content/`, {
      method: "POST",
      headers: { "content-type": "application/json", "live-server-secret-key": "fixture-secret" },
      body: JSON.stringify({
        workspace_slug: "test",
        project_id: projectId,
        user_id: userId,
        api_key: "fixture-api-key",
        expected_revision: page.updated_at,
        base_description_binary: page.description_binary,
        new_name: "Релиз 42",
      }),
    });
    expect(result.status).toBe(200);
    expect(page.name).toBe("Релиз 42");
    expect(openDocument.document!.getXmlFragment("default").toJSON()).toBe(bodyBefore);
    expect(openDocument.document!.getXmlFragment("title").toJSON()).toContain("Релиз 42");
    const persisted = new Y.Doc();
    Y.applyUpdate(persisted, Buffer.from(page.description_binary, "base64"));
    expect(persisted.getXmlFragment("title").toJSON()).toEqual(openDocument.document!.getXmlFragment("title").toJSON());
    persisted.destroy();
    await hocuspocus.hooks("beforeUnloadDocument", { instance: hocuspocus, documentName: pageId });
    expect(metadataWrites).toBe(0);
  });

  it("reports unknown outcome if a compensating write is rejected after the first write succeeded", async () => {
    afterWrite = () => {
      const paragraph = new Y.XmlElement("paragraph");
      const text = new Y.XmlText();
      text.insert(0, "Concurrent editor change");
      paragraph.insert(0, [text]);
      openDocument.document!.getXmlFragment("default").insert(0, [paragraph]);
      rejectNextWrite = true;
    };
    const result = await fetch(`${apiURL}/live/documents/${pageId}/content/`, {
      method: "POST",
      headers: { "content-type": "application/json", "live-server-secret-key": "fixture-secret" },
      body: JSON.stringify({
        workspace_slug: "test",
        project_id: projectId,
        user_id: userId,
        api_key: "fixture-api-key",
        expected_revision: page.updated_at,
        base_description_binary: page.description_binary,
        description_html: "<p>API publication</p>",
      }),
    });
    expect(result.status).toBe(503);
    expect(((await result.json()) as { outcome: string }).outcome).toBe("unknown");
    expect(openDocument.document!.getXmlFragment("default").toJSON()).toContain("Concurrent editor change");
    expect(page.description_html).toContain("API publication");
  });
});
