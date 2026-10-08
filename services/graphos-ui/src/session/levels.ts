import type { Level } from "../api/types";

const LEVELS: Level[] = ["read", "write", "maintain", "owner"];

/** `true` when `level` is at least `needed` (read < write < maintain < owner). */
export function atLeast(level: Level | null | undefined, needed: Level): boolean {
  return level != null && LEVELS.indexOf(level) >= LEVELS.indexOf(needed);
}
