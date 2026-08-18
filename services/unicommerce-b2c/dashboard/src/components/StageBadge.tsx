import { Badge } from "@/components/ui/badge";

// Pipeline-status values confirmed live in the codebase (post_invoices.py,
// credit_notes.py, store.py): fetched, ready, posting, verify, batch_hold,
// posted, failed, dead. Same map used for both orders (pipeline_status) and
// credit notes (status) — the vocabularies overlap.
const STAGE_VARIANT: Record<string, "default" | "success" | "warning" | "destructive" | "info"> = {
  fetched: "info",
  ready: "info",
  posting: "warning",
  processing: "warning",
  verify: "warning",
  po_created: "warning",
  grn_done: "warning",
  batch_hold: "warning",
  posted: "success",
  failed: "destructive",
  dead: "destructive",
};

export function StageBadge({ stage }: { stage?: string }) {
  const variant = (stage && STAGE_VARIANT[stage]) || "default";
  return <Badge variant={variant}>{stage || "—"}</Badge>;
}
