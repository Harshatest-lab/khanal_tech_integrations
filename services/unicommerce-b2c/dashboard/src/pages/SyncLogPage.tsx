import { useEffect, useMemo, useState } from "react";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { FilterShell, ActiveFilterChips, type ActiveFilterChip } from "@/components/FilterBar";
import { getSyncLog } from "@/lib/api";
import { formatDate, truncate } from "@/lib/utils";
import type { SyncLogRow } from "@/lib/types";
import { Loader2, RefreshCcw, History } from "lucide-react";

const MODES = [
  { value: "fetch_orders", label: "Fetch orders" },
  { value: "fetch_returns", label: "Fetch returns" },
  { value: "post_invoices", label: "Post invoices (scheduled)" },
  { value: "post_credit_notes", label: "Post credit notes (scheduled)" },
  { value: "retry_invoices", label: "Retry invoices (manual)" },
  { value: "retry_cn", label: "Retry credit notes (manual)" },
];

export function SyncLogPage() {
  const [items, setItems] = useState<SyncLogRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState("");

  function load(isManualRefresh = false) {
    if (isManualRefresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    getSyncLog(mode)
      .then((data) => setItems(data.items || []))
      .catch((e) => setError(e.message))
      .finally(() => {
        setLoading(false);
        setRefreshing(false);
      });
  }

  useEffect(load, [mode]);

  const chips: ActiveFilterChip[] = useMemo(() => {
    if (!mode) return [];
    const label = MODES.find((m) => m.value === mode)?.label || mode;
    return [{ key: "mode", label: `Mode: ${label}`, onRemove: () => setMode("") }];
  }, [mode]);

  return (
    <div className="space-y-3">
      <FilterShell>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Mode</label>
          <Select value={mode || "__all"} onValueChange={(v) => setMode(v === "__all" ? "" : v)}>
            <SelectTrigger className="w-64">
              <SelectValue placeholder="All modes" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">All modes</SelectItem>
              {MODES.map((m) => (
                <SelectItem key={m.value} value={m.value}>
                  {m.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => setMode("")} disabled={!mode}>
            Clear filters
          </Button>
          <Button size="sm" onClick={() => load(true)} disabled={refreshing}>
            {refreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCcw className="h-3.5 w-3.5" />}
            Refresh
          </Button>
        </div>
      </FilterShell>

      <ActiveFilterChips chips={chips} onClearAll={() => setMode("")} />

      {error && <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>}

      <div className="rounded-lg border bg-card">
        <div className="max-h-[calc(100vh-300px)] overflow-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">#</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Finished</TableHead>
                <TableHead>Window</TableHead>
                <TableHead>Counts</TableHead>
                <TableHead>Error</TableHead>
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
                      <History className="h-8 w-8 text-muted-foreground/50" />
                      <p className="text-sm text-muted-foreground">No sync runs yet.</p>
                    </div>
                  </TableCell>
                </TableRow>
              )}
              {!loading &&
                items.map((r, i) => (
                  <TableRow key={i}>
                    <TableCell className="text-muted-foreground">{i + 1}</TableCell>
                    <TableCell>{r.mode}</TableCell>
                    <TableCell>
                      {r.status === "success" ? (
                        <Badge variant="success">success</Badge>
                      ) : (
                        <Badge variant="destructive">{r.status}</Badge>
                      )}
                    </TableCell>
                    <TableCell>{formatDate(r.started_at)}</TableCell>
                    <TableCell>{formatDate(r.finished_at)}</TableCell>
                    <TableCell className="font-mono-tabular">
                      {truncate(r.window_from, 20) || "—"} → {truncate(r.window_to, 20) || "—"}
                    </TableCell>
                    <TableCell className="font-mono-tabular">{truncate(JSON.stringify(r.counts || {}), 60)}</TableCell>
                    <TableCell>{truncate(r.error_message, 60) || "—"}</TableCell>
                  </TableRow>
                ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  );
}
