import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { StageBadge } from "@/components/StageBadge";
import { FilterShell, ActiveFilterChips, type ActiveFilterChip } from "@/components/FilterBar";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { getReturns, retryCn } from "@/lib/api";
import { formatDateOnly, formatDate, truncate } from "@/lib/utils";
import type { ReturnRow } from "@/lib/types";
import { RotateCcw, RefreshCcw, Loader2, Undo2 } from "lucide-react";

const STATUSES = ["fetched", "ready", "posting", "posted", "failed", "dead"];

export function ReturnsPage({ onStatsRefresh }: { onStatsRefresh: () => void }) {
  const [items, setItems] = useState<ReturnRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [retrying, setRetrying] = useState(false);

  function load(isManualRefresh = false) {
    if (isManualRefresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    getReturns(status)
      .then((data) => setItems(data.items || []))
      .catch((e) => setError(e.message))
      .finally(() => {
        setLoading(false);
        setRefreshing(false);
      });
  }

  useEffect(load, [status]);

  const chips: ActiveFilterChip[] = useMemo(() => {
    if (!status) return [];
    return [{ key: "status", label: `Status: ${status}`, onRemove: () => setStatus("") }];
  }, [status]);

  async function handleRetry() {
    setRetrying(true);
    try {
      await retryCn();
      toast.success("Retry started", { description: "Check Sync history (mode: retry_cn) for progress." });
      onStatsRefresh();
      load();
    } catch (e: any) {
      toast.error("Retry failed", { description: e.message });
    } finally {
      setRetrying(false);
    }
  }

  return (
    <div className="space-y-3">
      <FilterShell>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Status</label>
          <Select value={status || "__all"} onValueChange={(v) => setStatus(v === "__all" ? "" : v)}>
            <SelectTrigger className="w-44">
              <SelectValue placeholder="All statuses" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">All statuses</SelectItem>
              {STATUSES.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => setStatus("")} disabled={!status}>
            Clear filters
          </Button>
          <Button size="sm" onClick={() => load(true)} disabled={refreshing}>
            {refreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCcw className="h-3.5 w-3.5" />}
            Refresh
          </Button>
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="secondary" size="sm">
                <RotateCcw className="h-3.5 w-3.5" />
                Retry failed credit notes
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Retry failed credit notes?</AlertDialogTitle>
                <AlertDialogDescription>
                  Retries all failed credit notes (up to 50). This posts real SAP credit notes for any returns it finds.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={handleRetry} disabled={retrying}>
                  {retrying && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                  {retrying ? "Retrying…" : "Retry"}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </FilterShell>

      <ActiveFilterChips chips={chips} onClearAll={() => setStatus("")} />

      {error && <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>}

      <div className="rounded-lg border bg-card">
        <div className="max-h-[calc(100vh-300px)] overflow-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">#</TableHead>
                <TableHead>Return ID</TableHead>
                <TableHead>Order #</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Return date</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>SAP CN DocEntry</TableHead>
                <TableHead>Last error</TableHead>
                <TableHead>Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading &&
                Array.from({ length: 6 }).map((_, i) => (
                  <TableRow key={i}>
                    <TableCell colSpan={9}>
                      <Skeleton className="h-5 w-full" />
                    </TableCell>
                  </TableRow>
                ))}
              {!loading && items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={9} className="py-12">
                    <div className="flex flex-col items-center gap-2 text-center">
                      <Undo2 className="h-8 w-8 text-muted-foreground/50" />
                      <p className="text-sm text-muted-foreground">No returns/credit-note activity yet.</p>
                    </div>
                  </TableCell>
                </TableRow>
              )}
              {!loading &&
                items.map((r, i) => (
                  <TableRow key={r.uc_return_id}>
                    <TableCell className="text-muted-foreground">{i + 1}</TableCell>
                    <TableCell className="font-mono-tabular">{r.uc_return_id}</TableCell>
                    <TableCell className="font-mono-tabular">{r.uc_order_id}</TableCell>
                    <TableCell>{r.return_type || "—"}</TableCell>
                    <TableCell>{formatDateOnly(r.return_date)}</TableCell>
                    <TableCell>
                      <StageBadge stage={r.status} />
                    </TableCell>
                    <TableCell className="font-mono-tabular">{r.sap_cn_docentry ?? "—"}</TableCell>
                    <TableCell>{truncate(r.last_error, 70) || "—"}</TableCell>
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
