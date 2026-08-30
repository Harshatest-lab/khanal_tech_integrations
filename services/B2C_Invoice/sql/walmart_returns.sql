CREATE TABLE IF NOT EXISTS walmart_returns (
    return_order_id          text        PRIMARY KEY,
    customer_order_id        text,
    return_order_date        timestamptz,
    refund_mode              text,
    total_refund_amount      numeric(10,2),
    total_refund_currency    text,
    purchase_order_ids       text,        -- distinct PO IDs across lines, comma-separated
    return_reasons           text,        -- distinct reasons across lines, comma-separated
    status                   text,        -- INITIATED or COMPLETED
    status_time              timestamptz,
    current_delivery_status  text,        -- NOT_SHIPPED, IN_TRANSIT, DELIVERED_TO_RC
    current_refund_status    text,        -- NOT_REFUNDED, REFUND_INITIATED, REFUND_COMPLETED, CANCELLED
    credit_note_status       text        NOT NULL DEFAULT 'Not Posted',
    raw_json                 jsonb,
    fetched_at               timestamptz,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_walmart_returns_status
    ON walmart_returns(status);

CREATE INDEX IF NOT EXISTS idx_walmart_returns_credit_note_status
    ON walmart_returns(credit_note_status);

CREATE INDEX IF NOT EXISTS idx_walmart_returns_return_order_date
    ON walmart_returns(return_order_date DESC);
