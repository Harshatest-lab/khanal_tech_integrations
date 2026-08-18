import { cn } from "@/lib/utils";
import type { LucideIcon } from "lucide-react";

export function StatCard({
  label,
  value,
  icon: Icon,
  tone = "default",
}: {
  label: string;
  value: number | string;
  icon: LucideIcon;
  tone?: "default" | "success" | "warning" | "destructive";
}) {
  const toneClasses: Record<string, string> = {
    default: "text-foreground",
    success: "text-success",
    warning: "text-amber-600 dark:text-amber-400",
    destructive: "text-destructive",
  };
  return (
    <div className="flex items-center gap-3 rounded-lg border bg-card px-4 py-3 shadow-sm">
      <div className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-muted", toneClasses[tone])}>
        <Icon className="h-4.5 w-4.5" />
      </div>
      <div className="min-w-0">
        <div className={cn("font-mono-tabular text-xl font-semibold leading-none", toneClasses[tone])}>{value}</div>
        <div className="mt-1 truncate text-xs text-muted-foreground">{label}</div>
      </div>
    </div>
  );
}
