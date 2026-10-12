/**
 * Saves text as a file through the browser's own download: a Blob behind a
 * temporary link, released at once. Nothing is kept in browser storage.
 */
export function saveFile(filename: string, text: string, type = "application/json"): void {
  saveBlob(filename, new Blob([text], { type }));
}

/** Saves a Blob through a temporary download link. */
export function saveBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** Where a streamed download is written: a file the user picked, or none (a Blob then). */
export type SaveTarget = { write(response: Response): Promise<void> } | null;

interface FilePickerWindow {
  showSaveFilePicker?: (options: { suggestedName: string }) => Promise<{
    createWritable(): Promise<WritableStream<Uint8Array>>;
  }>;
}

/**
 * Asks where to save `filename`, where the browser can write a file as it
 * arrives (the File System Access API, Chromium): memory then stays flat
 * however large the download. Must run in the click that asked for it.
 * Returns `null` where the browser cannot, or `undefined` when the user
 * cancelled the dialog.
 */
export async function pickSaveTarget(filename: string): Promise<SaveTarget | undefined> {
  const picker = (window as unknown as FilePickerWindow).showSaveFilePicker;
  if (!picker) return null;
  try {
    const handle = await picker({ suggestedName: filename });
    return {
      async write(response: Response) {
        if (!response.body) throw new Error("empty response");
        await response.body.pipeTo(await handle.createWritable());
      },
    };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return undefined;
    throw error;
  }
}

/** Saves a streamed response: into the picked file, or (none picked) as a Blob download. */
export async function saveResponse(filename: string, response: Response, target: SaveTarget): Promise<void> {
  if (target) await target.write(response);
  else saveBlob(filename, await response.blob());
}
