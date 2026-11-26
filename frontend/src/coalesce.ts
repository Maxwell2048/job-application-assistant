/**
 * Wrap an async refresh so overlapping calls never run in parallel.
 *
 * While a refresh is running, further calls only mark it dirty; when it
 * finishes, exactly one more refresh runs to pick up what changed meanwhile.
 * A burst of server events therefore costs at most two requests, not one per
 * event, and a slow response cannot be overtaken by an older one.
 */
export function coalesce(run: () => Promise<unknown>): () => Promise<void> {
  let running: Promise<void> | null = null
  let dirty = false
  const loop = async () => {
    do {
      dirty = false
      try {
        await run()
      } catch {
        // a failed refresh is shown by its callers; the next trigger retries
      }
    } while (dirty)
    running = null
  }
  return () => {
    if (running) {
      dirty = true
      return running
    }
    running = loop()
    return running
  }
}
