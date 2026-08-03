import type { MatchingDecisionReasonCode } from "../api/types";

export function hasRequiredDecisionReason(
  action: "reject" | "revoke",
  reasonCode: MatchingDecisionReasonCode | ""
) {
  return action === "reject" || action === "revoke" ? Boolean(reasonCode) : true;
}
