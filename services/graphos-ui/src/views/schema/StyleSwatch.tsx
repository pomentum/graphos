import {
  Button,
  ColorSwatch,
  Group,
  Popover,
  Select,
  Stack,
  Text,
  TextInput,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { IconCheck } from "@tabler/icons-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { DISPLAY_ICONS, type DisplayIcon, type VertexDisplay } from "../../api/types";
import { LABEL_ICONS } from "../../lib/labelIcons";
import { METRIC_PROPERTIES } from "../../lib/metrics";
import { CATEGORY_COLORS } from "../../theme/categories";
import classes from "./SchemaView.module.css";

const HEX = /^#[0-9A-Fa-f]{6}$/;

/**
 * A label's or relationship type's style on the Schema page: its colour
 * (and icon) as a swatch; maintainers click it to set the colour, caption,
 * size and icon everyone on the tenant sees. `shown` is what is drawn (a
 * child label takes its parent's colour); `own` what this one declares.
 */
export function StyleSwatch({
  kind,
  name,
  own,
  shown,
  properties,
  editable,
  saving,
  onSave,
}: {
  kind: "vertex" | "edge";
  name: string;
  own: VertexDisplay | null;
  shown: VertexDisplay | null;
  properties: string[];
  editable: boolean;
  saving: boolean;
  /** The entry to declare, or null to remove the declared one. */
  onSave: (entry: VertexDisplay | null, done: () => void) => void;
}) {
  const { t } = useTranslation();
  const [opened, setOpened] = useState(false);
  const [color, setColor] = useState(own?.color ?? "");
  const [caption, setCaption] = useState<string | null>(own?.caption ?? null);
  const [sizeBy, setSizeBy] = useState<string | null>(own?.size_by ?? null);
  const [icon, setIcon] = useState<DisplayIcon | null>(own?.icon ?? null);
  const open = () => {
    setColor(own?.color ?? "");
    setCaption(own?.caption ?? null);
    setSizeBy(own?.size_by ?? null);
    setIcon(own?.icon ?? null);
    setOpened(true);
  };
  const badColor = color !== "" && !HEX.test(color);
  const entry: VertexDisplay = Object.fromEntries(
    Object.entries({
      color: color || null,
      caption,
      size_by: kind === "vertex" ? sizeBy : null,
      icon: kind === "vertex" ? icon : null,
    }).filter(([, v]) => v !== null),
  );
  const summary = [
    shown?.caption ? t("views.schema.style.captionIs", { property: shown.caption }) : null,
    shown?.size_by ? t("views.schema.style.sizeIs", { property: shown.size_by }) : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const swatch = <Swatch style={shown} />;

  if (!editable) {
    return shown?.color || shown?.icon ? (
      <Tooltip label={summary || t("views.schema.style.declared")} withArrow>
        <span className={classes.swatchStatic}>{swatch}</span>
      </Tooltip>
    ) : (
      <span className={classes.swatchStatic} />
    );
  }
  return (
    <Popover opened={opened} onChange={setOpened} position="bottom-start" width={280} shadow="md" withArrow>
      <Popover.Target>
        <UnstyledButton
          className={classes.swatchButton}
          aria-label={t("views.schema.style.edit", { name })}
          onClick={() => (opened ? setOpened(false) : open())}
        >
          {swatch}
        </UnstyledButton>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="xs">
          <Text size="sm" fw={600}>
            {t("views.schema.style.title", { name })}
          </Text>
          <div>
            <Text size="xs" fw={500} mb={4}>
              {t("views.schema.style.color")}
            </Text>
            <Group gap={4} wrap="wrap" role="radiogroup" aria-label={t("views.schema.style.color")}>
              <ColorSwatch
                component="button"
                type="button"
                color="transparent"
                size={20}
                className={classes.swatchAuto}
                role="radio"
                aria-checked={color === ""}
                aria-label={t("views.schema.style.automatic")}
                title={t("views.schema.style.automatic")}
                onClick={() => setColor("")}
              >
                {color === "" ? <IconCheck size={12} /> : null}
              </ColorSwatch>
              {CATEGORY_COLORS.map((swatch) => (
                <ColorSwatch
                  key={swatch}
                  component="button"
                  type="button"
                  color={swatch}
                  size={20}
                  role="radio"
                  aria-checked={color.toUpperCase() === swatch}
                  aria-label={swatch}
                  onClick={() => setColor(swatch)}
                >
                  {color.toUpperCase() === swatch ? <IconCheck size={12} color="#fff" /> : null}
                </ColorSwatch>
              ))}
            </Group>
            <TextInput
              size="xs"
              mt={6}
              aria-label={t("views.schema.style.hex")}
              placeholder="#RRGGBB"
              value={color}
              onChange={(event) => setColor(event.currentTarget.value.trim())}
              error={badColor ? t("views.schema.style.badColor") : null}
              leftSection={HEX.test(color) ? <ColorSwatch color={color} size={14} /> : null}
            />
          </div>
          <Select
            size="xs"
            label={t("views.schema.style.caption")}
            placeholder={t("views.schema.style.captionDefault")}
            data={properties}
            value={caption}
            onChange={setCaption}
            clearable
            searchable
            comboboxProps={{ withinPortal: false }}
          />
          {kind === "vertex" ? (
            <>
              <Select
                size="xs"
                label={t("views.schema.style.sizeBy")}
                placeholder={t("views.schema.style.sizeDefault")}
                data={[...new Set([...METRIC_PROPERTIES, ...properties])]}
                value={sizeBy}
                onChange={setSizeBy}
                clearable
                searchable
                comboboxProps={{ withinPortal: false }}
              />
              <Select
                size="xs"
                label={t("views.schema.style.icon")}
                placeholder={t("views.schema.style.noIcon")}
                data={[...DISPLAY_ICONS]}
                value={icon}
                onChange={(value) => setIcon(value as DisplayIcon | null)}
                clearable
                searchable
                leftSection={icon ? <IconOf icon={icon} /> : null}
                renderOption={({ option }) => (
                  <Group gap={6}>
                    <IconOf icon={option.value as DisplayIcon} />
                    {option.value}
                  </Group>
                )}
                comboboxProps={{ withinPortal: false }}
              />
            </>
          ) : null}
          <Group justify="space-between" mt={4}>
            {own ? (
              <Button
                size="xs"
                variant="subtle"
                color="gray"
                disabled={saving}
                onClick={() => onSave(null, () => setOpened(false))}
              >
                {t("views.schema.style.reset")}
              </Button>
            ) : (
              <span />
            )}
            <Button
              size="xs"
              loading={saving}
              disabled={badColor || Object.keys(entry).length === 0}
              onClick={() => onSave(entry, () => setOpened(false))}
            >
              {t("common.actions.save")}
            </Button>
          </Group>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}

function IconOf({ icon }: { icon: DisplayIcon }) {
  const Icon = LABEL_ICONS[icon];
  return <Icon size={14} aria-hidden />;
}

/** The style as drawn: the icon in the colour, or a dot; an empty ring when nothing is declared. */
function Swatch({ style }: { style: VertexDisplay | null }) {
  const Icon = style?.icon ? LABEL_ICONS[style.icon] : null;
  if (Icon) return <Icon size={15} color={style?.color ?? "var(--gx-muted)"} aria-hidden />;
  return (
    <span
      className={classes.swatch}
      data-empty={!style?.color || undefined}
      style={style?.color ? { background: style.color } : undefined}
    />
  );
}
