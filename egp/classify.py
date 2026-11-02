"""Judge whether a procurement title is something OfficeMate can supply, using Gemini."""
from egp.gemini import FLASH_FIRST, SCAN_RESERVE, Gemini

CATEGORIES = [
    "กระดาษและเครื่องเขียน",
    "หมึกพิมพ์และวัสดุสิ้นเปลืองงานพิมพ์",
    "คอมพิวเตอร์และอุปกรณ์ไอที",
    "เครื่องใช้สำนักงาน",
    "เฟอร์นิเจอร์สำนักงาน",
    "วัสดุงานบ้านงานครัวและทำความสะอาด",
    "เครื่องใช้ไฟฟ้า",
    "อุปกรณ์ความปลอดภัยและเครื่องมือช่าง",
    "วัสดุสำนักงานรวมหลายหมวด",
    "ไม่เกี่ยวข้อง",
]

# Score bands shared by the Gemini prompt and the keyword rules (egp/rules.py), so a score means the
# same thing whoever produced it. Urgency and budget size are separate columns, not part of the score.
SCORE_RUBRIC = """score ให้ตามระดับนี้เท่านั้น (ประเมินความตรงกับสินค้า OfficeMate ไม่เกี่ยวกับขนาดงบหรือความเร่งด่วน):
- 90-100 สินค้าหลัก: วัสดุสำนักงาน กระดาษ หมึกพิมพ์/โทนเนอร์ เครื่องเขียน
- 75-89  ซื้อหรือเช่า คอมพิวเตอร์/อุปกรณ์ไอทีทั่วไป เฟอร์นิเจอร์สำนักงาน เครื่องใช้สำนักงาน (เครื่องถ่ายเอกสาร ฯลฯ)
- 60-74  โครงการผสมหลายรายการ ที่มีสินค้าของเราเป็นส่วนสำคัญ
- 40-59  ก้ำกึ่ง: เครื่องใช้ไฟฟ้า เครื่องมือช่าง อุปกรณ์ความปลอดภัย วัสดุงานบ้าน หรือชื่อโครงการกว้างจนไม่แน่ใจ
- 0-39   ไม่ใช่ของที่ OfficeMate ขาย
relevant = true เมื่อ score >= 40"""

PROMPT = """คุณคือผู้คัดกรองโครงการจัดซื้อจัดจ้างภาครัฐ (e-GP) ให้ทีมขายของ OfficeMate
OfficeMate เป็นผู้ค้าปลีก/ค้าส่ง "สินค้าสำเร็จรูป" สำหรับสำนักงาน: กระดาษ เครื่องเขียน หมึกพิมพ์ คอมพิวเตอร์ โน้ตบุ๊ก
เครื่องพิมพ์ อุปกรณ์ไอทีทั่วไป เครื่องใช้สำนักงาน เฟอร์นิเจอร์สำนักงาน วัสดุงานบ้าน/ทำความสะอาด เครื่องใช้ไฟฟ้า
อุปกรณ์ความปลอดภัยและเครื่องมือช่างทั่วไป

ตัดสินแต่ละชื่อโครงการว่า OfficeMate "ขายของในโครงการนี้ได้" หรือไม่:
- เกี่ยวข้อง: การ "ซื้อ" หรือ "เช่า" สินค้าในหมวดข้างต้น เช่น ซื้อวัสดุสำนักงาน, ซื้อครุภัณฑ์คอมพิวเตอร์, ซื้อโต๊ะเก้าอี้,
  ซื้อหมึกพิมพ์, เช่าเครื่องคอมพิวเตอร์ หรือแผนจัดซื้อ (P0) ของสินค้าเหล่านี้
- ไม่เกี่ยวข้อง: งาน "จ้าง" บริการ (จ้างบำรุงรักษา, จ้างซ่อม, จ้างทำความสะอาด, จ้างพัฒนาระบบ, จ้างที่ปรึกษา, จ้างก่อสร้าง),
  ครุภัณฑ์การแพทย์/วิทยาศาสตร์, ยานพาหนะ, วัสดุก่อสร้าง, ระบบเฉพาะทาง (ระบบสูบน้ำ, ระบบไฟฟ้าอาคาร, เครือข่ายขนาดใหญ่
  ที่ต้องติดตั้งเชิงวิศวกรรม), ยา อาหาร น้ำมัน เครื่องแบบ

{rubric}
category ต้องเป็นหนึ่งใน: {categories}
reason เป็นภาษาไทยสั้นๆ ไม่เกิน 15 คำ

ตอบเป็น JSON array เรียงตาม id ที่ให้ ครบทุก id

รายการ:
{items}
"""

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "INTEGER"},
            "relevant": {"type": "BOOLEAN"},
            "score": {"type": "INTEGER"},
            "category": {"type": "STRING", "enum": CATEGORIES},
            "reason": {"type": "STRING"},
        },
        "required": ["id", "relevant", "score", "category", "reason"],
    },
}


def classify_titles(rows: list[dict], gemini: Gemini) -> list[dict]:
    """rows: [{'title', 'announce_types', 'methods'}]. Returns one verdict dict per row, same order."""
    lines = []
    for i, r in enumerate(rows):
        hint = ", ".join(filter(None, [*(r.get("announce_types") or []), *(r.get("methods") or [])]))
        lines.append(f"{i}. [{hint}] {r['title'][:400]}")
    prompt = PROMPT.format(rubric=SCORE_RUBRIC, categories=", ".join(CATEGORIES), items="\n".join(lines))

    def complete(answer):
        missing = set(range(len(rows))) - {v["id"] for v in answer}
        if missing:
            raise ValueError(f"model skipped ids {sorted(missing)[:10]}")

    answer, model = gemini.json(prompt, SCHEMA, FLASH_FIRST, validate=complete, reserve=SCAN_RESERVE)
    by_id = {v["id"]: v for v in answer}
    out = []
    for i, r in enumerate(rows):
        v = by_id[i]
        score = max(0, min(100, int(v["score"])))
        out.append({"title": r["title"], "relevant": score >= 40, "score": score,
                    "category": v["category"], "reason": v["reason"][:300], "model": model})
    return out
