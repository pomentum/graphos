/** What survives a page reload within the tab: the token and the chosen tenant. */
export interface StoredSession {
  token: string;
  tenant: string | null;
}

const KEY = "graphos.session";

/**
 * Keeps the session in `sessionStorage`: it lasts for the tab and is gone
 * when the tab closes, unlike `localStorage`. Storage that is unavailable
 * (private mode, blocked site data) behaves as an empty store.
 */
export class SessionStore {
  constructor(private readonly storage: Storage | null = safeSessionStorage()) {}

  load(): StoredSession | null {
    try {
      const raw = this.storage?.getItem(KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as Partial<StoredSession>;
      return typeof parsed.token === "string" && parsed.token
        ? { token: parsed.token, tenant: typeof parsed.tenant === "string" ? parsed.tenant : null }
        : null;
    } catch {
      return null;
    }
  }

  save(session: StoredSession): void {
    try {
      this.storage?.setItem(KEY, JSON.stringify(session));
    } catch {
      /* not stored: the session lasts until reload */
    }
  }

  clear(): void {
    try {
      this.storage?.removeItem(KEY);
    } catch {
      /* nothing to clear */
    }
  }
}

function safeSessionStorage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}
