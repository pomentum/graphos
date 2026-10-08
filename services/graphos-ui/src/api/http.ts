import { t } from "../i18n";

/** Query string values; arrays repeat the key, `undefined` and `null` are left out. */
export type Query = Record<string, string | number | boolean | string[] | undefined | null>;

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  contentType?: string;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

/** A response body with the `ETag` header, for reads that precede a conditional write. */
export interface Tagged<T> {
  data: T;
  etag: string | null;
}

/** A non-2xx answer from graphos, with its status and the server's explanation. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly etag: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** The item changed since it was read (`412`): reload before writing. */
  get isConflict(): boolean {
    return this.status === 412;
  }
}

/** Turns graphos's `detail` (a string, or a list of validation errors) into one sentence. */
export function describeError(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (typeof d === "object" && d && "msg" in d ? String(d.msg) : String(d))).join("; ");
  }
  return t("errors.requestFailed", { status });
}

/**
 * Where the bearer token comes from: the signed-in session, which keeps it in
 * memory and renews it. `renew` is asked once when graphos answers 401 (the
 * token expired or was revoked); it returns whether a new token is ready.
 */
export interface TokenSource {
  token(): Promise<string>;
  renew(): Promise<boolean>;
}

/**
 * Sends requests to graphos with the bearer token. Knows HTTP only: paths,
 * query strings, JSON bodies, `ETag`s and errors; endpoints live in `GraphosApi`.
 * Without a token source (signing in) it sends no Authorization header.
 */
export class HttpClient {
  private readonly source: TokenSource | null;

  constructor(
    private readonly baseUrl: string,
    auth: TokenSource | string | null,
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {
    this.source =
      typeof auth === "string" ? { token: () => Promise.resolve(auth), renew: () => Promise.resolve(false) } : auth;
  }

  /** Sends a request and returns the decoded body with the response's `ETag`. */
  async request<T>(method: string, path: string, options: RequestOptions = {}): Promise<Tagged<T>> {
    const response = await this.send(method, path, options);
    const etag = response.headers.get("ETag");
    if (!response.ok) {
      throw new ApiError(response.status, describeError(response.status, await readJson(response)), etag);
    }
    return { data: (await readJson(response)) as T, etag };
  }

  /** `GET` returning only the body. */
  async get<T>(path: string, query?: Query, signal?: AbortSignal): Promise<T> {
    return (await this.request<T>("GET", path, { query, signal })).data;
  }

  /** `POST` with a JSON body, returning only the response body. */
  async post<T>(path: string, body: unknown, query?: Query): Promise<T> {
    return (await this.request<T>("POST", path, { body, query })).data;
  }

  /** Opens a request and returns the raw response, for streams the caller reads itself. */
  async open(path: string, options: RequestOptions = {}): Promise<Response> {
    const response = await this.send("GET", path, options);
    if (!response.ok) {
      throw new ApiError(response.status, describeError(response.status, await readJson(response)));
    }
    return response;
  }

  /** Sends the request; on 401, renews the token once and sends it again. */
  private async send(method: string, path: string, options: RequestOptions): Promise<Response> {
    const response = await this.sendOnce(method, path, options);
    if (response.status !== 401 || !this.source || !(await this.source.renew())) return response;
    return this.sendOnce(method, path, options);
  }

  private async sendOnce(method: string, path: string, options: RequestOptions): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
    if (this.source) headers.Authorization = `Bearer ${await this.source.token()}`;
    let body: string | undefined;
    if (options.body !== undefined) {
      headers["Content-Type"] = options.contentType ?? "application/json";
      body = JSON.stringify(options.body);
    }
    return this.fetchImpl(`${this.baseUrl}${path}${toQueryString(options.query)}`, {
      method,
      headers,
      body,
      signal: options.signal,
    });
  }
}

/** `?a=1&b=x&b=y` from a query object; empty when nothing is set. */
export function toQueryString(query: Query | undefined): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    for (const item of Array.isArray(value) ? value : [value]) params.append(key, String(item));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}
