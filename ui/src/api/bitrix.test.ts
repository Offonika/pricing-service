import { describe, expect, it, vi } from "vitest";

import {
  refreshBitrixReceivablesSession,
  type BitrixReceivablesSessionResponse,
} from "./bitrix";

describe("refreshBitrixReceivablesSession", () => {
  it("shares one refresh between parallel requests", async () => {
    let resolveRefresh: ((value: BitrixReceivablesSessionResponse) => void) | undefined;
    const requestSession = vi.fn(
      () =>
        new Promise<BitrixReceivablesSessionResponse>((resolve) => {
          resolveRefresh = resolve;
        })
    );

    const first = refreshBitrixReceivablesSession(requestSession);
    const second = refreshBitrixReceivablesSession(requestSession);
    expect(requestSession).toHaveBeenCalledTimes(1);

    resolveRefresh?.({ session_token: "token" } as BitrixReceivablesSessionResponse);
    await Promise.all([first, second]);
  });
});
