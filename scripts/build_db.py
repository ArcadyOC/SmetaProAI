"""Build SQLite database from official SN-2012 (01.07.2026) PDFs."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "data" / "raw" / "pdf"
MANIFEST = ROOT / "data" / "raw" / "manifest.json"
DB_PATH = ROOT / "data" / "sn2012_2026.sqlite"

RATE_CODE = re.compile(r"^\d{1,2}-\d{4}-\d+-\d+(?:/\d+)?$")
RES_CODE = re.compile(r"^\d+\.\d+(?:-\d+)+$")
MAT_POS = re.compile(r"^\d{1,2}-\d{1,2}-\d{1,3}$")
OKP = re.compile(r"^\d{8,12}$")
OKPD2 = re.compile(r"^\d{2}\.\d{2}")
TABLE_RE = re.compile(r"Таблица\s+([0-9]+(?:-[0-9]+)*)\.?\s*(.*)$", re.I)
NUM_TOKEN = re.compile(r"^(?:[-–—]|-?\d{1,3}(?:[ \u00a0]?\d{3})*(?:,\d+)?|-?\d+(?:,\d+)?)$")
HEADER_NOISE = {
    "шифр",
    "наименование",
    "работ",
    "прямые",
    "затраты",
    "заработная",
    "плата",
    "эксплуатация",
    "машин",
    "материальные",
    "ресурсы",
    "всего",
    "руб.",
    "руб",
    "в",
    "т.ч.",
    "оплата",
    "труда",
    "маши",
    "нистов",
    "масса,",
    "т",
    "объем,",
    "м3",
    "чел.-ч",
}


def parse_num(token: str) -> float | None:
    token = token.strip().replace("\u00a0", " ")
    if token in {"-", "–", "—", ""}:
        return None
    return float(token.replace(" ", "").replace(",", "."))


def is_num(token: str) -> bool:
    return bool(NUM_TOKEN.match(token.strip()))


def cluster_rows(page: fitz.Page, y_tol: float = 3.5) -> list[list[tuple[float, float, str]]]:
    words = page.get_text("words")
    items = [(w[0], w[1], w[4]) for w in words if str(w[4]).strip()]
    items.sort(key=lambda x: (round(x[1], 1), x[0]))
    rows: list[list[tuple[float, float, str]]] = []
    for x, y, text in items:
        if rows and abs(y - rows[-1][0][1]) <= y_tol:
            rows[-1].append((x, y, text))
        else:
            rows.append([(x, y, text)])
    for row in rows:
        row.sort(key=lambda x: x[0])
    return rows


def row_text(row: list[tuple[float, float, str]]) -> str:
    return " ".join(t[2] for t in row)


def row_tokens(row: list[tuple[float, float, str]]) -> list[str]:
    return [t[2] for t in row]


@dataclass
class Ctx:
    department: str = ""
    section: str = ""
    table_code: str = ""
    table_name: str = ""
    composition: str = ""
    unit: str = ""
    group_header: str = ""
    collecting_composition: bool = False
    pending_rate: dict | None = None
    rates: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    materials: list[dict] = field(default_factory=list)
    machines: list[dict] = field(default_factory=list)
    texts: list[tuple[int, str]] = field(default_factory=list)


def flush_pending(ctx: Ctx) -> None:
    if ctx.pending_rate and ctx.pending_rate.get("code"):
        if ctx.pending_rate.get("name") or ctx.pending_rate.get("direct_cost") is not None:
            ctx.rates.append(ctx.pending_rate)
    ctx.pending_rate = None


def attach_numbers(rate: dict, nums: list[float | None]) -> None:
    keys = [
        "direct_cost",
        "wages",
        "machines_total",
        "machines_wages",
        "materials",
        "labor_hours",
        "mass_t",
        "volume_m3",
    ]
    for key, val in zip(keys, nums):
        rate[key] = val


def split_name_and_nums(tokens: list[str]) -> tuple[str, list[float | None]]:
    i = len(tokens)
    while i > 0 and is_num(tokens[i - 1]):
        i -= 1
    # a real rate row usually ends with >= 4 numeric fields
    nums = [parse_num(t) for t in tokens[i:]]
    if len(nums) < 4:
        return " ".join(tokens).strip(), []
    name = " ".join(tokens[:i]).strip()
    return name, nums


def handle_context(ctx: Ctx, text: str) -> bool:
    t = re.sub(r"\s+", " ", text).strip()
    if not t:
        return False
    if t.startswith("Отдел "):
        flush_pending(ctx)
        ctx.department = t
        ctx.collecting_composition = False
        return True
    if t.startswith("Раздел "):
        ctx.section = t
        ctx.group_header = t
        ctx.collecting_composition = False
        return True
    m = TABLE_RE.search(t)
    if m and "........" not in t:
        flush_pending(ctx)
        ctx.table_code = m.group(1)
        ctx.table_name = m.group(2).strip(" .")
        ctx.composition = ""
        ctx.unit = ""
        ctx.collecting_composition = False
        return True
    if t.startswith("Состав работ"):
        ctx.composition = t.split(":", 1)[-1].strip()
        ctx.collecting_composition = True
        return True
    if t.startswith("Измеритель"):
        ctx.unit = t.split(":", 1)[-1].strip()
        ctx.collecting_composition = False
        return True
    if ctx.collecting_composition:
        if t.lower().startswith("шифр") or t.lower().startswith("в том числе"):
            ctx.collecting_composition = False
            return True
        ctx.composition = (ctx.composition + " " + t).strip()
        return True
    if t.endswith(":") and not RATE_CODE.match(t.split()[0]):
        ctx.group_header = t
        return False
    return False


def parse_rate_row(ctx: Ctx, tokens: list[str]) -> bool:
    if not tokens:
        return False
    code = tokens[0]
    if not RATE_CODE.match(code):
        if ctx.pending_rate:
            name, nums = split_name_and_nums(tokens)
            if nums:
                attach_numbers(ctx.pending_rate, nums)
                if name:
                    ctx.pending_rate["name"] = ((ctx.pending_rate.get("name") or "") + " " + name).strip()
                flush_pending(ctx)
                return True
            if name and not all(w.lower().strip(".,") in HEADER_NOISE for w in name.split()):
                ctx.pending_rate["name"] = (ctx.pending_rate.get("name", "") + " " + name).strip()
                return True
        return False

    rest = tokens[1:]
    if rest and (RES_CODE.match(rest[0]) or OKP.match(rest[0])):
        flush_pending(ctx)
        qty = parse_num(rest[-1]) if rest and is_num(rest[-1]) else None
        unit = rest[-2] if len(rest) >= 3 else ""
        name = " ".join(rest[1:-2] if len(rest) >= 3 else rest[1:])
        ctx.resources.append(
            {
                "rate_code": code,
                "resource_code": rest[0],
                "name": name.strip(),
                "unit": unit,
                "quantity": qty,
            }
        )
        return True

    flush_pending(ctx)
    name, nums = split_name_and_nums(rest)
    rate = {
        "code": code,
        "name": name,
        "unit": ctx.unit,
        "table_code": ctx.table_code,
        "table_name": ctx.table_name,
        "department": ctx.department,
        "section": ctx.section,
        "work_composition": ctx.composition,
        "direct_cost": None,
        "wages": None,
        "machines_total": None,
        "machines_wages": None,
        "materials": None,
        "labor_hours": None,
        "mass_t": None,
        "volume_m3": None,
    }
    if nums:
        attach_numbers(rate, nums)
        ctx.rates.append(rate)
        ctx.pending_rate = None
    else:
        ctx.pending_rate = rate
    return True


def parse_material_or_machine(ctx: Ctx, tokens: list[str], kind: str) -> bool:
    if len(tokens) < 4 or not MAT_POS.match(tokens[0]):
        return False
    pos = tokens[0]
    code = tokens[1] if len(tokens) > 1 else ""
    okpd = ""
    idx = 2
    if idx < len(tokens) and OKPD2.match(tokens[idx]):
        okpd = tokens[idx]
        idx += 1
    elif idx < len(tokens) and OKP.match(tokens[idx]):
        # sometimes two codes
        idx += 0
    name_tokens = []
    nums: list[float | None] = []
    # trailing numbers
    j = len(tokens)
    while j > idx and is_num(tokens[j - 1]):
        j -= 1
    name_tokens = tokens[idx:j]
    nums = [parse_num(t) for t in tokens[j:]]
    name = " ".join(name_tokens).strip()
    if ctx.group_header and (not name or name[0].isdigit() or len(name) < 8):
        name = (ctx.group_header.rstrip(":") + (": " + name if name else "")).strip()
    if kind == "material":
        # typical: unit, mass_net, mass_gross, price  OR unit may be in name tokens
        unit = ""
        if name_tokens and not is_num(name_tokens[-1]) and len(name_tokens[-1]) <= 6:
            maybe_unit = name_tokens[-1]
            if maybe_unit.lower() in {"т", "кг", "м", "м2", "м3", "шт", "км", "л", "компл", "1000шт", "10м2"} or re.match(
                r"^(шт|м[²³23]?|кг|т|км|л|пар|компл\.?)$", maybe_unit, re.I
            ):
                unit = maybe_unit
                name = " ".join(name_tokens[:-1]).strip() or name
        price = nums[-1] if nums else None
        mass_gross = nums[-2] if len(nums) >= 2 else None
        mass_net = nums[-3] if len(nums) >= 3 else None
        ctx.materials.append(
            {
                "pos_code": pos,
                "okp": code if OKP.match(code or "") else "",
                "okpd2": okpd or (code if OKPD2.match(code or "") else ""),
                "name": name,
                "unit": unit,
                "mass_net": mass_net,
                "mass_gross": mass_gross,
                "price": price,
            }
        )
        return True
    # machine: nums often cost, wages, energy; sometimes a parameter first
    price = None
    wages = None
    energy = None
    param = None
    if len(nums) >= 3:
        # last non-null-ish: energy may be dash
        energy = nums[-1]
        wages = nums[-2]
        price = nums[-3]
        if len(nums) >= 4:
            param = nums[0]
    elif nums:
        price = nums[0]
        if len(nums) > 1:
            wages = nums[1]
    ctx.machines.append(
        {
            "pos_code": pos,
            "code": code,
            "okpd2": okpd,
            "name": name,
            "param": param,
            "price_mach_hour": price,
            "wages": wages,
            "energy_kwh": energy,
            "section": ctx.section or ctx.group_header,
        }
    )
    return True


def parse_pdf(path: Path, kind: str) -> Ctx:
    ctx = Ctx()
    doc = fitz.open(path)
    for page_index, page in enumerate(doc):
        text = page.get_text("text")
        ctx.texts.append((page_index + 1, text))
        rows = cluster_rows(page)
        for row in rows:
            tokens = row_tokens(row)
            text_row = row_text(row)
            if handle_context(ctx, text_row):
                continue
            if kind == "rates":
                parse_rate_row(ctx, tokens)
            elif kind == "materials":
                parse_material_or_machine(ctx, tokens, "material")
            elif kind == "machines":
                parse_material_or_machine(ctx, tokens, "machine")
    flush_pending(ctx)
    return ctx


def classify(filename: str) -> str:
    low = filename.lower()
    if low.startswith("sn_gl"):
        return "rates"
    if low.startswith("sn21"):
        return "materials"
    if low.startswith("sn_22"):
        return "machines"
    return "other"


def collection_meta(filename: str) -> tuple[int | None, int | None, str]:
    m = re.search(r"SN_gl(\d+)_sb(\d+)", filename, re.I)
    if m:
        ch, sb = int(m.group(1)), int(m.group(2))
        return ch, sb, f"СН-2012.{ch}-{sb}"
    m = re.search(r"sn21_(\d+)", filename, re.I)
    if m:
        return 21, int(m.group(1)), f"СН-2012.21-{int(m.group(1))}"
    if filename.lower() == "sn21.pdf":
        return 21, 0, "СН-2012.21"
    if filename.lower() == "sn_22.pdf":
        return 22, 0, "СН-2012.22"
    if filename.lower() == "op.pdf":
        return 0, 0, "СН-2012.ОП"
    return None, None, ""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        DROP TABLE IF EXISTS documents;
        DROP TABLE IF EXISTS pages;
        DROP TABLE IF EXISTS rates;
        DROP TABLE IF EXISTS rate_resources;
        DROP TABLE IF EXISTS materials;
        DROP TABLE IF EXISTS machines;
        DROP TABLE IF EXISTS meta;
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            filename TEXT UNIQUE,
            title TEXT,
            chapter TEXT,
            chapter_no INTEGER,
            collection_no INTEGER,
            collection_code TEXT,
            url TEXT,
            rel_path TEXT,
            bytes INTEGER,
            sha256 TEXT,
            kind TEXT
        );
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            page_no INTEGER NOT NULL,
            text TEXT,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        CREATE TABLE rates (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            collection_code TEXT,
            full_code TEXT,
            code TEXT NOT NULL,
            name TEXT,
            unit TEXT,
            table_code TEXT,
            table_name TEXT,
            department TEXT,
            section TEXT,
            work_composition TEXT,
            direct_cost REAL,
            wages REAL,
            machines_total REAL,
            machines_wages REAL,
            materials REAL,
            labor_hours REAL,
            mass_t REAL,
            volume_m3 REAL,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        CREATE TABLE rate_resources (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            rate_code TEXT,
            resource_code TEXT,
            name TEXT,
            unit TEXT,
            quantity REAL,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        CREATE TABLE materials (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            pos_code TEXT,
            okp TEXT,
            okpd2 TEXT,
            name TEXT,
            unit TEXT,
            mass_net REAL,
            mass_gross REAL,
            price REAL,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        CREATE TABLE machines (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL,
            pos_code TEXT,
            code TEXT,
            okpd2 TEXT,
            name TEXT,
            param REAL,
            price_mach_hour REAL,
            wages REAL,
            energy_kwh REAL,
            section TEXT,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        );
        CREATE INDEX idx_rates_code ON rates(code);
        CREATE INDEX idx_rates_full ON rates(full_code);
        CREATE INDEX idx_rates_name ON rates(name);
        CREATE INDEX idx_mat_pos ON materials(pos_code);
        CREATE INDEX idx_mat_name ON materials(name);
        CREATE INDEX idx_mach_pos ON machines(pos_code);
        CREATE VIRTUAL TABLE IF NOT EXISTS rates_fts USING fts5(
            full_code, code, name, table_name, department, content='rates', content_rowid='id'
        );
        """
    )


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("title", manifest.get("title")))
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("source_page", manifest.get("source_page")))
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("source_api", manifest.get("source_api")))
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("price_level", manifest.get("price_level")))
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("order", manifest.get("order")))
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("date_published", manifest.get("date_published")))

    stats: list[str] = []
    for i, item in enumerate(sorted(manifest["files"], key=lambda x: x["filename"]), start=1):
        filename = item["filename"]
        kind = classify(filename)
        ch_no, sb_no, coll_code = collection_meta(filename)
        cur = conn.execute(
            """INSERT INTO documents(filename,title,chapter,chapter_no,collection_no,collection_code,url,rel_path,bytes,sha256,kind)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                filename,
                item.get("title"),
                item.get("chapter"),
                ch_no,
                sb_no,
                coll_code,
                item.get("url"),
                item.get("rel_path"),
                item.get("bytes"),
                item.get("sha256"),
                kind,
            ),
        )
        doc_id = cur.lastrowid
        path = ROOT / item["rel_path"]
        if not path.exists():
            stats.append(f"MISSING {filename}")
            continue
        ctx = parse_pdf(path, kind)
        conn.executemany(
            "INSERT INTO pages(document_id,page_no,text) VALUES(?,?,?)",
            [(doc_id, pno, txt) for pno, txt in ctx.texts],
        )
        cleaned_rates = []
        leftover_resources = []
        for r in ctx.rates:
            name = (r.get("name") or "").strip()
            if r.get("direct_cost") is None and re.match(r"^\d{8,}", name):
                parts = name.split()
                leftover_resources.append(
                    {
                        "rate_code": r["code"],
                        "resource_code": parts[0],
                        "name": " ".join(parts[1:-2]) if len(parts) >= 3 else " ".join(parts[1:]),
                        "unit": parts[-2] if len(parts) >= 3 else "",
                        "quantity": parse_num(parts[-1]) if len(parts) >= 2 and is_num(parts[-1]) else None,
                    }
                )
                continue
            cleaned_rates.append(r)
        ctx.rates = cleaned_rates
        ctx.resources.extend(leftover_resources)
        conn.executemany(
            """INSERT INTO rates(document_id,collection_code,full_code,code,name,unit,table_code,table_name,department,section,
               work_composition,direct_cost,wages,machines_total,machines_wages,materials,labor_hours,mass_t,volume_m3)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    doc_id,
                    coll_code,
                    f"{coll_code}:{r['code']}" if coll_code else r["code"],
                    r["code"],
                    r.get("name"),
                    r.get("unit"),
                    r.get("table_code"),
                    r.get("table_name"),
                    r.get("department"),
                    r.get("section"),
                    r.get("work_composition"),
                    r.get("direct_cost"),
                    r.get("wages"),
                    r.get("machines_total"),
                    r.get("machines_wages"),
                    r.get("materials"),
                    r.get("labor_hours"),
                    r.get("mass_t"),
                    r.get("volume_m3"),
                )
                for r in ctx.rates
            ],
        )
        conn.executemany(
            """INSERT INTO rate_resources(document_id,rate_code,resource_code,name,unit,quantity)
               VALUES(?,?,?,?,?,?)""",
            [
                (doc_id, r["rate_code"], r["resource_code"], r.get("name"), r.get("unit"), r.get("quantity"))
                for r in ctx.resources
            ],
        )
        conn.executemany(
            """INSERT INTO materials(document_id,pos_code,okp,okpd2,name,unit,mass_net,mass_gross,price)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            [
                (
                    doc_id,
                    m["pos_code"],
                    m.get("okp"),
                    m.get("okpd2"),
                    m.get("name"),
                    m.get("unit"),
                    m.get("mass_net"),
                    m.get("mass_gross"),
                    m.get("price"),
                )
                for m in ctx.materials
            ],
        )
        conn.executemany(
            """INSERT INTO machines(document_id,pos_code,code,okpd2,name,param,price_mach_hour,wages,energy_kwh,section)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    doc_id,
                    m["pos_code"],
                    m.get("code"),
                    m.get("okpd2"),
                    m.get("name"),
                    m.get("param"),
                    m.get("price_mach_hour"),
                    m.get("wages"),
                    m.get("energy_kwh"),
                    m.get("section"),
                )
                for m in ctx.machines
            ],
        )
        line = (
            f"{filename:18} kind={kind:9} pages={len(ctx.texts):4} "
            f"rates={len(ctx.rates):5} res={len(ctx.resources):5} "
            f"mat={len(ctx.materials):5} mach={len(ctx.machines):4}"
        )
        stats.append(line)
        print(line)
        if i % 10 == 0:
            conn.commit()
    conn.execute("INSERT INTO rates_fts(rates_fts) VALUES('rebuild')")
    conn.commit()
    counts = {
        "documents": conn.execute("SELECT count(*) FROM documents").fetchone()[0],
        "rates": conn.execute("SELECT count(*) FROM rates").fetchone()[0],
        "rates_with_price": conn.execute("SELECT count(*) FROM rates WHERE direct_cost IS NOT NULL").fetchone()[0],
        "resources": conn.execute("SELECT count(*) FROM rate_resources").fetchone()[0],
        "materials": conn.execute("SELECT count(*) FROM materials").fetchone()[0],
        "machines": conn.execute("SELECT count(*) FROM machines").fetchone()[0],
        "pages": conn.execute("SELECT count(*) FROM pages").fetchone()[0],
    }
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("counts", json.dumps(counts, ensure_ascii=False)))
    conn.commit()
    conn.close()
    (ROOT / "data" / "parse_stats.txt").write_text("\n".join(stats) + "\n" + json.dumps(counts, ensure_ascii=False, indent=2), encoding="utf-8")
    print("DB", DB_PATH, counts)


if __name__ == "__main__":
    main()
