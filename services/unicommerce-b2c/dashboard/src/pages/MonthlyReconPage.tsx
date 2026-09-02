import { useEffect, useMemo, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from "recharts";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { FilterShell } from "@/components/FilterBar";
import { StatCard } from "@/components/StatCard";
import { getReconSummary } from "@/lib/api";
import { formatMoney } from "@/lib/utils";
import type { ReconSummary, ReconSummaryRow } from "@/lib/types";
import { Download, FileSpreadsheet, Loader2, RefreshCcw, ScaleIcon } from "lucide-react";

// Status is reserved for state (matched vs mismatched), not identity --
// reusing the app's own success/destructive tokens (same ones StatCard
// already uses for Posted/Failed) rather than a fresh categorical palette.
const MATCHED_COLOR = "hsl(var(--success))";
const MISMATCH_COLOR = "hsl(var(--destructive))";

function defaultMonth(): string {
  const now = new Date();
  const prevMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  return `${prevMonth.getFullYear()}-${String(prevMonth.getMonth() + 1).padStart(2, "0")}`;
}

function monthToRange(month: string): { start: string; end: string } {
  const [y, m] = month.split("-").map(Number);
  const start = `${y}-${String(m).padStart(2, "0")}-01`;
  const lastDay = new Date(y, m, 0).getDate();
  const end = `${y}-${String(m).padStart(2, "0")}-${String(lastDay).padStart(2, "0")}`;
  return { start, end };
}

function byChannelForChart(rows: ReconSummaryRow[], types: string[]) {
  const byChannel = new Map<string, { channel: string; matched: number; mismatched: number }>();
  for (const r of rows) {
    if (!types.includes(r.type)) continue;
    const entry = byChannel.get(r.channel) || { channel: r.channel, matched: 0, mismatched: 0 };
    if (r.status === "amount_mismatch") entry.mismatched += r.count;
    else entry.matched += r.count;
    byChannel.set(r.channel, entry);
  }
  return Array.from(byChannel.values()).sort((a, b) => b.matched + b.mismatched - (a.matched + a.mismatched));
}

function toCsv(summary: ReconSummary): string {
  const header = "type,channel,status,count,api_total,sap_total,diff";
  const lines = summary.by_channel.map((r) =>
    [r.type, r.channel, r.status, r.count, r.api_total, r.sap_total, r.diff].join(",")
  );
  return [header, ...lines, "", `Grand Total,,,${summary.grand_total.count},${summary.grand_total.api_total},${summary.grand_total.sap_total},${summary.grand_total.diff}`].join("\n");
}

function ReconBarChart({ title, data }: { title: string; data: { channel: string; matched: number; mismatched: number }[] }) {
  if (data.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={Math.max(180, data.length * 32)}>
          <BarChart data={data} layout="vertical" margin={{ left: 8, right: 16 }}>
            <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="hsl(var(--border))" />
            <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} stroke="hsl(var(--muted-foreground))" />
            <YAxis type="category" dataKey="channel" width={150} tick={{ fontSize: 11 }} stroke="hsl(var(--muted-foreground))" />
            <Tooltip
              contentStyle={{ background: "hsl(var(--popover))", border: "1px solid hsl(var(--border))", borderRadius: 8, fontSize: 12 }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar dataKey="matched" name="Matched" stackId="a" fill={MATCHED_COLOR} radius={[0, 0, 0, 0]} />
            <Bar dataKey="mismatched" name="Mismatched" stackId="a" fill={MISMATCH_COLOR} radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}

export function MonthlyReconPage() {
  const [month, setMonth] = useState(defaultMonth());
  const [summary, setSummary] = useState<ReconSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function load(isManualRefresh = false) {
    if (isManualRefresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    const { start, end } = monthToRange(month);
    getReconSummary(start, end)
      .then(setSummary)
      .catch((e) => setError(e.message))
      .finally(() => {
        setLoading(false);
        setRefreshing(false);
      });
  }

  useEffect(load, [month]); // eslint-disable-line react-hooks/exhaustive-deps

  const invoiceChartData = useMemo(() => byChannelForChart(summary?.by_channel || [], ["Invoice"]), [summary]);
  const cnChartData = useMemo(
    () => byChannelForChart(summary?.by_channel || [], ["CreditNote-Standard", "CreditNote-Flex"]),
    [summary]
  );

  const invoiceRows = summary?.by_channel.filter((r) => r.type === "Invoice") || [];
  const cnRows = summary?.by_channel.filter((r) => r.type !== "Invoice") || [];
  const invoiceCount = invoiceRows.reduce((s, r) => s + r.count, 0);
  const invoiceMismatch = invoiceRows.filter((r) => r.status === "amount_mismatch").reduce((s, r) => s + r.count, 0);
  const cnCount = cnRows.reduce((s, r) => s + r.count, 0);
  const cnMismatch = cnRows.filter((r) => r.status === "amount_mismatch").reduce((s, r) => s + r.count, 0);
  const invoiceMatchRate = invoiceCount > 0 ? (((invoiceCount - invoiceMismatch) / invoiceCount) * 100).toFixed(1) : "—";
  const cnMatchRate = cnCount > 0 ? (((cnCount - cnMismatch) / cnCount) * 100).toFixed(1) : "—";

  function downloadCsv() {
    if (!summary) return;
    const blob = new Blob([toCsv(summary)], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `unicommerce-recon-${month}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="space-y-4">
      <FilterShell>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Month</label>
          <Input type="month" value={month} onChange={(e) => setMonth(e.target.value)} className="w-40" />
        </div>
        <div className="ml-auto flex gap-2">
          <Button size="sm" variant="outline" onClick={downloadCsv} disabled={!summary}>
            <Download className="h-3.5 w-3.5" />
            Download CSV
          </Button>
          <Button size="sm" onClick={() => load(true)} disabled={refreshing}>
            {refreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCcw className="h-3.5 w-3.5" />}
            Refresh
          </Button>
        </div>
      </FilterShell>

      {error && <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>}

      {loading ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-16" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Invoices reconciled" value={invoiceCount} icon={FileSpreadsheet} />
          <StatCard label="Invoice match rate" value={`${invoiceMatchRate}%`} icon={ScaleIcon} tone={invoiceMismatch > 0 ? "warning" : "success"} />
          <StatCard label="Credit notes reconciled" value={cnCount} icon={FileSpreadsheet} />
          <StatCard label="Credit note match rate" value={`${cnMatchRate}%`} icon={ScaleIcon} tone={cnMismatch > 0 ? "warning" : "success"} />
        </div>
      )}

      {!loading && summary && (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <ReconBarChart title="Invoices by channel" data={invoiceChartData} />
            <ReconBarChart title="Credit notes by channel" data={cnChartData} />
          </div>

          <div className="rounded-lg border bg-card">
            <div className="max-h-[calc(100vh-560px)] min-h-[200px] overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Type</TableHead>
                    <TableHead>Channel</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Count</TableHead>
                    <TableHead className="text-right">API total</TableHead>
                    <TableHead className="text-right">SAP total</TableHead>
                    <TableHead className="text-right">Diff</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {summary.by_channel.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={7} className="py-12 text-center text-sm text-muted-foreground">
                        No posted invoices or credit notes in this period.
                      </TableCell>
                    </TableRow>
                  )}
                  {summary.by_channel.map((r, i) => (
                    <TableRow key={i}>
                      <TableCell>{r.type}</TableCell>
                      <TableCell>{r.channel}</TableCell>
                      <TableCell className={r.status === "amount_mismatch" ? "text-destructive font-medium" : "text-success"}>
                        {r.status}
                      </TableCell>
                      <TableCell className="text-right font-mono-tabular">{r.count}</TableCell>
                      <TableCell className="text-right font-mono-tabular">{formatMoney(r.api_total)}</TableCell>
                      <TableCell className="text-right font-mono-tabular">{formatMoney(r.sap_total)}</TableCell>
                      <TableCell className="text-right font-mono-tabular">{formatMoney(r.diff)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
                {summary.by_channel.length > 0 && (
                  <tfoot>
                    <TableRow className="font-medium">
                      <TableCell colSpan={3}>Grand Total</TableCell>
                      <TableCell className="text-right font-mono-tabular">{summary.grand_total.count}</TableCell>
                      <TableCell className="text-right font-mono-tabular">{formatMoney(summary.grand_total.api_total)}</TableCell>
                      <TableCell className="text-right font-mono-tabular">{formatMoney(summary.grand_total.sap_total)}</TableCell>
                      <TableCell className="text-right font-mono-tabular">{formatMoney(summary.grand_total.diff)}</TableCell>
                    </TableRow>
                  </tfoot>
                )}
              </Table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
