import * as Y from "yjs";

export class DocumentContentConflict extends Error {
  readonly statusCode = 409;
  readonly code = "live_content_conflict";
}

export const PERSISTED_API_CONTENT = Symbol("persisted-api-content");

type PersistedContent = { updated_at: string; [key: string]: unknown };

/**
 * Persist a replacement before applying it to the open document. A rejected
 * optimistic database write therefore never changes the connected editor.
 * If the editor changes during persistence, restore its current content and
 * report a conflict instead of deleting edits that were not in the snapshot.
 * Caller holds withDocumentLock; ordinary database saves use the same lock.
 */
export async function replaceDocumentContent<T extends Record<string, unknown>>(
  document: Y.Doc,
  expected: Y.Doc,
  replacement: Y.Doc,
  revision: string,
  serialize: (document: Y.Doc) => T,
  persist: (content: T, expectedRevision: string) => Promise<PersistedContent>,
  fields = ["default"]
): Promise<PersistedContent & T> {
  if (
    ["default", ...fields].some(
      (field) => document.getXmlFragment(field).toJSON() !== expected.getXmlFragment(field).toJSON()
    )
  ) {
    throw new DocumentContentConflict("The open document has changes newer than the supplied revision.");
  }
  const before = Y.encodeStateAsUpdate(document);
  const candidate = new Y.Doc();
  try {
    Y.applyUpdate(candidate, before);
    candidate.transact(() => {
      for (const field of fields) {
        const fragment = candidate.getXmlFragment(field);
        fragment.delete(0, fragment.length);
        fragment.insert(
          0,
          replacement
            .getXmlFragment(field)
            .toArray()
            .map((node) => {
              if (!(node instanceof Y.XmlElement) && !(node instanceof Y.XmlText)) {
                throw new Error("Unsupported editor node type.");
              }
              return node.clone();
            })
        );
      }
    });
    const content = serialize(candidate);
    const saved = await persist(content, revision);
    if (!saved.updated_at) throw new Error("Persistence did not return the saved page revision.");

    if (!Buffer.from(before).equals(Buffer.from(Y.encodeStateAsUpdate(document)))) {
      await persist(serialize(document), saved.updated_at);
      throw new DocumentContentConflict(
        "The editor changed during publication; its changes were preserved. Read and retry."
      );
    }
    const update = Y.encodeStateAsUpdate(candidate, Y.encodeStateVector(document));
    // No synthetic connection origin: Hocuspocus would otherwise try to save
    // using an empty connection context. Persistence was confirmed above.
    document.transact((transaction) => {
      transaction.meta.set(PERSISTED_API_CONTENT, true);
      Y.applyUpdate(document, update);
    });
    return { ...saved, ...content };
  } finally {
    candidate.destroy();
  }
}
