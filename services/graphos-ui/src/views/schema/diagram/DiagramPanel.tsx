import { Badge, Button, CloseButton, Group, Stack, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { IconMap, IconPlus, IconTable } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";
import { Link as RouterLink, useNavigate } from "react-router-dom";
import { useWorkspace } from "../../../graph/WorkspaceProvider";
import { useAddToSchema, useRemoveEdgeEndpoint } from "../../../data/queries";
import { formatCount } from "../../../lib/time";
import { useTenantSession } from "../../../session/SessionProvider";
import { browseParams } from "../../browse/browseState";
import type { Diagram, Link, LinkType, Selection } from "./diagramModel";
import classes from "./SchemaDiagram.module.css";

export interface DiagramPanelProps {
  diagram: Diagram;
  selection: NonNullable<Selection>;
  /** Property names by label, from the catalogue. */
  properties: Map<string, string[]>;
  onSelect: (selection: Selection) => void;
}

/**
 * What is selected on the diagram. A label: its counts, parent and
 * children, properties, its lines in and out, and the ways to its nodes.
 * A line: the labels it joins and every relationship type on it, each with
 * how it stands against the declarations; administrators declare a pair
 * or remove a declared one here.
 */
export function DiagramPanel({ diagram, selection, properties, onSelect }: DiagramPanelProps) {
  const { t } = useTranslation();
  return (
    <aside className={classes.panel} aria-label={t("views.schema.diagram.panel.label")}>
      <CloseButton
        className={classes.panelClose}
        size="sm"
        aria-label={t("common.actions.close")}
        onClick={() => onSelect(null)}
      />
      {selection.kind === "label" ? (
        <LabelDetail diagram={diagram} label={selection.id} properties={properties} onSelect={onSelect} />
      ) : (
        <LinkDetail diagram={diagram} id={selection.id} onSelect={onSelect} />
      )}
    </aside>
  );
}

function LabelDetail({
  diagram,
  label,
  properties,
  onSelect,
}: {
  diagram: Diagram;
  label: string;
  properties: Map<string, string[]>;
  onSelect: (selection: Selection) => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { setMapLabel, setMode } = useWorkspace();
  const { canWrite } = useTenantSession();
  const box = diagram.boxes.find((b) => b.label === label);
  if (!box) return null;
  const children = diagram.boxes.filter((b) => b.parent === label).map((b) => b.label);
  const lines = diagram.links.filter((l) => l.kind === "relationship");
  const outgoing = lines.filter((l) => l.src === label);
  const incoming = lines.filter((l) => l.dst === label && l.src !== label);
  const props = properties.get(label) ?? [];

  const showOnMap = () => {
    setMapLabel(label);
    setMode("map");
    void navigate("/explore");
  };

  return (
    <Stack gap="md">
      <div className={classes.panelHead}>
        <Text fw={600} size="md" truncate title={label}>
          {label}
        </Text>
        <Group gap={6} mt={4}>
          {box.count !== null ? (
            <Badge variant="light" size="sm" radius="sm" tt="none">
              {t("common.counts.nodes", { count: box.count, formatted: formatCount(box.count) })}
            </Badge>
          ) : null}
          {box.parent ? (
            <UnstyledButton onClick={() => onSelect({ kind: "label", id: box.parent! })}>
              <Text size="xs" c="dimmed" className={classes.linkText}>
                {t("views.schema.labels.inherits", { parent: box.parent })}
              </Text>
            </UnstyledButton>
          ) : null}
        </Group>
        <Group gap={6} mt="sm">
          <Button
            size="xs"
            variant="default"
            leftSection={<IconTable size={14} />}
            component={RouterLink}
            to={`/browse?${browseParams({ label, filter: null }).toString()}`}
          >
            {t("views.schema.labels.browse")}
          </Button>
          <Button size="xs" variant="default" leftSection={<IconMap size={14} />} onClick={showOnMap}>
            {t("views.schema.diagram.panel.showOnMap")}
          </Button>
          {canWrite ? (
            <Button
              size="xs"
              variant="default"
              leftSection={<IconPlus size={14} />}
              onClick={() => void navigate(`/explore?new=${encodeURIComponent(label)}`)}
            >
              {t("views.schema.diagram.panel.newNode")}
            </Button>
          ) : null}
        </Group>
      </div>

      {children.length ? (
        <Section title={t("views.schema.diagram.panel.children")}>
          {children.map((child) => (
            <UnstyledButton
              key={child}
              className={classes.panelRow}
              onClick={() => onSelect({ kind: "label", id: child })}
            >
              <Text size="sm">{child}</Text>
            </UnstyledButton>
          ))}
        </Section>
      ) : null}

      <Section title={t("views.schema.diagram.panel.properties")}>
        <div className={classes.panelPad}>
          {props.length ? (
            <Group gap={4}>
              {props.map((name) => (
                <Badge key={name} variant="default" radius="sm" tt="none" size="sm">
                  {name}
                </Badge>
              ))}
            </Group>
          ) : (
            <Text size="xs" c="dimmed">
              {t("views.schema.labels.noProperties")}
            </Text>
          )}
        </div>
      </Section>

      <LineList title={t("views.schema.diagram.panel.outgoing")} lines={outgoing} end="dst" onSelect={onSelect} />
      <LineList title={t("views.schema.diagram.panel.incoming")} lines={incoming} end="src" onSelect={onSelect} />
    </Stack>
  );
}

function LinkDetail({ diagram, id, onSelect }: { diagram: Diagram; id: string; onSelect: (s: Selection) => void }) {
  const { t } = useTranslation();
  const link = diagram.links.find((l) => l.key === id);
  if (!link) return null;
  const labelButton = (label: string) => (
    <UnstyledButton onClick={() => onSelect({ kind: "label", id: label })}>
      <Text size="md" fw={600} className={classes.linkText}>
        {label}
      </Text>
    </UnstyledButton>
  );
  if (link.kind === "inherits") {
    return (
      <Stack gap="xs" className={classes.panelHead}>
        <Group gap={6}>
          {labelButton(link.src)}
          <Text size="sm" c="dimmed">
            {t("views.schema.diagram.inherits")}
          </Text>
          {labelButton(link.dst)}
        </Group>
        <Text size="xs" c="dimmed">
          {t("views.schema.diagram.panel.inheritsNote", { child: link.src, parent: link.dst })}
        </Text>
      </Stack>
    );
  }
  return (
    <Stack gap="md">
      <Group gap={6} className={classes.panelHead}>
        {labelButton(link.src)}
        <Text c="dimmed">→</Text>
        {labelButton(link.dst)}
      </Group>
      <Section title={t("views.schema.diagram.panel.types")}>
        {link.types.map((type) => (
          <TypeRow key={type.type} link={link} type={type} />
        ))}
      </Section>
    </Stack>
  );
}

/** One relationship type on a line: its edges, its status, and (for admins) declare or remove. */
function TypeRow({ link, type }: { link: Link; type: LinkType }) {
  const { t } = useTranslation();
  const { isAdmin } = useTenantSession();
  const declare = useAddToSchema();
  const remove = useRemoveEdgeEndpoint();
  const exact = type.via !== null && type.via.src === link.src && type.via.dst === link.dst;
  const statusLabel = {
    declared: exact
      ? t("views.schema.diagram.status.declared")
      : t("views.schema.diagram.status.declaredVia", { src: type.via?.src, dst: type.via?.dst }),
    undeclared: t("views.schema.diagram.status.undeclared"),
    open: t("views.schema.diagram.status.open"),
  }[type.status];
  return (
    <div className={classes.typeRow}>
      <div className={classes.typeHead}>
        <Text size="sm" className={classes.grow} truncate>
          {type.type}
        </Text>
        <Text size="xs" c="dimmed">
          {type.count === 0
            ? t("views.schema.diagram.noEdgesYet")
            : `${type.estimated ? "≈ " : ""}${t("common.counts.edges", { count: type.count, formatted: formatCount(type.count) })}`}
        </Text>
      </div>
      <Group gap={6} justify="space-between" wrap="nowrap">
        <Text size="xs" className={classes.typeStatus} data-status={type.status} truncate>
          {statusLabel}
        </Text>
        {isAdmin && type.status !== "declared" ? (
          <Tooltip
            label={t(
              type.status === "open" ? "views.schema.diagram.declareOpenHint" : "views.schema.diagram.declareHint",
              {
                type: type.type,
              },
            )}
            withArrow
            multiline
            w={260}
          >
            <Button
              size="compact-xs"
              variant="light"
              loading={declare.isPending}
              onClick={() => declare.mutate({ edge_endpoints: { [type.type]: [[link.src, link.dst]] } })}
            >
              {t("views.schema.diagram.declare")}
            </Button>
          </Tooltip>
        ) : null}
        {isAdmin && type.status === "declared" && exact ? (
          <Button
            size="compact-xs"
            variant="subtle"
            color="gray"
            loading={remove.isPending}
            onClick={() => remove.mutate({ type: type.type, src: link.src, dst: link.dst })}
          >
            {t("views.schema.diagram.undeclare")}
          </Button>
        ) : null}
      </Group>
    </div>
  );
}

function LineList({
  title,
  lines,
  end,
  onSelect,
}: {
  title: string;
  lines: Link[];
  end: "src" | "dst";
  onSelect: (selection: Selection) => void;
}) {
  const { t } = useTranslation();
  if (lines.length === 0) return null;
  return (
    <Section title={title}>
      {lines.map((line) => (
        <UnstyledButton
          key={line.key}
          className={classes.panelRow}
          onClick={() => onSelect({ kind: "link", id: line.key })}
        >
          <Text
            size="sm"
            className={classes.grow}
            truncate
            data-status={line.status}
            title={line.types.map((x) => x.type).join(", ")}
          >
            {line.types[0]?.type}
            {line.types.length > 1 ? ` ${t("views.schema.diagram.more", { count: line.types.length - 1 })}` : ""}
          </Text>
          <Text size="xs" c="dimmed" truncate>
            {end === "dst" ? `→ ${line.dst}` : `← ${line.src}`}
          </Text>
        </UnstyledButton>
      ))}
    </Section>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <Text size="xs" fw={600} c="dimmed" mb={4}>
        {title}
      </Text>
      <div className={classes.panelGroup}>{children}</div>
    </div>
  );
}
