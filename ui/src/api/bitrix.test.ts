import { afterEach, describe, expect, it, vi } from "vitest";

import {
  initializeBitrixLogisticsSession,
  refreshBitrixAuth,
  refreshBitrixReceivablesSession,
  type BitrixReceivablesSessionResponse,
} from "./bitrix";

const originalBX24 = window.BX24;

afterEach(() => {
  window.BX24 = originalBX24;
  window.sessionStorage.removeItem("mm_logistics_bitrix_session");
  document
    .querySelectorAll('script[src="https://api.bitrix24.com/api/v1/"]')
    .forEach((script) => script.remove());
});

describe("initializeBitrixLogisticsSession", () => {
  it("removes a failed SDK script so the next attempt starts a new load", async () => {
    window.BX24 = undefined;
    window.sessionStorage.removeItem("mm_logistics_bitrix_session");

    const firstAttempt = initializeBitrixLogisticsSession();
    const firstScript = document.querySelector<HTMLScriptElement>(
      'script[src="https://api.bitrix24.com/api/v1/"]'
    );
    expect(firstScript).not.toBeNull();
    firstScript?.dispatchEvent(new Event("error"));
    await expect(firstAttempt).rejects.toThrow("Не удалось загрузить Bitrix24 SDK");
    expect(document.body.contains(firstScript)).toBe(false);

    const secondAttempt = initializeBitrixLogisticsSession();
    const secondScript = document.querySelector<HTMLScriptElement>(
      'script[src="https://api.bitrix24.com/api/v1/"]'
    );
    expect(secondScript).not.toBeNull();
    expect(secondScript).not.toBe(firstScript);
    secondScript?.dispatchEvent(new Event("error"));
    await expect(secondAttempt).rejects.toThrow("Не удалось загрузить Bitrix24 SDK");
  });
});

describe("refreshBitrixAuth", () => {
  it("asks the SDK to renew auth before reading the access token", async () => {
    const freshAuth = {
      access_token: "fresh-access-token",
      domain: "example.bitrix24.ru",
      member_id: "member-1",
    };
    const getAuth = vi.fn(() => freshAuth);
    const refreshAuth = vi.fn((callback: () => void) => callback());
    window.BX24 = {
      init: vi.fn(),
      getAuth,
      refreshAuth,
      callMethod: vi.fn(),
    };

    await expect(refreshBitrixAuth()).resolves.toEqual(freshAuth);
    expect(refreshAuth).toHaveBeenCalledTimes(1);
    expect(getAuth).toHaveBeenCalledTimes(1);
    expect(refreshAuth.mock.invocationCallOrder[0]).toBeLessThan(
      getAuth.mock.invocationCallOrder[0]
    );
  });

  it("fails clearly when the loaded SDK cannot renew auth", async () => {
    window.BX24 = {
      init: vi.fn(),
      getAuth: vi.fn(() => false as const),
      callMethod: vi.fn(),
    };

    await expect(refreshBitrixAuth()).rejects.toThrow(
      "Bitrix24 SDK не поддерживает обновление OAuth-сессии"
    );
  });
});

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
