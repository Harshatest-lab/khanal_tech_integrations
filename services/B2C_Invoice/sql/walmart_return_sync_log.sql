CREATE TABLE IF NOT EXISTS walmart_return_sync_log (
    id                       bigserial   PRIMARY KEY,
    status                   text        NOT NULL,   -- 'success' or 'failed'
    sync_mode                text,                   -- 'backfill' or 'sync'
    from_date                timestamptz,
    to_date                  timestamptz,
    record_count             integer     NOT NULL DEFAULT 0,
    max_status_time_ms       bigint,
    next_last_modified_start text,
    message                  text,
    created_at               timestamptz NOT NULL DEFAULT now()
);
