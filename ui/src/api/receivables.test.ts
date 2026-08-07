import { describe, expect, it, vi } from "vitest";

import {
  buildReceivableWorkplaceActionPayload,
  receivablesErrorMessage,
  withReceivablesAuthRetry,
  type ReceivableWorkplaceEditState,
  type ReceivableWorkplaceItem,
} from "./receivables";

function item(overrides: Partial<ReceivableWorkplaceItem> = {}): ReceivableWorkplaceItem {
  return {
    snapshot_date: "2026-07-10",
    stable_key: "receivable:cp-1",
    counterparty_ref: "cp-1",
    counterparty_name: "Клиент",
    phone_status: "present",
    current_balance: "1000.00",
    overdue_amount: "1000.00",
    invoice_count: 1,
    overdue_invoice_count: 1,
    status: "new_debt",
    payment_postponed: false,
    payment_postponed_count: 0,
    needs_call_today: true,
    no_phone_marker: false,
    needs_credit_depth_default: false,
    criticality: "low",
    documents: [],
    staff_options: [
      {
        staff_ref: "staff-1",
        staff_name: "Менеджер 1",
      },
    ],
    ...overrides,
  };
}

function edit(overrides: Partial<ReceivableWorkplaceEditState> = {}): ReceivableWorkplaceEditState {
  return {
    status: "new_debt",
    contacted_staff_ref: "",
    promised_payment_date: "",
    last_contact_at: "",
    next_action_date: "",
    payment_postponed: false,
    comment: "",
    ...overrides,
  };
}

describe("buildReceivableWorkplaceActionPayload", () => {
  it("sends only a changed comment and preserves the action id", () => {
    expect(
      buildReceivableWorkplaceActionPayload(
        item(),
        edit({ comment: "Новый комментарий" }),
        "action-1"
      )
    ).toEqual({ action_id: "action-1", comment: "Новый комментарий" });
  });

  it("does not resend the initial system status or employee", () => {
    const current = item({ contacted_staff_ref: "staff-1", contacted_staff_name: "Менеджер 1" });
    expect(
      buildReceivableWorkplaceActionPayload(
        current,
        edit({ contacted_staff_ref: "staff-1", next_action_date: "2026-07-11" }),
        "action-2"
      )
    ).toEqual({ action_id: "action-2", next_action_date: "2026-07-11" });
  });
});

describe("withReceivablesAuthRetry", () => {
  it("refreshes once and repeats the same request after 401", async () => {
    const payload = { action_id: "same-action", comment: "Сохранить" };
    const request = vi
      .fn<() => Promise<typeof payload>>()
      .mockRejectedValueOnce({ response: { status: 401 } })
      .mockResolvedValue(payload);
    const refreshSession = vi.fn().mockResolvedValue(undefined);

    await expect(
      withReceivablesAuthRetry(request, {
        refreshSession,
        isBitrixRoute: () => true,
      })
    ).resolves.toBe(payload);
    expect(request).toHaveBeenCalledTimes(2);
    expect(refreshSession).toHaveBeenCalledTimes(1);
  });

  it("does not loop after a second 401", async () => {
    const request = vi.fn().mockRejectedValue({ response: { status: 401 } });
    const refreshSession = vi.fn().mockResolvedValue(undefined);

    await expect(
      withReceivablesAuthRetry(request, {
        refreshSession,
        isBitrixRoute: () => true,
      })
    ).rejects.toEqual({ response: { status: 401 } });
    expect(request).toHaveBeenCalledTimes(2);
    expect(refreshSession).toHaveBeenCalledTimes(1);
  });
});

describe("receivablesErrorMessage", () => {
  it("shows the server detail for 422", () => {
    expect(
      receivablesErrorMessage(
        { response: { status: 422, data: { detail: "Unsupported status" } } },
        "Request failed"
      )
    ).toBe("Unsupported status");
  });

  it("explains a failed session refresh without discarding screen values", () => {
    const message = receivablesErrorMessage(
      {
        response: {
          status: 401,
          data: { detail: "Bitrix access token was rejected" },
        },
      },
      "Request failed"
    );

    expect(message).toContain("Введённые данные остались на экране");
    expect(message).not.toContain("Bitrix access token was rejected");
  });
});
