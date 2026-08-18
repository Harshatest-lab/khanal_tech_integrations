import { cn } from "@/lib/utils";
import { Boxes, ListOrdered, Undo2, RefreshCcw, ScaleIcon, LogOut } from "lucide-react";
import type { PanelId } from "@/App";

const NAV: { id: PanelId; label: string; icon: typeof ListOrdered }[] = [
  { id: "orders", label: "Orders", icon: ListOrdered },
  { id: "returns", label: "Returns / credit notes", icon: Undo2 },
  { id: "sync", label: "Sync history", icon: RefreshCcw },
  { id: "recon", label: "Recon / mismatches", icon: ScaleIcon },
];

export function Sidebar({
  active,
  onSelect,
  onLogout,
}: {
  active: PanelId;
  onSelect: (id: PanelId) => void;
  onLogout: () => void;
}) {
  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground">
      <div className="flex items-center gap-3 px-5 py-5">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <Boxes className="h-4.5 w-4.5" />
        </div>
        <div>
          <div className="text-sm font-semibold leading-tight text-white">Unicommerce B2C</div>
          <div className="text-[10px] uppercase tracking-wider text-sidebar-foreground/60">Ops Console</div>
        </div>
      </div>

      <div className="px-5 pb-2 pt-4 text-[10px] font-semibold uppercase tracking-wider text-sidebar-foreground/40">
        Menu
      </div>
      <nav className="flex-1 space-y-0.5 px-3">
        {NAV.map((item) => {
          const Icon = item.icon;
          const isActive = item.id === active;
          return (
            <button
              key={item.id}
              onClick={() => onSelect(item.id)}
              className={cn(
                "flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
                isActive
                  ? "bg-primary/15 text-white font-medium"
                  : "text-sidebar-foreground/70 hover:bg-white/5 hover:text-white"
              )}
            >
              <Icon className="h-4 w-4" />
              {item.label}
            </button>
          );
        })}
      </nav>

      <div className="border-t border-sidebar-border px-4 py-4">
        <div className="mb-3 flex items-center gap-2 text-xs text-sidebar-foreground/60">
          <span className="h-1.5 w-1.5 rounded-full bg-success" />
          Live from RDS
        </div>
        <button
          onClick={onLogout}
          className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm text-sidebar-foreground/70 transition-colors hover:bg-white/5 hover:text-white"
        >
          <LogOut className="h-4 w-4" />
          Sign out
        </button>
      </div>
    </aside>
  );
}
