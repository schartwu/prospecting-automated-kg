-- Keep announcements nobody needs for 30 days instead of 60 (decided 2 Nov 2026): with local governments,
-- universities and purchasing units added, 60 days would fill the 500 MB Free plan around Feb 2027.
-- Before a row is deleted it is emailed as CSV (scripts/archive.py, archive.yml, Saturday 15:00 Bangkok),
-- and cleanup_old_data deletes only rows already archived, so a failed email delays the deletion
-- instead of losing data.
alter table announcements add column if not exists archived_at timestamptz;

-- The rows cleanup_old_data will delete: not relevant, and not a cancellation/change of a relevant project.
create or replace function droppable(a public.announcements)
returns boolean
language sql
stable
set search_path = ''
as $$
  select not exists (select 1 from public.classifications c where c.announcement_id = a.id and c.relevant)
     and not (a.announce_type in ('D1', 'D2', 'W1', 'W2') and a.project_no is not null
              and exists (select 1 from public.announcements k
                          join public.classifications c on c.announcement_id = k.id and c.relevant
                          where k.project_no = a.project_no));
$$;

-- One page of rows due for deletion within the next week (older than 23 days), not archived yet,
-- as a JSON array (a plain table response stops at 1,000 rows).
create or replace function archive_page(p_after bigint, p_limit integer)
returns jsonb
language sql
stable
set search_path = ''
as $$
  select coalesce(jsonb_agg(r order by r.id), '[]'::jsonb) from (
    select a.id, a.announce_type, a.project_no, a.plan_no, a.method, a.title, a.link, a.pub_date,
           a.dept_code, a.dept_name, a.first_seen_at, c.score, c.category, c.reason
    from public.announcements a
    left join public.classifications c on c.announcement_id = a.id
    where a.id > p_after
      and a.archived_at is null
      and a.first_seen_at < now() - interval '23 days'
      and public.droppable(a)
    order by a.id
    limit p_limit) r;
$$;

create or replace function mark_archived(p_ids bigint[])
returns integer
language sql
set search_path = ''
as $$
  with u as (update public.announcements set archived_at = now() where id = any (p_ids) returning 1)
  select count(*)::integer from u;
$$;

create or replace function cleanup_old_data()
returns jsonb
language plpgsql
set search_path = ''
as $$
declare v_deleted integer; v_raw_cleared integer; v_ai integer; v_visits integer;
begin
  delete from public.announcements a
  where a.first_seen_at < now() - interval '30 days'
    and a.archived_at is not null
    and public.droppable(a);
  get diagnostics v_deleted = row_count;
  update public.announcements set raw = '{}'::jsonb
  where first_seen_at < now() - interval '14 days' and raw <> '{}'::jsonb;
  get diagnostics v_raw_cleared = row_count;
  delete from public.ai_usage where day < current_date - 30;
  get diagnostics v_ai = row_count;
  delete from public.lead_visits where at < now() - interval '180 days';
  get diagnostics v_visits = row_count;
  delete from public.lead_presence where last_seen < now() - interval '30 days';
  return jsonb_build_object('announcements_deleted', v_deleted, 'raw_cleared', v_raw_cleared,
    'ai_usage_deleted', v_ai, 'visits_deleted', v_visits,
    'db_size', pg_size_pretty(pg_database_size(current_database())));
end; $$;

revoke execute on function droppable(public.announcements) from public, anon, authenticated;
revoke execute on function archive_page(bigint, integer) from public, anon, authenticated;
revoke execute on function mark_archived(bigint[]) from public, anon, authenticated;

-- Saturday 15:00 Bangkok, before egp-weekly-cleanup (Sunday 02:00 Bangkok).
select cron.schedule('egp-weekly-archive', '0 8 * * 6', $$select public.trigger_workflow('archive.yml')$$);
