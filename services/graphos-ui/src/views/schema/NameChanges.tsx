import {
  Alert,
  Autocomplete,
  Badge,
  Button,
  Group,
  Modal,
  Progress,
  Select,
  Stack,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import type { TFunction } from "i18next";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type {
  DeprecatedProperty,
  RenameKind,
  SchemaRename,
  SchemaRenameRequest,
  SchemaRenameResult,
  TenantSchema,
} from "../../api/types";
import { ConfirmDelete } from "../../components/ConfirmDelete";
import { useDeprecate, useDropLabel, useJob, useRefreshSchema, useRenameSchema } from "../../data/queries";
import {
  deprecatedProperties,
  deprecationOf,
  isDeprecated,
  nameableProperties,
  renamesNewestFirst,
  withDeprecation,
} from "../../lib/deprecation";
import { isActiveJob } from "../../lib/jobs";
import { PROPERTY_NAME, type SchemaPart } from "../../lib/propertySchema";
import { formatCount, relativeTime } from "../../lib/time";
import { useTenantSession } from "../../session/SessionProvider";
import { LABEL_NAME } from "./schemaModel";
import classes from "./SchemaView.module.css";

/** The label or type itself, in a "what" picker (a property is picked by its name). */
const ITSELF = "";

/** "use Person: until 2027", what a deprecation says. */
function advice(about: DeprecatedProperty, t: TFunction): string {
  const use = about.replaced_by ? t("views.schema.changes.useInstead", { name: about.replaced_by }) : "";
  return [use, about.note ?? ""].filter(Boolean).join(": ");
}

/** A "deprecated" mark on a label's or type's row, saying what replaces it. */
export function DeprecatedBadge({ schema, part, name }: { schema: TenantSchema; part: SchemaPart; name: string }) {
  const { t } = useTranslation();
  const entry = deprecationOf(schema, part, name);
  if (!entry?.deprecated) return null;
  const said = advice(entry, t);
  return (
    <Tooltip label={said || t("views.schema.changes.deprecatedTooltip")} withArrow multiline w={260}>
      <Badge size="xs" variant="light" color="orange" radius="sm" tt="none">
        {t("views.schema.changes.deprecated")}
      </Badge>
    </Tooltip>
  );
}

/** What is deprecated on a label or type, in its open row. */
export function DeprecationLines({ schema, part, name }: { schema: TenantSchema; part: SchemaPart; name: string }) {
  const { t } = useTranslation();
  const entry = deprecationOf(schema, part, name);
  const properties = deprecatedProperties(schema, part, name);
  if (!entry?.deprecated && properties.length === 0) return null;
  return (
    <Stack gap={2} mb={6}>
      {entry?.deprecated ? (
        <Text size="xs" c="orange">
          {[t(`views.schema.changes.itselfDeprecated.${part}`), advice(entry, t)].filter(Boolean).join(" · ")}
        </Text>
      ) : null}
      {properties.length ? (
        <Text size="xs" c="orange">
          {t("views.schema.changes.deprecatedProperties", {
            properties: properties
              .map(([property, about]) => (advice(about, t) ? `${property} (${advice(about, t)})` : property))
              .join(", "),
          })}
        </Text>
      ) : null}
    </Stack>
  );
}

/**
 * The buttons on a label's or type's open row: rename it or one of its
 * properties (owner), deprecate either (maintain), and drop it once it is
 * deprecated and empty (owner).
 */
export function NameActions({
  schema,
  part,
  name,
  rows,
  carried,
}: {
  schema: TenantSchema;
  part: SchemaPart;
  name: string;
  /** Its rows; null while unknown. */
  rows: number | null;
  carried: string[];
}) {
  const { t } = useTranslation();
  const { isOwner, canMaintain } = useTenantSession();
  const [dialog, setDialog] = useState<"rename" | "deprecate" | null>(null);
  const drop = useDropLabel();
  const deprecated = isDeprecated(schema, part, name);
  return (
    <>
      {isOwner ? (
        <Button size="compact-xs" variant="subtle" onClick={() => setDialog("rename")}>
          {t("views.schema.changes.rename")}
        </Button>
      ) : null}
      {canMaintain ? (
        <Button size="compact-xs" variant="subtle" onClick={() => setDialog("deprecate")}>
          {t("views.schema.changes.deprecate")}
        </Button>
      ) : null}
      {isOwner && deprecated && rows === 0 ? (
        <ConfirmDelete
          question={t(`views.schema.changes.dropQuestion.${part}`, { name })}
          confirmLabel={t("views.schema.changes.drop", { name })}
          busy={drop.isPending}
          onConfirm={() => drop.mutate(name)}
        />
      ) : null}
      {dialog === "rename" ? (
        <RenameDialog schema={schema} part={part} name={name} carried={carried} onClose={() => setDialog(null)} />
      ) : null}
      {dialog === "deprecate" ? (
        <DeprecateDialog schema={schema} part={part} name={name} carried={carried} onClose={() => setDialog(null)} />
      ) : null}
    </>
  );
}

interface NameDialogProps {
  schema: TenantSchema;
  part: SchemaPart;
  name: string;
  carried: string[];
  onClose: () => void;
}

function whatChoices(props: NameDialogProps, t: TFunction) {
  const { schema, part, name, carried } = props;
  return [
    { value: ITSELF, label: t(`views.schema.changes.itself.${part}`, { name }) },
    ...nameableProperties(schema, part, name, carried).map((p) => ({
      value: p,
      label: t("views.schema.changes.property", { name: p }),
    })),
  ];
}

/**
 * Renames a label, a relationship type or one of its properties (owner),
 * after a dry run that says what it changes: the schema parts that follow,
 * the rows, and the saved queries that name it (not rewritten).
 */
export function RenameDialog(props: NameDialogProps) {
  const { schema, part, name, onClose } = props;
  const { t } = useTranslation();
  const { api } = useTenantSession();
  const rename = useRenameSchema();
  const [what, setWhat] = useState(ITSELF);
  const [to, setTo] = useState("");
  const [preview, setPreview] = useState<SchemaRenameResult | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const kind: RenameKind = what !== ITSELF ? "property" : part === "vertex" ? "label" : "edge_type";
  const target = to.trim();
  const from = what === ITSELF ? name : what;
  const request: SchemaRenameRequest =
    kind === "property" ? { kind, on: name, from, to: target } : { kind, from, to: target };
  const taken = kind !== "property" && (schema.vertex_labels.includes(target) || schema.edge_labels.includes(target));
  const invalid = target !== "" && !(kind === "property" ? PROPERTY_NAME : LABEL_NAME).test(target);
  const error = invalid
    ? t(kind === "property" ? "views.schema.changes.invalidProperty" : "views.schema.changes.invalidName")
    : taken
      ? t("views.schema.changes.taken", { name: target })
      : target === from && target
        ? t("views.schema.changes.same")
        : null;
  const ready = target !== "" && error === null;
  const previewed = preview !== null && preview.from === from && preview.to === target && preview.kind === kind;

  const reset = (next: () => void) => {
    next();
    setPreview(null);
    setProblem(null);
  };
  const runPreview = async () => {
    setPreviewing(true);
    setProblem(null);
    try {
      setPreview(await api.renameSchema(request, true));
    } catch (err) {
      setProblem(err instanceof Error ? err.message : String(err));
    } finally {
      setPreviewing(false);
    }
  };
  const apply = () =>
    rename.mutate(request, {
      onSuccess: (done) => {
        notifications.show({
          message: done.job_id
            ? t("notices.renameStarted", { from, to: target })
            : t("notices.renamed", { from, to: target }),
          autoClose: 3000,
        });
        onClose();
      },
    });

  return (
    <Modal opened onClose={onClose} centered radius="lg" title={t("views.schema.changes.renameTitle", { name })}>
      <Stack gap="sm">
        <Select
          label={t("views.schema.changes.what")}
          data={whatChoices(props, t)}
          value={what}
          allowDeselect={false}
          onChange={(value) => reset(() => setWhat(value ?? ITSELF))}
        />
        <TextInput
          label={t("views.schema.changes.newName")}
          value={to}
          error={error}
          onChange={(e) => {
            const value = e.currentTarget.value;
            reset(() => setTo(value));
          }}
          data-autofocus
        />
        <Text size="xs" c="dimmed">
          {t(kind === "property" ? "views.schema.changes.propertyHint" : "views.schema.changes.inPlaceHint")}
        </Text>
        {problem ? (
          <Alert color="red" variant="light" radius="md">
            {problem}
          </Alert>
        ) : null}
        {previewed ? <RenameReport result={preview} /> : null}
        <Group justify="flex-end" gap="xs">
          <Button variant="default" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <Button variant="default" disabled={!ready} loading={previewing} onClick={() => void runPreview()}>
            {t("views.schema.changes.preview")}
          </Button>
          <Button color="red" disabled={!ready || !previewed} loading={rename.isPending} onClick={apply}>
            {t("views.schema.changes.apply")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** What a dry run of a rename says it would change. */
function RenameReport({ result }: { result: SchemaRenameResult }) {
  const { t } = useTranslation();
  const rows = result.rows ?? 0;
  return (
    <Stack gap={4}>
      <Text size="xs">
        {result.kind === "property"
          ? t("views.schema.changes.report.rewrites", { count: rows, formatted: formatCount(rows) })
          : t("views.schema.changes.report.keeps", { count: rows, formatted: formatCount(rows) })}
      </Text>
      {result.spec_parts.length ? (
        <Text size="xs" c="dimmed">
          {t("views.schema.changes.report.parts", { parts: result.spec_parts.join(", ") })}
        </Text>
      ) : null}
      {result.indexes.length || result.endpoints ? (
        <Text size="xs" c="dimmed">
          {t("views.schema.changes.report.follow", { indexes: result.indexes.length, pairs: result.endpoints })}
        </Text>
      ) : null}
      {result.saved_queries.length ? (
        <Alert color="yellow" variant="light" radius="md" p="xs">
          <Text size="xs">
            {t("views.schema.changes.report.savedQueries", {
              name: result.from,
              queries: result.saved_queries.join(", "),
            })}
          </Text>
        </Alert>
      ) : null}
      <Text size="xs" c="dimmed">
        {t("views.schema.changes.report.afterwards", { name: result.from })}
      </Text>
    </Stack>
  );
}

/**
 * Marks a label, a relationship type or one of its properties deprecated,
 * with what replaces it and why (maintain), or takes the mark back. Writes
 * naming it still succeed, with a warning.
 */
export function DeprecateDialog(props: NameDialogProps) {
  const { schema, part, name, carried, onClose } = props;
  const { t } = useTranslation();
  const deprecate = useDeprecate();
  const entry = deprecationOf(schema, part, name);
  const [what, setWhat] = useState(ITSELF);
  const current = what === ITSELF ? (entry?.deprecated ? entry : null) : (entry?.properties?.[what] ?? null);
  const [replacedBy, setReplacedBy] = useState(current?.replaced_by ?? "");
  const [note, setNote] = useState(current?.note ?? "");
  const pick = (value: string) => {
    const next = value === ITSELF ? (entry?.deprecated ? entry : null) : (entry?.properties?.[value] ?? null);
    setWhat(value);
    setReplacedBy(next?.replaced_by ?? "");
    setNote(next?.note ?? "");
  };
  const others = (part === "vertex" ? schema.vertex_labels : schema.edge_labels).filter((n) => n !== name);
  const properties = nameableProperties(schema, part, name, carried).filter((p) => p !== what);
  const replacement = replacedBy.trim();
  const invalid =
    replacement !== "" && !(what === ITSELF ? others.includes(replacement) : PROPERTY_NAME.test(replacement));
  const save = (change: DeprecatedProperty | null) =>
    deprecate.mutate(
      { [part]: { [name]: withDeprecation(entry, what === ITSELF ? null : what, change) } },
      { onSuccess: onClose },
    );

  return (
    <Modal opened onClose={onClose} centered radius="lg" title={t("views.schema.changes.deprecateTitle", { name })}>
      <Stack gap="sm">
        <Select
          label={t("views.schema.changes.what")}
          data={whatChoices(props, t)}
          value={what}
          allowDeselect={false}
          onChange={(value) => pick(value ?? ITSELF)}
        />
        {what === ITSELF ? (
          <Select
            label={t("views.schema.changes.replacedBy")}
            description={t("views.schema.changes.replacedByHint")}
            data={others}
            value={replacement || null}
            searchable
            clearable
            onChange={(value) => setReplacedBy(value ?? "")}
          />
        ) : (
          <Autocomplete
            label={t("views.schema.changes.replacedBy")}
            description={t("views.schema.changes.replacedByHint")}
            data={properties}
            value={replacedBy}
            error={invalid ? t("views.schema.changes.invalidProperty") : null}
            onChange={setReplacedBy}
          />
        )}
        <TextInput
          label={t("views.schema.changes.note")}
          description={t("views.schema.changes.noteHint")}
          value={note}
          maxLength={500}
          onChange={(e) => setNote(e.currentTarget.value)}
        />
        <Text size="xs" c="dimmed">
          {t("views.schema.changes.deprecateHint")}
        </Text>
        <Group justify="space-between" gap="xs">
          {current ? (
            <Button variant="subtle" color="gray" loading={deprecate.isPending} onClick={() => save(null)}>
              {t("views.schema.changes.undeprecate")}
            </Button>
          ) : (
            <span />
          )}
          <Group gap="xs">
            <Button variant="default" onClick={onClose}>
              {t("common.actions.cancel")}
            </Button>
            <Button
              disabled={invalid}
              loading={deprecate.isPending}
              onClick={() => save({ replaced_by: replacement, note })}
            >
              {t(current ? "common.actions.save" : "views.schema.changes.deprecateApply")}
            </Button>
          </Group>
        </Group>
      </Stack>
    </Modal>
  );
}

/** The tenant's renames, the newest first, with a running property rename's progress. */
export function RenamesSection({ schema }: { schema: TenantSchema }) {
  const { t } = useTranslation();
  const renames = renamesNewestFirst(schema);
  if (renames.length === 0) return null;
  return (
    <section>
      <Title order={2} size="sm" mb="xs">
        {t("views.schema.sections.renames")} · {renames.length}
      </Title>
      <div className={classes.group}>
        {renames.map((r, i) => (
          <RenameRow key={`${r.kind}:${r.on ?? ""}:${r.from}:${r.at}:${i}`} rename={r} />
        ))}
      </div>
    </section>
  );
}

function RenameRow({ rename: r }: { rename: SchemaRename }) {
  const { t } = useTranslation();
  const { isOwner } = useTenantSession();
  const job = useJob(r.status === "running" ? r.job_id : null);
  const refresh = useRefreshSchema();
  const again = useRenameSchema();
  const wasRunning = useRef(false);
  const running = isActiveJob(job.data);
  // The job ended: the schema now says done (or failed).
  useEffect(() => {
    if (wasRunning.current && !running) void refresh();
    wasRunning.current = running;
  }, [running, refresh]);
  const progress = job.data?.progress;
  const what =
    r.kind === "property"
      ? t("views.schema.changes.kinds.property", { on: r.on })
      : t(`views.schema.changes.kinds.${r.kind}`);
  return (
    <div className={classes.row}>
      <Text size="sm" fw={500} className={classes.name}>
        {r.from} → {r.to}
      </Text>
      <Text size="xs" c="dimmed">
        {what}
      </Text>
      {r.status !== "done" ? (
        <Badge size="xs" variant="light" radius="sm" tt="none" color={r.status === "failed" ? "red" : "blue"}>
          {t(`views.schema.changes.status.${r.status}`)}
        </Badge>
      ) : null}
      <div className={classes.pairs}>
        {r.status === "running" && progress ? (
          <Tooltip
            label={t("views.schema.changes.progress", {
              done: formatCount(progress.done),
              total: progress.total === null ? "?" : formatCount(progress.total),
            })}
            withArrow
          >
            <Progress
              size="sm"
              radius="sm"
              value={progress.total ? Math.min(100, (100 * progress.done) / progress.total) : 0}
              aria-label={t("views.schema.changes.progressLabel", { name: r.from })}
            />
          </Tooltip>
        ) : null}
        {r.status === "failed" && isOwner ? (
          <Button
            size="compact-xs"
            variant="subtle"
            loading={again.isPending}
            onClick={() => again.mutate({ kind: "property", on: r.on ?? undefined, from: r.from, to: r.to })}
          >
            {t("views.schema.changes.resume")}
          </Button>
        ) : null}
      </div>
      <Text size="xs" c="dimmed" className={classes.end}>
        {r.by ? t("views.schema.changes.when", { when: relativeTime(r.at), by: r.by }) : relativeTime(r.at)}
      </Text>
    </div>
  );
}
