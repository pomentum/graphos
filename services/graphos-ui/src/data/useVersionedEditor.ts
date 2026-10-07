import { useMemo, useState } from "react";
import { ApiError } from "../api/http";
import type { Properties } from "../api/types";
import { t } from "../i18n";
import { isEmptyPatch, mergePatch } from "../lib/properties";

/** Anything graphos versions: properties plus the `ETag` of this revision. */
export interface Versioned {
  properties: Properties;
  rev: number;
  etag: string;
}

/** An edit in progress: the version it started from and the properties as edited. */
interface Edit<T extends Versioned> {
  base: T;
  draft: Properties;
}

export interface VersionedEditor<T extends Versioned> {
  record: T | undefined;
  isLoading: boolean;
  /** What the form shows: the draft while editing, else the record. */
  values: Properties;
  /** Only the keys the user changed, as a JSON Merge Patch. */
  patch: Properties;
  dirty: boolean;
  /** Someone saved a newer version after this edit began. */
  stale: boolean;
  /** Who saved it, when the live feed says. */
  changedBy: string | null;
  saving: boolean;
  error: string | null;
  change: (draft: Properties) => void;
  discard: () => void;
  /** Saves the user's changes; when `stale`, applies them on top of the newer version. */
  save: () => Promise<T | null>;
}

export interface VersionedSource<T extends Versioned> {
  /** The record as last read (a query's data). */
  latest: T | undefined;
  isLoading: boolean;
  refetch: () => Promise<unknown>;
  /** Writes `patch` if the record is still at `etag`; rejects with a 412 `ApiError` otherwise. */
  write: (patch: Properties, etag: string) => Promise<T>;
  saving: boolean;
  /** Who last changed the record, other than the signed-in user, if known. */
  changedBy: string | null;
}

/**
 * Editing any versioned record (a node, an edge) safely. The draft keeps the
 * version it started from, so a refresh in the background never wipes it,
 * and saving sends only the keys the user changed with `If-Match` on that
 * version. If someone saved in between, `stale` is set instead of
 * overwriting their work.
 */
export function useVersionedEditor<T extends Versioned>(source: VersionedSource<T>): VersionedEditor<T> {
  const [edit, setEdit] = useState<Edit<T> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { latest } = source;

  const patch = useMemo(() => (edit ? mergePatch(edit.base.properties, edit.draft) : {}), [edit]);
  const stale = Boolean(edit && latest && latest.etag !== edit.base.etag);

  const save = async (): Promise<T | null> => {
    if (!edit || !latest || isEmptyPatch(patch)) return null;
    setError(null);
    // Not stale: write only if nobody saved since the edit began.
    // Stale and confirmed by the user: apply their keys on the latest version.
    const etag = stale ? latest.etag : edit.base.etag;
    try {
      const saved = await source.write(patch, etag);
      setEdit(null);
      return saved;
    } catch (e) {
      if (e instanceof ApiError && e.isConflict) await source.refetch();
      else setError(e instanceof Error ? e.message : t("errors.couldNotSave"));
      return null;
    }
  };

  return {
    record: latest,
    isLoading: source.isLoading,
    values: edit?.draft ?? latest?.properties ?? {},
    patch,
    dirty: !isEmptyPatch(patch),
    stale,
    changedBy: stale ? source.changedBy : null,
    saving: source.saving,
    error,
    change: (draft) => {
      if (latest) setEdit((current) => ({ base: current?.base ?? latest, draft }));
    },
    discard: () => {
      setEdit(null);
      setError(null);
    },
    save,
  };
}
