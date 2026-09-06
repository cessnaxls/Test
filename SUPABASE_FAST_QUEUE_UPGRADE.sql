alter table instagram_profiles add column if not exists index_status text not null default 'queued';
alter table instagram_profiles add column if not exists index_attempts integer not null default 0;
alter table instagram_profiles add column if not exists index_error text;
alter table instagram_profiles add column if not exists indexed_at timestamptz;

update instagram_profiles
set index_status = case when embedding is null then 'queued' else 'indexed' end
where index_status is null
   or (embedding is not null and index_status <> 'indexed');

create index if not exists instagram_profiles_queue_idx
  on instagram_profiles (index_status, last_seen)
  where embedding is null;
