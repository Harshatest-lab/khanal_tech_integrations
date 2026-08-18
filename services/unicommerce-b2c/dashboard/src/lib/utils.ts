import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function formatDateOnly(value?: string | null): string {
  if (!value) return "—";
  return String(value).slice(0, 10);
}

export function formatMoney(amount: number | null | undefined): string {
  if (amount == null || Number.isNaN(Number(amount))) return "—";
  try {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(Number(amount));
  } catch {
    return `₹${Number(amount).toFixed(2)}`;
  }
}

export function truncate(text: string | null | undefined, max = 60): string {
  const s = String(text || "");
  return s.length <= max ? s : `${s.slice(0, max)}…`;
}
