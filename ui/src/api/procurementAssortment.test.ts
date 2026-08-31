import { describe, expect, it } from "vitest";

import { filenameFromContentDisposition } from "./procurementAssortment";


describe("filenameFromContentDisposition", () => {
  it("предпочитает RFC 5987 имя с русским номером 1С", () => {
    const disposition = [
      'attachment; filename="supplier-order-14-labels-50x40.pdf"',
      "filename*=UTF-8''supplier-order-%D0%A0%D0%91%D0%93%D0%A30000543-labels-50x40.pdf",
    ].join("; ");

    expect(filenameFromContentDisposition(disposition, "fallback.pdf")).toBe(
      "supplier-order-РБГУ0000543-labels-50x40.pdf"
    );
  });

  it("использует ASCII fallback при некорректном filename*", () => {
    const disposition = [
      'attachment; filename="supplier-order-14-labels-50x40.pdf"',
      "filename*=UTF-8''%ZZ",
    ].join("; ");

    expect(filenameFromContentDisposition(disposition, "fallback.pdf")).toBe(
      "supplier-order-14-labels-50x40.pdf"
    );
  });
});
