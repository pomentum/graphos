import type { TokenSource } from "../api/http";
import { ApiError, describeError } from "../api/http";
import type { TokenResponse } from "../api/types";

/** A token is renewed before use once this share of its life has passed. */
const RENEW_AFTER = 0.8;

/**
 * A signed-in person's tokens. The access token lives in page memory only.
 * The session that renews it is graphos's `graphos_session` cookie (HttpOnly:
 * no script on the page can read it), so a reload or a new tab carries on
 * without signing in again, until the session ends (an hour unused, twelve
 * hours at most) or the person signs out. The access token is renewed before
 * it expires, and once more when graphos answers 401; graphos rotates the
 * cookie each time. A refresh token held in memory (from elsewhere than the
 * UI's sign-in) is used instead when there is one. When the token cannot be
 * renewed (the session ended, was revoked, or the password changed
 * elsewhere) `onEnded` is called once.
 */
export class Credentials implements TokenSource {
  private access = "";
  private refresh: string | null = null;
  /** A session renews the access token: the refresh token, or the cookie. */
  private renewable = false;
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
    // A renewal inside the grace for another tab carries no new refresh token: keep the one held.
    this.refresh = tokens.refresh_token ?? (tokens.session ? this.refresh : null);
    this.renewable = Boolean(tokens.refresh_token) || Boolean(tokens.session);
    this.renewAt = this.now() + tokens.expires_in * 1000 * RENEW_AFTER;
  }

  async token(): Promise<string> {
    if (this.now() >= this.renewAt && this.renewable && !this.ended) await this.renew();
    return this.access;
  }

  /** Renews the access token; concurrent callers share one renewal. */
  renew(): Promise<boolean> {
    if (this.ended || !this.renewable) {
      this.end();
      return Promise.resolve(false);
    }
    const body = this.refresh ? { refresh_token: this.refresh } : { session_cookie: true };
    this.pending ??= this.post("/v1/auth/refresh", body)
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
    const renewable = this.renewable;
    this.ended = true;
    this.access = "";
    this.refresh = null;
    this.renewable = false;
    if (!renewable) return null;
    // graphos clears the session cookie; the token in memory, if any, names the session.
    const answer = await this.post<{ end_session_url?: string | null }>("/v1/auth/logout", {
      ...(refresh ? { refresh_token: refresh } : { session_cookie: true }),
      post_logout_redirect_uri: postLogoutRedirectUri,
    }).catch(() => null);
    return answer?.end_session_url ?? null;
  }

  private end(): void {
    if (this.ended) return;
    this.ended = true;
    this.access = "";
    this.refresh = null;
    this.renewable = false;
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
