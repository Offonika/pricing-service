import type { ExecutiveDashboardBlock } from "../api/executiveDashboard";

export function splitManagementBalanceBlock(blocks: ExecutiveDashboardBlock[]) {
  const managementBalance = blocks.find((block) => block.key === "creditors_payables") || null;
  return {
    metricBlocks: blocks.filter((block) => block.key !== "creditors_payables"),
    managementBalance,
  };
}
