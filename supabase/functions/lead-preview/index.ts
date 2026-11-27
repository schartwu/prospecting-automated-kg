// UAT copy of the `lead` function, called only by the test page web-preview/ (published at /preview/).
// It starts as an exact copy of supabase/functions/lead/index.ts and shares the same database. A change is
// tried here first; once approved, the same change goes into `lead` and this file is copied from it again.
//
// KG Lead Loop API for the web app (web/index.html on GitHub Pages).
//
// Every call is POST {token, action, ...}. The token is the person's own link (lead_users.token), so the
// function checks it itself instead of a Supabase login (deployed with verify_jwt = false).
//   me     -> {name, role}
//   board  -> leads of the last `days` days in Sales' provinces, with latest status and e-GP outcome
//   lead   -> {announcements, events} of one lead
//   save   -> add one status (role dv or sale; viewer only looks)
//   assign -> hand a lead to a Sale (GXX), or take it back (can_assign accounts: DV, Tele, admin)
//   assignments -> every lead handed out, for the มอบหมายงาน tab (DV, Tele, admin)
//   ping   -> {device, view, visit}: the page is open (every minute) / a page was opened (visit = true)
//   usage  -> who uses the web app (admin accounts only, ADMIN_EMAILS)
//   (any action) as -> the admin views as another account, read only
//
// A Sale (role sale without can_assign) sees only the leads handed to them (phase 2, 27 Nov 2026): their
// Lead list, ติดตามสถานะ and lead pages are cut here, not only hidden on the page.
import { createClient } from "npm:@supabase/supabase-js@2.45.4";

const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
  auth: { persistSession: false },
});

// Same list as DEFAULT_PROVINCES in scripts/digest.py; TARGET_PROVINCES (function secret) overrides it.
const PROVINCES = (Deno.env.get("TARGET_PROVINCES") ??
  "กรุงเทพมหานคร, นนทบุรี, ปทุมธานี, สมุทรปราการ, สมุทรสาคร, สมุทรสงคราม, นครปฐม, " +
    "พระนครศรีอยุธยา, สระบุรี, นครนายก, ปราจีนบุรี, ฉะเชิงเทรา, ชลบุรี, ระยอง, จันทบุรี")
  .split(",").map((p) => p.trim()).filter(Boolean);

// Who may read the usage summary. A function secret overrides it.
const ADMIN_EMAILS = (Deno.env.get("ADMIN_EMAILS") ?? "schartwu@gmail.com").split(",").map((e) => e.trim().toLowerCase());
const VIEWS = ["leads", "tracking", "results", "overview", "assign", "guide", "usage", "lead"];

// The statuses that can be saved, as the DV / SR process with KG Lead (26 Nov 2026). contacted and
// sent_to_mer are no longer offered; older records keep them.
const STATUSES = ["received", "admin_pricing", "mer_replied", "pricing_done", "bid", "no_bid", "other"];
const REASONS = ["ไม่ใช่สินค้าเรา", "Spec ล็อกยี่ห้อ", "ราคาสู้ไม่ได้", "Mer ไม่สามารถหาสินค้าตาม TOR ได้", "Vender เข้าตรง", "อื่น ๆ"];

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// The API hands back at most 1,000 rows per call (PostgREST max rows), so each list comes back from
// web_list() as one JSON array. Without it, "ทั้งหมด" in ผลลัพธ์ stopped at 1,000 of 1,386 rows.
async function allRows(kind: string, args: Record<string, unknown> = {}) {
  const { data, error } = await db.rpc("web_list", { p_kind: kind, ...args });
  return { data: (data ?? []) as unknown[], error };
}

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" },
  });
}

type Person = { id: number; name: string; role: string; can_assign: boolean; active: boolean };
type Hand = { id: number; lead_key: string; assignee_id: number | null; assigned_by: number; assigned_at: string };
type Current = { assignee_id: number; assignee: string; assigned_by: string; assigned_at: string };

// Every hand-out with names; the latest row of a lead is who has it now (assignee_id null = taken back).
async function handOuts() {
  const [{ data: rows, error }, { data: people, error: pErr }] = await Promise.all([
    db.from("lead_assignments").select("id, lead_key, assignee_id, assigned_by, assigned_at")
      .order("assigned_at", { ascending: true }).order("id", { ascending: true }),
    db.from("lead_users").select("id, name, role, can_assign, active").order("id"),
  ]);
  if (error || pErr) return null;
  const name = new Map((people as Person[]).map((p) => [p.id, p.name]));
  const history = (rows as Hand[]).map((h) => ({
    lead_key: h.lead_key, assignee_id: h.assignee_id, assignee: h.assignee_id ? name.get(h.assignee_id) ?? "" : null,
    assigned_by: name.get(h.assigned_by) ?? "", at: h.assigned_at,
  }));
  const current = new Map<string, Current>();
  for (const h of history) {
    if (h.assignee_id) {
      current.set(h.lead_key, { assignee_id: h.assignee_id, assignee: h.assignee ?? "", assigned_by: h.assigned_by, assigned_at: h.at });
    } else current.delete(h.lead_key);
  }
  // the Sales a lead can be handed to: active, role sale, not a DV / Tele account
  const sales = (people as Person[]).filter((p) => p.active && p.role === "sale" && !p.can_assign)
    .map((p) => ({ id: p.id, name: p.name }));
  return { history, current, sales };
}

// A plan (P:...) handed out before e-GP announced its project keeps its plan key, while the lead is then
// keyed by the project: look under both.
function handOf(current: Map<string, Current>, key: string, linkedPlan?: string | null) {
  return current.get(key) ?? (linkedPlan ? current.get("P:" + linkedPlan) : undefined);
}

// who has each row's lead now, on every list row
function withHand(rows: unknown[], current: Map<string, Current>) {
  return rows.map((r) => {
    const row = r as { lead_key?: string; linked_plan?: string | null };
    const c = handOf(current, String(row.lead_key ?? ""), row.linked_plan);
    return c ? { ...(r as object), ...c } : r;
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (req.method !== "POST") return reply({ error: "ใช้ POST เท่านั้น" }, 405);

  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return reply({ error: "รูปแบบคำขอไม่ถูกต้อง" }, 400);
  }
  const token = typeof body.token === "string" ? body.token : "";
  if (!/^[0-9a-f]{64}$/.test(token)) return reply({ error: "ลิงก์ไม่ถูกต้อง" }, 401);

  const { data: self, error: userErr } = await db.from("lead_users")
    .select("id, name, role, email, can_assign").eq("token", token).eq("active", true).maybeSingle();
  if (userErr) return reply({ error: "ระบบขัดข้อง ลองใหม่อีกครั้ง" }, 500);
  if (!self) return reply({ error: "ลิงก์นี้ใช้ไม่ได้แล้ว ติดต่อผู้ดูแลระบบ" }, 401);
  // The admin can look through another account's eyes (body.as = its id), read only: to check what a
  // Sale sees without their link. Saving, handing out and the usage log are off while doing so.
  let user = self;
  const viewingAs = ADMIN_EMAILS.includes(String(self.email).toLowerCase()) && Number.isInteger(body.as) && body.as !== self.id;
  if (viewingAs) {
    const { data: other } = await db.from("lead_users")
      .select("id, name, role, email, can_assign").eq("id", body.as as number).eq("active", true).maybeSingle();
    if (!other) return reply({ error: "ไม่พบบัญชีที่เลือก" }, 404);
    user = other;
  }
  if (viewingAs && ["save", "assign", "usage"].includes(String(body.action))) {
    return reply({ error: "กำลังดูในมุมมองของคนอื่น บันทึกไม่ได้ · กลับเป็นบัญชีของคุณก่อน" }, 403);
  }
  if (viewingAs && body.action === "ping") return reply({ ok: true });
  const admin = ADMIN_EMAILS.includes(String(user.email).toLowerCase());
  const canAssign = !!user.can_assign;
  const sr = user.role === "sale" && !canAssign;          // a Sale: sees only the leads handed to them
  const me = { name: user.name, role: user.role, admin, can_assign: canAssign, sr, viewing_as: viewingAs ? user.id : null };
  const NOT_YOURS = "งานนี้ไม่ได้มอบหมายให้คุณ ถ้าต้องการทำงานนี้ ติดต่อ DV";

  switch (body.action) {
    case "me":
      return reply(me);

    case "ping": {
      const device = String(body.device ?? ""), view = String(body.view ?? "");
      if (!/^[0-9a-f]{16}$/.test(device) || !VIEWS.includes(view)) return reply({ error: "รูปแบบคำขอไม่ถูกต้อง" }, 400);
      const { error } = await db.from("lead_presence")
        .upsert({ user_id: user.id, device, view, last_seen: new Date().toISOString() }, { onConflict: "user_id,device" });
      if (!error && body.visit === true) await db.from("lead_visits").insert({ user_id: user.id, device, view });
      return reply({ ok: !error });
    }

    case "usage": {
      if (!admin) return reply({ error: "เฉพาะผู้ดูแลระบบ" }, 403);
      const { data, error } = await db.rpc("web_usage", { p_exclude: ADMIN_EMAILS });
      if (error) return reply({ error: "โหลดข้อมูลการใช้งานไม่สำเร็จ" }, 500);
      return reply({ user: me, ...data });
    }

    case "items":
    case "leads":
    case "outcomes": {
      const hands = await handOuts();
      if (!hands) return reply({ error: "โหลดรายการไม่สำเร็จ" }, 500);
      const extra = canAssign ? { sales: hands.sales } : {};
      if (sr) {
        // a Sale's Lead list: every lead handed to them, whatever its email day
        if (body.action !== "leads") return reply({ error: "หน้านี้สำหรับ DV" }, 403);
        const keys = [...hands.current].filter(([, c]) => c.assignee_id === user.id).map(([k]) => k);
        if (!keys.length) return reply({ user: me, provinces: PROVINCES, rows: [] });
        const { data, error } = await db.rpc("web_list_keys", { p_keys: keys });
        if (error) return reply({ error: "โหลดรายการไม่สำเร็จ" }, 500);
        return reply({ user: me, provinces: PROVINCES, rows: withHand((data ?? []) as unknown[], hands.current) });
      }
      // Lists by morning-email day: leads (D0/B0/15/P0), results (W0/D1), or both (items, the old Excel tab).
      const from = String(body.from ?? ""), to = String(body.to ?? "");
      const ok = (d: string) => /^\d{4}-\d{2}-\d{2}$/.test(d) && !isNaN(Date.parse(d));
      if (!ok(from) || !ok(to) || from > to) return reply({ error: "เลือกช่วงวันที่ให้ถูกต้อง" }, 400);
      const { data, error } = await allRows(body.action as string, { p_from: from, p_to: to });
      if (error) return reply({ error: "โหลดรายการไม่สำเร็จ" }, 500);
      return reply({ user: me, provinces: PROVINCES, rows: withHand(data, hands.current), ...extra });
    }

    case "tracking": {
      const [{ data, error }, hands] = await Promise.all([allRows("tracking"), handOuts()]);
      if (error || !hands) return reply({ error: "โหลดรายการติดตามไม่สำเร็จ" }, 500);
      const rows = withHand(data, hands.current) as { assignee_id?: number }[];
      return reply({ user: me, provinces: PROVINCES, rows: sr ? rows.filter((r) => r.assignee_id === user.id) : rows });
    }

    case "days": {
      const { data, error } = await db.rpc("web_email_days");
      if (error) return reply({ error: "โหลดปฏิทินไม่สำเร็จ" }, 500);
      return reply({ user: me, days: data });
    }

    case "board": {
      if (sr) return reply({ error: "หน้านี้สำหรับ DV" }, 403);
      const days = Math.min(Math.max(Number(body.days) || 30, 1), 120);
      const { data, error } = await db.rpc("lead_board", { p_days: days, p_provinces: PROVINCES });
      if (error) return reply({ error: "โหลดรายการไม่สำเร็จ" }, 500);
      return reply({ user: me, leads: data });
    }

    case "lead": {
      const key = String(body.key ?? "");
      if (!/^(P:)?[A-Z0-9]{8,20}$/.test(key)) return reply({ error: "ไม่พบ Lead นี้" }, 404);
      const [{ data, error }, hands] = await Promise.all([db.rpc("lead_detail", { p_key: key }), handOuts()]);
      if (error || !hands) return reply({ error: "โหลด Lead ไม่สำเร็จ" }, 500);
      if (!data?.announcements?.length) return reply({ error: "ไม่พบ Lead นี้" }, 404);
      const k = String(data.lead_key || key), mine = handOf(hands.current, k, data.linked_plan);
      const planKey = data.linked_plan ? "P:" + data.linked_plan : null;
      if (sr && mine?.assignee_id !== user.id) return reply({ error: NOT_YOURS }, 403);
      return reply({
        user: me, ...data, assignment: mine ?? null,
        assign_history: hands.history.filter((h) => h.lead_key === k || h.lead_key === planKey),
        ...(canAssign ? { sales: hands.sales } : {}),
      });
    }

    case "assign": {
      if (!canAssign) return reply({ error: "บัญชีนี้มอบหมายงานไม่ได้" }, 403);
      const key = String(body.key ?? "");
      if (!/^(P:)?[A-Z0-9]{8,20}$/.test(key)) return reply({ error: "ไม่พบ Lead นี้" }, 404);
      const to = body.assignee_id === null || body.assignee_id === "" || body.assignee_id === undefined ? null : Number(body.assignee_id);
      const [{ data: lead, error }, hands] = await Promise.all([db.rpc("lead_detail", { p_key: key }), handOuts()]);
      if (error || !hands) return reply({ error: "มอบหมายไม่สำเร็จ ลองใหม่อีกครั้ง" }, 500);
      if (!lead?.announcements?.length) return reply({ error: "ไม่พบ Lead นี้" }, 404);
      if (to !== null && !hands.sales.some((s) => s.id === to)) return reply({ error: "เลือก GXX ที่จะมอบหมาย" }, 400);
      const k = String(lead.lead_key || key), planKey = lead.linked_plan ? "P:" + lead.linked_plan : null;
      const now = handOf(hands.current, k, lead.linked_plan)?.assignee_id ?? null;
      if (now !== to) {
        // a hand-out still under the plan's key is closed, so the lead has one holder under its project key
        const rows = [{ lead_key: k, assignee_id: to, assigned_by: user.id }];
        if (planKey && planKey !== k && hands.current.has(planKey)) rows.unshift({ lead_key: planKey, assignee_id: null, assigned_by: user.id });
        const { error: insErr } = await db.from("lead_assignments").insert(rows);
        if (insErr) return reply({ error: "มอบหมายไม่สำเร็จ ลองใหม่อีกครั้ง" }, 500);
      }
      const after = await handOuts();
      if (!after) return reply({ error: "มอบหมายแล้ว แต่โหลดข้อมูลใหม่ไม่สำเร็จ รีเฟรชหน้า" }, 500);
      return reply({ saved: true, lead_key: k, assignment: handOf(after.current, k, lead.linked_plan) ?? null,
        assign_history: after.history.filter((h) => h.lead_key === k || h.lead_key === planKey) });
    }

    case "assignments": {
      // the มอบหมายงาน tab: who has which lead now, with the lead's status and result
      if (sr || user.role === "viewer") return reply({ error: "หน้านี้สำหรับ DV" }, 403);
      const hands = await handOuts();
      if (!hands) return reply({ error: "โหลดข้อมูลการมอบหมายไม่สำเร็จ" }, 500);
      const keys = [...hands.current.keys()];
      const [{ data, error }, tr] = await Promise.all([
        keys.length ? db.rpc("web_list_keys", { p_keys: keys }) : Promise.resolve({ data: [], error: null }),
        allRows("tracking"),
      ]);
      if (error || tr.error) return reply({ error: "โหลดข้อมูลการมอบหมายไม่สำเร็จ" }, 500);
      // the no-bid reasons of the latest status, from ติดตามสถานะ
      const reasons = Object.fromEntries((tr.data as { lead_key: string; status: string; reasons?: string[] }[])
        .filter((r) => r.status === "no_bid" && r.reasons?.length).map((r) => [r.lead_key, r.reasons]));
      return reply({ user: me, sales: hands.sales, history: hands.history, reasons,
        rows: withHand((data ?? []) as unknown[], hands.current) });
    }

    case "save": {
      if (!["dv", "sale"].includes(user.role)) return reply({ error: "บัญชีนี้ดูได้อย่างเดียว" }, 403);
      const key = String(body.key ?? "");
      const status = String(body.status ?? "");
      const reasons = Array.isArray(body.reasons) ? body.reasons.map(String).filter((r) => REASONS.includes(r)) : [];
      const note = typeof body.note === "string" ? body.note.trim().slice(0, 1000) : "";
      const price = body.bid_price === null || body.bid_price === undefined || body.bid_price === ""
        ? null : Number(String(body.bid_price).replace(/,/g, ""));
      if (!/^(P:)?[A-Z0-9]{8,20}$/.test(key)) return reply({ error: "ไม่พบ Lead นี้" }, 404);
      if (!STATUSES.includes(status)) return reply({ error: "เลือกสถานะก่อนกดบันทึก" }, 400);
      if (status === "no_bid" && !reasons.length) return reply({ error: "เลือกเหตุผลที่ไม่ยื่นอย่างน้อย 1 ข้อ" }, 400);
      if (status === "other" && !note) return reply({ error: "สถานะ “อื่น ๆ” ต้องใส่หมายเหตุ" }, 400);
      if (price !== null && !(price >= 0)) return reply({ error: "ราคาที่ยื่นต้องเป็นตัวเลข" }, 400);
      // The same locks as the web page, so an old tab or a hand-made request cannot save past them.
      const { data: lead, error: leadError } = await db.rpc("lead_detail", { p_key: key });
      if (leadError) return reply({ error: "บันทึกไม่สำเร็จ ลองใหม่อีกครั้ง" }, 500);
      const anns: { type: string; project_no: string | null }[] = lead?.announcements ?? [];
      if (!anns.length) return reply({ error: "ไม่พบ Lead นี้" }, 404);
      const project = String(lead.lead_key ?? "");
      if (sr) {
        const hands = await handOuts();
        if (!hands) return reply({ error: "บันทึกไม่สำเร็จ ลองใหม่อีกครั้ง" }, 500);
        if (handOf(hands.current, project || key, lead.linked_plan)?.assignee_id !== user.id) return reply({ error: NOT_YOURS }, 403);
      }
      if (!project.startsWith("P:")) {
        if (anns.some((a) => a.type === "D1" && a.project_no === project)) {
          return reply({ error: "โครงการนี้ถูกยกเลิกแล้ว บันทึกสถานะไม่ได้" }, 409);
        }
        const { data: award } = await db.rpc("project_award", { p_project: project });
        if (award?.[0]?.awarded && !["bid", "no_bid"].includes(status)) {
          return reply({ error: "ประกาศผู้ชนะแล้ว บันทึกได้แค่ “ยื่นราคาแล้ว” หรือ “ไม่ยื่น”" }, 409);
        }
      }
      const { error } = await db.from("lead_events").insert({
        lead_key: key, status, reasons: status === "no_bid" ? reasons : [],
        bid_price: status === "bid" ? price : null, note: note || null, user_id: user.id,
      });
      if (error) return reply({ error: "บันทึกไม่สำเร็จ ลองใหม่อีกครั้ง" }, 500);
      const { data } = await db.rpc("lead_detail", { p_key: key });
      return reply({ saved: true, events: data?.events ?? [] });
    }

    default:
      return reply({ error: "ไม่รู้จักคำสั่งนี้" }, 400);
  }
});
