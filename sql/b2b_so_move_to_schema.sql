-- Move existing B2B tables from public → schema b2b, then drop the b2b_ prefix.
-- Run this once on khanaltestdb (you already have the tables in public).
-- Does NOT touch walmart_* or sap_fg_items.
--
-- After this:
--   \dt b2b.*
--   \dt public.*     -- walmart + sap_fg_items still here
--
-- Then redeploy B2B_so so Lambda uses b2b.purchase_orders etc.

BEGIN;

CREATE SCHEMA IF NOT EXISTS b2b;

ALTER TABLE IF EXISTS public.b2b_po_lines          SET SCHEMA b2b;
ALTER TABLE IF EXISTS public.b2b_purchase_orders     SET SCHEMA b2b;
ALTER TABLE IF EXISTS public.b2b_sku_map            SET SCHEMA b2b;
ALTER TABLE IF EXISTS public.b2b_ship_to_aliases     SET SCHEMA b2b;
ALTER TABLE IF EXISTS public.b2b_processed_emails     SET SCHEMA b2b;
ALTER TABLE IF EXISTS public.b2b_notifications        SET SCHEMA b2b;
ALTER TABLE IF EXISTS public.b2b_email_log           SET SCHEMA b2b;
ALTER TABLE IF EXISTS public.b2b_audit_log           SET SCHEMA b2b;
ALTER TABLE IF EXISTS public.b2b_users               SET SCHEMA b2b;
ALTER TABLE IF EXISTS public.b2b_roles               SET SCHEMA b2b;

ALTER TABLE IF EXISTS b2b.b2b_purchase_orders RENAME TO purchase_orders;
ALTER TABLE IF EXISTS b2b.b2b_po_lines        RENAME TO po_lines;
ALTER TABLE IF EXISTS b2b.b2b_sku_map         RENAME TO sku_map;
ALTER TABLE IF EXISTS b2b.b2b_ship_to_aliases RENAME TO ship_to_aliases;
ALTER TABLE IF EXISTS b2b.b2b_processed_emails RENAME TO processed_emails;
ALTER TABLE IF EXISTS b2b.b2b_notifications    RENAME TO notifications;
ALTER TABLE IF EXISTS b2b.b2b_email_log       RENAME TO email_log;
ALTER TABLE IF EXISTS b2b.b2b_audit_log       RENAME TO audit_log;
ALTER TABLE IF EXISTS b2b.b2b_roles          RENAME TO roles;
ALTER TABLE IF EXISTS b2b.b2b_users          RENAME TO users;

COMMIT;
