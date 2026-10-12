/**
 * Saves text as a file through the browser's own download: a Blob behind a
 * temporary link, released at once. Nothing is kept in browser storage.
 */
export function saveFile(filename: string, text: string, type = "application/json"): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
