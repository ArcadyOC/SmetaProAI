"""Shared helpers for talking to the mos.ru documents API.

The public API has no working server-side search: any `filter[...]` parameter
answers HTTP 500 and `q=` is ignored. Sorting does work, so a new quarterly
release is found by walking `sort=-date_published` back to a known date.
"""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PDF_DIR = DATA / "pdf"
MANIFEST = DATA / "manifest.json"
DOC_DUMP = DATA / "mos_document.json"
RELEASES = DATA / "releases.json"
ARCHIVE = DATA / "archive"
DB_DIR = DATA / "db"
REPORTS = DATA / "reports"

API_ROOT = "https://www.mos.ru/api/documents/v2/frontend/json/ru/documents"
SITE_ROOT = "https://www.mos.ru"
SECTION_URL = (
    "https://www.mos.ru/depr/documents/cenovaya-politika/cenoobrazovanie-v-gorodskom-khozyaistve"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# mos.ru titles are peppered with word joiners and soft hyphens: "СН-⁠2012".
INVISIBLE = dict.fromkeys(map(ord, "\u2060\u00ad\u200b\ufeff"), None)

RELEASE_TITLE = re.compile(
    r"Сборник\s+СН-?\s?2012\s+в\s+текущих\s+ценах\s+по\s+состоянию\s+на\s+(\d{2}\.\d{2}\.\d{4})",
    re.I,
)
ORDER_TITLE = re.compile(
    r"Распоряжени\w+.*?№\s*(ДПР-Р-\d+/\d+).*?СН-?\s?2012.*?на\s+(\d{2}\.\d{2}\.\d{4})",
    re.I | re.S,
)
CHAPTER_PDF = re.compile(r"SN_gl\d+_sb\d+\.pdf$", re.I)
MATERIALS_PDF = re.compile(r"sn21(_\d+)?\.pdf$", re.I)


def use_utf8_stdout() -> None:
    """Windows consoles default to cp1251 and blow up on Cyrillic output."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.translate(INVISIBLE).replace("\u00a0", " ")).strip()


def price_level_to_slug(price_level: str) -> str:
    day, month, year = price_level.split(".")
    return f"{year}-{month}-{day}"


def fetch(url: str, dest: Path | None = None, retries: int = 5) -> bytes:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                data = resp.read()
            if dest is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            return data
        except Exception as exc:  # noqa: BLE001 - urllib raises a zoo of errors
            last_err = exc
            time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"Не удалось скачать {url}: {last_err}")


def api_document(doc_id: int, dest: Path | None = None) -> dict:
    """Fetch one document. Attachments (e.g. the approving order) need `expand`."""
    return json.loads(fetch(f"{API_ROOT}/{doc_id}?expand=attachments", dest).decode("utf-8"))


def api_page(page: int, per_page: int = 50) -> dict:
    url = f"{API_ROOT}?per-page={per_page}&page={page}&sort=-date_published"
    return json.loads(fetch(url).decode("utf-8"))


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href = href
                self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, clean("".join(self._buf))))
            self._href = None
            self._buf = []


def pdf_links(html: str | None) -> list[tuple[str, str]]:
    parser = LinkParser()
    parser.feed(html or "")
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for href, title in parser.links:
        if not href.lower().endswith(".pdf"):
            continue
        name = href.rsplit("/", 1)[-1].lower()
        if name in seen:
            continue
        seen.add(name)
        out.append((href if href.startswith("http") else SITE_ROOT + href, title))
    return out


def abs_url(href: str) -> str:
    return href if href.startswith("http") else SITE_ROOT + href


def attachment_links(doc: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in doc.get("attachments") or []:
        url = item.get("url") or ""
        if url.lower().endswith(".pdf"):
            out.append((abs_url(url), clean(item.get("name"))))
    return out


def merge_links(*groups: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for group in groups:
        for url, title in group:
            name = url.rsplit("/", 1)[-1].lower()
            if name in seen:
                continue
            seen.add(name)
            out.append((url, title))
    return out


@dataclass
class Release:
    price_level: str
    document_id: int
    title: str
    date_published: str
    files: list[tuple[str, str]]
    order_document_id: int | None = None
    order_number: str = ""

    @property
    def is_plausible(self) -> bool:
        """A real release lists ~97 PDFs across chapters; refuse anything thinner."""
        chapters = sum(1 for url, _ in self.files if CHAPTER_PDF.search(url))
        materials = sum(1 for url, _ in self.files if MATERIALS_PDF.search(url))
        return len(self.files) >= 50 and chapters >= 40 and materials >= 5

    def describe(self) -> str:
        return (
            f"СН-2012 в ценах на {self.price_level} — документ {self.document_id}, "
            f"опубликован {self.date_published}, файлов {len(self.files)}"
        )


def load_release(doc_id: int, dest: Path | None = None) -> Release:
    doc = api_document(doc_id, dest)
    title = clean(doc.get("title"))
    match = RELEASE_TITLE.search(title)
    if not match:
        raise ValueError(f"Документ {doc_id} не похож на выпуск СН-2012: {title!r}")
    return Release(
        price_level=match.group(1),
        document_id=doc_id,
        title=title,
        date_published=doc.get("date_published") or "",
        files=merge_links(pdf_links(doc.get("text")), attachment_links(doc)),
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def discover_releases(
    since: datetime,
    max_pages: int = 400,
    workers: int = 8,
    progress: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Walk newest-first documents down to `since`, collecting SN-2012 candidates.

    Returns (release candidates, approving order candidates) as raw API items.
    """
    releases: list[dict] = []
    orders: list[dict] = []
    page = 1
    scanned = 0
    stop = False
    while not stop and page <= max_pages:
        batch = list(range(page, min(page + workers, max_pages + 1)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            payloads = list(pool.map(api_page, batch))
        for payload in payloads:
            items = payload.get("items") or []
            if not items:
                stop = True
                break
            for item in items:
                scanned += 1
                published = _parse_dt(item.get("date_published"))
                if published and published < since:
                    stop = True
                    continue
                title = clean(item.get("title"))
                if RELEASE_TITLE.search(title):
                    releases.append(item)
                elif ORDER_TITLE.search(title):
                    orders.append(item)
        page = batch[-1] + 1
        if progress:
            print(
                f"  просмотрено документов: {scanned}, найдено выпусков: {len(releases)}",
                flush=True,
            )
    return releases, orders


def latest_release(
    since: datetime,
    max_pages: int = 400,
    progress: bool = True,
) -> Release | None:
    """Find the newest published SN-2012 release and its approving order."""
    releases, orders = discover_releases(since, max_pages=max_pages, progress=progress)
    if not releases:
        return None

    def level_key(item: dict) -> tuple[int, int, int]:
        match = RELEASE_TITLE.search(clean(item.get("title")))
        day, month, year = (match.group(1) if match else "01.01.1970").split(".")
        return int(year), int(month), int(day)

    newest = max(releases, key=level_key)
    release = load_release(int(newest["id"]))
    for order in sorted(orders, key=lambda x: -int(x.get("id") or 0)):
        match = ORDER_TITLE.search(clean(order.get("title")))
        if match and match.group(2) == release.price_level:
            release.order_document_id = int(order["id"])
            release.order_number = match.group(1)
            break
    return release


def read_releases() -> dict:
    if RELEASES.exists():
        return json.loads(RELEASES.read_text(encoding="utf-8"))
    return {"current": None, "releases": []}


def write_releases(state: dict) -> None:
    RELEASES.parent.mkdir(parents=True, exist_ok=True)
    RELEASES.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def known_levels(state: dict) -> set[str]:
    return {entry.get("price_level") for entry in state.get("releases", [])}


def last_published(state: dict) -> datetime | None:
    dates = [_parse_dt(entry.get("date_published")) for entry in state.get("releases", [])]
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def db_path_for(price_level: str) -> Path:
    return DB_DIR / f"sn2012_{price_level_to_slug(price_level)}.sqlite"
