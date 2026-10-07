import ELK, { type ELK as ElkEngine } from "elkjs/lib/elk-api";
import workerUrl from "elkjs/lib/elk-worker.min.js?url";
import { layoutKey, readElkLayout, toElkGraph, type Diagram, type Layout } from "./diagramModel";

let engine: ElkEngine | null = null;

/** ELK in a web worker, started on first use and kept for the next layout. */
function elk(): ElkEngine {
  engine ??= new ELK({ workerUrl });
  return engine;
}

/** Recent layouts by `layoutKey`: coming back to the diagram does not lay it out again. */
const cache = new Map<string, Promise<Layout>>();
const CACHED = 8;

/**
 * Lays the diagram out with ELK, off the main thread. `using` lets tests
 * pass ELK's in-process build instead of the worker (and skips the cache).
 */
export function layoutDiagram(diagram: Diagram, using?: Pick<ElkEngine, "layout">): Promise<Layout> {
  if (using) return using.layout(toElkGraph(diagram)).then(readElkLayout);
  const key = layoutKey(diagram);
  const known = cache.get(key);
  if (known) return known;
  const running = elk().layout(toElkGraph(diagram)).then(readElkLayout);
  running.catch(() => {
    // Not kept, and a worker that failed once is not trusted again.
    cache.delete(key);
    engine?.terminateWorker();
    engine = null;
  });
  cache.set(key, running);
  while (cache.size > CACHED) cache.delete(cache.keys().next().value!);
  return running;
}
