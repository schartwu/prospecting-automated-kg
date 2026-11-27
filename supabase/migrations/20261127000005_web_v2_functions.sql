-- KG Lead speed (27 Nov 2026), part 2: the list and lead-page functions read the stored plan_links_cache
-- (part 1) instead of computing plan_links() on every call, and look announcements up by index.
-- Each *_v2 gives the same rows as the function it copies; only lead-preview (UAT) calls them until the
-- release is approved, then `lead` switches too.

-- lead_detail: the announcements of a lead by three index lookups (project, plan of a P: key, linked
-- plans) instead of one OR over the whole table.
create or replace function public.lead_detail_v2(p_key text)
returns jsonb
language sql
stable
set search_path = ''
as $$
  with links as (select c.plan_no, c.project_no from public.plan_links_cache c),
  k as (select coalesce((select l.project_no from links l where 'P:' || l.plan_no = p_key), p_key) as key),
  plans as (select l.plan_no from links l, k where l.project_no = k.key),
  ids as (
    select a.id from public.announcements a, k
     where left(k.key, 2) = 'P:' and a.announce_type = 'P0' and a.plan_no = substr(k.key, 3)
    union
    select a.id from public.announcements a, k
     where left(k.key, 2) <> 'P:' and a.project_no = k.key
    union
    select a.id from public.announcements a, k
     where left(k.key, 2) <> 'P:' and a.announce_type = 'P0' and a.plan_no in (select plan_no from plans)
  )
  select jsonb_build_object(
    'lead_key', (select key from k),
    'linked_plan', (select min(plan_no) from plans),
    'announcements', coalesce((
      select jsonb_agg(jsonb_build_object(
               'type', a.announce_type, 'title', a.title, 'link', a.link, 'pub_date', a.pub_date,
               'dept_name', a.dept_name, 'method', a.method, 'project_no', a.project_no, 'plan_no', a.plan_no,
               'budget_thb', d.budget_thb, 'bid_date', d.bid_date, 'bid_time', d.extracted ->> 'bid_time',
               'expected_month', d.extracted ->> 'expected_month', 'province', d.extracted ->> 'province',
               'winners', d.extracted -> 'winners', 'winner_price', d.extracted -> 'winner_price')
             order by a.pub_date, a.id)
      from ids join public.announcements a on a.id = ids.id
      left join public.announcement_details d on d.announcement_id = a.id), '[]'::jsonb),
    'events', coalesce((
      select jsonb_agg(jsonb_build_object(
               'status', e.status, 'reasons', e.reasons, 'bid_price', e.bid_price, 'note', e.note,
               'by', u.name, 'at', e.created_at)
             order by e.created_at)
      from public.lead_events e join public.lead_users u on u.id = e.user_id, k
      where e.lead_key = k.key or e.lead_key in (select 'P:' || plan_no from plans)), '[]'::jsonb));
$$;

create or replace function public.web_leads_v2(p_from date, p_to date)
returns setof jsonb
language sql
stable
set search_path = ''
as $$
  with links as (select c.plan_no, c.project_no from public.plan_links_cache c),
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
$$;

create or replace function public.web_leads_by_keys_v2(p_keys text[])
returns setof jsonb
language sql
stable
set search_path = ''
as $$
  with links as (select c.plan_no, c.project_no from public.plan_links_cache c),
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
      and a.id in (
        select x.id from public.announcements x where x.project_no = any (p_keys)
        union
        select x.id from public.announcements x
        where x.announce_type = 'P0'
          and x.plan_no in (select substr(k, 3) from unnest(p_keys) k where k like 'P:%'
                            union select l.plan_no from links l where l.project_no = any (p_keys)))
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
  where b.keys && p_keys;
$$;

-- web_tracking: a P: key looks up its plan by plan_no (index) instead of 'P:' || plan_no (whole table).
create or replace function public.web_tracking_v2()
returns setof jsonb
language sql
stable
set search_path = ''
as $$
  with links as (select c.plan_no, c.project_no from public.plan_links_cache c),
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
    from (select distinct a.project_no from public.announcement_details d
          join public.announcements a on a.id = d.announcement_id
          where a.announce_type in ('W0', 'W2') and a.project_no is not null
            and replace(d.extracted ->> 'winners', 'ํา', 'ำ') ~ 'ออฟฟิศ\s*เมท') p
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
  ),
  t2 as (
    select t.*, case when left(t.lead_key, 2) = 'P:' then substr(t.lead_key, 3) end as plan_of from tracked t
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
                    where (a.project_no = t.lead_key or a.plan_no = t.plan_of)
                      and d.budget_thb is not null and a.announce_type in ('D0', '15', 'B0', 'P0')
                    order by array_position(array['D0','15','B0','P0'], a.announce_type), a.pub_date desc
                    limit 1),
    'bid_date', (select max(d.bid_date) from public.announcements a
                   join public.announcement_details d on d.announcement_id = a.id
                  where a.project_no = t.lead_key and a.announce_type = 'D0'),
    'province', (select min(d.extracted ->> 'province') from public.announcements a
                   join public.announcement_details d on d.announcement_id = a.id
                  where a.project_no = t.lead_key or a.plan_no = t.plan_of),
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
  from t2 t
  left join lateral (
    select a.title, a.dept_name, a.announce_type, a.link, a.method from public.announcements a
    where (a.project_no = t.lead_key or a.plan_no = t.plan_of)
      and a.announce_type in ('D0', 'B0', '15', 'P0')
    order by array_position(array['D0','15','B0','P0'], a.announce_type) limit 1) info on true
  left join lateral public.project_award(t.lead_key) w on t.lead_key not like 'P:%';
$$;

-- web_outcomes: "came from a Lead" is checked with a hashed IN list instead of scanning the leads list once
-- per winner announcement.
create or replace function public.web_outcomes_v2(p_from date, p_to date)
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
      and (coalesce(a.method, '') !~ 'เฉพาะเจาะจง' or a.project_no in (select l.project_no from leads l))
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
      and (coalesce(a.method, '') !~ 'เฉพาะเจาะจง' or a.project_no in (select l.project_no from leads l))
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

create or replace function public.web_list_v2(p_kind text, p_from date default null, p_to date default null)
returns jsonb
language plpgsql
stable
set search_path = ''
as $$
begin
  if p_kind = 'leads' then
    return (select coalesce(jsonb_agg(j), '[]'::jsonb) from public.web_leads_v2(p_from, p_to) j);
  elsif p_kind = 'outcomes' then
    return (select coalesce(jsonb_agg(j), '[]'::jsonb) from public.web_outcomes_v2(p_from, p_to) j);
  elsif p_kind = 'items' then
    return (select coalesce(jsonb_agg(j), '[]'::jsonb) from public.web_items(p_from, p_to) j);
  elsif p_kind = 'tracking' then
    return (select coalesce(jsonb_agg(j), '[]'::jsonb) from public.web_tracking_v2() j);
  end if;
  raise exception 'unknown list %', p_kind;
end;
$$;

create or replace function public.web_list_keys_v2(p_keys text[])
returns jsonb
language sql
stable
set search_path = ''
as $$ select coalesce(jsonb_agg(j), '[]'::jsonb) from public.web_leads_by_keys_v2(p_keys) j $$;

-- Like the other functions: only the service role (Edge Function, scripts) calls them.
revoke execute on function public.lead_detail_v2(text) from public, anon, authenticated;
revoke execute on function public.web_leads_v2(date, date) from public, anon, authenticated;
revoke execute on function public.web_leads_by_keys_v2(text[]) from public, anon, authenticated;
revoke execute on function public.web_tracking_v2() from public, anon, authenticated;
revoke execute on function public.web_outcomes_v2(date, date) from public, anon, authenticated;
revoke execute on function public.web_list_v2(text, date, date) from public, anon, authenticated;
revoke execute on function public.web_list_keys_v2(text[]) from public, anon, authenticated;
grant execute on function public.lead_detail_v2(text) to service_role;
grant execute on function public.web_leads_v2(date, date) to service_role;
grant execute on function public.web_leads_by_keys_v2(text[]) to service_role;
grant execute on function public.web_tracking_v2() to service_role;
grant execute on function public.web_outcomes_v2(date, date) to service_role;
grant execute on function public.web_list_v2(text, date, date) to service_role;
grant execute on function public.web_list_keys_v2(text[]) to service_role;
