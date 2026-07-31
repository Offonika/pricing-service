import { describe, expect, it } from "vitest";
import { hasRequiredDecisionReason } from "./CandidatePanel";

describe("matching decision reason", () => {
  it("requires a structured reason for reject and revoke", () => {
    expect(hasRequiredDecisionReason("reject", "")).toBe(false);
    expect(hasRequiredDecisionReason("revoke", "")).toBe(false);
    expect(hasRequiredDecisionReason("reject", "wrong_model")).toBe(true);
    expect(hasRequiredDecisionReason("revoke", "auto_false_positive")).toBe(true);
  });
});
