import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";
import {
  initializeBitrixLogisticsSession,
  refreshBitrixAuth,
  refreshBitrixLogisticsSession,
  refreshBitrixReceivablesSession,
  type BitrixLogisticsSessionResponse,
  type BitrixReceivablesSessionResponse,
} from "./bitrix";

const originalBX24 = window.BX24;
const originalLaunch = window.__MM_BITRIX_LAUNCH__;

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  window.BX24 = originalBX24;
  window.__MM_BITRIX_LAUNCH__ = originalLaunch;
  window.sessionStorage.removeItem("mm_logistics_bitrix_session");
  window.sessionStorage.removeItem("mm_logistics_bitrix_left_menu_bound");
  document
    .querySelectorAll('script[src="https://api.bitrix24.com/api/v1/"]')
    .forEach((script) => script.remove());
});

describe("initializeBitrixLogisticsSession", () => {
  it("removes a failed SDK script so the next attempt starts a new load", async () => {
    vi.spyOn(api, "get").mockRejectedValue({ response: { status: 401 } });
    window.BX24 = undefined;
    window.sessionStorage.removeItem("mm_logistics_bitrix_session");

    const firstAttempt = initializeBitrixLogisticsSession();
    await vi.waitFor(() =>
      expect(
        document.querySelector<HTMLScriptElement>(
          'script[src="https://api.bitrix24.com/api/v1/"]'
        )
      ).not.toBeNull()
    );
    const firstScript = document.querySelector<HTMLScriptElement>(
      'script[src="https://api.bitrix24.com/api/v1/"]'
    );
    expect(firstScript).not.toBeNull();
    firstScript?.dispatchEvent(new Event("error"));
    await expect(firstAttempt).rejects.toThrow("Не удалось загрузить Bitrix24 SDK");
    expect(document.body.contains(firstScript)).toBe(false);

    const secondAttempt = initializeBitrixLogisticsSession();
    await vi.waitFor(() =>
      expect(
        document.querySelector<HTMLScriptElement>(
          'script[src="https://api.bitrix24.com/api/v1/"]'
        )
      ).not.toBeNull()
    );
    const secondScript = document.querySelector<HTMLScriptElement>(
      'script[src="https://api.bitrix24.com/api/v1/"]'
    );
    expect(secondScript).not.toBeNull();
    expect(secondScript).not.toBe(firstScript);
    secondScript?.dispatchEvent(new Event("error"));
    await expect(secondAttempt).rejects.toThrow("Не удалось загрузить Bitrix24 SDK");
  });

  it("restores the BFF session after WebView reload without loading the SDK", async () => {
    const resumedSession = {
      session_token: "resumed-token",
      token_type: "bearer",
      expires_at: new Date(Date.now() + 3_600_000).toISOString(),
      expires_in: 3_600,
      profile: {
        id: 1,
        full_name: "Получатель",
        role: "receiver",
        default_warehouse_id: 2,
        default_warehouse_name: "Тёплый Стан",
      },
    } satisfies BitrixLogisticsSessionResponse;
    const get = vi.spyOn(api, "get").mockResolvedValue({ data: resumedSession });
    const post = vi.spyOn(api, "post");
    window.BX24 = undefined;
    window.sessionStorage.removeItem("mm_logistics_bitrix_session");

    await expect(initializeBitrixLogisticsSession()).resolves.toEqual(resumedSession);

    expect(get).toHaveBeenCalledWith("/bitrix/logistics/session/resume", { timeout: 4_000 });
    expect(post).not.toHaveBeenCalled();
    expect(document.querySelector(`script[src="https://api.bitrix24.com/api/v1/"]`)).toBeNull();
  });

  it("prefers a fresh Bitrix launch over a cookie from a previous account", async () => {
    const freshSession = {
      session_token: "fresh-token",
      token_type: "bearer",
      expires_at: new Date(Date.now() + 3_600_000).toISOString(),
      expires_in: 3_600,
      profile: {
        id: 2,
        full_name: "Новый пользователь",
        role: "sender",
        default_warehouse_id: 1,
        default_warehouse_name: "Центральный склад",
      },
    } satisfies BitrixLogisticsSessionResponse;
    window.__MM_BITRIX_LAUNCH__ = {
      access_token: "fresh-oauth-token",
      domain: "portal.example",
      member_id: "member-1",
      placement: "LEFT_MENU",
      placement_options: {},
    };
    window.sessionStorage.setItem("mm_logistics_bitrix_left_menu_bound", "1");
    const get = vi.spyOn(api, "get");
    const post = vi.spyOn(api, "post").mockResolvedValue({ data: freshSession });

    await expect(initializeBitrixLogisticsSession()).resolves.toEqual(freshSession);

    expect(get).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith("/bitrix/logistics/session", {
      access_token: "fresh-oauth-token",
      domain: "portal.example",
      member_id: "member-1",
    });
  });

  it("fails visibly instead of waiting forever when BX24.init does not answer", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "get").mockRejectedValue({ response: { status: 401 } });
    window.BX24 = {
      init: vi.fn(),
      getAuth: vi.fn(() => false as const),
      callMethod: vi.fn(),
    };

    const attempt = initializeBitrixLogisticsSession();
    const rejection = expect(attempt).rejects.toThrow("Bitrix24 SDK не ответил за 10 секунд");
    await vi.runAllTimersAsync();
    await rejection;
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

describe("refreshBitrixLogisticsSession", () => {
  it("shares one refresh between parallel requests", async () => {
    let resolveRefresh: ((value: BitrixLogisticsSessionResponse) => void) | undefined;
    const requestSession = vi.fn(
      () =>
        new Promise<BitrixLogisticsSessionResponse>((resolve) => {
          resolveRefresh = resolve;
        })
    );

    const first = refreshBitrixLogisticsSession(requestSession);
    const second = refreshBitrixLogisticsSession(requestSession);
    expect(requestSession).toHaveBeenCalledTimes(1);

    resolveRefresh?.({ session_token: "token" } as BitrixLogisticsSessionResponse);
    await Promise.all([first, second]);
  });
});
