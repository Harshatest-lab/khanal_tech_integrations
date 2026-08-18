import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { StageBadge } from "@/components/StageBadge";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateOnly, formatMoney, truncate } from "@/lib/utils";
import { getOrderDetail } from "@/lib/api";
import type { OrderDetail } from "@/lib/types";

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border bg-muted/30 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm font-medium font-mono-tabular">{value}</div>
    </div>
  );
}

function ReconTable({ title, h, prefix }: { title: string; h: Record<string, any>; prefix: string }) {
  return (
    <div className="rounded-lg border">
      <div className="border-b bg-muted/30 px-4 py-2 text-sm font-semibold">{title}</div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead></TableHead>
            <TableHead>Expected</TableHead>
            <TableHead>Actual</TableHead>
            <TableHead>Diff</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell className="text-muted-foreground">Tax</TableCell>
            <TableCell className="font-mono-tabular">{formatMoney(h[`${prefix}_tax_expected`])}</TableCell>
            <TableCell className="font-mono-tabular">{formatMoney(h[`${prefix}_tax_actual`])}</TableCell>
            <TableCell className="font-mono-tabular">{formatMoney(h[`${prefix}_tax_diff`])}</TableCell>
          </TableRow>
          <TableRow>
            <TableCell className="text-muted-foreground">Unit price</TableCell>
            <TableCell className="font-mono-tabular">{formatMoney(h[`${prefix}_unit_price_expected`])}</TableCell>
            <TableCell className="font-mono-tabular">{formatMoney(h[`${prefix}_unit_price_actual`])}</TableCell>
            <TableCell className="font-mono-tabular">{formatMoney(h[`${prefix}_unit_price_diff`])}</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </div>
  );
}

export function OrderDetailSheet({ uniwareId, onClose }: { uniwareId: string | null; onClose: () => void }) {
  const [detail, setDetail] = useState<OrderDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);

  useEffect(() => {
    if (!uniwareId) return;
    setDetail(null);
    setError(null);
    getOrderDetail(uniwareId)
      .then(setDetail)
      .catch((e) => setError(e.message));
  }, [uniwareId]);

  const h = detail?.header || {};
  const lines = detail?.lines || [];
  const creditNotes = detail?.credit_notes || [];

  return (
    <Dialog open={!!uniwareId} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Order {uniwareId}</DialogTitle>
        </DialogHeader>
        <div className="flex-1 space-y-5 overflow-y-auto p-6">
          {error && <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>}
          {!detail && !error && (
            <div className="grid grid-cols-3 gap-2">
              {Array.from({ length: 12 }).map((_, i) => (
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          )}
          {detail && (
            <>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                <Field label="Stage" value={<StageBadge stage={h.pipeline_status} />} />
                <Field label="Status" value={h.status || "—"} />
                <Field label="Channel" value={h.channel_name || "—"} />
                <Field label="Order date" value={formatDateOnly(h.order_date)} />
                <Field label="City / State" value={`${h.city || "—"}, ${h.state || "—"}`} />
                <Field label="Total" value={formatMoney(h.uc_total)} />
                <Field label="SAP Invoice DocEntry" value={h.sap_ar_invoice_docentry ?? "—"} />
                <Field label="SAP Invoice DocNum" value={h.sap_ar_invoice_docnum ?? "—"} />
                <Field label="SAP CN DocEntry" value={h.sap_ar_creditnote_docentry ?? "—"} />
                <Field label="Attempts" value={h.attempt_count ?? 0} />
                <Field label="Shipping provider" value={h.shippingprovider || "—"} />
                <Field label="Tracking #" value={h.trackingnumber || "—"} />
                <Field label="Tracking status" value={h.trackingstatus || "—"} />
                <Field label="Shipment status" value={h.shipment_status || "—"} />
                <Field label="Shipment date" value={formatDateOnly(h.shipment_date)} />
                <Field label="COD" value={h.cod || "—"} />
              </div>

              {h.last_error && (
                <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  {h.last_error}
                </p>
              )}
              {h.ar_invoice_mismatch_reason && (
                <p className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
                  Recon mismatch: {h.ar_invoice_mismatch_reason}
                </p>
              )}

              <ReconTable title="Reconciliation — Invoice" h={h} prefix="ar_invoice" />
              <ReconTable title="Reconciliation — Credit note" h={h} prefix="ar_creditnote" />

              <div className="rounded-lg border">
                <div className="border-b bg-muted/30 px-4 py-2 text-sm font-semibold">Lines</div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>SKU</TableHead>
                      <TableHead>Qty</TableHead>
                      <TableHead>Unit price</TableHead>
                      <TableHead>Total</TableHead>
                      <TableHead>Batch</TableHead>
                      <TableHead>Status</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {lines.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={6} className="text-center text-muted-foreground">
                          No lines.
                        </TableCell>
                      </TableRow>
                    )}
                    {lines.map((l, i) => (
                      <TableRow key={i}>
                        <TableCell className="font-mono-tabular">{l.sellerskucode || l.itemsku}</TableCell>
                        <TableCell>{l.quantity}</TableCell>
                        <TableCell className="font-mono-tabular">{formatMoney(l.selling_price)}</TableCell>
                        <TableCell className="font-mono-tabular">{formatMoney(l.total_price)}</TableCell>
                        <TableCell className="font-mono-tabular">{l.batchcode || "—"}</TableCell>
                        <TableCell>{l.statuscode || "—"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              <div className="rounded-lg border">
                <div className="border-b bg-muted/30 px-4 py-2 text-sm font-semibold">Credit notes</div>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Return ID</TableHead>
                      <TableHead>Type</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>SAP DocEntry</TableHead>
                      <TableHead>Error</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {creditNotes.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={5} className="text-center text-muted-foreground">
                          No returns for this order.
                        </TableCell>
                      </TableRow>
                    )}
                    {creditNotes.map((r, i) => (
                      <TableRow key={i}>
                        <TableCell className="font-mono-tabular">{r.uc_return_id}</TableCell>
                        <TableCell>{r.return_type || "—"}</TableCell>
                        <TableCell>
                          <StageBadge stage={r.status} />
                        </TableCell>
                        <TableCell className="font-mono-tabular">{r.sap_cn_docentry ?? "—"}</TableCell>
                        <TableCell>{truncate(r.last_error, 60) || "—"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              <div className="rounded-lg border">
                <button
                  onClick={() => setShowRaw((v) => !v)}
                  className="w-full border-b bg-muted/30 px-4 py-2 text-left text-sm font-semibold"
                >
                  {showRaw ? "▾" : "▸"} All fields (raw)
                </button>
                {showRaw && (
                  <pre className="max-h-96 overflow-auto p-4 text-xs">{JSON.stringify(detail, null, 2)}</pre>
                )}
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
