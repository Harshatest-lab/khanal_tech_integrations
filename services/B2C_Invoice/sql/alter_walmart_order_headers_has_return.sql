-- Add has_return flag to walmart_order_headers.
-- Run once against the live DB; idempotent (IF NOT EXISTS).

ALTER TABLE walmart_order_headers
    ADD COLUMN IF NOT EXISTS has_return boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_walmart_order_headers_has_return
    ON walmart_order_headers(has_return)
    WHERE has_return = true;

-- Back-fill: mark any order that already has a row in walmart_returns.
UPDATE walmart_order_headers h
SET has_return = true
WHERE EXISTS (
    SELECT 1
    FROM walmart_returns r
    WHERE r.purchase_order_ids LIKE '%' || h.purchase_order_id || '%'
);
