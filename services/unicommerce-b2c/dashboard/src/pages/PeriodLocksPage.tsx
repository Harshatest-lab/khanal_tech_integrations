import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { FilterShell } from "@/components/FilterBar";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { getPeriodLocks, lockPeriod, unlockPeriod } from "@/lib/api";
import { formatDate } from "@/lib/utils";
import type { PeriodLock } from "@/lib/types";
import { Lock, Unlock, Loader2, RefreshCcw, ShieldOff } from "lucide-react";

function defaultMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

export function PeriodLocksPage() {
  const [items, setItems] = useState<PeriodLock[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [month, setMonth] = useState(defaultMonth());
  const [note, setNote] = useState("");
  const [busyPeriod, setBusyPeriod] = useState<string | null>(null);

  function load(isManualRefresh = false) {
    if (isManualRefresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    getPeriodLocks()
      .then((data) => setItems(data.items || []))
      .catch((e) => setError(e.message))
      .finally(() => {
        setLoading(false);
        setRefreshing(false);
      });
  }

  useEffect(load, []);

  async function handleLock() {
    setBusyPeriod(month);
    try {
      const result = await lockPeriod(month, note);
      toast.success(`${month} locked`, {
        description: `${(result as any).orders_held ?? 0} order(s) and ${(result as any).credit_notes_held ?? 0} credit note(s) held.`,
      });
      setNote("");
      load();
    } catch (e: any) {
      toast.error("Lock failed", { description: e.message });
    } finally {
      setBusyPeriod(null);
    }
  }

  async function handleUnlock(period: string) {
    setBusyPeriod(period);
    try {
      const result = await unlockPeriod(period);
      toast.success(`${period} unlocked`, {
        description: `${(result as any).orders_resumed ?? 0} order(s) and ${(result as any).credit_notes_resumed ?? 0} credit note(s) resumed.`,
      });
      load();
    } catch (e: any) {
      toast.error("Unlock failed", { description: e.message });
    } finally {
      setBusyPeriod(null);
    }
  }

  const alreadyLocked = items.some((r) => r.period === month && r.locked);

  return (
    <div className="space-y-4">
      <FilterShell>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Month to lock</label>
          <Input type="month" value={month} onChange={(e) => setMonth(e.target.value)} className="w-40" />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">Note (optional)</label>
          <Input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. finance closed books"
            className="w-64"
          />
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => load(true)} disabled={refreshing}>
            {refreshing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCcw className="h-3.5 w-3.5" />}
            Refresh
          </Button>
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button size="sm" disabled={alreadyLocked || busyPeriod === month}>
                <Lock className="h-3.5 w-3.5" />
                {alreadyLocked ? "Already locked" : "Lock month"}
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Lock {month}?</AlertDialogTitle>
                <AlertDialogDescription>
                  No AR invoice or credit note dated in {month} will be able to post to SAP until this month is
                  unlocked again. Orders/credit notes currently waiting to post in this month will be held and
                  clearly marked — nothing is lost, they resume automatically on unlock.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={handleLock} disabled={busyPeriod === month}>
                  {busyPeriod === month && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
                  Lock
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </FilterShell>

      {error && <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>}

      <div className="rounded-lg border bg-card">
        <div className="max-h-[calc(100vh-320px)] overflow-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Period</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Locked by</TableHead>
                <TableHead>Locked at</TableHead>
                <TableHead>Unlocked by</TableHead>
                <TableHead>Unlocked at</TableHead>
                <TableHead>Note</TableHead>
                <TableHead className="w-32" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading &&
                Array.from({ length: 4 }).map((_, i) => (
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
                      <ShieldOff className="h-8 w-8 text-muted-foreground/50" />
                      <p className="text-sm text-muted-foreground">No months have been locked yet.</p>
                    </div>
                  </TableCell>
                </TableRow>
              )}
              {!loading &&
                items.map((r) => (
                  <TableRow key={r.period}>
                    <TableCell className="font-mono-tabular">{r.period}</TableCell>
                    <TableCell>
                      {r.locked ? (
                        <span className="inline-flex items-center gap-1 rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
                          <Lock className="h-3 w-3" /> Locked
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 rounded-full bg-success/10 px-2 py-0.5 text-xs font-medium text-success">
                          <Unlock className="h-3 w-3" /> Unlocked
                        </span>
                      )}
                    </TableCell>
                    <TableCell>{r.locked_by || "—"}</TableCell>
                    <TableCell>{formatDate(r.locked_at)}</TableCell>
                    <TableCell>{r.unlocked_by || "—"}</TableCell>
                    <TableCell>{formatDate(r.unlocked_at)}</TableCell>
                    <TableCell>{r.note || "—"}</TableCell>
                    <TableCell>
                      {r.locked && (
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => handleUnlock(r.period)}
                          disabled={busyPeriod === r.period}
                        >
                          {busyPeriod === r.period ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <Unlock className="h-3.5 w-3.5" />
                          )}
                          Unlock
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  );
}
