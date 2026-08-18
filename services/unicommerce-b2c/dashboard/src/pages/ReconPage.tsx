import { useEffect, useState } from "react";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { FilterShell } from "@/components/FilterBar";
import { getReconGaps } from "@/lib/api";
import { formatDate, formatMoney, truncate } from "@/lib/utils";
import { useDebouncedValue } from "@/hooks/useDebounce";
import type { ReconGapRow } from "@/lib/types";
import { Loader2, RefreshCcw, ScaleIcon } from "lucide-react";

export function ReconPage() {
  const [items, setItems] = useState<ReconGapRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [minDiffInput, setMinDiffInput] = useState("1");
  const minDiff = useDebouncedValue(minDiffInput, 400);

  function load(isManualRefresh = false) {
    if (isManualRefresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    getReconGaps(minDiff)
      .then((data) => setItems(data.items || []))
      .catch((e) => setError(e.message))
      .finally(() => {
        setLoading(false);
        setRefreshing(false);
      });
  }

  useEffect(load, [minDiff]);

  return (
    <div className="space-y-3">
      <FilterShell>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Min diff (₹)</label>
          <Input
            type="number"
            min={0}
            step={0.5}
            value={minDiffInput}
            onChange={(e) => setMinDiffInput(e.target.value)}
            className="w-32"
          />
        </div>
        <div className="ml-auto">
          <Button size="sm" onClick={() => load(true)} disabled={refreshing}>
            {refreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCcw className="h-3.5 w-3.5" />}
            Refresh
          </Button>
        </div>
      </FilterShell>

      {error && <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>}

      <div className="rounded-lg border bg-card">
        <div className="max-h-[calc(100vh-300px)] overflow-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">#</TableHead>
                <TableHead>Order #</TableHead>
                <TableHead>Channel</TableHead>
                <TableHead>SAP DocEntry</TableHead>
                <TableHead>Tax diff</TableHead>
                <TableHead>Unit price diff</TableHead>
                <TableHead>Reason</TableHead>
                <TableHead>Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading &&
                Array.from({ length: 6 }).map((_, i) => (
                  <TableRow key={i}>
                    <TableCell colSpan={8}>
                      <Skeleton className="h-5 w-full" />
                    </TableCell>
                  </TableRow>
                ))}
              {!loading && items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={8} className="py-12">
                    <div className="flex flex-col items-center gap-2 text-center">
                      <ScaleIcon className="h-8 w-8 text-muted-foreground/50" />
                      <p className="text-sm text-muted-foreground">No mismatches at or above this threshold.</p>
                    </div>
                  </TableCell>
                </TableRow>
              )}
              {!loading &&
                items.map((r, i) => (
                  <TableRow key={r.uniware_id}>
                    <TableCell className="text-muted-foreground">{i + 1}</TableCell>
                    <TableCell className="font-mono-tabular">{r.uniware_id}</TableCell>
                    <TableCell>{r.channel_name || "—"}</TableCell>
                    <TableCell className="font-mono-tabular">{r.sap_ar_invoice_docentry ?? "—"}</TableCell>
                    <TableCell className="font-mono-tabular">{formatMoney(r.ar_invoice_tax_diff)}</TableCell>
                    <TableCell className="font-mono-tabular">{formatMoney(r.ar_invoice_unit_price_diff)}</TableCell>
                    <TableCell>{truncate(r.ar_invoice_mismatch_reason, 90) || "—"}</TableCell>
                    <TableCell>{formatDate(r.updated_at)}</TableCell>
                  </TableRow>
                ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  );
}
