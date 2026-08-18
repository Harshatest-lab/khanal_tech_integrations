import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { StageBadge } from "@/components/StageBadge";
import { FilterShell, ActiveFilterChips, DatePresets, type ActiveFilterChip } from "@/components/FilterBar";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { OrderDetailSheet } from "@/components/OrderDetailSheet";
import { getOrders, retryInvoices } from "@/lib/api";
import { useDebouncedValue } from "@/hooks/useDebounce";
import { formatDateOnly, formatDate, formatMoney } from "@/lib/utils";
import type { OrderRow } from "@/lib/types";
import { ArrowUp, ArrowDown, ChevronLeft, ChevronRight, RotateCcw, RefreshCcw, Loader2, PackageSearch } from "lucide-react";

const STAGES = ["fetched", "ready", "posting", "verify", "batch_hold", "posted", "failed", "dead"];
const SORTABLE: Record<string, string> = { order_date: "Order date", uc_total: "Total", updated_at: "Updated" };

export function OrdersPage({ onStatsRefresh }: { onStatsRefresh: () => void }) {
  const [items, setItems] = useState<OrderRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [orderFilterInput, setOrderFilterInput] = useState("");
  const orderFilter = useDebouncedValue(orderFilterInput, 350);
  const [stageFilter, setStageFilter] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [pageSize, setPageSize] = useState(100);
  const [offset, setOffset] = useState(0);
  const [sortBy, setSortBy] = useState("updated_at");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [selected, setSelected] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "/" && document.activeElement?.tagName !== "INPUT") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const load = useCallback(
    (resetOffset: boolean, isManualRefresh = false) => {
      const useOffset = resetOffset ? 0 : offset;
      if (resetOffset) setOffset(0);
      if (isManualRefresh) setRefreshing(true);
      else setLoading(true);
      setError(null);
      getOrders({
        limit: pageSize,
        offset: useOffset,
        sortBy,
        sortDir,
        uniwareId: orderFilter,
        pipelineStatus: stageFilter,
        orderDateFrom: dateFrom,
        orderDateTo: dateTo,
      })
        .then((page) => {
          setItems(page.items || []);
          setTotal(page.total || 0);
        })
        .catch((e) => setError(e.message))
        .finally(() => {
          setLoading(false);
          setRefreshing(false);
        });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pageSize, offset, sortBy, sortDir, orderFilter, stageFilter, dateFrom, dateTo]
  );

  useEffect(() => {
    load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageSize, sortBy, sortDir, stageFilter, dateFrom, dateTo, orderFilter]);

  useEffect(() => {
    load(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset]);

  function toggleSort(key: string) {
    if (sortBy === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortBy(key);
      setSortDir("desc");
    }
  }

  function clearFilters() {
    setOrderFilterInput("");
    setStageFilter("");
    setDateFrom("");
    setDateTo("");
  }

  const chips: ActiveFilterChip[] = useMemo(() => {
    const list: ActiveFilterChip[] = [];
    if (orderFilter) list.push({ key: "order", label: `Order: ${orderFilter}`, onRemove: () => setOrderFilterInput("") });
    if (stageFilter) list.push({ key: "stage", label: `Stage: ${stageFilter}`, onRemove: () => setStageFilter("") });
    if (dateFrom || dateTo) {
      list.push({
        key: "date",
        label: `Date: ${dateFrom || "…"} → ${dateTo || "…"}`,
        onRemove: () => {
          setDateFrom("");
          setDateTo("");
        },
      });
    }
    return list;
  }, [orderFilter, stageFilter, dateFrom, dateTo]);

  async function handleRetry() {
    setRetrying(true);
    try {
      await retryInvoices();
      toast.success("Retry started", { description: "Check Sync history (mode: retry_invoices) for progress." });
      onStatsRefresh();
      load(false);
    } catch (e: any) {
      toast.error("Retry failed", { description: e.message });
    } finally {
      setRetrying(false);
    }
  }

  const start = total === 0 ? 0 : offset + 1;
  const end = offset + items.length;
  const hasFilters = chips.length > 0;

  return (
    <div className="space-y-3">
      <FilterShell>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Order #</label>
          <SearchInput
            ref={searchRef}
            placeholder="Search uniware id…  (/)"
            value={orderFilterInput}
            onChange={(e) => setOrderFilterInput(e.target.value)}
            onClear={() => setOrderFilterInput("")}
            className="w-48"
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Stage</label>
          <Select value={stageFilter || "__all"} onValueChange={(v) => setStageFilter(v === "__all" ? "" : v)}>
            <SelectTrigger className="w-40">
              <SelectValue placeholder="All stages" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">All stages</SelectItem>
              {STAGES.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Order date from</label>
          <Input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} className="w-40" />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Order date to</label>
          <Input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} className="w-40" />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Quick range</label>
          <DatePresets onSelect={(from, to) => { setDateFrom(from); setDateTo(to); }} />
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={clearFilters} disabled={!hasFilters}>
            Clear filters
          </Button>
          <Button size="sm" onClick={() => load(true, true)} disabled={refreshing}>
            {refreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCcw className="h-3.5 w-3.5" />}
            Refresh
          </Button>
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="secondary" size="sm">
                <RotateCcw className="h-3.5 w-3.5" />
                Retry failed invoices
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Retry failed invoices?</AlertDialogTitle>
                <AlertDialogDescription>
                  Retries all ready/failed invoices (up to 50). This posts real SAP invoices for any orders it finds.
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

      <ActiveFilterChips chips={chips} onClearAll={clearFilters} />

      {error && <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>}

      <div className="rounded-lg border bg-card">
        <div className="max-h-[calc(100vh-360px)] overflow-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10">#</TableHead>
                <TableHead>Order #</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Stage</TableHead>
                {Object.entries(SORTABLE).map(([key, label]) => (
                  <TableHead key={key}>
                    <button className="flex items-center gap-1 hover:text-foreground" onClick={() => toggleSort(key)}>
                      {label}
                      {sortBy === key && (sortDir === "asc" ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />)}
                    </button>
                  </TableHead>
                ))}
                <TableHead>Channel</TableHead>
                <TableHead>City</TableHead>
                <TableHead>SAP DocEntry</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading &&
                Array.from({ length: 8 }).map((_, i) => (
                  <TableRow key={i}>
                    <TableCell colSpan={10}>
                      <Skeleton className="h-5 w-full" />
                    </TableCell>
                  </TableRow>
                ))}
              {!loading && items.length === 0 && (
                <TableRow>
                  <TableCell colSpan={10} className="py-12">
                    <div className="flex flex-col items-center gap-2 text-center">
                      <PackageSearch className="h-8 w-8 text-muted-foreground/50" />
                      <p className="text-sm text-muted-foreground">No orders found.</p>
                      {hasFilters && (
                        <Button variant="link" size="sm" onClick={clearFilters}>
                          Clear filters
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              )}
              {!loading &&
                items.map((row, i) => (
                  <TableRow key={row.uniware_id} className="cursor-pointer" onClick={() => setSelected(row.uniware_id)}>
                    <TableCell className="text-muted-foreground">{offset + i + 1}</TableCell>
                    <TableCell className="font-mono-tabular">{row.uniware_id}</TableCell>
                    <TableCell>{row.status || "—"}</TableCell>
                    <TableCell>
                      <StageBadge stage={row.pipeline_status} />
                    </TableCell>
                    <TableCell>{formatDateOnly(row.order_date)}</TableCell>
                    <TableCell className="font-mono-tabular">{formatMoney(row.uc_total)}</TableCell>
                    <TableCell>{formatDate(row.updated_at)}</TableCell>
                    <TableCell>{row.channel_name || "—"}</TableCell>
                    <TableCell>{row.city || "—"}</TableCell>
                    <TableCell className="font-mono-tabular">
                      {row.sap_ar_invoice_docentry != null ? row.sap_ar_invoice_docentry : "—"}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button variant="outline" size="sm" onClick={(e) => { e.stopPropagation(); setSelected(row.uniware_id); }}>
                        View
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
            </TableBody>
          </Table>
        </div>
      </div>

      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>
          Showing {start}–{end} of {total}
        </span>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-xs">
            Per page
            <Select value={String(pageSize)} onValueChange={(v) => setPageSize(Number(v))}>
              <SelectTrigger className="h-8 w-20">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="50">50</SelectItem>
                <SelectItem value="100">100</SelectItem>
                <SelectItem value="200">200</SelectItem>
              </SelectContent>
            </Select>
          </label>
          <Button variant="outline" size="sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>
            <ChevronLeft className="h-3.5 w-3.5" /> Previous
          </Button>
          <Button variant="outline" size="sm" disabled={end >= total} onClick={() => setOffset(offset + pageSize)}>
            Next <ChevronRight className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      <OrderDetailSheet uniwareId={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
