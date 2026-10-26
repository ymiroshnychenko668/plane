/** Serialize API replacements and database saves in this live-server process. */
const tails = new Map<string, Promise<void>>();

export async function withDocumentLock<T>(pageId: string, operation: () => Promise<T>): Promise<T> {
  const previous = tails.get(pageId) ?? Promise.resolve();
  let release!: () => void;
  const tail = new Promise<void>((resolve) => {
    release = resolve;
  });
  tails.set(pageId, tail);
  await previous;
  try {
    return await operation();
  } finally {
    release();
    if (tails.get(pageId) === tail) tails.delete(pageId);
  }
}
