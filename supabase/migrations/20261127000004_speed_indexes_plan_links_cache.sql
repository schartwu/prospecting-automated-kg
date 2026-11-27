-- KG Lead speed (27 Nov 2026), part 1: indexes and a stored copy of plan_links().
--
-- Every list and lead page computed plan_links() again (all plans × all projects with a budget), and
-- several lookups by plan number or by email day read the whole announcements table because no index
-- covered them. On this small database that took seconds a call.
--
-- Indexes change no result. The stored copy is read only by the *_v2 functions (next migration), which
-- only the UAT function lead-preview calls until the release is approved.

create index if not exists announcements_plan_no_idx on public.announcements (plan_no) where plan_no is not null;
create index if not exists announcements_type_seen_idx on public.announcements (announce_type, first_seen_at);
create index if not exists digests_kind_sent_idx on public.digests (kind, sent_at);

-- plan_links() as it was at the last refresh: plans and projects only change when the pipeline loads or
-- reads documents, so a copy refreshed every 10 minutes gives the same pairs.
create table if not exists public.plan_links_cache (
  plan_no text not null,
  project_no text not null,
  primary key (plan_no, project_no)
);
create index if not exists plan_links_cache_project_idx on public.plan_links_cache (project_no);
alter table public.plan_links_cache enable row level security;
revoke all on public.plan_links_cache from anon, authenticated;

-- Rewrites the copy only when the pairs changed. Returns the number of pairs.
create or replace function public.refresh_plan_links()
returns integer
language plpgsql
set search_path = ''
as $$
declare n integer;
begin
  create temp table if not exists _plan_links_now (plan_no text, project_no text) on commit drop;
  truncate _plan_links_now;
  insert into _plan_links_now select l.plan_no, l.project_no from public.plan_links() l;
  if exists (select plan_no, project_no from _plan_links_now except select plan_no, project_no from public.plan_links_cache)
     or exists (select plan_no, project_no from public.plan_links_cache except select plan_no, project_no from _plan_links_now) then
    delete from public.plan_links_cache;
    insert into public.plan_links_cache (plan_no, project_no) select distinct plan_no, project_no from _plan_links_now;
  end if;
  select count(*) into n from public.plan_links_cache;
  return n;
end;
$$;
revoke execute on function public.refresh_plan_links() from public, anon, authenticated;
grant execute on function public.refresh_plan_links() to service_role;

select public.refresh_plan_links();

select cron.schedule('kg-plan-links-refresh', '*/10 * * * *', 'select public.refresh_plan_links()');
