-- The latest winner announcement of a project is the one with the highest seqNo in its e-GP link
-- (…&temp_Announ=D&temp_itemNo=1&seqNo=2). Ordering by publication day and then by when we first saw it put
-- a W1 (fetched on Fridays only) after a W0 re-announced the same day: on 2 Nov 2026, 165 projects (44 in
-- our categories, one an OfficeMate win) showed "winner withdrawn" although a newer W0 named the winner.
create or replace function project_award(p_project text)
returns table(awarded boolean, withdrawn boolean, winners text, winner_price numeric, ours boolean,
              at timestamptz, changed boolean, first_winners text)
language sql
stable
set search_path = ''
as $$
  with ev as (
    select a.announce_type as t, a.pub_date, a.first_seen_at, a.id,
           (substring(a.link from 'seqNo=(\d+)'))::integer as seq,
           (select string_agg(x, ', ') from jsonb_array_elements_text(
              case when jsonb_typeof(d.extracted -> 'winners') = 'array' then d.extracted -> 'winners' end) x) as winners,
           (d.extracted ->> 'winner_price')::numeric as price
    from public.announcements a
    left join public.announcement_details d on d.announcement_id = a.id
    where a.project_no = p_project and a.announce_type in ('W0', 'W1', 'W2')
  ),
  last as (select * from ev order by seq desc nulls last, pub_date desc nulls last, first_seen_at desc, id desc limit 1)
  select l.t <> 'W1', l.t = 'W1',
         case when l.t <> 'W1' then nullif(l.winners, '') end,
         case when l.t <> 'W1' then l.price end,
         l.t <> 'W1' and replace(coalesce(l.winners, ''), 'ํา', 'ำ') ~ 'ออฟฟิศ\s*เมท',
         l.first_seen_at,
         exists (select 1 from ev where t in ('W1', 'W2')),
         (select nullif(winners, '') from ev where t = 'W0' and winners is not null
          order by seq nulls last, pub_date, first_seen_at, id limit 1)
  from last l;
$$;

-- B2: rows stored from the nationwide feed before 30 Oct 2026 have no department. Where another
-- announcement of the same project names it, copy it over (89 rows on 2 Nov 2026).
update announcements a
set dept_code = s.dept_code, dept_name = s.dept_name
from (select distinct on (project_no) project_no, dept_code, dept_name
      from announcements
      where dept_code is not null and project_no is not null
      order by project_no, id desc) s
where a.dept_code is null and a.project_no = s.project_no;
