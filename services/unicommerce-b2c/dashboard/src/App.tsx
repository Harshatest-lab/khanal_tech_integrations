import { useCallback, useEffect, useState } from "react";
import { Sidebar } from "@/components/Sidebar";
import { StatCard } from "@/components/StatCard";
import { LoginPage } from "@/components/LoginPage";
import { Skeleton } from "@/components/ui/skeleton";
import { OrdersPage } from "@/pages/OrdersPage";
import { ReturnsPage } from "@/pages/ReturnsPage";
import { SyncLogPage } from "@/pages/SyncLogPage";
import { ReconPage } from "@/pages/ReconPage";
import { MonthlyReconPage } from "@/pages/MonthlyReconPage";
import { ensureAuthenticated, clearAuthSession } from "@/lib/auth";
import { getStats } from "@/lib/api";
import type { Stats } from "@/lib/types";
import { Package, CheckCircle2, Clock, Inbox, PauseCircle, XCircle } from "lucide-react";

export type PanelId = "orders" | "returns" | "sync" | "recon" | "recon-summary";

const PANEL_META: Record<PanelId, { eyebrow: string; title: string }> = {
  orders: { eyebrow: "Orders", title: "Orders" },
  returns: { eyebrow: "Returns / credit notes", title: "Returns & credit notes" },
  sync: { eyebrow: "Sync history", title: "Sync history" },
  recon: { eyebrow: "Recon / mismatches", title: "Recon & mismatches" },
  "recon-summary": { eyebrow: "Monthly reconciliation", title: "Monthly reconciliation" },
};

function StatsBar({ stats, loading }: { stats: Stats | null; loading: boolean }) {
  if (loading || !stats) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
        {Array.from({ length: 7 }).map((_, i) => (
          <Skeleton key={i} className="h-16" />
        ))}
      </div>
    );
  }
  const byStatus = stats.by_pipeline_status || {};
  const posted = byStatus.posted || 0;
  const ready = byStatus.ready || 0;
  const fetched = byStatus.fetched || 0;
  const batchHold = byStatus.batch_hold || 0;
  const failed = (byStatus.failed || 0) + (byStatus.dead || 0);
  const cnPosted = stats.cn_by_status?.posted || 0;
  const cnFailed = (stats.cn_by_status?.failed || 0) + (stats.cn_by_status?.dead || 0);

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
      <StatCard label="Total orders" value={stats.total_orders ?? 0} icon={Package} />
      <StatCard label="Posted" value={posted} icon={CheckCircle2} tone="success" />
      <StatCard label="Ready" value={ready} icon={Clock} />
      <StatCard label="Fetched" value={fetched} icon={Inbox} />
      <StatCard label="Batch hold" value={batchHold} icon={PauseCircle} tone="warning" />
      <StatCard label="Failed" value={failed} icon={XCircle} tone={failed > 0 ? "destructive" : "default"} />
      <StatCard label="CN posted" value={cnPosted} icon={CheckCircle2} tone="success" />
      <StatCard label="CN failed" value={cnFailed} icon={XCircle} tone={cnFailed > 0 ? "destructive" : "default"} />
    </div>
  );
}

export default function App() {
  const [authChecked, setAuthChecked] = useState(false);
  const [authed, setAuthed] = useState(false);
  const [panel, setPanel] = useState<PanelId>("orders");
  const [stats, setStats] = useState<Stats | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);

  const refreshStats = useCallback(() => {
    setStatsLoading(true);
    getStats()
      .then(setStats)
      .catch(() => {})
      .finally(() => setStatsLoading(false));
  }, []);

  useEffect(() => {
    ensureAuthenticated().then((ok) => {
      setAuthed(ok);
      setAuthChecked(true);
      if (ok) refreshStats();
    });
  }, [refreshStats]);

  function handleLoginSuccess() {
    setAuthed(true);
    refreshStats();
  }

  function handleLogout() {
    clearAuthSession();
    setAuthed(false);
  }

  if (!authChecked) return null;
  if (!authed) return <LoginPage onSuccess={handleLoginSuccess} />;

  const meta = PANEL_META[panel];

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      <Sidebar active={panel} onSelect={setPanel} onLogout={handleLogout} />
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-20 space-y-4 border-b bg-background/80 px-6 py-5 backdrop-blur">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{meta.eyebrow}</p>
            <h1 className="text-2xl font-semibold">{meta.title}</h1>
          </div>
          <StatsBar stats={stats} loading={statsLoading} />
        </header>
        <div className="px-6 py-5">
          {panel === "orders" && <OrdersPage onStatsRefresh={refreshStats} />}
          {panel === "returns" && <ReturnsPage onStatsRefresh={refreshStats} />}
          {panel === "sync" && <SyncLogPage />}
          {panel === "recon" && <ReconPage />}
          {panel === "recon-summary" && <MonthlyReconPage />}
        </div>
      </main>
    </div>
  );
}
