import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getProcurementProductId,
  isBitrixProductInsightsPlacement,
  refreshBitrixAuth,
  refreshBitrixReceivablesSession,
  resolveBitrixProductUrl,
  type BitrixReceivablesSessionResponse,
} from "./bitrix";

const originalBX24 = window.BX24;
const originalLaunch = window.__MM_BITRIX_LAUNCH__;

afterEach(() => {
  window.BX24 = originalBX24;
  window.__MM_BITRIX_LAUNCH__ = originalLaunch;
});

describe("native Bitrix product card helpers", () => {
  it("recognizes product placement and resolves its native catalog URL", () => {
    window.__MM_BITRIX_LAUNCH__ = {
      domain: "crm.example.test",
      placement: "CRM_PRODUCT_DETAIL_TAB",
      placement_options: { PRODUCT_ID: 1646 },
    };

    expect(isBitrixProductInsightsPlacement()).toBe(true);
    expect(getProcurementProductId()).toBe("1646");
    expect(resolveBitrixProductUrl("1646")).toBe(
      "https://crm.example.test/crm/catalog/17/product/1646/"
    );
  });

  it("rejects a non-numeric product identifier", () => {
    window.__MM_BITRIX_LAUNCH__ = { domain: "crm.example.test" };
    expect(resolveBitrixProductUrl("../1646")).toBe("");
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
