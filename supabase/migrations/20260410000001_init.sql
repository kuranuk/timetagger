-- TimeTagger initial schema.
--
-- Mirrors the three per-user itemdb tables (records / settings / userinfo)
-- as one shared table each, partitioned by `username`. Fields that the API
-- in timetagger/server/_apiserver.py queries directly (t1, t2, ds) are
-- promoted from JSONB to real columns so queries translate cleanly.
--
-- Applied via `supabase db push` or `python scripts/migrate.py`.

create table if not exists public.records (
  username text             not null,
  key      text             not null,
  st       double precision not null,
  mt       bigint           not null,
  t1       bigint           not null,
  t2       bigint           not null,
  ds       text,
  ob       jsonb            not null,
  primary key (username, key)
);
create index if not exists records_st_idx on public.records (username, st);
create index if not exists records_t1_idx on public.records (username, t1);
create index if not exists records_t2_idx on public.records (username, t2);

create table if not exists public.settings (
  username text             not null,
  key      text             not null,
  st       double precision not null,
  mt       bigint           not null,
  ob       jsonb            not null,
  primary key (username, key)
);
create index if not exists settings_st_idx on public.settings (username, st);

create table if not exists public.userinfo (
  username text             not null,
  key      text             not null,
  st       double precision not null,
  mt       bigint           not null,
  ob       jsonb            not null,
  primary key (username, key)
);

-- Replaces itemdb's file-mtime-based `db.mtime` used by the /updates fast path
-- at timetagger/server/_apiserver.py:328. Written on every mutation.
create table if not exists public.db_meta (
  username text primary key,
  mtime    double precision not null
);

-- TimeTagger manages auth itself (bcrypt credentials + JWT). These tables are
-- accessed exclusively through the service-role connection from the Vercel
-- Python Function, never directly from the browser. Enable RLS with no policies
-- so anon/authenticated keys cannot reach the data even if they are ever
-- exposed accidentally.
alter table public.records   enable row level security;
alter table public.settings  enable row level security;
alter table public.userinfo  enable row level security;
alter table public.db_meta   enable row level security;
