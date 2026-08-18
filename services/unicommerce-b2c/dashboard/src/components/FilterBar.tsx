import { X, SlidersHorizontal } from "lucide-react";
import { cn } from "@/lib/utils";

export function FilterShell({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-end gap-3 rounded-lg border bg-card px-4 py-3 shadow-sm", className)}>
      <div className="flex items-center gap-1.5 pb-1.5 pr-1 text-muted-foreground">
        <SlidersHorizontal className="h-4 w-4" />
      </div>
      {children}
    </div>
  );
}

export interface ActiveFilterChip {
  key: string;
  label: string;
  onRemove: () => void;
}

export function ActiveFilterChips({ chips, onClearAll }: { chips: ActiveFilterChip[]; onClearAll: () => void }) {
  if (chips.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-muted-foreground">Active:</span>
      {chips.map((chip) => (
        <button
          key={chip.key}
          onClick={chip.onRemove}
          className="group inline-flex items-center gap-1 rounded-full border border-primary/30 bg-primary/10 py-1 pl-2.5 pr-1.5 text-xs font-medium text-primary transition-colors hover:border-primary/50 hover:bg-primary/15"
        >
          {chip.label}
          <X className="h-3 w-3 opacity-60 transition-opacity group-hover:opacity-100" />
        </button>
      ))}
      <button onClick={onClearAll} className="text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">
        Clear all
      </button>
    </div>
  );
}

export function DatePresets({ onSelect }: { onSelect: (from: string, to: string) => void }) {
  function iso(d: Date) {
    return d.toISOString().slice(0, 10);
  }
  function preset(days: number) {
    const to = new Date();
    const from = new Date();
    from.setDate(from.getDate() - days);
    onSelect(iso(from), iso(to));
  }
  const presets: { label: string; days: number }[] = [
    { label: "Today", days: 0 },
    { label: "7d", days: 7 },
    { label: "30d", days: 30 },
    { label: "90d", days: 90 },
  ];
  return (
    <div className="flex items-center gap-1">
      {presets.map((p) => (
        <button
          key={p.label}
          type="button"
          onClick={() => preset(p.days)}
          className="rounded-md px-2 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}
