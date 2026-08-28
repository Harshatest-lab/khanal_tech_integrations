# safexpress

SafeExpress (carrier) waybill/POD tracking for SAP B1 AR Invoices —
Lambda replacement for the Frappe pipeline in
`khanal_tech_integrations/utils/safexpress` + `utils/safexpress2`.

**Status: scaffolded, not deployed.** Schedules are `enabled: false` on
every stage. See `docs/architecture/PLATFORM_ARCHITECTURE.md` §5 for the
full design rationale and open items before this goes live.

## What changed vs. the Frappe pipeline

The old pipeline replicated every matching AR invoice's full header + line
items into a Frappe MariaDB doctype (`SAP AR Invoice Detail`), with 2 extra
SAP calls per invoice, just to answer "does this invoice have a waybill
yet" — a question that needs ~4 columns. This service does not replicate
invoices anywhere:

- **`hana_candidates.py`** queries SAP HANA directly, live, each run —
  `TransportationCode = 1 AND U_TrackingNo IS NULL`, in a lookback window.
  No local invoice mirror.
- **`store.py`**'s `safexpress_pod_tracking` table (see
  `sql/safexpress.sql`) holds only tracking/POD state and retry
  bookkeeping — DocEntry, waybill, POD URL/date, status, attempt count.
  Not an invoice replica.
- **`client.py`** / **`auth.py`** / **`sap_client.py`** port the working
  parts of the legacy integration close to 1:1 (SafeExpress API shapes,
  SAP PATCH payload) — those were correct, just wired to the wrong data
  source.

## Backlog design (fixes a real gap in the legacy pipeline)

The legacy pipeline's `process_invoices_without_pod(days=N)` used a single
rolling date window for both *discovering* new invoices and *deciding what
to retry* — so an invoice whose SafeExpress waybill/POD showed up more than
N days after its last SAP update silently stopped being polled forever
(their workaround was a manual `get_all_pods_from_last_year` backfill run
when someone noticed a miss). This port fixes that instead of inheriting it:

- **`hana_candidates.find_candidates(days)`** — small rolling window,
  *discovery only*. Its only job is noticing invoices new since the last
  run.
- **`safexpress_pod_tracking`** (Postgres) is the actual queue. Once a
  DocEntry is in the table, it keeps getting retried — regardless of how
  old its SAP UpdateDate is — until it resolves or goes `stale`.
- **`store.list_open_candidates`** pulls each run's work mostly
  newest-invoice-first (`doc_entry DESC` — recent shipments are far more
  likely to already have a waybill ready), with a small reserved
  oldest-first slice (`OLDEST_FAIRNESS_FRACTION`, config.py) so old open
  rows aren't starved forever behind a stream of new ones. Capped at
  `batch_size` rows per run regardless of backlog size, so growth in
  invoice volume can't push a single invocation past the Lambda timeout.
- **`store.mark_stale_if_expired`** gives up on anything still open past
  `STALE_AFTER_DAYS` (config.py, default 120) — past that point it's more
  likely cancelled or mis-tagged with the wrong transporter than genuinely
  still in transit. A later HANA sighting revives a `stale` row back to
  `pending` (`upsert_candidate`).
- **`mode=backfill`** (`pod_service.seed_backfill`) — run once by hand at
  go-live cutover: an unbounded HANA scan (no date filter at all) for
  every currently-untracked SafeExpress invoice, seeded straight into the
  backlog. Doesn't process anything itself; the next scheduled `poll` runs
  work it down like any other backlog.

## Open item, unverified against real data

`hana_candidates.py`'s `OINV` → `OSRI` join to resolve a `series_name`
prefix (e.g. `KAIN24`) has **not** been checked against real HANA data —
the legacy pipeline populated this field through a path this port didn't
fully trace. If the join doesn't resolve a name, `client.py` falls back to
trying every prefix in `config.INVOICE_SERIES_PREFIXES`, so correctness
doesn't depend on the join working — but it's worth confirming during dev
testing, since a broken join means every candidate takes the slow path.

## Setup before first dev run

1. Create SSM SecureString `/safexpress/credentials` (see
   `credentials.py`'s docstring for the JSON shape) — pull the real values
   from `khanal_tech_integrations/utils/safexpress2/auth.py`'s
   `_renew_token`, `Safexpress Settings` doctype fields, and
   `config.py`/environment. **The legacy code had the OAuth client's Basic
   auth header hardcoded directly in source — do not repeat that here.**
2. Create the `safexpress_prod` database + `safexpress_prod_app` role on
   `khanal-db-prod` (mirrors `amazon_us_b2c_prod`/`unicommerce_b2c_prod`,
   see `docs/architecture/PLATFORM_ARCHITECTURE.md` §3), then run
   `sql/safexpress.sql` against it.
3. Add the `SAFEXPRESS_PROD_DATABASE_URL` GitHub secret.
4. Deploy `dev` with schedules still `enabled: false`, invoke
   `track_handler` manually with `{"mode": "poll", "days": 4}` against the
   `india_test` SAP company, and check `safexpress_pod_tracking` +
   CloudWatch logs before enabling any schedule.
5. At real go-live cutover (prod), invoke `track_handler` once with
   `{"mode": "backfill"}` to seed every currently-untracked SafeExpress
   invoice into the backlog — see "Backlog design" below. Do this once
   schedules are about to be enabled, not before; there's no reason to seed
   the backlog long before `poll` is actually running to work it down.

## Handler

`safexpressTrack` (`src.handlers.track_handler`), modes:

- `poll` (default, scheduled every 3h) — small HANA window for new-invoice
  discovery, then works the Postgres backlog (all open rows, not just
  freshly-discovered ones) end-to-end: SafeExpress tracking → POD
  download/upload → SAP PATCH.
- `retry_pending_sap` (scheduled daily) — retries only the SAP PATCH for
  rows that already have a POD uploaded but a previously failed write; no
  fresh HANA/SafeExpress calls.
- `backfill` (manual, one-time at cutover) — unbounded HANA scan seeding
  every currently-untracked SafeExpress invoice into the backlog.

## POD storage

Kept on the existing `us-west-1` API Gateway + S3 bucket
(`khanalfoods-fileupload-bucket`) by explicit decision (2026-08-24) — not
migrated to `ap-south-1`.
