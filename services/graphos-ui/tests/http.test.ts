import { ApiError, describeError, HttpClient, toQueryString } from "../src/api/http";

function fakeFetch(status: number, body: unknown, headers: Record<string, string> = {}) {
  const calls: { url: string; init: RequestInit }[] = [];
  const impl = (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return new Response(body === undefined ? "" : JSON.stringify(body), { status, headers });
  }) as unknown as typeof fetch;
  return { impl, calls };
}

describe("toQueryString", () => {
  it("repeats array keys and leaves out empty values", () => {
    expect(
      toQueryString({ q: "a b", rel_types: ["X", "Y"], label: undefined, limit: 5, fuzzy: false, none: null }),
    ).toBe("?q=a+b&rel_types=X&rel_types=Y&limit=5&fuzzy=false");
    expect(toQueryString({})).toBe("");
    expect(toQueryString(undefined)).toBe("");
  });
});

describe("describeError", () => {
  it("uses the server's detail, string or validation list", () => {
    expect(describeError(404, { detail: "no such node" })).toBe("no such node");
    expect(describeError(422, { detail: [{ msg: "field required" }, { msg: "too long" }] })).toBe(
      "field required; too long",
    );
    expect(describeError(500, null)).toBe("Request failed (500)");
  });
});

describe("HttpClient", () => {
  it("renews the token once on 401 and sends the request again", async () => {
    let token = "old";
    const seen: string[] = [];
    const fetchImpl = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      const auth = new Headers(init?.headers).get("Authorization") ?? "";
      seen.push(auth);
      return new Response(auth === "Bearer new" ? "{}" : '{"detail":"expired"}', {
        status: auth === "Bearer new" ? 200 : 401,
      });
    }) as unknown as typeof fetch;
    const source = {
      token: () => Promise.resolve(token),
      renew: vi.fn(async () => {
        token = "new";
        return true;
      }),
    };
    await new HttpClient("/api", source, fetchImpl).get("/v1/me");
    expect(seen).toEqual(["Bearer old", "Bearer new"]);
    expect(source.renew).toHaveBeenCalledTimes(1);
  });

  it("keeps the code of a refusal: a schema managed by a program", async () => {
    const { impl } = fakeFetch(409, {
      detail: "The schema of 'acme' is managed by user:importer",
      code: "schema_managed",
      schema_manager: "user:importer",
    });
    const error = await new HttpClient("/api", "t", impl).post("/v1/tenants", {}).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "schema_managed", isSchemaManaged: true, isConflict: false });
    expect((error as ApiError).message).toBe("The schema of 'acme' is managed by user:importer");
    expect(new ApiError(409, "taken").isSchemaManaged).toBe(false);
  });

  it("sends no Authorization header without a token source (signing in)", async () => {
    const fetchImpl = vi.fn(async () => new Response("{}")) as unknown as typeof fetch;
    await new HttpClient("/api", null, fetchImpl).post("/v1/auth/login", {});
    const [, init] = (fetchImpl as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls[0]!;
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
  });

  it("sends the bearer token and returns the body with the ETag", async () => {
    const { impl, calls } = fakeFetch(200, { ok: true }, { ETag: '"12.3"' });
    const http = new HttpClient("/api", "secret", impl);
    const result = await http.request<{ ok: boolean }>("GET", "/v1/me", { query: { a: 1 } });
    expect(result).toEqual({ data: { ok: true }, etag: '"12.3"' });
    expect(calls[0]?.url).toBe("/api/v1/me?a=1");
    expect((calls[0]?.init.headers as Record<string, string>).Authorization).toBe("Bearer secret");
  });

  it("encodes JSON bodies with the given content type", async () => {
    const { impl, calls } = fakeFetch(200, {});
    await new HttpClient("", "t", impl).request("PATCH", "/x", {
      body: { a: null },
      contentType: "application/merge-patch+json",
    });
    expect(calls[0]?.init.body).toBe('{"a":null}');
    expect((calls[0]?.init.headers as Record<string, string>)["Content-Type"]).toBe("application/merge-patch+json");
  });

  it("turns a failed response into an ApiError; 412 is a conflict", async () => {
    const { impl } = fakeFetch(412, { detail: "changed" }, { ETag: '"1.4"' });
    const error = await new HttpClient("", "t", impl).get("/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 412, message: "changed", etag: '"1.4"', isConflict: true });
  });
});
