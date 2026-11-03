-- One stage order everywhere: D0 > 15 > B0 > P0 (the latest stage a project reached first).
-- Budget = the reference price of the latest stage that has one (the rule web_tracking already used,
-- 2 Nov 2026). web_leads and digest_items took max(D0, B0 regex) first, so a project without a regex D0
-- fell back to its own row: e.g. 69059356869 showed 33,000 from a 15 that read the unit price, while its D0
-- says 23,100,000. The tracking title/link also preferred B0 over 15.

create or replace function public.project_budget(p_project text)
returns numeric
language sql
stable
set search_path to ''
as $function$
  select d.budget_thb from public.announcements a
    join public.announcement_details d on d.announcement_id = a.id
   where a.project_no = p_project and d.budget_thb is not null and a.announce_type in ('D0', '15', 'B0', 'P0')
   order by array_position(array['D0','15','B0','P0'], a.announce_type), a.pub_date desc
   limit 1;
$function$;

CREATE OR REPLACE FUNCTION public.digest_items(p_since timestamp with time zone, p_types text[], p_min_score integer DEFAULT 50)
 RETURNS TABLE(id bigint, announce_type text, project_no text, plan_no text, method text, title text, link text, pub_date date, dept_name text, first_seen_at timestamp with time zone, score smallint, category text, reason text, budget_thb numeric, bid_date date, bid_time text, expected_month text, cancelled boolean, amended boolean, stages text[], province text, winners text, winner_price numeric)
 LANGUAGE sql
 STABLE
 SET search_path TO ''
AS $function$
  select a.id, a.announce_type, a.project_no, a.plan_no, a.method, a.title, a.link,
         a.pub_date, a.dept_name, a.first_seen_at,
         c.score, c.category, c.reason,
         coalesce(
           public.project_budget(a.project_no),
           d.budget_thb,
           (select max(d2.budget_thb) from public.announcements x
              join public.announcement_details d2 on d2.announcement_id = x.id
             where x.project_no = a.project_no)) as budget_thb,
         d.bid_date, d.extracted ->> 'bid_time', d.extracted ->> 'expected_month',
         exists (select 1 from public.announcements x
                 where x.project_no = a.project_no and x.announce_type = 'D1'),
         exists (select 1 from public.announcements x
                 where x.project_no = a.project_no and x.announce_type = 'D2'),
         (select array_agg(distinct x.announce_type) from public.announcements x
           where x.project_no = a.project_no),
         coalesce(d.extracted ->> 'province',
                  (select min(d2.extracted ->> 'province') from public.announcements x
                     join public.announcement_details d2 on d2.announcement_id = x.id
                    where x.project_no = a.project_no)),
         (select string_agg(w, ', ') from jsonb_array_elements_text(
            case when jsonb_typeof(d.extracted -> 'winners') = 'array' then d.extracted -> 'winners'
                 else '[]'::jsonb end) w),
         (d.extracted ->> 'winner_price')::numeric
  from public.announcements a
  join public.classifications c on c.announcement_id = a.id
  left join public.announcement_details d on d.announcement_id = a.id
  where c.relevant
    and c.score >= p_min_score
    and a.first_seen_at >= p_since
    and a.announce_type = any (p_types)
  order by c.score desc, a.pub_date desc;
$function$;

CREATE OR REPLACE FUNCTION public.web_leads(p_from date, p_to date)
 RETURNS SETOF jsonb
 LANGUAGE sql
 STABLE
 SET search_path TO ''
AS $function$
  with links as (select * from public.plan_links()),
  base as (
    select a.id, a.announce_type, a.project_no, a.plan_no, a.method, a.title, a.link, a.pub_date, a.dept_name,
           c.score, c.category, c.reason, d.budget_thb as own_budget, d.bid_date, d.extracted,
           public.email_date(a.first_seen_at) as email_date,
           case when a.announce_type = 'P0' then coalesce(lp.project_no, 'P:' || a.plan_no) else a.project_no end as lead_key,
           case when a.announce_type = 'P0' then lp.plan_no else lj.plan_no end as linked_plan
    from public.announcements a
    join public.classifications c on c.announcement_id = a.id and c.relevant and c.score >= 50
    left join public.announcement_details d on d.announcement_id = a.id
    left join links lp on a.announce_type = 'P0' and lp.plan_no = a.plan_no
    left join links lj on a.announce_type <> 'P0' and lj.project_no = a.project_no
    where a.announce_type in ('D0', 'B0', '15', 'P0')
      and a.first_seen_at >= (p_from - 10)::timestamp at time zone 'Asia/Bangkok'
      and a.first_seen_at < (p_to + 1)::timestamp at time zone 'Asia/Bangkok'
  ),
  keyed as (
    select b.*, array_remove(array[b.lead_key, 'P:' || b.linked_plan], null) as keys from base b
  )
  select jsonb_build_object(
    'type', b.announce_type, 'project_no', b.project_no, 'plan_no', b.plan_no, 'lead_key', b.lead_key,
    'linked_plan', b.linked_plan,
    'method', b.method, 'title', b.title, 'link', b.link, 'pub_date', b.pub_date, 'dept_name', b.dept_name,
    'email_date', b.email_date, 'score', b.score, 'category', b.category, 'reason', b.reason,
    'budget_thb', coalesce(
       public.project_budget(b.project_no),
       b.own_budget,
       (select max(d2.budget_thb) from public.announcements x
          join public.announcement_details d2 on d2.announcement_id = x.id
         where x.project_no = b.project_no)),
    'bid_date', b.bid_date, 'bid_time', b.extracted ->> 'bid_time',
    'expected_month', b.extracted ->> 'expected_month',
    'province', coalesce(b.extracted ->> 'province',
       (select min(d2.extracted ->> 'province') from public.announcements x
          join public.announcement_details d2 on d2.announcement_id = x.id
         where x.project_no = b.project_no)),
    'cancelled', exists (select 1 from public.announcements x
                         where x.project_no = b.project_no and x.announce_type = 'D1'),
    'amended', exists (select 1 from public.announcements x
                       where x.project_no = b.project_no and x.announce_type = 'D2'),
    'status', e.status, 'status_at', e.created_at, 'status_by', e.name,
    'awarded', coalesce(w.awarded, false), 'award_changed', coalesce(w.changed, false),
    'prev_winners', case when w.changed then w.first_winners end,
    'winners', w.winners, 'winner_price', w.winner_price,
    'result', case when not coalesce(w.awarded, false) or w.winners is null then null
                   when w.ours then 'win'
                   when exists (select 1 from public.lead_events x where x.lead_key = any (b.keys) and x.status = 'bid')
                     then 'lost'
                   else 'other_won' end)
  from keyed b
  left join lateral (
    select ev.status, ev.created_at, u.name from public.lead_events ev
    join public.lead_users u on u.id = ev.user_id
    where ev.lead_key = any (b.keys) order by ev.created_at desc limit 1) e on true
  left join lateral public.project_award(b.project_no) w on b.project_no is not null
  where b.email_date between p_from and p_to;
$function$;

CREATE OR REPLACE FUNCTION public.web_tracking()
 RETURNS SETOF jsonb
 LANGUAGE sql
 STABLE
 SET search_path TO ''
AS $function$
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
    order by array_position(array['D0','15','B0','P0'], a.announce_type) limit 1) info on true
  left join lateral public.project_award(t.lead_key) w on t.lead_key not like 'P:%';
$function$;
