import { Button, Menu } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconBraces, IconDownload, IconPhoto, IconVectorTriangle } from "@tabler/icons-react";
import type Graph from "graphology";
import type { RefObject } from "react";
import type Sigma from "sigma";
import { useTranslation } from "react-i18next";
import type { EdgeAttributes, NodeAttributes } from "../graph/GraphModel";
import { canvasJson, canvasPng, canvasSvg } from "../lib/canvasExport";
import { exportName } from "../lib/csv";
import { saveBlob, saveFile } from "../lib/download";
import type { Palette } from "../theme/tokens";

/**
 * Saves the canvas as a picture (PNG, SVG) or its graph as JSON, as it is
 * drawn now: the same nodes, colours and labels. Made in the browser.
 */
export function CanvasExportMenu({
  renderer,
  graph,
  palette,
  name,
}: {
  renderer: RefObject<Sigma<NodeAttributes, EdgeAttributes> | null>;
  graph: Graph<NodeAttributes, EdgeAttributes>;
  palette: Palette;
  /** Parts of the file name, e.g. the tenant and the view. */
  name: string[];
}) {
  const { t } = useTranslation();
  const failed = (error: unknown) =>
    notifications.show({
      color: "red",
      title: t("common.export.failed"),
      message: error instanceof Error ? error.message : String(error),
    });
  const png = async () => {
    const current = renderer.current;
    if (!current) return;
    try {
      saveBlob(exportName(name, "png"), await canvasPng(current, palette));
    } catch (error) {
      failed(error);
    }
  };
  const svg = () => {
    const current = renderer.current;
    if (current) saveFile(exportName(name, "svg"), canvasSvg(current, graph, palette), "image/svg+xml");
  };
  const json = () => saveFile(exportName(name, "json"), canvasJson(graph));
  return (
    <Menu position="bottom-start" width={200}>
      <Menu.Target>
        <Button size="compact-xs" variant="default" leftSection={<IconDownload size={14} />}>
          {t("common.export.label")}
        </Button>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Item leftSection={<IconPhoto size={14} />} onClick={() => void png()}>
          {t("common.export.png")}
        </Menu.Item>
        <Menu.Item leftSection={<IconVectorTriangle size={14} />} onClick={svg}>
          {t("common.export.svg")}
        </Menu.Item>
        <Menu.Item leftSection={<IconBraces size={14} />} onClick={json}>
          {t("common.export.json")}
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}
