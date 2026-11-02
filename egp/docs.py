"""Read budget (ราคากลาง) and bid date (วันเสนอราคา) from the document an announcement links to.

D0 links are a PDF announcement. B0 and some 15 links are a ZIP holding the draft announcement
(annoudoc_*.pdf / pB0.pdf), the draft bid document (doc_*.pdf) and the TOR (Attach_TOR_*.pdf).
The PDF text layer comes out with words in jumbled order, so the patterns look for the pieces
separately instead of expecting a clean sentence.
"""
import html
import io
import re
import zipfile
from datetime import date, timedelta

import pymupdf as fitz
import requests

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
MONTHS = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
          "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
MONEY = r"(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})\s*บาท"
# B0 drafts come as one ZIP with the TOR attachments; 5 relevant ones were over the old 15 MB cap (Nov 2026).
MAX_DOWNLOAD = 80 * 1024 * 1024
# The e-GP document server's firewall answers a bare "Mozilla/5.0" with a 244-byte "Request Rejected"
# page; a complete browser User-Agent gets the document.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
# Inside a ZIP, the short announcement documents carry the numbers; the TOR is long and comes last.
ZIP_ORDER = [r"^annoudoc", r"^pB0", r"^doc_", r"\.pdf$"]


# e-GP pages name their file after the purchasing unit that published them:
# "templateName = 'announcewin_020473000005_69109346498.html'" (W0), 'aplt_020410000021_M69100035460.html' (P0).
UNIT_IN_PAGE = re.compile(rb"templateName\s*=\s*'[A-Za-z]+_(\d{8,20})_")


def fetch_text(url: str) -> tuple[str, dict, list[bytes]]:
    """Download the linked document. Returns (plain text, info about what was read, the short PDFs
    themselves). The PDFs are kept for scanned documents: they have no text layer, so Gemini reads
    the page images instead."""
    resp = requests.get(url, timeout=90, headers={"User-Agent": USER_AGENT}, stream=True)
    resp.raise_for_status()
    data = resp.raw.read(MAX_DOWNLOAD + 1, decode_content=True)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError(f"document larger than {MAX_DOWNLOAD // 1024 // 1024} MB")
    info = {"content_type": resp.headers.get("Content-Type", ""), "bytes": len(data)}
    if data[:4] == b"%PDF":
        return _pdf_text(data), {**info, "kind": "pdf"}, [data]
    if data[:2] == b"PK":
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
        info.update(kind="zip", files=names, tor=[n for n in names if "tor" in n.lower()])
        ordered = sorted(names, key=lambda n: next(i for i, p in enumerate(ZIP_ORDER) if re.search(p, n, re.I)))
        pdfs = [zf.read(n) for n in ordered if "tor" not in n.lower()][:3]
        return "\n".join(_pdf_text(b) for b in pdfs), info, pdfs
    unit = UNIT_IN_PAGE.search(data[:20000])
    if unit:
        info["unit"] = unit.group(1).decode()
    if b"Request Rejected" in data[:500]:
        raise RuntimeError("e-GP firewall rejected the request")
    if "html" in info["content_type"].lower() and b"<table" in data.lower():
        # P0 plan pages are TIS-620 HTML with the plan lines in a table.
        return html_text(data), {**info, "kind": "html"}, []
    return "", {**info, "kind": "other"}, []  # e.g. a page that needs an e-GP login


def html_text(data: bytes) -> str:
    """HTML to text with table cells separated by ' | ' and rows on their own lines."""
    raw = data.decode("cp874", errors="replace")  # TIS-620 plus the no-break space (0xA0) the pages use
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<br\s*/?>|</tr>|</p>|</div>", "\n", raw, flags=re.I)
    raw = re.sub(r"</t[dh]>", " | ", raw, flags=re.I)
    raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "\n".join(re.sub(r"[ \t\xa0]+", " ", line).strip() for line in raw.splitlines())


def parse_plan_lines(text: str) -> dict[str, dict]:
    """P0 plan page table: 'ลำดับ | รหัสแผน | ชื่อโครงการ | งบประมาณ (บาท) | คาดว่าจะประกาศ (เดือน/ปี)'.
    Returns {plan_no: {"budget_thb": float, "expected_month": "YYYY-MM" or None}}. The month cell is
    sometimes malformed at the source (e.g. '09//259'); the budget is still kept."""
    text = text.translate(THAI_DIGITS)
    lines = {}
    for m in re.finditer(r"(P\d{11})\s*\|(.*?)\|\s*([\d,]+\.\d{2})\s*\|([^|\n]*)", text, re.S):
        expected = None
        my = re.fullmatch(r"\s*(\d{1,2})\s*/\s*(\d{4})\s*", m.group(4))
        if my and 1 <= int(my.group(1)) <= 12:
            expected = f"{int(my.group(2)) - 543:04d}-{int(my.group(1)):02d}"
        lines[m.group(1)] = {"budget_thb": float(m.group(3).replace(",", "")), "expected_month": expected}
    return lines


def _pdf_text(data: bytes) -> str:
    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(THAI_DIGITS))


def parse_budget(text: str) -> float | None:
    """ราคากลาง ... เป็นเงินทั้งสิ้น 1,600,000.00 บาท (words in between are often shuffled in)."""
    for anchor in ("เป็นเงินทั้งสิ้น", "ราคากลาง", "วงเงิน"):
        for m in re.finditer(anchor, text):
            hit = re.search(MONEY, text[m.end():m.end() + 160])
            if hit:
                return float(hit.group(1).replace(",", ""))
    return None


def parse_bid_date(text: str) -> tuple[date | None, str | None]:
    """'เสนอราคา...อิเล็กทรอนิกส์ในวันที่ 21 ระหว่างเวลา น. ถึง น. ... ตุลาคม 2569 09.00 12.00'.
    Day comes first, then month + Buddhist-era year and the time window further on."""
    m = re.search(r"อิเล็กทรอนิกส์\s*ในวันที่\s*(\d{1,2})", text)
    if not m:
        return None, None
    tail = text[m.end():m.end() + 400]
    my = re.search(r"(" + "|".join(MONTHS) + r")\s*(?:พ\.ศ\.\s*)?(\d{4})", tail)
    if not my:
        return None, None
    try:
        d = date(int(my.group(2)) - 543, MONTHS.index(my.group(1)) + 1, int(m.group(1)))
    except ValueError:
        return None, None
    times = re.findall(r"\b(\d{2}\.\d{2})\b", tail[my.end():my.end() + 60])
    return d, (f"{times[0]}-{times[1]}" if len(times) >= 2 else None)


def parse_winners(text: str) -> tuple[list[str], float | None]:
    """W0: '... ผู้ได้รับการคัดเลือก ได้แก่ บริษัท X จำกัด (ขายส่ง,ขายปลีก) โดยเสนอราคา เป็นเงินทั้งสิ้น
    273,000.00 บาท'. An announcement with several lots names a winner per lot; returns the distinct
    winners and the total of their prices. PDF text (e-bidding results) comes out jumbled; when a
    'name' does not look like one, nothing is returned so the caller can ask Gemini."""
    winners, total = [], 0.0
    for m in re.finditer(r"ได้แก่\s*(.{3,200}?)\s*โดย\s*(?:ได้\s*)?เสนอราคา", text):
        name = re.sub(r"\s*\((?:ส่งออก|ขาย|ให้บริการ|ผลิต|ผู้ผลิต)[^)]*\)\s*$", "", m.group(1))
        # some pages add the address or "ผู้ประกอบการที่มีอาชีพขาย..." after the name
        name = re.sub(r"\s+(?:เลขที่|ผู้ประกอบการ)\s?.*$", "", name).strip(" ,")
        if not name or re.search(r"จำนวน|โครงการ|ซึ่ง|ได้แก่", name) or len(name) > 120:
            return [], None
        if name not in winners:
            winners.append(name)
        price = re.search(MONEY, text[m.end():m.end() + 200])
        if price:
            total += float(price.group(1).replace(",", ""))
    if not winners:
        return parse_winner_table(text)
    return winners, (total or None)


def parse_winner_table(text: str) -> tuple[list[str], float | None]:
    """W2 (winners changed) lists the result as a table: 'รายการที่พิจารณา | ผู้เสนอราคาดีที่สุด | ราคาที่เสนอ |'
    then 'item | winner | 1,998,300.11 |' per lot."""
    head = re.search(r"ผู้เสนอราคาดีที่สุด\s*\|\s*ราคาที่เสนอ\s*\|", text)
    if not head:
        return [], None
    cells = [c.strip() for c in text[head.end():].split("|")]
    cells = [c for c in cells if c]
    winners, total, i = [], 0.0, 0
    while i + 2 < len(cells) and re.fullmatch(r"[\d,]+(?:\.\d+)?", cells[i + 2]):
        name = cells[i + 1]
        if name not in winners:
            winners.append(name)
        total += float(cells[i + 2].replace(",", ""))
        i += 3
    return winners, (total or None)


WINNER_PROMPT = """ข้อความด้านล่างมาจากประกาศผู้ชนะการเสนอราคาของภาครัฐ (ข้อความอาจเรียงสลับตำแหน่ง)
ให้ดึงข้อมูลต่อไปนี้ ถ้าไม่มีในเอกสารให้ใส่ null:
- winners: รายชื่อผู้ชนะ/ผู้ได้รับการคัดเลือก (ชื่อบริษัท ห้าง ร้าน หรือบุคคล ไม่ต้องใส่ประเภทกิจการในวงเล็บ) ถ้ามีหลายรายการให้ใส่ทุกราย ไม่ซ้ำ
- price_thb: ราคาที่ผู้ชนะเสนอ รวมทุกรายการ เป็นตัวเลขบาท

เอกสาร:
{text}
"""

WINNER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "winners": {"type": "ARRAY", "items": {"type": "STRING"}},
        "price_thb": {"type": "NUMBER", "nullable": True},
    },
    "required": ["winners", "price_thb"],
}


def parse_department(text: str) -> str | None:
    """'(สำเนา) ประกาศกรมตรวจบัญชีสหกรณ์ เรื่อง ...' -> 'กรมตรวจบัญชีสหกรณ์' (top of D0/B0 announcements).
    W0 pages start with their title, 'ประกาศรายชื่อผู้ชนะการเสนอราคา ( สำเนา ) ประกาศ กรมชลประทาน เรื่อง',
    so the name is what follows the last 'ประกาศ' before 'เรื่อง'."""
    m = re.search(r"ประกาศ\s*((?:(?!ประกาศ)[^|]){4,80}?)\s*\|?\s*เรื่อง", text[:600])
    return m.group(1).strip() if m else None


def plausible(bid: date | None, pub: date | None) -> bool:
    """A bid date before the announcement or months later means the pattern grabbed the wrong date."""
    return bid is not None and (pub is None or pub <= bid <= pub + timedelta(days=90))


EXTRACT_PROMPT = """ข้อความด้านล่างมาจากเอกสารประกาศจัดซื้อจัดจ้างภาครัฐ (ข้อความอาจเรียงสลับตำแหน่ง)
ให้ดึงข้อมูลต่อไปนี้ ถ้าไม่มีในเอกสารให้ใส่ null:
- budget_thb: ราคากลาง/วงเงินงบประมาณ "ยอดรวมทั้งโครงการ" เป็นตัวเลขบาท (ถ้ามีตารางหลายรายการ ให้ใช้ยอดรวม ไม่ใช่ราคาต่อรายการ อ่านตัวเลขทุกหลักให้ครบ)
- bid_date: วันที่ยื่นข้อเสนอ/เสนอราคา รูปแบบ YYYY-MM-DD เป็นปี ค.ศ. (แปลง พ.ศ. ลบ 543)
- bid_time: ช่วงเวลายื่นข้อเสนอ เช่น "09.00-12.00"
- item_count: จำนวนรายการสินค้า (ตัวเลข)

เอกสาร:
{text}
"""

EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "budget_thb": {"type": "NUMBER", "nullable": True},
        "bid_date": {"type": "STRING", "nullable": True},
        "bid_time": {"type": "STRING", "nullable": True},
        "item_count": {"type": "INTEGER", "nullable": True},
    },
    "required": ["budget_thb", "bid_date", "bid_time", "item_count"],
}
