-- ===========================================================================
-- Migration 002 — Persephone rebrand (product formerly named "Whisp").
--
-- FORWARD-ONLY and NON-DESTRUCTIVE. Safe to run on an existing project that was
-- created with 001_initial_schema.sql. Do NOT edit 001 — it is historical and
-- already applied.
--
-- What this migration does:
--   * Creates the new private Storage bucket 'persephone-audio' (idempotent).
--   * Leaves the old 'whisp-audio' bucket and all historical audio UNTOUCHED.
--   * Preserves RLS and every existing table's data.
--
-- What it deliberately does NOT do:
--   * It does not rename or drop any table, function, trigger, index, or bucket.
--     The table names (events, rounds, questions, clusters, badges,
--     worker_heartbeats, transcription_attempts, cluster_members) are generic
--     domain terms, not product branding, so they stay as-is.
--   * It does not move or delete objects already stored in 'whisp-audio'.
--     Historical WAVs remain readable there. New uploads default to
--     'persephone-audio' (application default; override with SUPABASE_AUDIO_BUCKET
--     if a deployment must temporarily keep using the old bucket).
-- ===========================================================================

-- Create the new private bucket (private == public:false). Idempotent.
-- If your project restricts storage.buckets inserts, create it in the Dashboard:
--   Storage → New bucket → name "persephone-audio", Public = OFF.
insert into storage.buckets (id, name, public)
values ('persephone-audio', 'persephone-audio', false)
on conflict (id) do nothing;

-- Belt-and-suspenders: ensure it is private even if a row pre-existed as public.
update storage.buckets set public = false where id = 'persephone-audio';

-- Storage access is governed by Supabase's storage.objects RLS. The trusted
-- server/worker uses the service_role key (which bypasses RLS), exactly as it
-- already does for 'whisp-audio'. No object or table data is modified here.
--
-- Historical note: audio uploaded before this rebrand remains in the private
-- 'whisp-audio' bucket. It is intentionally left in place (no data migration).
