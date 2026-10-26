import { describe, expect, it, vi } from "vitest";
import * as Y from "yjs";
import { DocumentContentConflict, replaceDocumentContent } from "@/lib/document-content";
import { withDocumentLock } from "@/lib/document-lock";

function document(text: string) {
  const doc = new Y.Doc();
  const paragraph = new Y.XmlElement("paragraph");
  const content = new Y.XmlText();
  content.insert(0, text);
  paragraph.insert(0, [content]);
  doc.getXmlFragment("default").insert(0, [paragraph]);
  return doc;
}

function copy(doc: Y.Doc) {
  const result = new Y.Doc();
  Y.applyUpdate(result, Y.encodeStateAsUpdate(doc));
  return result;
}

const serialize = (doc: Y.Doc) => ({ content: doc.getXmlFragment("default").toJSON() });
const text = (doc: Y.Doc) => doc.getXmlFragment("default").get(0) as Y.XmlElement;

describe("API content publication", () => {
  it("updates the same live Ydoc, synchronizes a connected replica, and preserves title", async () => {
    const live = document("before");
    live.getText("title").insert(0, "Keep title");
    const expected = copy(live);
    const peer = copy(live);
    live.on("update", (update: Uint8Array) => Y.applyUpdate(peer, update));
    const persist = vi.fn(async () => ({ updated_at: "revision-2" }));

    const saved = await replaceDocumentContent(live, expected, document("after"), "revision-1", serialize, persist);

    expect(persist).toHaveBeenCalledWith({ content: "<paragraph>after</paragraph>" }, "revision-1");
    expect(saved.updated_at).toBe("revision-2");
    expect(serialize(peer)).toEqual(serialize(live));
    expect(serialize(live).content).toBe("<paragraph>after</paragraph>");
    expect(live.getText("title").toString()).toBe("Keep title");
  });

  it("rejects unsaved editor changes before attempting a database mutation", async () => {
    const live = document("before");
    const expected = copy(live);
    (text(live).get(0) as Y.XmlText).insert(6, " editor");
    const persist = vi.fn();
    await expect(
      replaceDocumentContent(live, expected, document("after"), "r1", serialize, persist)
    ).rejects.toBeInstanceOf(DocumentContentConflict);
    expect(persist).not.toHaveBeenCalled();
    expect(serialize(live).content).toContain("before editor");
  });

  it("a failed revision check leaves live document and connected replicas untouched", async () => {
    const live = document("before");
    const expected = copy(live);
    const before = Y.encodeStateAsUpdate(live);
    const error = Object.assign(new Error("precondition failed"), { statusCode: 412 });
    await expect(
      replaceDocumentContent(live, expected, document("after"), "r1", serialize, async () => {
        throw error;
      })
    ).rejects.toBe(error);
    expect(Y.encodeStateAsUpdate(live)).toEqual(before);
  });

  it("preserves an editor update arriving during persistence and reconciles the stored result", async () => {
    const live = document("before");
    const expected = copy(live);
    const writes: Array<{ content: string; revision: string }> = [];
    const persist = async (content: { content: string }, revision: string) => {
      writes.push({ ...content, revision });
      if (writes.length === 1) (text(live).get(0) as Y.XmlText).insert(6, " concurrent");
      return { updated_at: `r${writes.length + 1}` };
    };
    await expect(
      replaceDocumentContent(live, expected, document("after"), "r1", serialize, persist)
    ).rejects.toBeInstanceOf(DocumentContentConflict);
    expect(writes).toEqual([
      { content: "<paragraph>after</paragraph>", revision: "r1" },
      { content: "<paragraph>before concurrent</paragraph>", revision: "r2" },
    ]);
    expect(serialize(live).content).toContain("before concurrent");
  });

  it("does not claim a conflict was recovered when compensating persistence fails", async () => {
    const live = document("before");
    const expected = copy(live);
    let calls = 0;
    const failed = new Error("database unavailable");
    const persist = async () => {
      if (++calls === 2) throw failed;
      (text(live).get(0) as Y.XmlText).insert(6, " concurrent");
      return { updated_at: "r2" };
    };
    await expect(replaceDocumentContent(live, expected, document("after"), "r1", serialize, persist)).rejects.toBe(
      failed
    );
    expect(serialize(live).content).toContain("before concurrent");
  });
});

describe("document storage serialization", () => {
  it("queues a background save until replacement completes and lets the save observe current content", async () => {
    let complete!: () => void;
    const order: string[] = [];
    let content = "old";
    const update = withDocumentLock("page", async () => {
      order.push("update");
      await new Promise<void>((resolve) => {
        complete = resolve;
      });
      content = "new";
    });
    await Promise.resolve();
    const save = withDocumentLock("page", async () => {
      order.push(`save:${content}`);
    });
    complete();
    await Promise.all([update, save]);
    expect(order).toEqual(["update", "save:new"]);
  });

  it("releases the lock after failure", async () => {
    await expect(
      withDocumentLock("failed", async () => {
        throw new Error("failed");
      })
    ).rejects.toThrow("failed");
    expect(await withDocumentLock("failed", async () => "continued")).toBe("continued");
  });
});
