-- 1. ผลลัพธ์ leaves out เฉพาะเจาะจง awards of projects that were never a Lead (decided 2 Nov 2026). They were
--    ~84% of the rows and the web page dropped them anyway; 12 months took 2.3 s warm and two loads hit the
--    8 s API statement timeout on 2 Nov 2026 12:13.
create or replace function web_outcomes(p_from date, p_to date)
returns setof jsonb
language sql
stable
set search_path = ''
as $$
  with leads as (
    select x.project_no, array_agg(distinct x.announce_type order by x.announce_type) as types,
           min(x.first_seen_at) as first_seen
    from public.announcements x
    join public.classifications c on c.announcement_id = x.id and c.relevant and c.score >= 50
    where x.announce_type in ('B0', '15', 'D0') and x.project_no is not null
    group by x.project_no
  ),
  project_info as (
    select x.project_no, min(d.extracted ->> 'province') as province, max(d.budget_thb) as budget_thb
    from public.announcements x
    join public.announcement_details d on d.announcement_id = x.id
    where x.project_no is not null
    group by x.project_no
  ),
  seen as (
    select distinct on (a.project_no) a.*
    from public.announcements a
    where a.announce_type in ('W0', 'W2')
      and a.first_seen_at >= (p_from - 10)::timestamp at time zone 'Asia/Bangkok'
      and a.first_seen_at < (p_to + 1)::timestamp at time zone 'Asia/Bangkok'
      and (coalesce(a.method, '') !~ 'เฉพาะเจาะจง' or exists (select 1 from leads l where l.project_no = a.project_no))
      and exists (select 1 from public.announcements y
                  join public.classifications c on c.announcement_id = y.id and c.relevant
                  where y.project_no = a.project_no)
    order by a.project_no, a.first_seen_at desc, a.id desc
  ),
  changed_seen as (
    select distinct on (a.project_no) a.*
    from public.announcements a
    where a.announce_type in ('W1', 'W2')
      and a.first_seen_at >= (p_from - 10)::timestamp at time zone 'Asia/Bangkok'
      and a.first_seen_at < (p_to + 1)::timestamp at time zone 'Asia/Bangkok'
      and (coalesce(a.method, '') !~ 'เฉพาะเจาะจง' or exists (select 1 from leads l where l.project_no = a.project_no))
      and exists (select 1 from public.announcements y
                  join public.classifications c on c.announcement_id = y.id and c.relevant
                  where y.project_no = a.project_no)
    order by a.project_no, a.pub_date desc, a.first_seen_at desc, a.id desc
  ),
  rows as (
    select 'result' as kind, a.project_no, a.title, a.dept_name, a.method,
           coalesce(d.extracted ->> 'province', pi.province) as province, a.pub_date,
           w.winners, w.winner_price, w.ours,
           case when l.first_seen < a.first_seen_at then l.types end as lead_types,
           pi.budget_thb, a.link, a.first_seen_at, w.changed, case when w.changed then w.first_winners end as prev_winners,
           false as withdrawn, null::text as change_type
    from seen a
    left join public.announcement_details d on d.announcement_id = a.id
    cross join lateral public.project_award(a.project_no) w
    left join leads l on l.project_no = a.project_no
    left join project_info pi on pi.project_no = a.project_no
    where w.awarded and w.winners is not null
    union all
    select 'changed', a.project_no, a.title, a.dept_name, a.method, pi.province, a.pub_date,
           w.winners, w.winner_price, w.ours, l.types, pi.budget_thb, a.link, a.first_seen_at, true,
           w.first_winners, w.withdrawn, a.announce_type
    from changed_seen a
    cross join lateral public.project_award(a.project_no) w
    left join leads l on l.project_no = a.project_no
    left join project_info pi on pi.project_no = a.project_no
    union all
    select 'cancelled', a.project_no, a.title, a.dept_name, a.method, pi.province, a.pub_date,
           null, null, false, l.types, pi.budget_thb, a.link, a.first_seen_at, false, null, false, null
    from public.announcements a
    join leads l on l.project_no = a.project_no
    left join project_info pi on pi.project_no = a.project_no
    where a.announce_type = 'D1'
      and a.first_seen_at >= (p_from - 10)::timestamp at time zone 'Asia/Bangkok'
      and a.first_seen_at < (p_to + 1)::timestamp at time zone 'Asia/Bangkok'
  )
  select to_jsonb(r) || jsonb_build_object('email_date', public.email_date(r.first_seen_at),
    'score', (select max(c.score) from public.announcements a
               join public.classifications c on c.announcement_id = a.id and c.relevant
              where a.project_no = r.project_no))
  from rows r
  where public.email_date(r.first_seen_at) between p_from and p_to;
$$;

-- 2. Leads stored from the nationwide feed with no department and no other announcement to borrow it from
--    (21 projects on 2 Nov 2026): enrich.py reads the name from the document heading, 30 a night.
create or replace function nationwide_to_name(p_limit integer)
returns table(id bigint, project_no text, link text)
language sql
stable
set search_path = ''
as $$
  select a.id, a.project_no, a.link
  from public.announcements a
  join public.classifications c on c.announcement_id = a.id and c.relevant and c.score >= 50
  where a.dept_code is null and a.announce_type in ('D0', 'B0', '15')
    and coalesce(a.dept_name, '') in ('', '(ไม่ระบุหน่วยงาน)')
  order by md5(a.id::text || current_date::text)
  limit p_limit;
$$;

create or replace function set_announcement_dept_name(p_id bigint, p_project text, p_name text)
returns integer
language sql
set search_path = ''
as $$
  with u as (
    update public.announcements set dept_name = p_name
    where dept_code is null and coalesce(dept_name, '') in ('', '(ไม่ระบุหน่วยงาน)')
      and (id = p_id or (p_project is not null and project_no = p_project))
    returning 1)
  select count(*)::integer from u;
$$;

-- 4. The weekly cleanup keeps every announcement of a project that has a relevant one, whatever its type,
--    so a Lead's winner announcement classified differently is not deleted after 30 days.
create or replace function droppable(a public.announcements)
returns boolean
language sql
stable
set search_path = ''
as $$
  select not exists (select 1 from public.classifications c where c.announcement_id = a.id and c.relevant)
     and not (a.project_no is not null
              and exists (select 1 from public.announcements k
                          join public.classifications c on c.announcement_id = k.id and c.relevant
                          where k.project_no = a.project_no));
$$;

revoke execute on function nationwide_to_name(integer) from public, anon, authenticated;
revoke execute on function set_announcement_dept_name(bigint, text, text) from public, anon, authenticated;
