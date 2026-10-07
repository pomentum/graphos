import {
  Alert,
  Badge,
  Button,
  Group,
  Menu,
  SegmentedControl,
  Skeleton,
  Stack,
  Text,
  Title,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { IconChevronDown, IconPlus } from "@tabler/icons-react";
import { lazy, Suspense, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";
import { useAddToSchema, useCatalogue, useGraphMap, useSchema } from "../../data/queries";
import { formatCount } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import { browseParams } from "../browse/browseState";
import { AddToSchemaDialog, type AdditionKind } from "./AddToSchemaDialog";
import { labelTree, propertiesByLabel, relationshipRows } from "./schemaModel";
import type { DraftLine } from "./diagram/SchemaDiagram";
import classes from "./SchemaView.module.css";

// The diagram brings React Flow and ELK: loaded only when it is opened.
const SchemaDiagram = lazy(() => import("./diagram/SchemaDiagram").then((m) => ({ default: m.SchemaDiagram })));

/** Label pairs listed per relationship type; the rest are summarised. */
const SHOWN_PAIRS = 3;

/**
 * Schema: what the tenant's graph is made of. The labels as a tree under
 * their parents, with node counts and the properties their nodes carry;
 * the relationship types with their edge counts and the label pairs they
 * join; the search indexes. Administrators can add labels, relationship
 * types and search indexes; nothing is ever removed here (graphos keeps
 * every label and the data in it). The same schema can be drawn as a
 * diagram (`?view=diagram`), where labels are added and relationship types
 * drawn from one label to another.
 */
export function SchemaView() {
  const { t } = useTranslation();
  const { tenant, isAdmin } = useTenantSession();
  const schema = useSchema();
  const map = useGraphMap();
  const catalogue = useCatalogue();
  const add = useAddToSchema();
  const [adding, setAdding] = useState<AdditionKind | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "diagram" ? "diagram" : "list";
  const setView = (next: string) => setParams(next === "diagram" ? { view: "diagram" } : {}, { replace: true });
  // A relationship drawn on the diagram, waiting for its name; then shown until edges use it.
  const [pair, setPair] = useState<{ src: string; dst: string } | null>(null);
  const [drafts, setDrafts] = useState<DraftLine[]>([]);

  const counts = useMemo(
    () => (map.data ? new Map(map.data.labels.map((l) => [l.label, l.count] as const)) : null),
    [map.data],
  );
  const properties = useMemo(() => propertiesByLabel(catalogue.data?.by_property ?? {}), [catalogue.data]);
  const labels = schema.data ? labelTree(schema.data, counts, properties) : [];
  const relationships = schema.data ? relationshipRows(schema.data, map.data) : [];
  const indexes = Object.entries(schema.data?.search_indexes ?? {});
  const allProperties = Object.keys(catalogue.data?.by_property ?? {}).sort();

  if (schema.error) {
    return (
      <div className={classes.root}>
        <Alert color="red" variant="light" radius="md">
          {schema.error.message}
        </Alert>
      </div>
    );
  }

  const addRelationshipBetween = (src: string, dst: string) => {
    setPair({ src, dst });
    setAdding("relationship");
  };
  const closeDialog = () => {
    setAdding(null);
    setPair(null);
  };
  const draw = (type: string) => {
    if (pair) setDrafts((current) => [...current, { type, ...pair }]);
    closeDialog();
  };

  return (
    <div className={view === "diagram" ? classes.diagramRoot : classes.root}>
      <Group justify="space-between" align="flex-start" mb="xs">
        <div>
          <Title order={1} size="h4">
            {t("views.schema.heading", { tenant })}
          </Title>
          <Text size="xs" c="dimmed">
            {isAdmin ? t("views.schema.neverRemoves") : t("views.schema.readOnly")}
          </Text>
        </div>
        <Group gap="xs">
          <SegmentedControl
            size="xs"
            aria-label={t("views.schema.view.label")}
            data={[
              { value: "list", label: t("views.schema.view.list") },
              { value: "diagram", label: t("views.schema.view.diagram") },
            ]}
            value={view}
            onChange={setView}
          />
          {isAdmin && schema.data ? (
            <Menu position="bottom-end" width={200}>
              <Menu.Target>
                <Button size="xs" leftSection={<IconPlus size={14} />} rightSection={<IconChevronDown size={12} />}>
                  {t("views.schema.add")}
                </Button>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item onClick={() => setAdding("label")}>{t("views.schema.addLabel")}</Menu.Item>
                <Menu.Item onClick={() => setAdding("relationship")}>{t("views.schema.addRelationship")}</Menu.Item>
                <Menu.Item onClick={() => setAdding("index")}>{t("views.schema.addIndex")}</Menu.Item>
              </Menu.Dropdown>
            </Menu>
          ) : null}
        </Group>
      </Group>

      {schema.isLoading ? <Skeleton h={240} radius="lg" /> : null}

      {schema.data && view === "diagram" ? (
        <div className={classes.diagram}>
          <Suspense fallback={<Skeleton h="100%" radius="lg" />}>
            <SchemaDiagram
              tenant={tenant}
              schema={schema.data}
              map={map.data}
              properties={properties}
              editable={isAdmin}
              drafts={drafts}
              onAddLabel={() => setAdding("label")}
              onDrawRelationship={addRelationshipBetween}
            />
          </Suspense>
        </div>
      ) : null}

      {schema.data && view === "list" ? (
        <Stack gap="xl" mt="md">
          <section>
            <Title order={2} size="sm" mb="xs">
              {t("views.schema.sections.labels")} · {schema.data.vertex_labels.length}
            </Title>
            <div className={classes.group}>
              {labels.map((row) => (
                <div key={row.label}>
                  <UnstyledButton
                    className={classes.row}
                    style={{ paddingLeft: 12 + row.depth * 18 }}
                    onClick={() => setOpen(open === row.label ? null : row.label)}
                    aria-expanded={open === row.label}
                  >
                    <Text size="sm" fw={500} className={classes.name}>
                      {row.label}
                    </Text>
                    {row.parent ? (
                      <Text size="xs" c="dimmed">
                        {t("views.schema.labels.inherits", { parent: row.parent })}
                      </Text>
                    ) : null}
                    <Text size="xs" c="dimmed" className={classes.end}>
                      {row.count === null
                        ? ""
                        : t("common.counts.nodes", { count: row.count, formatted: formatCount(row.count) })}
                      {" · "}
                      {t("views.schema.labels.properties", { count: row.properties.length })}
                    </Text>
                  </UnstyledButton>
                  {open === row.label ? (
                    <div className={classes.detail} style={{ paddingLeft: 12 + row.depth * 18 }}>
                      {row.properties.length ? (
                        <Group gap={4}>
                          {row.properties.map((name) => (
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
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        mt={6}
                        component={Link}
                        to={`/browse?${browseParams({ label: row.label, filter: null }).toString()}`}
                      >
                        {t("views.schema.labels.browse")}
                      </Button>
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </section>

          <section>
            <Title order={2} size="sm" mb="xs">
              {t("views.schema.sections.relationships")} · {relationships.length}
            </Title>
            <div className={classes.group}>
              {relationships.map((row) => (
                <div key={row.type} className={classes.row}>
                  <Text size="sm" fw={500} className={classes.name}>
                    {row.type}
                  </Text>
                  {row.derived ? (
                    <Tooltip label={t("views.schema.relationships.derivedTooltip")} withArrow>
                      <Badge size="xs" variant="light" radius="sm" tt="none">
                        {t("views.schema.relationships.derived")}
                      </Badge>
                    </Tooltip>
                  ) : null}
                  <Text size="xs" c="dimmed" className={classes.pairs} truncate>
                    {row.pairs.length === 0
                      ? t("views.schema.relationships.none")
                      : row.pairs
                          .slice(0, SHOWN_PAIRS)
                          .map((p) => `${p.src} → ${p.dst}`)
                          .join(", ") +
                        (row.pairs.length > SHOWN_PAIRS
                          ? ` ${t("views.schema.relationships.pairsMore", { count: row.pairs.length - SHOWN_PAIRS })}`
                          : "")}
                  </Text>
                  <Text size="xs" c="dimmed" className={classes.end}>
                    {row.estimated ? "≈ " : ""}
                    {t("common.counts.edges", { count: row.count, formatted: formatCount(row.count) })}
                  </Text>
                </div>
              ))}
            </div>
          </section>

          <section>
            <Title order={2} size="sm" mb="xs">
              {t("views.schema.sections.indexes")} · {indexes.length}
            </Title>
            {indexes.length === 0 ? (
              <Text size="sm" c="dimmed">
                {t("views.schema.indexes.none")}
              </Text>
            ) : (
              <div className={classes.group}>
                {indexes.map(([name, spec]) => (
                  <div key={name} className={classes.row}>
                    <Text size="sm" fw={500} className={classes.name}>
                      {name}
                    </Text>
                    <Badge size="xs" variant="light" radius="sm" tt="none">
                      {spec.analyzer}
                    </Badge>
                    <Text size="xs" c="dimmed" className={classes.pairs} truncate>
                      {t("views.schema.indexes.covers", {
                        properties: spec.properties.join(", "),
                        labels: spec.labels.includes("*")
                          ? t("views.schema.indexes.allLabels")
                          : spec.labels.join(", "),
                      })}
                    </Text>
                  </div>
                ))}
              </div>
            )}
          </section>
        </Stack>
      ) : null}

      {adding && schema.data ? (
        <AddToSchemaDialog
          kind={adding}
          schema={schema.data}
          properties={allProperties}
          saving={add.isPending}
          pair={adding === "relationship" ? (pair ?? undefined) : undefined}
          onCancel={closeDialog}
          onAdd={(addition) => {
            const type = addition.edge_labels?.[0];
            // A type already in the schema, picked for a drawn line: nothing to register.
            if (pair && type && schema.data?.edge_labels.includes(type)) {
              draw(type);
              return;
            }
            add.mutate(addition, { onSuccess: () => (pair && type ? draw(type) : closeDialog()) });
          }}
        />
      ) : null}
    </div>
  );
}
