import { useMemo, useState } from "react";
import { ApiError } from "../api/http";
import type { NodeRecord, Properties } from "../api/types";
import { isEmptyPatch, mergePatch } from "../lib/properties";
import { useTenantSession } from "../session/SessionProvider";
import { useActivity } from "./ActivityProvider";
import { useNode, useSaveNode } from "./queries";

/** An edit in progress: the version it started from and the properties as edited. */
interface Edit {
  base: NodeRecord;
  draft: Properties;
}

export interface NodeEditor {
  node: NodeRecord | undefined;
  isLoading: boolean;
  /** What the form shows: the draft while editing, else the node. */
  values: Properties;
  /** Only the keys the user changed, as a JSON Merge Patch. */
  patch: Properties;
  dirty: boolean;
  /** The node was saved by someone else after this edit began. */
  stale: boolean;
  /** Who saved it, when the live feed says. */
  changedBy: string | null;
  saving: boolean;
  error: string | null;
  change: (draft: Properties) => void;
  discard: () => void;
  /** Saves the user's changes; when `stale`, applies them on top of the newer version. */
  save: () => Promise<NodeRecord | null>;
}

/**
 * Editing one node safely. The draft keeps the version it started from, so
 * a refresh in the background never wipes it, and saving sends only the
 * keys the user changed with `If-Match` on that version. If someone saved
 * in between, `stale` is set instead of overwriting their work.
 */
export function useNodeEditor(ref: { id: string; label: string }): NodeEditor {
  const { me } = useTenantSession();
  const node = useNode(ref);
  const saveNode = useSaveNode();
  const { changes } = useActivity();
  const [edit, setEdit] = useState<Edit | null>(null);
  const [error, setError] = useState<string | null>(null);

  const patch = useMemo(() => (edit ? mergePatch(edit.base.properties, edit.draft) : {}), [edit]);
  const latest = node.data;
  const stale = Boolean(edit && latest && latest.etag !== edit.base.etag);
  const changedBy = stale
    ? (changes.find(
        (c) => c.kind === "node" && c.label === ref.label && c.ids?.includes(ref.id) && c.consumer !== me.consumer,
      )?.consumer ?? null)
    : null;

  const save = async (): Promise<NodeRecord | null> => {
    if (!edit || !latest || isEmptyPatch(patch)) return null;
    setError(null);
    // Not stale: write only if nobody saved since the edit began.
    // Stale and confirmed by the user: apply their keys on the latest version.
    const etag = stale ? latest.etag : edit.base.etag;
    try {
      const result = await saveNode.mutateAsync({ ...ref, patch, etag });
      setEdit(null);
      return result.data;
    } catch (e) {
      if (e instanceof ApiError && e.isConflict) await node.refetch();
      else setError(e instanceof Error ? e.message : "Couldn't save");
      return null;
    }
  };

  return {
    node: latest,
    isLoading: node.isLoading,
    values: edit?.draft ?? latest?.properties ?? {},
    patch,
    dirty: !isEmptyPatch(patch),
    stale,
    changedBy,
    saving: saveNode.isPending,
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
