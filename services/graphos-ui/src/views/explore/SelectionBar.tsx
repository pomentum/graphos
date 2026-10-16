import { Button, CloseButton, Group, Menu, Popover, Text, Tooltip } from "@mantine/core";
import { IconBraces, IconDownload, IconEyeOff, IconFileSpreadsheet, IconTrash, IconZoomIn } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import classes from "./ExploreView.module.css";

export interface SelectionBarProps {
  count: number;
  canWrite: boolean;
  /** At most this many nodes are expanded at once. */
  expandLimit: number;
  expanding: boolean;
  deleting: boolean;
  onExpand: () => void;
  onHide: () => void;
  onExportJson: () => void;
  onExportCsv: () => void;
  onDelete: () => void;
  onClear: () => void;
}

/** What can be done with several nodes at once: expand, hide, export, delete. */
export function SelectionBar(props: SelectionBarProps) {
  const { t } = useTranslation();
  const { count, expandLimit } = props;
  return (
    <Group gap={6} className={classes.selectionBar} role="toolbar" aria-label={t("views.explore.selection.region")}>
      <Text size="xs" fw={600}>
        {t("views.explore.selection.count", { count })}
      </Text>
      <Tooltip
        label={
          count > expandLimit
            ? t("views.explore.selection.expandFirst", { count: expandLimit })
            : t("views.explore.selection.expandTooltip")
        }
        withArrow
      >
        <Button
          size="compact-xs"
          variant="default"
          leftSection={<IconZoomIn size={14} />}
          loading={props.expanding}
          onClick={props.onExpand}
        >
          {t("views.explore.selection.expand")}
        </Button>
      </Tooltip>
      <Button size="compact-xs" variant="default" leftSection={<IconEyeOff size={14} />} onClick={props.onHide}>
        {t("views.explore.selection.hide")}
      </Button>
      <Menu position="top" width={180}>
        <Menu.Target>
          <Button size="compact-xs" variant="default" leftSection={<IconDownload size={14} />}>
            {t("common.export.label")}
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Item leftSection={<IconBraces size={14} />} onClick={props.onExportJson}>
            {t("common.export.json")}
          </Menu.Item>
          <Menu.Item leftSection={<IconFileSpreadsheet size={14} />} onClick={props.onExportCsv}>
            {t("common.export.csv")}
          </Menu.Item>
        </Menu.Dropdown>
      </Menu>
      {props.canWrite ? (
        <Popover position="top" withArrow>
          <Popover.Target>
            <Button size="compact-xs" variant="subtle" color="red" leftSection={<IconTrash size={14} />}>
              {t("common.actions.delete")}
            </Button>
          </Popover.Target>
          <Popover.Dropdown>
            <Text size="xs" mb="xs" maw={240}>
              {t("views.explore.selection.deleteQuestion", { count })}
            </Text>
            <Button size="compact-xs" color="red" loading={props.deleting} onClick={props.onDelete}>
              {t("views.explore.selection.deleteConfirm", { count })}
            </Button>
          </Popover.Dropdown>
        </Popover>
      ) : null}
      <CloseButton size="sm" aria-label={t("views.explore.selection.clear")} onClick={props.onClear} />
    </Group>
  );
}
