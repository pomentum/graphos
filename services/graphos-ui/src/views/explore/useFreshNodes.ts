import { useCallback, useState } from "react";

const NONE = new Set<string>();

/**
 * The nodes the last expand added, while the node that was expanded stays
 * selected: the canvas labels them so the eye finds what appeared. Selecting
 * anything else lets them fall back to ordinary nodes.
 */
export function useFreshNodes(selected: string | null): [Set<string>, (center: string, keys: string[]) => void] {
  const [fresh, setFresh] = useState<{ center: string; keys: Set<string> } | null>(null);
  const mark = useCallback((center: string, keys: string[]) => setFresh({ center, keys: new Set(keys) }), []);
  return [fresh && fresh.center === selected ? fresh.keys : NONE, mark];
}
