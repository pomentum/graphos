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
import { IconChevronDown, IconDownload, IconPlus, IconUpload } from "@tabler/icons-react";
import { lazy, Suspense, useMemo, useState, type ReactNode } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";
import {
  useAddToSchema,
  useCatalogue,
  useExportSchema,
  useGraphMap,
  useImportSchema,
  usePropertyDefinitions,
  useSchema,
} from "../../data/queries";
import { ALL, entryOf, type SchemaPart } from "../../lib/propertySchema";
import { mayCreate } from "../../lib/schemaFile";
import { formatCount } from "../../lib/time";
import { useSessionContext, useTenantSession } from "../../session/SessionProvider";
import { browseParams } from "../browse/browseState";
import { AddToSchemaDialog, type AdditionKind } from "./AddToSchemaDialog";
import { ImportSchemaDialog } from "./ImportSchemaDialog";
import { LockedHint, SchemaManagerBanner, useLockedReason } from "./SchemaManager";
import { DeprecatedBadge, DeprecationLines, NameActions, RenamesSection } from "./NameChanges";
import { PropertyChecks } from "./properties/PropertyChecks";
import { AddPropertyDialog } from "./properties/AddPropertyDialog";
import { EdgeProperties, SchemaProperties } from "./properties/SchemaProperties";
import { labelTree, propertiesByLabel, relationshipRows } from "./schemaModel";
import { StrictEndpoints } from "./StrictEndpoints";
import { StyleSwatch } from "./StyleSwatch";
import type { VertexDisplay } from "../../api/types";
import { labelStyles } from "../../theme/labelStyles";
import classes from "./SchemaView.module.css";

// The diagram brings React Flow and ELK: loaded only when it is opened.
const SchemaDiagram = lazy(() => import("./diagram/SchemaDiagram").then((m) => ({ default: m.SchemaDiagram })));

/** Label pairs listed per relationship type; the rest are summarised. */
const SHOWN_PAIRS = 3;

/**
 * Schema: what the tenant's graph is made of. The labels as a tree under
 * their parents, with node counts and the properties their nodes carry;
 * the relationship types with their edge counts and the label pairs they
 * join; the search indexes; the renames. Administrators can add labels,
 * relationship types and search indexes, and mark a label, a type or a
 * property deprecated with what replaces it; owners rename them (after a dry
 * run of what follows) and drop a deprecated label or type once it is empty.
 * No data is ever deleted here. The same schema can be drawn as a
 * diagram (`?view=diagram`), where labels are added and a relationship
 * type's label pairs declared by drawing from one label to another. Declared
 * pairs are the schema's associations; administrators can have graphos
 * refuse writes outside them (strict). A label or type opens to its property
 * definitions (its own, every label's or every type's, its parents') and the
 * properties its rows carry that nothing declares; maintainers declare them
 * there, set how strictly writes are held to them, and check the stored rows.
 * Maintainers can export the whole schema as a file and import one (here or
 * into another tenant), after a preview of what it changes. When a program
 * manages the schema, its structure controls stay in view but disabled for
 * everyone else, who can still change styles (see `SchemaManagerBanner`).
 */
export function SchemaView() {
  const { t } = useTranslation();
  const { tenant, canMaintain, api, me } = useTenantSession();
  const { refreshMe } = useSessionContext();
  const schema = useSchema();
  const map = useGraphMap();
  const catalogue = useCatalogue();
  const definitions = usePropertyDefinitions();
  const add = useAddToSchema();
  const exportSchema = useExportSchema();
  const importSchema = useImportSchema();
  const locked = useLockedReason();
  const [importing, setImporting] = useState(false);
  const [adding, setAdding] = useState<AdditionKind | null>(null);
  const [addingProperty, setAddingProperty] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "diagram" ? "diagram" : "list";
  const setView = (next: string) => setParams(next === "diagram" ? { view: "diagram" } : {}, { replace: true });
  // A label pair drawn on the diagram, waiting for the relationship type it declares.
  const [pair, setPair] = useState<{ src: string; dst: string } | null>(null);

  const counts = useMemo(
    () => (map.data ? new Map(map.data.labels.map((l) => [l.label, l.count] as const)) : null),
    [map.data],
  );
  const properties = useMemo(() => propertiesByLabel(catalogue.data?.by_property ?? {}), [catalogue.data]);
  const labels = schema.data ? labelTree(schema.data, counts, properties) : [];
  const relationships = schema.data ? relationshipRows(schema.data, map.data) : [];
  const indexes = Object.entries(schema.data?.search_indexes ?? {});
  const allProperties = Object.keys(catalogue.data?.by_property ?? {}).sort();
  const styles = useMemo(() => labelStyles(schema.data), [schema.data]);
  const saveStyle = (kind: "vertex" | "edge", name: string, entry: VertexDisplay | null, done: () => void) =>
    add.mutate({ display: { [kind]: { [name]: entry } } }, { onSuccess: done });
  const toggle = (key: string) => setOpen(open === key ? null : key);

  const declaresProperties = Boolean(
    Object.keys(schema.data?.property_schema?.vertex ?? {}).length ||
    Object.keys(schema.data?.property_schema?.edge ?? {}).length,
  );
  /** How many properties a label or type has definitions for; null while the tenant declares none. */
  const definedCount = (part: SchemaPart, name: string): number | null => {
    const policy = definitions.data?.[part]?.[name];
    return policy && declaresProperties ? Object.keys(policy.properties).length : null;
  };
  const panel = (part: SchemaPart, owner: string, carried: string[], actions?: ReactNode) =>
    schema.data ? (
      <SchemaProperties schema={schema.data} part={part} owner={owner} carried={carried} actions={actions} />
    ) : null;

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

  return (
    <div className={view === "diagram" ? classes.diagramRoot : classes.root}>
      <Group justify="space-between" align="flex-start" mb="xs">
        <div>
          <Title order={1} size="h4">
            {t("views.schema.heading", { tenant })}
          </Title>
          <Text size="xs" c="dimmed">
            {canMaintain ? t("views.schema.neverRemoves") : t("views.schema.readOnly")}
          </Text>
        </div>
        <Group gap="md">
          {schema.data ? <PropertyChecks schema={schema.data} editable={canMaintain} locked={locked} /> : null}
          {schema.data ? (
            <StrictEndpoints schema={schema.data} map={map.data} editable={canMaintain} locked={locked} />
          ) : null}
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
          {canMaintain && schema.data ? (
            <Group gap={6}>
              <Tooltip label={t("views.schema.file.exportTooltip")} withArrow multiline w={240}>
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<IconDownload size={14} />}
                  loading={exportSchema.isPending}
                  onClick={() => exportSchema.mutate()}
                >
                  {t("views.schema.file.export")}
                </Button>
              </Tooltip>
              <Tooltip label={t("views.schema.file.importTooltip")} withArrow multiline w={240}>
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<IconUpload size={14} />}
                  onClick={() => setImporting(true)}
                >
                  {t("views.schema.file.import")}
                </Button>
              </Tooltip>
            </Group>
          ) : null}
          {canMaintain && schema.data && locked ? (
            <LockedHint reason={locked}>
              <Button
                size="xs"
                leftSection={<IconPlus size={14} />}
                rightSection={<IconChevronDown size={12} />}
                disabled
              >
                {t("views.schema.add")}
              </Button>
            </LockedHint>
          ) : canMaintain && schema.data ? (
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
                <Menu.Item onClick={() => setAddingProperty(true)}>{t("views.schema.addProperty")}</Menu.Item>
              </Menu.Dropdown>
            </Menu>
          ) : null}
        </Group>
      </Group>

      <SchemaManagerBanner />

      {schema.isLoading ? <Skeleton h={240} radius="lg" /> : null}

      {schema.data && view === "diagram" ? (
        <div className={classes.diagram}>
          <Suspense fallback={<Skeleton h="100%" radius="lg" />}>
            <SchemaDiagram
              tenant={tenant}
              schema={schema.data}
              map={map.data}
              properties={properties}
              editable={canMaintain}
              locked={locked}
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
              <AllRow
                part="vertex"
                open={open === "vertex:*"}
                onToggle={() => toggle("vertex:*")}
                count={Object.keys(entryOf(schema.data, "vertex", ALL)?.properties ?? {}).length}
              />
              {open === "vertex:*" ? <div className={classes.detail}>{panel("vertex", ALL, [])}</div> : null}
              {labels.map((row) => (
                <div key={row.label}>
                  <div className={classes.styledRow} style={{ paddingLeft: 12 + row.depth * 18 }}>
                    <StyleSwatch
                      kind="vertex"
                      name={row.label}
                      own={ownStyle(schema.data?.display?.vertex?.[row.label])}
                      shown={styles.vertex.get(row.label) ?? null}
                      properties={row.properties}
                      editable={canMaintain}
                      saving={add.isPending}
                      onSave={(entry, done) => saveStyle("vertex", row.label, entry, done)}
                    />
                    <UnstyledButton
                      className={classes.row}
                      onClick={() => toggle(`vertex:${row.label}`)}
                      aria-expanded={open === `vertex:${row.label}`}
                    >
                      <Text size="sm" fw={500} className={classes.name}>
                        {row.label}
                      </Text>
                      <DeprecatedBadge schema={schema.data!} part="vertex" name={row.label} />
                      {row.parent ? (
                        <Text size="xs" c="dimmed">
                          {t("views.schema.labels.inherits", { parent: row.parent })}
                        </Text>
                      ) : null}
                      <Text size="xs" c="dimmed" className={classes.pairs} truncate>
                        {entryOf(schema.data, "vertex", row.label)?.description ?? ""}
                      </Text>
                      <Text size="xs" c="dimmed" className={classes.end}>
                        {row.count === null
                          ? ""
                          : t("common.counts.nodes", { count: row.count, formatted: formatCount(row.count) })}
                        {" · "}
                        {definedCount("vertex", row.label) === null
                          ? t("views.schema.labels.properties", { count: row.properties.length })
                          : t("views.schema.labels.defined", { count: definedCount("vertex", row.label)! })}
                      </Text>
                    </UnstyledButton>
                  </div>
                  {open === `vertex:${row.label}` ? (
                    <div className={classes.detail} style={{ paddingLeft: 12 + row.depth * 18 }}>
                      <DeprecationLines schema={schema.data!} part="vertex" name={row.label} />
                      {panel(
                        "vertex",
                        row.label,
                        row.properties,
                        <>
                          <Button
                            size="compact-xs"
                            variant="subtle"
                            component={Link}
                            to={`/browse?${browseParams({ label: row.label, filter: null }).toString()}`}
                          >
                            {t("views.schema.labels.browse")}
                          </Button>
                          <NameActions
                            schema={schema.data!}
                            part="vertex"
                            name={row.label}
                            rows={row.count}
                            carried={row.properties}
                          />
                        </>,
                      )}
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
              <AllRow
                part="edge"
                open={open === "edge:*"}
                onToggle={() => toggle("edge:*")}
                count={Object.keys(entryOf(schema.data, "edge", ALL)?.properties ?? {}).length}
              />
              {open === "edge:*" ? <div className={classes.detail}>{panel("edge", ALL, [])}</div> : null}
              {relationships.map((row) => (
                <div key={row.type}>
                  <div className={classes.styledRow} style={{ paddingLeft: 12 }}>
                    <StyleSwatch
                      kind="edge"
                      name={row.type}
                      own={ownStyle(schema.data?.display?.edge?.[row.type])}
                      shown={styles.edge.get(row.type) ?? null}
                      editable={canMaintain}
                      saving={add.isPending}
                      onSave={(entry, done) => saveStyle("edge", row.type, entry, done)}
                    />
                    <UnstyledButton
                      className={classes.row}
                      onClick={() => toggle(`edge:${row.type}`)}
                      aria-expanded={open === `edge:${row.type}`}
                    >
                      <Text size="sm" fw={500} className={classes.name}>
                        {row.type}
                      </Text>
                      <DeprecatedBadge schema={schema.data!} part="edge" name={row.type} />
                      {row.derived ? (
                        <Tooltip label={t("views.schema.relationships.derivedTooltip")} withArrow>
                          <Badge size="xs" variant="light" radius="sm" tt="none">
                            {t("views.schema.relationships.derived")}
                          </Badge>
                        </Tooltip>
                      ) : null}
                      {row.undeclared ? (
                        <Tooltip label={t("views.schema.relationships.undeclaredTooltip")} withArrow multiline w={260}>
                          <Badge size="xs" variant="light" color="red" radius="sm" tt="none">
                            {t("views.schema.relationships.undeclared", { count: row.undeclared })}
                          </Badge>
                        </Tooltip>
                      ) : null}
                      <Text size="xs" c="dimmed" className={classes.pairs} truncate>
                        {entryOf(schema.data, "edge", row.type)?.description ??
                          (row.declared.length
                            ? t("views.schema.relationships.declared", { pairs: pairList(row.declared, t) })
                            : row.pairs.length === 0
                              ? t("views.schema.relationships.none")
                              : pairList(row.pairs, t))}
                      </Text>
                      <Text size="xs" c="dimmed" className={classes.end}>
                        {row.estimated ? "≈ " : ""}
                        {t("common.counts.edges", { count: row.count, formatted: formatCount(row.count) })}
                        {definedCount("edge", row.type) === null
                          ? ""
                          : ` · ${t("views.schema.labels.defined", { count: definedCount("edge", row.type)! })}`}
                      </Text>
                    </UnstyledButton>
                  </div>
                  {open === `edge:${row.type}` ? (
                    <div className={classes.detail} style={{ paddingLeft: 12 }}>
                      <DeprecationLines schema={schema.data!} part="edge" name={row.type} />
                      <EdgeProperties type={row.type}>
                        {(carried) =>
                          panel(
                            "edge",
                            row.type,
                            carried,
                            <NameActions
                              schema={schema.data!}
                              part="edge"
                              name={row.type}
                              rows={map.data && !row.estimated ? row.count : null}
                              carried={carried}
                            />,
                          )
                        }
                      </EdgeProperties>
                      {row.declared.length ? (
                        <Text size="xs" c="dimmed" mt={6}>
                          {t("views.schema.relationships.declared", { pairs: pairList(row.declared, t) })}
                        </Text>
                      ) : null}
                    </div>
                  ) : null}
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

          <RenamesSection schema={schema.data} />
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
          onAdd={(addition) => add.mutate(addition, { onSuccess: closeDialog })}
        />
      ) : null}

      {addingProperty && schema.data ? (
        <AddPropertyDialog schema={schema.data} carriedByLabel={properties} onClose={() => setAddingProperty(false)} />
      ) : null}

      {importing ? (
        <ImportSchemaDialog
          tenant={tenant}
          locked={locked}
          mayCreate={(name) => mayCreate(me.create_tenants, me.admin, name)}
          onPreview={(doc, options) => api.previewSchema(doc, options)}
          onImport={(doc, options) =>
            importSchema.mutate(
              { doc, options },
              {
                onSuccess: () => {
                  setImporting(false);
                  // Into another tenant, perhaps a new one: the tenant list changes.
                  if (options.name && options.name !== tenant) void refreshMe();
                },
              },
            )
          }
          importing={importSchema.isPending}
          onCancel={() => setImporting(false)}
        />
      ) : null}
    </div>
  );
}

/** What a label or type declares itself, without the nulls the server answers for left-out keys. */
function ownStyle(entry: VertexDisplay | null | undefined): VertexDisplay | null {
  if (!entry) return null;
  const kept = Object.fromEntries(Object.entries(entry).filter(([, v]) => v !== null && v !== undefined));
  return Object.keys(kept).length ? kept : null;
}

/** "A → B, C → D and 2 more pairs": the first pairs of a relationship type. */
function pairList(pairs: { src: string; dst: string }[], t: TFunction): string {
  const shown = pairs
    .slice(0, SHOWN_PAIRS)
    .map((p) => `${p.src} → ${p.dst}`)
    .join(", ");
  return pairs.length > SHOWN_PAIRS
    ? `${shown} ${t("views.schema.relationships.pairsMore", { count: pairs.length - SHOWN_PAIRS })}`
    : shown;
}

/** The `*` row of a section: the definitions every label (or every relationship type) carries. */
function AllRow({
  part,
  open,
  onToggle,
  count,
}: {
  part: SchemaPart;
  open: boolean;
  onToggle: () => void;
  count: number;
}) {
  const { t } = useTranslation();
  return (
    <UnstyledButton className={classes.row} onClick={onToggle} aria-expanded={open}>
      <Text size="sm" fw={500} fs="italic" className={classes.name}>
        {part === "edge" ? t("views.schema.properties.allTypes") : t("views.schema.properties.allLabels")}
      </Text>
      <Text size="xs" c="dimmed" className={classes.end}>
        {t("views.schema.properties.definitions", { count })}
      </Text>
    </UnstyledButton>
  );
}
