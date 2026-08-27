import { describe, expect, it, vi } from "vitest";

import { withLogisticsAuthRetry } from "./logistics";

describe("withLogisticsAuthRetry", () => {
  it("refreshes once and repeats the request after 401", async () => {
    const request = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce({ response: { status: 401 } })
      .mockResolvedValue("ok");
    const refreshSession = vi.fn().mockResolvedValue(undefined);

    await expect(
      withLogisticsAuthRetry(request, {
        refreshSession,
        isBitrixRoute: () => true,
      })
    ).resolves.toBe("ok");
    expect(request).toHaveBeenCalledTimes(2);
    expect(refreshSession).toHaveBeenCalledTimes(1);
  });

  it("does not loop after a second 401", async () => {
    const error = { response: { status: 401 } };
    const request = vi.fn().mockRejectedValue(error);
    const refreshSession = vi.fn().mockResolvedValue(undefined);

    await expect(
      withLogisticsAuthRetry(request, {
        refreshSession,
        isBitrixRoute: () => true,
      })
    ).rejects.toBe(error);
    expect(request).toHaveBeenCalledTimes(2);
    expect(refreshSession).toHaveBeenCalledTimes(1);
  });
});
