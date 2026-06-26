import { api } from "./client";

export interface ProcurementLabelRow {
  line_no: number;
  onec_item_code: string;
  item_name: string;
  sku: string;
  barcode: string;
  unit: string;
  quantity: string;
  price?: string | null;
  amount?: string | null;
  certificate_id: string;
  certificate_status: string;
  eac_allowed: boolean;
  status: string;
  blockers: string[];
}

export interface ProcurementLabelPreview {
  item_id: string;
  entity_type_id: number;
  onec_number: string;
  title: string;
  contour: string;
  status: string;
  ready: boolean;
  blocked: boolean;
  blockers: string[];
  rows: ProcurementLabelRow[];
  artifact_version?: number | null;
  zip_url?: string | null;
  disk_file_id?: string | null;
}

export interface ProcurementLabelGenerateResponse {
  preview: ProcurementLabelPreview;
  generated: boolean;
  artifact_version?: number | null;
  zip_filename?: string | null;
  zip_url?: string | null;
  disk_file_id?: string | null;
}

export async function fetchProcurementLabelPreview(itemId: string) {
  const { data } = await api.get<ProcurementLabelPreview>(
    `/procurement-labels/orders/${encodeURIComponent(itemId)}/preview`
  );
  return data;
}

export async function generateProcurementLabels(itemId: string) {
  const { data } = await api.post<ProcurementLabelGenerateResponse>(
    `/procurement-labels/orders/${encodeURIComponent(itemId)}/generate`,
    { dry_run: false }
  );
  return data;
}

export async function approveProcurementLabels(itemId: string) {
  const { data } = await api.post<{
    item_id: string;
    status: string;
    artifact_version?: number | null;
    zip_url?: string | null;
  }>(`/procurement-labels/orders/${encodeURIComponent(itemId)}/approve`, {});
  return data;
}
