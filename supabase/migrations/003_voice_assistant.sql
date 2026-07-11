-- ===========================================================================
-- Migration 003 — Voice assistant (chatbot answers for spoken questions).
--
-- FORWARD-ONLY and NON-DESTRUCTIVE. Safe to run on a project already migrated
-- with 001_initial_schema.sql (+ 002_rebrand_persephone.sql). Do NOT edit 001 or
-- 002 — they are historical and already applied.
--
-- What this migration adds:
--   * public.assistant_responses — at most ONE chatbot answer per question
--     (question_id is UNIQUE). RLS enabled, no permissive policies (service_role
--     only, like every other table). No provider credentials are ever stored.
--   * claim_next_assistant_response() — atomic, crash-safe job claim mirroring
--     claim_next_question() (FOR UPDATE SKIP LOCKED + lease reclamation).
--   * enqueue_missing_assistant_responses() — idempotent reconciliation: ensures
--     every done question with a non-empty transcript has exactly one queued
--     answer job (crash-safe; survives a worker dying after transcription).
--   * enqueue_assistant_response() / requeue_assistant_response() — the atomic
--     primitives behind the host "generate / retry / regenerate" actions. All
--     are idempotent and cannot create duplicate jobs (question_id is unique).
--
-- What it deliberately does NOT do:
--   * It does not modify or drop any existing table, function, index, or bucket.
--   * It does not touch questions, transcripts, clusters, or audio. A chatbot
--     failure is fully independent of transcription success.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- assistant_responses  (one queued/generating/done/error answer per question)
-- ---------------------------------------------------------------------------
create table if not exists public.assistant_responses (
    id                 uuid primary key default gen_random_uuid(),
    -- UNIQUE: the active answer for a question. Regeneration resets this row in
    -- place (it is never duplicated), so "exactly one answer per question" holds.
    question_id        uuid not null unique
                       references public.questions(id) on delete cascade,
    round_id           uuid references public.rounds(id) on delete set null,
    status             text not null default 'queued'
                       check (status in ('queued','generating','done','error')),
    response_text      text,
    provider           text,                 -- e.g. 'ollama' | 'openai_compatible' | 'mock'
    model              text,                 -- e.g. 'llama3.2:3b' (NOT a secret)
    processing_ms      integer,
    safe_error_message text,                 -- public-safe; never raw provider output
    attempt_count      integer not null default 0,
    worker_id          text,
    lease_expires_at   timestamptz,
    created_at         timestamptz not null default now(),
    started_at         timestamptz,
    completed_at       timestamptz,
    updated_at         timestamptz not null default now()
);
create index if not exists assistant_responses_status_idx
    on public.assistant_responses(status, created_at);
create index if not exists assistant_responses_round_idx
    on public.assistant_responses(round_id);
-- question_id is already indexed by its UNIQUE constraint.

-- Reuse the shared updated_at trigger from 001.
drop trigger if exists assistant_responses_touch on public.assistant_responses;
create trigger assistant_responses_touch before update on public.assistant_responses
    for each row execute function public.touch_updated_at();

-- ===========================================================================
-- claim_next_assistant_response — atomic, crash-safe answer-job claim.
--   * picks the oldest row that is queued, OR whose lease expired while
--     generating (crashed-worker reclamation);
--   * FOR UPDATE SKIP LOCKED so concurrent workers never claim the same row;
--   * marks it generating with a fresh lease + worker id, increments the attempt
--     counter, and stamps started_at on the first attempt;
--   * returns the full row (or nothing if there is no work).
-- The per-attempt increment lets the worker enforce a retry ceiling and lets a
-- crash-looped row (attempt_count past the ceiling) be failed rather than retried
-- forever. Invoke via
--   supabase.rpc('claim_next_assistant_response', {p_worker_id, p_lease_seconds}).
-- ===========================================================================
create or replace function public.claim_next_assistant_response(
    p_worker_id text,
    p_lease_seconds integer default 90
)
returns setof public.assistant_responses
language plpgsql
as $$
declare
    v_id uuid;
begin
    select ar.id into v_id
    from public.assistant_responses ar
    where ar.status = 'queued'
       or (ar.status = 'generating' and ar.lease_expires_at < now())
    order by ar.created_at
    for update skip locked
    limit 1;

    if v_id is null then
        return;
    end if;

    return query
    update public.assistant_responses
       set status = 'generating',
           worker_id = p_worker_id,
           lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           started_at = coalesce(started_at, now()),
           attempt_count = attempt_count + 1
     where id = v_id
    returning *;
end;
$$;

-- ===========================================================================
-- enqueue_missing_assistant_responses — idempotent reconciliation.
-- Ensures every done question with a non-empty transcript has exactly one queued
-- answer job. Safe to call repeatedly (ON CONFLICT DO NOTHING + NOT EXISTS +
-- the UNIQUE(question_id) constraint make duplicates impossible). Returns the
-- number of jobs newly enqueued. The worker calls this ONLY when chatbot
-- generation is enabled, so disabled mode enqueues nothing.
-- ===========================================================================
create or replace function public.enqueue_missing_assistant_responses(
    p_limit integer default 50
)
returns integer
language plpgsql
as $$
declare
    v_count integer;
begin
    with ins as (
        insert into public.assistant_responses (question_id, round_id, status)
        select q.id, q.round_id, 'queued'
        from public.questions q
        where q.status = 'done'
          and q.transcript is not null
          and length(btrim(q.transcript)) > 0
          and not exists (
              select 1 from public.assistant_responses ar
              where ar.question_id = q.id
          )
        order by q.created_at
        limit greatest(p_limit, 0)
        on conflict (question_id) do nothing
        returning 1
    )
    select count(*) into v_count from ins;
    return coalesce(v_count, 0);
end;
$$;

-- ===========================================================================
-- enqueue_assistant_response — create the queued answer job for ONE question if
-- it does not exist yet (host "generate" action). Idempotent: repeated calls
-- return the same row and never create duplicates. Returns the row, or nothing
-- if the question id is unknown (the API maps that to 404).
-- ===========================================================================
create or replace function public.enqueue_assistant_response(
    p_question_id uuid
)
returns setof public.assistant_responses
language plpgsql
as $$
begin
    insert into public.assistant_responses (question_id, round_id, status)
    select q.id, q.round_id, 'queued'
    from public.questions q
    where q.id = p_question_id
    on conflict (question_id) do nothing;

    return query
    select * from public.assistant_responses where question_id = p_question_id;
end;
$$;

-- ===========================================================================
-- requeue_assistant_response — reset an existing answer back to queued so the
-- worker regenerates it (host "retry" / "regenerate" actions). Creates the job
-- first if it is missing (so retry/regenerate also work on a never-generated
-- question). The reset only fires when the current status is in p_from_states,
-- so an in-flight ('generating') job is never disturbed:
--   * retry      -> p_from_states = {'error'}
--   * regenerate -> p_from_states = {'done','error'}
-- Idempotent; returns the resulting row (or nothing if the question is unknown).
-- ===========================================================================
create or replace function public.requeue_assistant_response(
    p_question_id uuid,
    p_from_states text[]
)
returns setof public.assistant_responses
language plpgsql
as $$
begin
    -- Create the job if this question has never been queued.
    insert into public.assistant_responses (question_id, round_id, status)
    select q.id, q.round_id, 'queued'
    from public.questions q
    where q.id = p_question_id
    on conflict (question_id) do nothing;

    -- Reset an existing terminal row back to queued.
    update public.assistant_responses
       set status = 'queued',
           response_text = null,
           safe_error_message = null,
           provider = null,
           model = null,
           processing_ms = null,
           attempt_count = 0,
           worker_id = null,
           lease_expires_at = null,
           started_at = null,
           completed_at = null,
           updated_at = now()
     where question_id = p_question_id
       and status = any(p_from_states);

    return query
    select * from public.assistant_responses where question_id = p_question_id;
end;
$$;

-- ===========================================================================
-- Row Level Security: lock the table (no policies => anon/authenticated denied).
-- The trusted API + worker use the service_role key, which bypasses RLS.
-- ===========================================================================
alter table public.assistant_responses enable row level security;

-- ===========================================================================
-- service_role grants (idempotent; explicit so a tightened project still works).
-- ===========================================================================
grant select, insert, update, delete on table public.assistant_responses to service_role;

grant execute on function public.claim_next_assistant_response(text, integer) to service_role;
grant execute on function public.enqueue_missing_assistant_responses(integer) to service_role;
grant execute on function public.enqueue_assistant_response(uuid) to service_role;
grant execute on function public.requeue_assistant_response(uuid, text[]) to service_role;
