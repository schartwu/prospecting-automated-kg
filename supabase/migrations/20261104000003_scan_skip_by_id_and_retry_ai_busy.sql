-- 1. enrich.py skips a scan when a later stage has the budget, but the queue (announcements_to_enrich)
--    does not return the project number, so the skip never happened. Checked by announcement id instead.
-- 2. Documents whose AI read failed with Google's 500/503 ("high demand") were saved with the error and
--    never read again: 14 W0 without winners and D0 69109256065 without a bid date (22 Oct - 4 Nov 2026).
--    The queue now takes them again (within the usual 30 days).
create or replace function public.later_stage_budget_of(p_id bigint)
returns boolean
language sql
stable
set search_path to ''
as $function$
  select coalesce((select public.later_stage_budget(a.project_no, a.announce_type)
                     from public.announcements a where a.id = p_id and a.project_no is not null), false);
$function$;
revoke execute on function public.later_stage_budget_of(bigint) from public, anon, authenticated;
grant execute on function public.later_stage_budget_of(bigint) to service_role;

CREATE OR REPLACE FUNCTION public.announcements_to_enrich(p_types text[], p_min_score integer, p_limit integer)
 RETURNS TABLE(id bigint, announce_type text, link text, pub_date date, title text, dept_code text, dept_needs_name boolean, plan_no text)
 LANGUAGE sql
 STABLE
 SET search_path TO ''
AS $function$
  select a.id, a.announce_type, a.link, a.pub_date, a.title, a.dept_code,
         coalesce(dep.name like 'หน่วยงานรหัส %', false), a.plan_no
  from public.announcements a
  join public.classifications c on c.announcement_id = a.id
  left join public.announcement_details d on d.announcement_id = a.id
  left join public.departments dep on dep.code = a.dept_code
  where (d.announcement_id is null or d.error = 'ValueError: document larger than 15 MB'
         or d.error ~ '^ai: .*HTTP 50[03]')
    and c.relevant and c.score >= p_min_score
    and a.announce_type = any (p_types)
    and a.first_seen_at > now() - interval '30 days'
  order by array_position(array['D0','B0','15','P0','W0'], a.announce_type), c.score desc
  limit p_limit;
$function$;
