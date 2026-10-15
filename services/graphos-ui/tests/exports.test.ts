import Graph from "graphology";
import type { GraphosApi } from "../src/api/graphosApi";
import type { NodesScanRequest } from "../src/api/types";
import type { EdgeAttributes, NodeAttributes } from "../src/graph/GraphModel";
import { canvasJson, canvasLabelText, canvasPng, canvasSvg } from "../src/lib/canvasExport";
import { csvCell, exportName, toCsv } from "../src/lib/csv";
import { fontFaces, subsetsFor } from "../src/lib/svgFont";
import { exportNodesCsv } from "../src/lib/tableExport";
import { DARK } from "../src/theme/tokens";

const toBlob = vi.hoisted(() => vi.fn(async () => new Blob(["png"], { type: "image/png" })));
vi.mock("@sigma/export-image", () => ({ toBlob }));

describe("CSV", () => {
  it("quotes what needs it and keeps spreadsheets from running formulas", () => {
    expect(csvCell("plain")).toBe("plain");
    expect(csvCell('say "hi", twice')).toBe('"say ""hi"", twice"');
    expect(csvCell("two\nlines")).toBe('"two\nlines"');
    expect(csvCell("=SUM(A1:A9)")).toBe("'=SUM(A1:A9)");
    expect(csvCell("+1")).toBe("'+1");
    expect(csvCell("-2")).toBe("'-2");
    expect(csvCell("@cmd")).toBe("'@cmd");
    expect(csvCell(-2)).toBe("-2"); // a number is not a formula
    expect(csvCell(null)).toBe("");
    expect(csvCell(true)).toBe("true");
    expect(csvCell(["a", "b"])).toBe('"[""a"",""b""]"');
    expect(csvCell("Grace Hörmann")).toBe("Grace Hörmann");
  });

  it("writes a BOM, a header and CRLF lines, under a dated, safe name", () => {
    expect(toCsv(["id", "name"], [["d1", "Notes"]])).toBe("\uFEFFid,name\r\nd1,Notes\r\n");
    expect(exportName(["acme", "My label/x"], "csv")).toMatch(/^acme-My_label_x-\d{4}-\d{2}-\d{2}\.csv$/);
  });
});

describe("a label as CSV", () => {
  it("pages through every matching node and writes them as they come", async () => {
    const asked: NodesScanRequest[] = [];
    const pages = [
      { items: [{ id: "p1", label: "Person", properties: { name: "Ada", tags: ["x"] } }], next_cursor: "c1" },
      { items: [{ id: "e1", label: "Employee", properties: { name: "=cmd" } }], next_cursor: null },
    ];
    const api = {
      scanNodes: vi.fn(async (request: NodesScanRequest) => {
        asked.push(request);
        return pages[asked.length - 1]!;
      }),
    } as unknown as GraphosApi;
    let written = "";
    const progress: number[] = [];
    const count = await exportNodesCsv(
      api,
      {
        label: "Person",
        where: [{ property: "team", op: "=", value: "a" }],
        columns: ["name", "tags"],
        filename: "x.csv",
      },
      // The bytes as written: text() would drop the BOM (as the Fetch spec decodes).
      {
        write: async (response) =>
          void (written = new TextDecoder("utf-8", { ignoreBOM: true }).decode(await response.arrayBuffer())),
      },
      (rows) => progress.push(rows),
      new AbortController().signal,
    );
    // A node without one of the columns gets an empty cell.
    expect(count).toBe(2);
    expect(progress).toEqual([1, 2]);
    expect(asked.map((r) => r.cursor)).toEqual([null, "c1"]);
    expect(asked[0]).toMatchObject({ labels: ["Person"], project: ["name", "tags"], limit: 2000 });
    expect(written).toBe('\uFEFFid,label,name,tags\r\np1,Person,Ada,"[""x""]"\r\ne1,Employee,\'=cmd,\r\n');
  });
});

/** A graph of two nodes and an edge, and a renderer that places them as sigma would. */
function canvas() {
  const graph = new Graph<NodeAttributes, EdgeAttributes>({ multi: true });
  graph.addNode("Doc|d1", { id: "d1", vertexLabel: "Doc", properties: { name: "A & B" }, x: 0, y: 0, size: 5 });
  graph.addNode("Doc|d2", { id: "d2", vertexLabel: "Doc", properties: {}, x: 1, y: 1, size: 5 });
  graph.addNode("more|d1", { id: "more", vertexLabel: "", properties: {}, x: 2, y: 2, size: 3, more: 4 });
  graph.addEdgeWithKey("e1", "Doc|d1", "Doc|d2", { edgeType: "LINKS", properties: { w: 1 } });
  graph.addEdgeWithKey("e2", "Doc|d1", "more|d1", { edgeType: "", properties: {}, more: true });
  const display = {
    "Doc|d1": {
      x: 0,
      y: 0,
      size: 5,
      color: "#C9825B",
      label: "A & B",
      forceLabel: true,
      highlighted: false,
      hidden: false,
      zIndex: 1,
    },
    "Doc|d2": {
      x: 1,
      y: 1,
      size: 5,
      color: "#5FA584",
      label: "d2",
      forceLabel: false,
      highlighted: false,
      hidden: false,
      zIndex: 0,
    },
    "more|d1": {
      x: 2,
      y: 2,
      size: 3,
      color: "#999999",
      label: "+4",
      forceLabel: false,
      highlighted: false,
      hidden: true,
      zIndex: 0,
    },
  } as Record<
    string,
    {
      x: number;
      y: number;
      size: number;
      color: string;
      label: string;
      forceLabel: boolean;
      highlighted: boolean;
      hidden: boolean;
      zIndex: number;
    }
  >;
  const renderer = {
    getDimensions: () => ({ width: 400, height: 300 }),
    getNodeDisplayData: (key: string) => display[key],
    getEdgeDisplayData: () => ({ color: "#888888", size: 1, hidden: false }),
    framedGraphToViewport: ({ x, y }: { x: number; y: number }) => ({ x: 100 + x * 200, y: 100 + y * 100 }),
    scaleSize: (size: number) => size,
  };
  return { graph, renderer: renderer as never };
}

describe("the canvas as a file", () => {
  it("draws an SVG of what is shown: lines, circles, the labels sigma shows, escaped", () => {
    const { graph, renderer } = canvas();
    const svg = canvasSvg(renderer, graph, DARK);
    expect(svg).toMatch(/^<svg xmlns="http:\/\/www\.w3\.org\/2000\/svg" [^>]*width="400" height="300"/);
    expect(svg).toContain(`fill="${DARK.canvas}"`);
    expect(svg.match(/<circle /g)).toHaveLength(2); // the hidden placeholder is left out
    expect(svg.match(/<line /g)).toHaveLength(1);
    expect(svg).toContain(">A &amp; B</text>");
    expect(svg).not.toContain(">d2</text>"); // sigma shows no label for it either
    expect(svg).toContain('marker-end="url(#a0)"');
  });

  it("draws edges sharing their two nodes curved, as the canvas does", () => {
    const { graph, renderer } = canvas();
    const curved = {
      ...(renderer as unknown as Record<string, unknown>),
      getEdgeDisplayData: () => ({ color: "#888888", size: 1, hidden: false, curvature: 0.25 }),
    };
    const svg = canvasSvg(curved as never, graph, DARK);
    expect(svg).not.toContain("<line ");
    const path = /<path d="M([\d.]+),([\d.]+) Q([\d.]+),([\d.]+) ([\d.]+),([\d.]+)" fill="none"/.exec(svg);
    expect(path).not.toBeNull();
    const [x1, y1, cx, cy, x2, y2] = path!.slice(1).map(Number);
    // Its control point off the straight line, to the side sigma bends it (left of the direction, y down).
    const cross = (x2! - x1!) * (cy! - y1!) - (y2! - y1!) * (cx! - x1!);
    expect(cross).toBeLessThan(0);
    expect(svg).toContain('marker-end="url(#a0)"');
  });

  it("draws each node's pictogram on its disc, tinted as the canvas tints it", () => {
    const { graph, renderer } = canvas();
    const white = `data:image/svg+xml;charset=utf-8,${encodeURIComponent('<svg stroke="#FFFFFF"/>')}`;
    const withIcon = {
      ...(renderer as unknown as Record<string, unknown>),
      getNodeDisplayData: (key: string) => ({
        ...(renderer as unknown as { getNodeDisplayData: (k: string) => object }).getNodeDisplayData(key),
        ...(key === "Doc|d1" ? { image: white, pictoColor: "#112233" } : {}),
      }),
    };
    const svg = canvasSvg(withIcon as never, graph, DARK);
    expect(svg.match(/<image /g)).toHaveLength(1);
    expect(svg).toContain(encodeURIComponent('stroke="#112233"'));
    // Inside the disc of radius 5, as the image program's padding leaves it: 10 / 1.48.
    expect(svg).toContain('width="6.8" height="6.8"');
    expect(svg).toContain('xmlns:xlink="http://www.w3.org/1999/xlink"');
  });

  it("embeds the labels' font when given, and names it first", () => {
    const { graph, renderer } = canvas();
    expect(canvasLabelText(renderer, graph)).toBe("A & B");
    const svg = canvasSvg(renderer, graph, DARK, '@font-face{font-family:"Inter"}');
    expect(svg).toContain('<style>@font-face{font-family:"Inter"}</style>');
    expect(svg).toContain('font-family="Inter, ');
    expect(canvasSvg(renderer, graph, DARK)).not.toContain("<style>");
  });

  it("embeds only the font subsets the labels' letters need", async () => {
    const ranges = (text: string) => subsetsFor(text).map((s) => s.range.split(",")[0]);
    expect(ranges("")).toEqual([]);
    expect(ranges("pipelineflow")).toEqual(["U+0000-00FF"]);
    expect(ranges("Αθήνα")).toEqual(["U+0370-0377"]);
    expect(ranges("Αθήνα, Łódź")).toEqual(["U+0370-0377", "U+0100-02BA", "U+0000-00FF"]);
    const fetched: string[] = [];
    vi.stubGlobal("fetch", async (url: string) => {
      fetched.push(url);
      return new Response(new Uint8Array([1, 2, 3]));
    });
    try {
      const css = await fontFaces("Αθήνα");
      expect(fetched).toHaveLength(1);
      expect(css).toContain("src:url(data:font/woff2;base64,AQID)");
      expect(css).toContain("unicode-range:U+0370-0377,");
      expect(await fontFaces("")).toBe("");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("saves the graph as JSON without the +N placeholders", () => {
    const { graph } = canvas();
    const doc = JSON.parse(canvasJson(graph)) as { nodes: unknown[]; edges: unknown[] };
    expect(doc.nodes).toEqual([
      { id: "d1", label: "Doc", properties: { name: "A & B" } },
      { id: "d2", label: "Doc", properties: {} },
    ]);
    expect(doc.edges).toEqual([
      { src_id: "d1", src_label: "Doc", dst_id: "d2", dst_label: "Doc", type: "LINKS", properties: { w: 1 } },
    ]);
  });

  it("asks sigma for a PNG on the canvas's background", async () => {
    const { renderer } = canvas();
    const blob = await canvasPng(renderer, DARK);
    expect(blob.type).toBe("image/png");
    expect(toBlob).toHaveBeenCalledWith(renderer, { format: "png", backgroundColor: DARK.canvas });
  });
});
