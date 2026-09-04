import { api } from "./client";

export type OrderClosureReasonCode = "execution" | "cancellation";

export interface OrderClosureItem {
  id: number;
  position: number;
  input_number: string;
  input_period: string | null;
  onec_order_ref: string | null;
  onec_order_number: string | null;
  onec_order_date: string | null;
  site_order_number: string | null;
  department_name: string | null;
  status: string;
  eligible: boolean;
  blocker_code: string | null;
  blocker_text: string | null;
  facts: Record<string, unknown>;
  state_hash: string | null;
  reason_code: OrderClosureReasonCode | null;
  reason_ref: string | null;
  reason_name: string | null;
  result_document_ref: string | null;
  result_document_number: string | null;
}

export interface OrderClosureBatch {
  id: string;
  status: string;
  source_type: string;
  actor_id: string;
  actor_name: string | null;
  confirmed_by: string | null;
  diagnosis_hash: string | null;
  command_kind: string | null;
  attempt_count: number;
  last_error_code: string | null;
  last_polled_at: string | null;
  lease_until: string | null;
  applied_at: string | null;
  created_at: string;
  updated_at: string;
  items: OrderClosureItem[];
}

export interface OrderClosureReason {
  code: OrderClosureReasonCode;
  name: "Исполнение заказа" | "Отмена заказа";
  ref: string | null;
}

export async function createExcelClosureBatch(pastedText: string) {
  const { data } = await api.post<OrderClosureBatch>("/order-closures/batches", {
    source_type: "excel",
    pasted_text: pastedText,
  });
  return data;
}

export async function createFilterClosureBatch(filters: {
  year: number;
  department_ref?: string;
  category: "all" | "web" | "onec";
  state: "all" | "eligible" | "blocked" | "closed";
}) {
  const { data } = await api.post<OrderClosureBatch>("/order-closures/batches", {
    source_type: "filter",
    filters,
  });
  return data;
}

export async function readClosureBatch(batchId: string) {
  const { data } = await api.get<OrderClosureBatch>(`/order-closures/batches/${batchId}`);
  return data;
}

export async function repeatClosureDiagnosis(batchId: string) {
  const { data } = await api.post<OrderClosureBatch>(
    `/order-closures/batches/${batchId}/diagnose`
  );
  return data;
}

export async function readClosureReasons(batchId: string) {
  const { data } = await api.get<OrderClosureReason[]>("/order-closures/reasons", {
    params: { batch_id: batchId },
  });
  return data;
}

export async function confirmClosureBatch(
  batch: OrderClosureBatch,
  assignments: Array<{
    item_id: number;
    reason_code: OrderClosureReasonCode;
    reason_ref: string;
    reason_name: string;
  }>
) {
  const { data } = await api.post<OrderClosureBatch>(
    `/order-closures/batches/${batch.id}/confirm`,
    { diagnosis_hash: batch.diagnosis_hash, assignments }
  );
  return data;
}
