import { Alert, Button, FileInput, Group, List, Modal, Radio, Stack, Text, TextInput } from "@mantine/core";
import { IconFileUpload } from "@tabler/icons-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ImportOptions } from "../../api/graphosApi";
import type { SchemaDocument, SchemaDryRun } from "../../api/types";
import { describeSchema } from "../../lib/changes";
import {
  readFileText,
  readSchemaFile,
  SCHEMA_VERSION,
  TENANT_NAME,
  type SchemaFileProblem,
} from "../../lib/schemaFile";

export interface ImportSchemaDialogProps {
  /** The tenant the page shows: the default target. */
  tenant: string;
  /**
   * Why a schema cannot be imported into this tenant (a program manages its
   * schema): only another tenant can be chosen. Null otherwise.
   */
  locked?: string | null;
  /** Whether the user may create a tenant of this name (`create_tenants`). */
  mayCreate: (name: string) => boolean;
  /** The dry run: what importing would change, writing nothing. */
  onPreview: (doc: SchemaDocument, options: ImportOptions) => Promise<SchemaDryRun>;
  /** The import itself. */
  onImport: (doc: SchemaDocument, options: ImportOptions) => void;
  importing: boolean;
  onCancel: () => void;
}

/**
 * Imports a schema file: choose the file, the tenant (this one, or another,
 * created when new) and the mode (merge, or replace), preview what graphos
 * would change, then import. The file is read in the page and sent as the
 * registration it is; nothing is kept in the browser.
 */
export function ImportSchemaDialog({
  tenant,
  locked = null,
  mayCreate,
  onPreview,
  onImport,
  importing,
  onCancel,
}: ImportSchemaDialogProps) {
  const { t } = useTranslation();
  const [file, setFile] = useState<File | null>(null);
  const [doc, setDoc] = useState<SchemaDocument | null>(null);
  const [problem, setProblem] = useState<SchemaFileProblem | null>(null);
  const [target, setTarget] = useState<"this" | "other">(locked ? "other" : "this");
  const [otherName, setOtherName] = useState("");
  const [mode, setMode] = useState<"merge" | "replace">("merge");
  const [preview, setPreview] = useState<SchemaDryRun | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);

  const name = target === "this" ? tenant : otherName.trim();
  const nameError =
    target === "this" || !name
      ? null
      : !TENANT_NAME.test(name)
        ? t("views.schema.file.invalidName")
        : name !== tenant && !mayCreate(name)
          ? t("views.schema.file.mayNotCreate", { name })
          : null;
  const options: ImportOptions = { name, replace: mode === "replace" };
  const ready = Boolean(doc) && Boolean(name) && !nameError;

  // Any change to what would be imported asks for a new preview.
  const reset = () => {
    setPreview(null);
    setPreviewError(null);
  };

  const choose = async (next: File | null) => {
    setFile(next);
    setDoc(null);
    setProblem(null);
    reset();
    if (!next) return;
    const read = readSchemaFile(await readFileText(next));
    if ("problem" in read) setProblem(read.problem);
    else setDoc(read.doc);
  };

  const runPreview = async () => {
    if (!doc || !ready) return;
    setPreviewing(true);
    reset();
    try {
      setPreview(await onPreview(doc, options));
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : String(error));
    } finally {
      setPreviewing(false);
    }
  };

  const header = doc?._export;
  const problemText = !problem
    ? null
    : problem.kind === "newerVersion"
      ? t("views.schema.file.newerVersion", { version: problem.version, supported: SCHEMA_VERSION })
      : problem.kind === "notJson"
        ? t("views.schema.file.notJson")
        : t("views.schema.file.notSchema");

  return (
    <Modal opened onClose={onCancel} title={t("views.schema.file.title")} centered radius="lg" size="md">
      <Stack gap="sm">
        <FileInput
          label={t("views.schema.file.file")}
          description={t("views.schema.file.fileHint")}
          accept=".json,application/json"
          leftSection={<IconFileUpload size={16} />}
          value={file}
          onChange={(next) => void choose(next)}
          error={problemText}
          clearable
          data-autofocus
        />
        {doc ? (
          <Text size="xs" c="dimmed">
            {header?.tenant
              ? t("views.schema.file.source", {
                  tenant: header.tenant,
                  graphos: header.graphos ?? "?",
                  date: header.exported_at ?? "?",
                })
              : t("views.schema.file.sourceUnknown")}
          </Text>
        ) : null}

        <Radio.Group
          label={t("views.schema.file.target")}
          value={target}
          onChange={(next) => {
            setTarget(next as "this" | "other");
            reset();
          }}
        >
          <Stack gap={6} mt={4}>
            <Radio
              value="this"
              label={t("views.schema.file.targetThis", { tenant })}
              description={locked ?? undefined}
              disabled={locked !== null}
            />
            <Radio value="other" label={t("views.schema.file.targetNew")} />
          </Stack>
        </Radio.Group>
        {target === "other" ? (
          <TextInput
            label={t("views.schema.file.newName")}
            description={t("views.schema.file.newNameHint")}
            value={otherName}
            error={nameError}
            onChange={(e) => {
              setOtherName(e.currentTarget.value);
              reset();
            }}
          />
        ) : null}

        <Radio.Group
          label={t("views.schema.file.mode")}
          value={mode}
          onChange={(next) => {
            setMode(next as "merge" | "replace");
            reset();
          }}
        >
          <Stack gap={6} mt={4}>
            <Radio value="merge" label={t("views.schema.file.merge")} description={t("views.schema.file.mergeHint")} />
            <Radio
              value="replace"
              label={t("views.schema.file.replace")}
              description={t("views.schema.file.replaceHint")}
            />
          </Stack>
        </Radio.Group>

        {preview ? (
          <Alert variant="light" color="gray" radius="md">
            <Stack gap={4}>
              {!preview.exists ? (
                <Text size="sm">{t("views.schema.file.willCreate", { name: preview.tenant })}</Text>
              ) : null}
              <Text size="sm">
                {preview.changes && preview.exists
                  ? describeSchema(preview.changes)
                  : preview.exists
                    ? t("views.schema.file.nothingChanges")
                    : null}
              </Text>
            </Stack>
          </Alert>
        ) : doc ? (
          <Text size="xs" c="dimmed">
            {t("views.schema.file.previewFirst")}
          </Text>
        ) : null}
        {preview?.breaking?.length ? (
          <Alert variant="light" color="red" radius="md" title={t("views.schema.file.breaking")}>
            <List size="xs" spacing={2}>
              {preview.breaking.map((item) => (
                <List.Item key={`${item.name ?? ""}:${item.change}:${item.detail}`}>{item.detail}</List.Item>
              ))}
            </List>
          </Alert>
        ) : null}
        {preview?.warnings.length ? (
          <Alert variant="light" color="yellow" radius="md" title={t("views.schema.file.warnings")}>
            <List size="xs" spacing={2}>
              {preview.warnings.map((warning) => (
                <List.Item key={warning}>{warning}</List.Item>
              ))}
            </List>
          </Alert>
        ) : null}
        {previewError ? (
          <Alert variant="light" color="red" radius="md">
            <Text size="xs">{previewError}</Text>
          </Alert>
        ) : null}

        <Group justify="flex-end" gap="xs" mt="xs">
          <Button variant="default" size="xs" onClick={onCancel}>
            {t("common.actions.cancel")}
          </Button>
          <Button variant="light" size="xs" disabled={!ready} loading={previewing} onClick={() => void runPreview()}>
            {t("views.schema.file.preview")}
          </Button>
          <Button
            size="xs"
            color={mode === "replace" ? "red" : undefined}
            disabled={!ready || !preview}
            loading={importing}
            onClick={() => doc && onImport(doc, options)}
          >
            {t("views.schema.file.apply")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
