-- ติดตามสถานะ shows the same budget as the Lead list and the project page: the latest stage that states one
-- (ประกาศเชิญชวน, ราคากลาง, ร่าง TOR, แผน) instead of the largest of all announcements.
create or replace function web_tracking()
returns setof jsonb
language sql
stable
set search_path = ''
as $$
  with links as (select * from public.plan_links()),
  evk as (
    select e.*, coalesce(l.project_no, e.lead_key) as k
    from public.lead_events e left join links l on e.lead_key = 'P:' || l.plan_no
  ),
  ev as (
    select k as lead_key, count(*)::int as n_events from evk group by k
  ),
  last_ev as (
    select distinct on (e.k) e.k as lead_key, e.status, e.created_at, e.bid_price, e.reasons, e.note, u.name
    from evk e join public.lead_users u on u.id = e.user_id
    order by e.k, e.created_at desc
  ),
  ours as (
    -- projects OfficeMate holds now (latest winner announcement names us)
    select p.project_no, w.at
    from (select distinct a.project_no from public.announcements a
          where a.announce_type in ('W0', 'W2') and a.project_no is not null
            and exists (select 1 from public.announcement_details d where d.announcement_id = a.id
                        and replace(d.extracted ->> 'winners', 'ํา', 'ำ') ~ 'ออฟฟิศ\s*เมท')) p
    cross join lateral public.project_award(p.project_no) w
    where w.ours
  ),
  tracked as (
    select ev.lead_key, ev.n_events, l.status, l.created_at as status_at, l.name as status_by, l.bid_price,
           l.reasons, l.note, false as auto_result
    from ev join last_ev l on l.lead_key = ev.lead_key
    union all
    -- OfficeMate won a project that went out as a lead, and nobody pressed a status on it
    select o.project_no, 0, null, o.at, null, null, null, null, true
    from ours o
    where not exists (select 1 from ev where ev.lead_key = o.project_no)
      and exists (select 1 from public.announcements x
                  join public.classifications c on c.announcement_id = x.id and c.relevant and c.score >= 50
                  where x.project_no = o.project_no and x.announce_type in ('D0', 'B0', '15'))
  )
  select jsonb_build_object(
    'lead_key', t.lead_key, 'n_events', t.n_events, 'auto_result', t.auto_result,
    'status', t.status, 'status_at', t.status_at, 'status_by', t.status_by, 'bid_price', t.bid_price,
    'reasons', t.reasons, 'note', t.note,
    'title', info.title, 'dept_name', info.dept_name, 'type', info.announce_type, 'link', info.link,
    'method', info.method,
    'linked_plan', (select lk.plan_no from links lk where lk.project_no = t.lead_key),
    'budget_thb', (select d.budget_thb from public.announcements a
                     join public.announcement_details d on d.announcement_id = a.id
                    where ((a.project_no = t.lead_key) or ('P:' || a.plan_no = t.lead_key))
                      and d.budget_thb is not null and a.announce_type in ('D0', '15', 'B0', 'P0')
                    order by array_position(array['D0','15','B0','P0'], a.announce_type), a.pub_date desc
                    limit 1),
    'bid_date', (select max(d.bid_date) from public.announcements a
                   join public.announcement_details d on d.announcement_id = a.id
                  where a.project_no = t.lead_key and a.announce_type = 'D0'),
    'province', (select min(d.extracted ->> 'province') from public.announcements a
                   join public.announcement_details d on d.announcement_id = a.id
                  where (a.project_no = t.lead_key) or ('P:' || a.plan_no = t.lead_key)),
    'cancelled', exists (select 1 from public.announcements a
                         where a.project_no = t.lead_key and a.announce_type = 'D1'),
    'awarded', coalesce(w.awarded, false), 'award_changed', coalesce(w.changed, false),
    'prev_winners', case when w.changed then w.first_winners end,
    'winners', w.winners, 'winner_price', w.winner_price,
    'result', case when not coalesce(w.awarded, false) or w.winners is null then null
                   when w.ours then 'win'
                   when exists (select 1 from evk x where x.k = t.lead_key and x.status = 'bid')
                     then 'lost'
                   else 'other_won' end)
  from tracked t
  left join lateral (
    select a.title, a.dept_name, a.announce_type, a.link, a.method from public.announcements a
    where ((a.project_no = t.lead_key) or ('P:' || a.plan_no = t.lead_key))
      and a.announce_type in ('D0', 'B0', '15', 'P0')
    order by array_position(array['D0','B0','15','P0'], a.announce_type) limit 1) info on true
  left join lateral public.project_award(t.lead_key) w on t.lead_key not like 'P:%';
$$;
