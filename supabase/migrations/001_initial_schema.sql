-- ===========================================================================
-- Whisp — initial schema
-- Apply in the Supabase SQL editor (or `supabase db push`).
--
-- Design notes:
--  * Every table has RLS ENABLED with NO permissive policies, so the anon/
--    authenticated roles are denied by default. Trusted servers (the API and
--    the worker) use the SERVICE_ROLE key, which BYPASSES RLS by design.
--  * Job claiming is atomic via claim_next_question() using
--    FOR UPDATE SKIP LOCKED, with lease reclamation for crashed workers.
--  * Cluster embeddings are stored as double precision[] so pgvector is NOT
--    required. (pgvector is documented as an optional upgrade in ARCHITECTURE.)
-- ===========================================================================

create extension if not exists "pgcrypto";  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- events
-- ---------------------------------------------------------------------------
create table if not exists public.events (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    join_code   text not null unique,
    active      boolean not null default true,
    created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- rounds  (a host prompt / question round; only one open per event by convention)
-- ---------------------------------------------------------------------------
create table if not exists public.rounds (
    id                      uuid primary key default gen_random_uuid(),
    event_id                uuid not null references public.events(id) on delete cascade,
    prompt                  text,
    status                  text not null default 'open'
                            check (status in ('open', 'closed')),
    opened_at               timestamptz not null default now(),
    closed_at               timestamptz,
    -- set by the admin "recluster" action; the worker reclusters when this is
    -- newer than reclustered_at.
    recluster_requested_at  timestamptz,
    reclustered_at          timestamptz
);
create index if not exists rounds_event_idx on public.rounds(event_id);
create index if not exists rounds_open_idx on public.rounds(event_id) where status = 'open';

-- ---------------------------------------------------------------------------
-- badges
-- ---------------------------------------------------------------------------
create table if not exists public.badges (
    id            text not null,
    event_id      uuid references public.events(id) on delete set null,
    display_alias text,
    last_seen_at  timestamptz,
    created_at    timestamptz not null default now(),
    primary key (id)
);

-- ---------------------------------------------------------------------------
-- clusters  (semantic grouping of questions within a round)
-- ---------------------------------------------------------------------------
create table if not exists public.clusters (
    id                 uuid primary key default gen_random_uuid(),
    round_id           uuid references public.rounds(id) on delete cascade,
    canonical_question text not null,
    question_count     integer not null default 0,
    embedding          double precision[],           -- centroid/canonical vector
    status             text not null default 'open'
                       check (status in ('open', 'answered')),
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    answered_at        timestamptz
);
create index if not exists clusters_round_idx on public.clusters(round_id);

-- ---------------------------------------------------------------------------
-- questions
-- ---------------------------------------------------------------------------
create table if not exists public.questions (
    id                 uuid primary key default gen_random_uuid(),
    event_id           uuid references public.events(id) on delete set null,
    round_id           uuid references public.rounds(id) on delete set null,
    badge_id           text,
    status             text not null default 'queued'
                       check (status in ('queued','claimed','transcribing','done','empty','error')),
    audio_storage_path text,
    transcript         text,
    language           text,
    provider_used      text,
    fallback_used      boolean not null default false,
    processing_ms      integer,
    worker_id          text,
    lease_expires_at   timestamptz,
    cluster_id         uuid references public.clusters(id) on delete set null,
    error_code         text,
    safe_error_message text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    answered_at        timestamptz
);
create index if not exists questions_status_idx on public.questions(status, created_at);
create index if not exists questions_round_idx on public.questions(round_id);
create index if not exists questions_cluster_idx on public.questions(cluster_id);

-- ---------------------------------------------------------------------------
-- transcription_attempts  (one row per provider attempt; audit trail)
-- ---------------------------------------------------------------------------
create table if not exists public.transcription_attempts (
    id                 uuid primary key default gen_random_uuid(),
    question_id        uuid not null references public.questions(id) on delete cascade,
    provider           text not null,
    attempt_order      integer not null,
    status             text not null,               -- success | empty | error | timeout | skipped
    started_at         timestamptz,
    finished_at        timestamptz,
    latency_ms         integer,
    safe_error_code    text,
    safe_error_message text,
    provider_metadata  jsonb not null default '{}'::jsonb,   -- NO secrets
    created_at         timestamptz not null default now()
);
create index if not exists attempts_question_idx on public.transcription_attempts(question_id);

-- ---------------------------------------------------------------------------
-- cluster_members
-- ---------------------------------------------------------------------------
create table if not exists public.cluster_members (
    cluster_id        uuid not null references public.clusters(id) on delete cascade,
    question_id       uuid not null references public.questions(id) on delete cascade,
    cosine_similarity double precision,
    created_at        timestamptz not null default now(),
    primary key (cluster_id, question_id)
);

-- ---------------------------------------------------------------------------
-- worker_heartbeats
-- ---------------------------------------------------------------------------
create table if not exists public.worker_heartbeats (
    worker_id         text primary key,
    version           text,
    transcription_mode text,
    status            text,
    last_seen_at      timestamptz not null default now()
);

-- ===========================================================================
-- updated_at trigger
-- ===========================================================================
create or replace function public.touch_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists questions_touch on public.questions;
create trigger questions_touch before update on public.questions
    for each row execute function public.touch_updated_at();

drop trigger if exists clusters_touch on public.clusters;
create trigger clusters_touch before update on public.clusters
    for each row execute function public.touch_updated_at();

-- ===========================================================================
-- claim_next_question — atomic, crash-safe job claim.
--   * picks the oldest row that is queued, OR whose lease expired while
--     claimed/transcribing (crashed worker reclamation);
--   * FOR UPDATE SKIP LOCKED so concurrent workers never collide;
--   * marks it claimed with a fresh lease + worker id;
--   * returns the full row (or nothing if the queue is empty).
-- Invoke via supabase.rpc('claim_next_question', {p_worker_id, p_lease_seconds}).
-- ===========================================================================
create or replace function public.claim_next_question(
    p_worker_id text,
    p_lease_seconds integer default 120
)
returns setof public.questions
language plpgsql
as $$
declare
    v_id uuid;
begin
    select q.id into v_id
    from public.questions q
    where q.status = 'queued'
       or (q.status in ('claimed','transcribing') and q.lease_expires_at < now())
    order by q.created_at
    for update skip locked
    limit 1;

    if v_id is null then
        return;
    end if;

    return query
    update public.questions
       set status = 'claimed',
           worker_id = p_worker_id,
           lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     where id = v_id
    returning *;
end;
$$;

-- ===========================================================================
-- add_question_to_cluster — transactionally attach a question to a cluster and
-- recompute the cluster's member count. Optionally updates the centroid vector.
-- ===========================================================================
create or replace function public.add_question_to_cluster(
    p_cluster_id uuid,
    p_question_id uuid,
    p_similarity double precision,
    p_embedding double precision[] default null
)
returns integer
language plpgsql
as $$
declare
    v_count integer;
begin
    insert into public.cluster_members (cluster_id, question_id, cosine_similarity)
    values (p_cluster_id, p_question_id, p_similarity)
    on conflict (cluster_id, question_id) do update
        set cosine_similarity = excluded.cosine_similarity;

    select count(*) into v_count
    from public.cluster_members where cluster_id = p_cluster_id;

    update public.clusters
       set question_count = v_count,
           embedding = coalesce(p_embedding, embedding),
           updated_at = now()
     where id = p_cluster_id;

    update public.questions
       set cluster_id = p_cluster_id
     where id = p_question_id;

    return v_count;
end;
$$;

-- ===========================================================================
-- Row Level Security: lock every table. No policies => anon/authenticated are
-- denied. The service_role key (server/worker only) bypasses RLS.
-- ===========================================================================
alter table public.events                 enable row level security;
alter table public.rounds                 enable row level security;
alter table public.badges                 enable row level security;
alter table public.questions              enable row level security;
alter table public.transcription_attempts enable row level security;
alter table public.clusters               enable row level security;
alter table public.cluster_members        enable row level security;
alter table public.worker_heartbeats      enable row level security;

-- ===========================================================================
-- Storage bucket for private WAV audio.
-- (If your project restricts storage.buckets inserts, create the bucket in the
--  Dashboard: Storage → New bucket → name "whisp-audio", Public = OFF.)
-- ===========================================================================
insert into storage.buckets (id, name, public)
values ('whisp-audio', 'whisp-audio', false)
on conflict (id) do nothing;
