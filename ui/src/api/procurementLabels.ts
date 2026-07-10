import { api } from "./client";

export interface ProcurementLabelRow {
  line_no: number;
  onec_item_code: string;
  item_name: string;
  article_1c: string;
  sku: string;
  barcode: string;
  barcode_source: string;
  unit: string;
  quantity: string;
  price?: string | null;
  amount?: string | null;
  certificate_id: string;
  certificate_item_id: string;
  certificate_number: string;
  certificate_valid_to: string;
  certificate_file_id: string;
  certificate_status: string;
  eac_allowed: boolean;
  product_passport_item_id: string;
  trade_name: string;
  tnved: string;
  manufacturer: string;
  product_series: string;
  label_warnings: string[];
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

export interface ProcurementCertificationDocsGenerateResponse extends ProcurementLabelGenerateResponse {
  gtin_rows: number;
  missing_rows: number;
  document_checklist: string[];
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

export async function generateProcurementCertificationDocs(itemId: string) {
  const { data } = await api.post<ProcurementCertificationDocsGenerateResponse>(
    `/procurement-labels/orders/${encodeURIComponent(itemId)}/certification-docs/generate`,
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

export async function sendProcurementLabelsToFactory(itemId: string) {
  const { data } = await api.post<{
    item_id: string;
    status: string;
    artifact_version?: number | null;
    zip_url?: string | null;
  }>(`/procurement-labels/orders/${encodeURIComponent(itemId)}/send-to-factory`, {});
  return data;
}
