create table if not exists public.clip_profiles (
  device_id text not null,
  username text not null,
  full_name text,
  profile_url text,
  source_url text,
  original_image_url text,
  thumbnail_b64 text,
  embedding jsonb,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  seen_count integer not null default 1,
  primary key (device_id, username)
);
create index if not exists clip_profiles_device_seen_idx on public.clip_profiles(device_id, last_seen desc);
alter table public.clip_profiles enable row level security;
-- The Render backend uses the Supabase service-role key, which bypasses RLS.
