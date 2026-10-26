import { Buffer } from "node:buffer";
import type { Hocuspocus } from "@hocuspocus/server";
import { TiptapTransformer } from "@hocuspocus/transformer";
import type { AnyExtension, JSONContent } from "@tiptap/core";
import type { Request, Response } from "express";
import * as Y from "yjs";
import { z } from "zod";
import { Controller, Middleware, Post } from "@plane/decorators";
import {
  getBinaryDataFromDocumentEditorHTMLString,
  getAllDocumentFormatsFromDocumentEditorBinaryData,
  extractTextFromHTML,
  generateTitleProsemirrorJson,
  TITLE_EDITOR_EXTENSIONS,
} from "@plane/editor";
import { requireSecretKey } from "@/lib/auth-middleware";
import { replaceDocumentContent, DocumentContentConflict } from "@/lib/document-content";
import { withDocumentLock } from "@/lib/document-lock";
import { AppError } from "@/lib/errors";
import { ProjectPageService } from "@/services/page/project-page.service";
import type { HocusPocusServerContext } from "@/types";

const contentSchema = z
  .object({
    workspace_slug: z.string().min(1),
    project_id: z.string().uuid(),
    user_id: z.string().uuid(),
    api_key: z.string().min(1),
    description_html: z
      .string()
      .max(5 * 1024 * 1024)
      .optional(),
    expected_revision: z.string().min(1),
    base_description_binary: z.string(),
    base_description_html: z.string().optional(),
    name: z.string().optional(),
    new_name: z.string().min(1).max(255).optional(),
  })
  .refine((body) => body.description_html !== undefined || body.new_name !== undefined, {
    message: "Provide description_html or new_name.",
  });

function serialize(document: Y.Doc) {
  const { contentHTML, contentJSON, contentBinaryEncoded } = getAllDocumentFormatsFromDocumentEditorBinaryData(
    Y.encodeStateAsUpdate(document),
    false
  );
  return { description_html: contentHTML, description_json: contentJSON, description_binary: contentBinaryEncoded };
}

function titleDocument(name: string) {
  return TiptapTransformer.toYdoc(
    generateTitleProsemirrorJson(name) as JSONContent,
    "title",
    TITLE_EDITOR_EXTENSIONS as AnyExtension[]
  );
}

@Controller("/documents")
export class DocumentContentController {
  constructor(private readonly hocuspocus: Hocuspocus) {}

  @Post("/:pageId/content/")
  @Middleware(requireSecretKey)
  async updateContent(req: Request, res: Response) {
    let changedOrSent = false;
    let persistenceConfirmed = false;
    try {
      const pageId = z.string().uuid().parse(req.params.pageId);
      const body = contentSchema.parse(req.body);
      const context: HocusPocusServerContext = {
        workspaceSlug: body.workspace_slug,
        projectId: body.project_id,
        userId: body.user_id,
        documentType: "project_page",
        cookie: "",
        apiKey: body.api_key,
      };
      const service = new ProjectPageService(context);
      const result = await withDocumentLock(pageId, async () => {
        const expected = new Y.Doc();
        const replacement = new Y.Doc();
        let connection: Awaited<ReturnType<Hocuspocus["openDirectConnection"]>> | undefined;
        try {
          Y.applyUpdate(
            expected,
            body.base_description_binary
              ? Buffer.from(body.base_description_binary, "base64")
              : getBinaryDataFromDocumentEditorHTMLString(body.base_description_html || "<p></p>", body.name)
          );
          // Match TitleSync's migration for older/API-created page binaries.
          if (expected.getXmlFragment("title").length === 0 && body.name !== undefined) {
            const title = titleDocument(body.name);
            try {
              Y.applyUpdate(expected, Y.encodeStateAsUpdate(title));
            } finally {
              title.destroy();
            }
          }
          Y.applyUpdate(replacement, getBinaryDataFromDocumentEditorHTMLString(body.description_html || "<p></p>"));
          if (body.new_name !== undefined) {
            const title = titleDocument(body.new_name);
            try {
              Y.applyUpdate(replacement, Y.encodeStateAsUpdate(title));
            } finally {
              title.destroy();
            }
          }
          connection = await this.hocuspocus.openDirectConnection(pageId, context);
          if (!connection.document) throw new Error("Document connection was closed.");
          return await replaceDocumentContent(
            connection.document,
            expected,
            replacement,
            body.expected_revision,
            (document) => ({
              ...serialize(document),
              ...(body.new_name !== undefined
                ? { name: extractTextFromHTML(document.getXmlFragment("title").toJSON()) }
                : {}),
            }),
            async (content, revision) => {
              changedOrSent = true;
              const saved = (await service.updateDescriptionBinary(pageId, content, revision)) as {
                updated_at: string;
              };
              persistenceConfirmed = true;
              return saved;
            },
            [
              ...(body.description_html === undefined ? [] : ["default"]),
              ...(body.new_name === undefined ? [] : ["title"]),
            ]
          );
        } finally {
          expected.destroy();
          replacement.destroy();
          // DirectConnection.disconnect always saves, even after a rejected CAS.
          // Release without that extra write, preserving any editor save queue.
          if (connection?.document) {
            const document = connection.document;
            document.removeDirectConnection();
            connection.document = null;
            if (
              !document.getConnectionsCount() &&
              !this.hocuspocus.debouncer.isDebounced(`onStoreDocument-${pageId}`)
            ) {
              await this.hocuspocus.unloadDocument(document);
            }
          }
        }
      });
      res.status(200).json(result);
    } catch (error) {
      if (error instanceof z.ZodError) {
        return res.status(400).json({ code: "invalid_input", errors: error.flatten() });
      }
      if (error instanceof DocumentContentConflict) {
        return res.status(409).json({ code: error.code, message: error.message });
      }
      const appError = new AppError(error);
      const status = appError.statusCode;
      if (!persistenceConfirmed && status && status >= 400 && status < 500) {
        return res.status(status).json({ code: "persistence_rejected", message: appError.message });
      }
      return res.status(503).json({
        code: "content_persistence_failed",
        outcome: changedOrSent ? "unknown" : "not_started",
        message: "Unable to confirm the content update. Read the page before retrying.",
      });
    }
  }
}
