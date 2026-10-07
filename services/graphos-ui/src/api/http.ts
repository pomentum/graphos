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
  return `Request failed (${status})`;
}

/**
 * Sends requests to graphos with the bearer token. Knows HTTP only: paths,
 * query strings, JSON bodies, `ETag`s and errors; endpoints live in `GraphosApi`.
 */
export class HttpClient {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

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

  private send(method: string, path: string, options: RequestOptions): Promise<Response> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      Authorization: `Bearer ${this.token}`,
      ...options.headers,
    };
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
