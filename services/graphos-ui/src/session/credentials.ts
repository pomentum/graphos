import type { TokenSource } from "../api/http";
import { ApiError, describeError } from "../api/http";
import type { TokenResponse } from "../api/types";

/** A token is renewed before use once this share of its life has passed. */
const RENEW_AFTER = 0.8;

/**
 * A signed-in person's tokens, held in page memory only: nothing is written
 * to cookies or browser storage, so a reload or a closed tab ends the
 * session and the person signs in again. While the page is open the access
 * token is renewed with the refresh token (which graphos rotates on each
 * use) before it expires, and once more when graphos answers 401. When it
 * cannot be renewed (the session ended, was revoked, or the password
 * changed elsewhere) `onEnded` is called once.
 */
export class Credentials implements TokenSource {
  private access = "";
  private refresh: string | null = null;
  private renewAt = 0;
  private pending: Promise<boolean> | null = null;
  private ended = false;

  constructor(
    tokens: TokenResponse,
    private readonly baseUrl: string,
    private readonly onEnded: () => void,
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
    private readonly now: () => number = () => Date.now(),
  ) {
    this.apply(tokens);
  }

  /** Uses new tokens (after a renewal or a password change). */
  apply(tokens: TokenResponse): void {
    this.access = tokens.access_token;
    this.refresh = tokens.refresh_token;
    this.renewAt = this.now() + tokens.expires_in * 1000 * RENEW_AFTER;
  }

  async token(): Promise<string> {
    if (this.now() >= this.renewAt && this.refresh && !this.ended) await this.renew();
    return this.access;
  }

  /** Renews the access token; concurrent callers share one renewal. */
  renew(): Promise<boolean> {
    if (this.ended || !this.refresh) {
      this.end();
      return Promise.resolve(false);
    }
    this.pending ??= this.post("/v1/auth/refresh", { refresh_token: this.refresh })
      .then((tokens) => {
        this.apply(tokens);
        return true;
      })
      .catch(() => {
        this.end();
        return false;
      })
      .finally(() => {
        this.pending = null;
      });
    return this.pending;
  }

  /**
   * Ends the session on graphos too; the page forgets the tokens either way.
   * Returns the identity provider's sign-out address for a session that signed
   * in there (ending it there too), or null.
   */
  async signOut(postLogoutRedirectUri?: string): Promise<string | null> {
    const refresh = this.refresh;
    this.ended = true;
    this.access = "";
    this.refresh = null;
    if (!refresh) return null;
    const answer = await this.post<{ end_session_url?: string | null }>("/v1/auth/logout", {
      refresh_token: refresh,
      post_logout_redirect_uri: postLogoutRedirectUri,
    }).catch(() => null);
    return answer?.end_session_url ?? null;
  }

  private end(): void {
    if (this.ended) return;
    this.ended = true;
    this.access = "";
    this.refresh = null;
    this.onEnded();
  }

  private async post<T = TokenResponse>(path: string, body: unknown): Promise<T> {
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const text = await response.text();
    const data: unknown = text ? JSON.parse(text) : null;
    if (!response.ok) throw new ApiError(response.status, describeError(response.status, data));
    return data as T;
  }
}
