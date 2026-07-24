# Phase 2 — B2B PO automation → Lambda

## Source (Frappe, verified)

`khanal_tech_integrations/utils/B2B/`: `Bigbasket.py` + `Bigbasket_SAP.py`,
`Blink.py` + `Blink_SAP.py`, `Flipkart.py` + `flipkart_SAP.py`,
`Instamart.py` + `Insta_SAP.py`, `More.py` + `More_SAP.py`, `Zepto.py` +
`Zepto_SAP.py`, plus `email_template.html`. Doctypes: `b2b`,
`b2b_customer_details`, `b2b_email`, `table_b2b`.

Cron (from `hooks.py`, every 5 minutes):

```python
"*/5 * * * *":[
    "khanal_tech_integrations.utils.purchase.purchaseorder.update_purchase_orders",
    "khanal_tech_integrations.utils.B2B.Blink.process_email_attachments",
    "khanal_tech_integrations.utils.B2B.Instamart.process_email_attachments",
    "khanal_tech_integrations.utils.B2B.Bigbasket.process_email_attachments",
    "khanal_tech_integrations.utils.B2B.Zepto.process_email_attachments",
    "khanal_tech_integrations.utils.B2B.More.process_email_attachments",
],
```

**Flipkart is not in this list.** Its `.py` files exist but no scheduled
call was found anywhere in `hooks.py`. Before scoping this phase's work,
confirm whether Flipkart POs are: (a) processed by a mechanism this
search missed, (b) triggered manually today, or (c) actually dead code.
This changes whether Phase 2 covers five marketplaces or six.

**Mailbox access confirmed: IMAP.** Frappe polls each marketplace's
mailbox via IMAP today (likely via Frappe's Email Account doctype
config — confirm the exact mailbox provider, e.g. Gmail/O365/other, since
that affects whether app-password or OAuth2 IMAP auth is needed).

## Architecture decision

Unlike Unicommerce, this is **six independent business flows sharing one
pattern** (IMAP fetch → parse PO attachment → map → post to SAP), not one
flow. Two structural choices:

1. **One `services/b2b-po-automation` stack, one Lambda function per
   marketplace** (`bigbasketPoWorker`, `blinkPoWorker`, etc.), sharing a
   common IMAP-fetch + SAP-post helper in `khanal_shared`. This is the
   recommended shape — it matches the "one stack per domain" convention
   (B2B PO automation is one *domain*, marketplaces are variations
   within it), and keeps IAM/deploy simple (one `serverless.yml`, one
   CI entry) while still isolating blast radius at the function level
   (one marketplace's bad parse doesn't touch another's Lambda
   invocation).
2. Reject: one service per marketplace. Six stacks for near-identical
   logic is deploy/IAM overhead without a real isolation benefit — a
   parsing bug in Bigbasket's PO format doesn't need a separate
   CloudFormation stack to contain, since Lambda invocations are already
   isolated per function.

**IMAP from Lambda, no VPC.** Lambda has internet egress by default
outside a VPC — IMAPS (port 993, TLS) works exactly like the HTTPS calls
`amazon-us-b2c` already makes to SP-API and SAP SL. No NAT, no VPC
endpoint needed for this. Store each mailbox's credentials as its own SSM
`SecureString` parameter (`/b2b/<marketplace>/imap_credentials`) — six
parameters, not one shared one, so a credential rotation or compromise on
one marketplace doesn't touch the others.

## Infrastructure changes

- New stack: `services/b2b-po-automation/`
- Six scheduled Lambda functions, 5-min cadence (or per-marketplace
  cadence if volumes differ enough to justify it — check actual PO
  volume per marketplace before assuming uniform 5-min polling is
  necessary for all six; IMAP polling more often than needed just burns
  invocations for no benefit).
- `khanal_shared` gets a new `b2b_email` (or similarly named) module:
  generic IMAP-fetch-unread-with-attachment helper, parameterized by
  mailbox SSM path — the six workers call this with their own config and
  their own SAP mapper, not six copies of IMAP-connection code.
- IAM: `ssm:GetParameter` scoped to the six `/b2b/<marketplace>/*`
  parameter paths + the shared `/sap/inc/*` family, `ses:SendEmail` for
  failure alerts.
- Postgres: `b2b` schema — tables for parsed PO records and SAP posting
  log, replacing `b2b`, `b2b_customer_details`, `b2b_email`, `table_b2b`
  doctypes. Check whether `b2b_customer_details` is actually
  marketplace-specific config (customer mapping per marketplace) — if so
  it likely becomes SSM/env config rather than a table, similar to how
  `unicommerce_settings` was handled in Phase 1.

## Cutover runbook

1. Confirm the Flipkart question above before finalizing scope.
2. Get IMAP credentials for each of the (five or six) mailboxes into SSM
   as `SecureString` parameters — do this without disturbing Frappe's
   existing IMAP polling (most mail providers allow multiple concurrent
   IMAP connections against the same mailbox; confirm this isn't an
   `IDLE`-exclusive setup that would lock Frappe out).
3. Build and deploy `services/b2b-po-automation` with **all schedules
   `enabled: false`**, using a **read-only mailbox test** first: fetch
   and parse only, log the parsed PO to CloudWatch/Postgres, do not post
   to SAP yet.
4. Enable one marketplace's fetch-and-parse (lowest PO volume first) with
   schedule on, Frappe's polling for that marketplace **left running in
   parallel** — IMAP fetch-only doesn't conflict with itself across two
   readers as long as neither marks messages as read/deleted in a way
   that starves the other (check whether Frappe marks emails as
   read/moves them to a processed folder — if so, the Lambda side needs
   its own IMAP search criteria, e.g. by date range, not "unread", during
   the parallel-run window).
5. Once parsed output matches Frappe's for a verification window, enable
   the SAP-posting half for that one marketplace, disabling Frappe's
   corresponding cron entry immediately beforehand (same back-to-back
   discipline as every other phase).
6. Repeat steps 4–5 for each remaining marketplace, one at a time —
   six separate cutover risks, don't batch them.
7. After all marketplaces are stable on Lambda, delete
   `khanal_tech_integrations/utils/B2B/` and its doctypes.

## Risks

- **IMAP read-state conflicts** between Frappe and Lambda both reading
  the same mailbox during dual-run — the single biggest risk in this
  phase, worth a spike/test before step 4 rather than discovering it live.
- PO attachment format drift per marketplace (Excel/PDF/CSV) — port each
  marketplace's existing parser logic directly rather than
  re-implementing from scratch, to avoid silently changing parsing
  behavior.
- Six independent cutovers means six chances to get the "disable Frappe
  cron right before enabling Lambda" step wrong — consider a checklist
  or small script that does both in one action per marketplace, rather
  than relying on manual discipline six times.

## Cost estimate (incremental, `ap-south-1`)

| Item | Estimate | Notes |
|---|---|---|
| Lambda (5–6 functions, 5-min cadence) | $1–4/mo | ~288 invocations/day per function at 5-min cadence × 6 ≈ 52k/month total, well under the 1M free-tier requests; compute cost depends on IMAP fetch + parse duration, likely low-second executions |
| SSM SecureString parameters (6+) | $0 | Standard parameters are free; SecureString with the default AWS-managed KMS key is also free — only customer-managed KMS keys cost ($1/mo/key) |
| SES alerts | <$1/mo | Failure-only volume |
| Postgres schema addition | $0 incremental | Existing shared RDS |

**Total: roughly $1–5/month incremental.** This phase's cost is trivial —
its actual cost is engineering time across six sequential cutovers, not
AWS spend.
