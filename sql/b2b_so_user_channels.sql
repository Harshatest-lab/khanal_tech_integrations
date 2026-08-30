-- Assign marketplace channels per user. Admin still sees every channel in the UI.
ALTER TABLE b2b.users
  ADD COLUMN IF NOT EXISTS channels text[] NOT NULL DEFAULT ARRAY['blinkit']::text[];
