// KG Lead Loop API for the web app (web/index.html on GitHub Pages).
//
// Every call is POST {token, action, ...}. The token is the person's own link (lead_users.token), so the
// function checks it itself instead of a Supabase login (deployed with verify_jwt = false).
//   me     -> {name, role}
//   board  -> leads of the last `days` days in Sales' provinces, with latest status and e-GP outcome
//   lead   -> {announcements, events} of one lead
//   save   -> add one status (role dv or sale; viewer only looks)
//   ping   -> {device, view, visit}: the page is open (every minute) / a page was opened (visit = true)
//   usage  -> who uses the web app (admin accounts only, ADMIN_EMAILS)
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
const VIEWS = ["leads", "tracking", "results", "overview", "guide", "usage", "lead"];

const STATUSES = ["received", "contacted", "sent_to_mer", "mer_replied", "bid", "no_bid", "other"];
const REASONS = ["ไม่ใช่สินค้าเรา", "Spec ล็อกยี่ห้อ", "ราคาสู้ไม่ได้", "อื่น ๆ"];

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

  const { data: user, error: userErr } = await db.from("lead_users")
    .select("id, name, role, email").eq("token", token).eq("active", true).maybeSingle();
  if (userErr) return reply({ error: "ระบบขัดข้อง ลองใหม่อีกครั้ง" }, 500);
  if (!user) return reply({ error: "ลิงก์นี้ใช้ไม่ได้แล้ว ติดต่อผู้ดูแลระบบ" }, 401);
  const admin = ADMIN_EMAILS.includes(String(user.email).toLowerCase());

  switch (body.action) {
    case "me":
      return reply({ name: user.name, role: user.role, admin });

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
      return reply({ user: { name: user.name, role: user.role, admin }, ...data });
    }

    case "items":
    case "leads":
    case "outcomes": {
      // Lists by morning-email day: leads (D0/B0/15/P0), results (W0/D1), or both (items, the old Excel tab).
      const from = String(body.from ?? ""), to = String(body.to ?? "");
      const ok = (d: string) => /^\d{4}-\d{2}-\d{2}$/.test(d) && !isNaN(Date.parse(d));
      if (!ok(from) || !ok(to) || from > to) return reply({ error: "เลือกช่วงวันที่ให้ถูกต้อง" }, 400);
      const { data, error } = await allRows(body.action as string, { p_from: from, p_to: to });
      if (error) return reply({ error: "โหลดรายการไม่สำเร็จ" }, 500);
      return reply({ user: { name: user.name, role: user.role, admin }, provinces: PROVINCES, rows: data });
    }

    case "tracking": {
      const { data, error } = await allRows("tracking");
      if (error) return reply({ error: "โหลดรายการติดตามไม่สำเร็จ" }, 500);
      return reply({ user: { name: user.name, role: user.role, admin }, provinces: PROVINCES, rows: data });
    }

    case "days": {
      const { data, error } = await db.rpc("web_email_days");
      if (error) return reply({ error: "โหลดปฏิทินไม่สำเร็จ" }, 500);
      return reply({ user: { name: user.name, role: user.role, admin }, days: data });
    }

    case "board": {
      const days = Math.min(Math.max(Number(body.days) || 30, 1), 120);
      const { data, error } = await db.rpc("lead_board", { p_days: days, p_provinces: PROVINCES });
      if (error) return reply({ error: "โหลดรายการไม่สำเร็จ" }, 500);
      return reply({ user: { name: user.name, role: user.role, admin }, leads: data });
    }

    case "lead": {
      const key = String(body.key ?? "");
      if (!/^(P:)?[A-Z0-9]{8,20}$/.test(key)) return reply({ error: "ไม่พบ Lead นี้" }, 404);
      const { data, error } = await db.rpc("lead_detail", { p_key: key });
      if (error) return reply({ error: "โหลด Lead ไม่สำเร็จ" }, 500);
      if (!data?.announcements?.length) return reply({ error: "ไม่พบ Lead นี้" }, 404);
      return reply({ user: { name: user.name, role: user.role, admin }, ...data });
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
