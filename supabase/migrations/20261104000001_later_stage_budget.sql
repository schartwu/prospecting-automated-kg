-- Whether a later stage of the project already has a budget (D0 > 15 > B0 > P0, as project_budget()).
-- enrich.py skips reading a scan when it does: the latest-stage rule would never show the scan's number.
-- On 4 Nov 2026, 26 of the 27 scanned reference prices (15) waiting for Flash had a D0 with a budget.
create or replace function public.later_stage_budget(p_project text, p_type text)
returns boolean
language sql
stable
set search_path to ''
as $function$
  select exists (
    select 1 from public.announcements a
      join public.announcement_details d on d.announcement_id = a.id
     where a.project_no = p_project and d.budget_thb is not null
       and array_position(array['D0','15','B0','P0'], a.announce_type)
           < coalesce(array_position(array['D0','15','B0','P0'], p_type), 0));
$function$;
