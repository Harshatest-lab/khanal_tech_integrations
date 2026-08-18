export interface OrderRow {
  uniware_id: string;
  status?: string;
  pipeline_status?: string;
  order_date?: string;
  channel_name?: string;
  city?: string;
  uc_total?: number;
  sap_ar_invoice_docentry?: number | null;
  updated_at?: string;
}

export interface OrdersPage {
  items: OrderRow[];
  total: number;
}

export interface OrderDetail {
  header: Record<string, any>;
  lines: Array<Record<string, any>>;
  credit_notes: Array<Record<string, any>>;
}

export interface ReturnRow {
  uc_return_id: string;
  uc_order_id: string;
  return_type?: string;
  return_date?: string;
  status?: string;
  sap_cn_docentry?: number | null;
  last_error?: string;
  updated_at?: string;
}

export interface SyncLogRow {
  mode: string;
  status: string;
  started_at?: string;
  finished_at?: string;
  window_from?: string;
  window_to?: string;
  counts?: Record<string, number>;
  error_message?: string;
}

export interface ReconGapRow {
  uniware_id: string;
  channel_name?: string;
  sap_ar_invoice_docentry?: number | null;
  ar_invoice_tax_diff?: number;
  ar_invoice_unit_price_diff?: number;
  ar_invoice_mismatch_reason?: string;
  updated_at?: string;
}

export interface Stats {
  total_orders?: number;
  by_pipeline_status?: Record<string, number>;
  cn_by_status?: Record<string, number>;
}
