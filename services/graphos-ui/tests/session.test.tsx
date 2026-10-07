import { render, screen, waitFor } from "@testing-library/react";
import { SessionProvider, useSessionContext } from "../src/session/SessionProvider";
import { SessionStore } from "../src/session/sessionStore";

function memoryStore(): SessionStore {
  const data = new Map<string, string>();
  return new SessionStore({
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  } as Storage);
}

function Probe() {
  const { session, restoring, restoreFailed } = useSessionContext();
  return <p>{restoring ? "restoring" : session ? "signed in" : restoreFailed ? "unreachable" : "signed out"}</p>;
}

describe("restoring a session after a reload", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("keeps the token when graphos cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const store = memoryStore();
    store.save({ token: "t", tenant: "a" });
    render(
      <SessionProvider store={store}>
        <Probe />
      </SessionProvider>,
    );
    await waitFor(() => expect(screen.getByText("unreachable")).toBeInTheDocument());
    expect(store.load()).toEqual({ token: "t", tenant: "a" });
  });

  it("forgets a token graphos refuses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "bad token" }), { status: 401 })),
    );
    const store = memoryStore();
    store.save({ token: "t", tenant: "a" });
    render(
      <SessionProvider store={store}>
        <Probe />
      </SessionProvider>,
    );
    await waitFor(() => expect(screen.getByText("signed out")).toBeInTheDocument());
    expect(store.load()).toBeNull();
  });
});
