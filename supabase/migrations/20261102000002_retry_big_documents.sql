-- Documents skipped by the old 15 MB download cap (5 relevant B0 drafts by 2 Nov 2026) are read again now
-- that egp/docs.py allows 80 MB. Only that exact error is retried, so a file over 80 MB is not read nightly.
create or replace function announcements_to_enrich(p_types text[], p_min_score integer, p_limit integer)
returns table(id bigint, announce_type text, link text, pub_date date, title text, dept_code text,
              dept_needs_name boolean, plan_no text)
language sql
stable
set search_path = ''
as $$
  select a.id, a.announce_type, a.link, a.pub_date, a.title, a.dept_code,
         coalesce(dep.name like 'หน่วยงานรหัส %', false), a.plan_no
  from public.announcements a
  join public.classifications c on c.announcement_id = a.id
  left join public.announcement_details d on d.announcement_id = a.id
  left join public.departments dep on dep.code = a.dept_code
  where (d.announcement_id is null or d.error = 'ValueError: document larger than 15 MB')
    and c.relevant and c.score >= p_min_score
    and a.announce_type = any (p_types)
    and a.first_seen_at > now() - interval '30 days'
  order by array_position(array['D0','B0','15','P0','W0'], a.announce_type), c.score desc
  limit p_limit;
$$;
