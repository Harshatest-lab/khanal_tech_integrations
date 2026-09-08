import { authHeaders, handleUnauthorized } from "./auth";
import type { OrdersPage, OrderDetail, ReturnRow, SyncLogRow, ReconGapRow, ReconSummary, Stats, PeriodLock } from "./types";

const API_BASE = "https://coqi1t0nvb.execute-api.ap-south-1.amazonaws.com";

export class UnauthorizedError extends Error {}

function isFileProtocol() {
  return window.location.protocol === "file:";
}

function fileProtocolHelp() {
  return (
    "Chrome blocks API calls when opened as a file (file://). Run `npm run dev` " +
    "in services/unicommerce-b2c/dashboard and open the printed localhost URL instead."
  );
}

async function apiGet<T>(path: string): Promise<T> {
  try {
    const res = await fetch(`${API_BASE}${path}`, { headers: authHeaders() });
    if (res.status === 401) {
      if (await handleUnauthorized()) return apiGet<T>(path);
      throw new UnauthorizedError("Unauthorized — sign in again");
    }
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  } catch (err) {
    if (isFileProtocol() && err instanceof TypeError) throw new Error(fileProtocolHelp());
    throw err;
  }
}

async function apiPost<T>(path: string): Promise<T> {
  try {
    const res = await fetch(`${API_BASE}${path}`, { method: "POST", headers: authHeaders() });
    if (res.status === 401) {
      if (await handleUnauthorized()) return apiPost<T>(path);
      throw new UnauthorizedError("Unauthorized — sign in again");
    }
    const body = await res.text();
    const data = body ? JSON.parse(body) : null;
    if (!res.ok) throw new Error(data?.error || `${res.status} ${body}`);
    return data;
  } catch (err) {
    if (isFileProtocol() && err instanceof TypeError) throw new Error(fileProtocolHelp());
    throw err;
  }
}

export function getStats() {
  return apiGet<Stats>("/v1/unicommerce/stats");
}

export interface OrdersQuery {
  limit: number;
  offset: number;
  sortBy: string;
  sortDir: "asc" | "desc";
  uniwareId?: string;
  pipelineStatus?: string;
  orderDateFrom?: string;
  orderDateTo?: string;
}

export function getOrders(q: OrdersQuery) {
  const params = new URLSearchParams();
  params.set("limit", String(q.limit));
  params.set("offset", String(q.offset));
  params.set("sort_by", q.sortBy);
  params.set("sort_dir", q.sortDir);
  const add = (key: string, value?: string) => {
    const trimmed = (value || "").trim();
    if (trimmed) params.set(key, trimmed);
  };
  add("uniware_id", q.uniwareId);
  add("pipeline_status", q.pipelineStatus);
  add("order_date_from", q.orderDateFrom);
  add("order_date_to", q.orderDateTo);
  return apiGet<OrdersPage>(`/v1/unicommerce/orders?${params.toString()}`);
}

export function getOrderDetail(uniwareId: string) {
  return apiGet<OrderDetail>(`/v1/unicommerce/order-detail?uniware_id=${encodeURIComponent(uniwareId)}`);
}

export function getReturns(status?: string) {
  const query = status ? `?status=${encodeURIComponent(status)}&limit=100` : "?limit=100";
  return apiGet<{ items: ReturnRow[] }>(`/v1/unicommerce/returns${query}`);
}

export function getSyncLog(mode?: string) {
  const query = mode ? `?mode=${encodeURIComponent(mode)}&limit=50` : "?limit=50";
  return apiGet<{ items: SyncLogRow[] }>(`/v1/unicommerce/sync-log${query}`);
}

export function getReconGaps(minDiff: string) {
  return apiGet<{ items: ReconGapRow[] }>(`/v1/unicommerce/recon-gaps?min_diff=${encodeURIComponent(minDiff)}&limit=100`);
}

export function getReconSummary(startDate: string, endDate: string) {
  const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
  return apiGet<ReconSummary>(`/v1/unicommerce/recon-summary?${params.toString()}`);
}

export function retryInvoices() {
  return apiPost<unknown>("/v1/unicommerce/retry-invoices?limit=50");
}

export function retryCn() {
  return apiPost<unknown>("/v1/unicommerce/retry-cn?limit=50");
}

export function getPeriodLocks() {
  return apiGet<{ items: PeriodLock[] }>("/v1/unicommerce/period-locks");
}

export function lockPeriod(period: string, note: string) {
  const params = new URLSearchParams({ period, note });
  return apiPost<PeriodLock>(`/v1/unicommerce/period-locks/lock?${params.toString()}`);
}

export function unlockPeriod(period: string) {
  const params = new URLSearchParams({ period });
  return apiPost<PeriodLock>(`/v1/unicommerce/period-locks/unlock?${params.toString()}`);
}
