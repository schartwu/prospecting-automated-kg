-- KG Lead phase 2 (27 Nov 2026): DV and Tele hand each lead to one Sale (GXX); a Sale sees only the leads
-- handed to them. Additive only: the page the team uses today does not read any of this.

-- Who may hand out leads (DV, Tele, the admin). Their role stays as it is, so nothing else changes for them.
alter table lead_users add column can_assign boolean not null default false;

-- Every hand-out, never updated or deleted: the latest row of a lead is who has it now (assignee_id null =
-- taken back). The older rows are the history the management tab and the lead page show.
create table lead_assignments (
  id bigint generated always as identity primary key,
  lead_key text not null,
  assignee_id bigint references lead_users (id),
  assigned_by bigint not null references lead_users (id),
  assigned_at timestamptz not null default now(),
  notified_at timestamptz            -- when the Sale's batched "new work" email went out
);
alter table lead_assignments enable row level security;
create index lead_assignments_key on lead_assignments (lead_key, assigned_at desc);

-- The same rows as web_leads(), but for the given lead keys whatever their email day: a Sale's own leads and
-- the management tab, without loading every day since 9 Oct.
create function web_leads_by_keys(p_keys text[])
returns setof jsonb
language sql
stable
set search_path = ''
as $$
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
      and (a.project_no = any (p_keys)
           or a.plan_no in (select substr(k, 3) from unnest(p_keys) k where k like 'P:%')
           or a.plan_no in (select l.plan_no from links l where l.project_no = any (p_keys)))
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

-- One JSON array per call, like web_list (PostgREST returns at most 1,000 rows).
create function web_list_keys(p_keys text[])
returns jsonb
language sql
stable
set search_path = ''
as $$ select coalesce(jsonb_agg(j), '[]'::jsonb) from public.web_leads_by_keys(p_keys) j $$;

revoke execute on function public.web_leads_by_keys(text[]) from public, anon, authenticated;
revoke execute on function public.web_list_keys(text[]) from public, anon, authenticated;
grant execute on function public.web_leads_by_keys(text[]) to service_role;
grant execute on function public.web_list_keys(text[]) to service_role;

-- DV, Tele and the admin hand out leads.
update lead_users set can_assign = true
where split_part(email, '@', 1) in ('orachun.ph', 'kasinee.si', 'schartwu');
