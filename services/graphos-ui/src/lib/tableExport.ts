import type { GraphosApi } from "../api/graphosApi";
import type { ScanFilter } from "../api/types";
import { csvLine, csvStart, CSV_TYPE } from "./csv";
import { saveBlob, type SaveTarget } from "./download";

/** Rows asked for per page while exporting (graphos's `nodes:scan` allows up to 2,000). */
export const EXPORT_PAGE = 2000;

/**
 * Every node of `label` matching `where`, as CSV: the id, the node's own
 * label, then `columns`. Pages through `nodes:scan` and writes as it goes,
 * into the file the user picked where the browser allows it (memory stays
 * flat), else into a Blob saved at the end. `onRows` reports the rows
 * written; `signal` stops it. Resolves with the number of rows.
 */
export async function exportNodesCsv(
  api: GraphosApi,
  request: { label: string; where: ScanFilter[]; columns: string[]; filename: string },
  target: SaveTarget,
  onRows: (rows: number) => void,
  signal: AbortSignal,
): Promise<number> {
  const encoder = new TextEncoder();
  let cursor: string | null = null;
  let started = false;
  let rows = 0;
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (!started) {
        started = true;
        controller.enqueue(encoder.encode(csvStart(["id", "label", ...request.columns])));
        return;
      }
      const page = await api.scanNodes(
        {
          labels: [request.label],
          where: request.where,
          project: request.columns.length ? request.columns : ["*"],
          limit: EXPORT_PAGE,
          cursor,
        },
        signal,
      );
      const text = page.items
        .map((item) => csvLine([item.id, item.label, ...request.columns.map((c) => item.properties[c])]))
        .join("");
      if (text) controller.enqueue(encoder.encode(text));
      rows += page.items.length;
      onRows(rows);
      cursor = page.next_cursor ?? null;
      if (cursor === null) controller.close();
    },
  });
  const response = new Response(stream, { headers: { "Content-Type": CSV_TYPE } });
  if (target) await target.write(response);
  else saveBlob(request.filename, new Blob([await response.arrayBuffer()], { type: CSV_TYPE }));
  return rows;
}
