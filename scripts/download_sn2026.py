"""Download official SN-2012 (01.07.2026) PDFs from mos.ru."""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PDF_DIR = RAW / "pdf"
JSON_PATH = RAW / "mos_document_344770220.json"
MANIFEST = RAW / "manifest.json"
SOURCE_URL = "https://www.mos.ru/depr/documents/cenovaya-politika/cenoobrazovanie-v-gorodskom-khozyaistve/cenoobrazovanie-v-gorodskom-khozyaistve-2026-god/view/344770220/"
API_URL = "https://www.mos.ru/api/documents/v2/frontend/json/ru/documents/344770220"
ORDER_API = "https://www.mos.ru/api/documents/v2/frontend/json/ru/documents/344769220"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


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
            title = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            self.links.append((self._href, title))
            self._href = None
            self._buf = []


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
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"Failed {url}: {last_err}")


def abs_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://www.mos.ru" + href


def chapter_from_name(filename: str, title: str) -> str:
    m = re.search(r"SN_gl(\d+)_sb(\d+)", filename, re.I)
    if m:
        return f"Глава {m.group(1)}, сборник {m.group(2)}"
    m = re.search(r"sn21_(\d+)", filename, re.I)
    if m:
        return f"Глава 21, раздел {m.group(1)}"
    if filename.lower() == "sn21.pdf":
        return "Глава 21, общие положения"
    if filename.lower() == "sn_22.pdf":
        return "Глава 22"
    if filename.lower() == "op.pdf":
        return "Общие положения"
    if "rasporyajenie" in filename.lower() or "распоряжение" in title.lower():
        return "Распоряжение"
    return title[:80]


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    doc = json.loads(fetch(API_URL, JSON_PATH).decode("utf-8"))
    parser = LinkParser()
    parser.feed(doc.get("text") or "")

    items: list[dict] = []
    seen: set[str] = set()
    for href, title in parser.links:
        if not href.lower().endswith(".pdf"):
            continue
        url = abs_url(href)
        filename = Path(href).name
        if filename.lower() in seen:
            continue
        seen.add(filename.lower())
        items.append(
            {
                "filename": filename,
                "title": title,
                "url": url,
                "rel_path": f"data/raw/pdf/{filename}",
                "chapter": chapter_from_name(filename, title),
            }
        )

    # Official approving order
    try:
        order = json.loads(fetch(ORDER_API).decode("utf-8"))
        op = LinkParser()
        op.feed(order.get("text") or "")
        extra_title = order.get("title") or "Распоряжение ДПР-Р-15/26"
        extra_hrefs = [(h, t or extra_title) for h, t in op.links if h.lower().endswith(".pdf")]
        if not extra_hrefs:
            extra_hrefs = [
                (
                    "/upload/documents/files/7280/Rasporyajenieot25062026_DPR-R-15_26_.pdf",
                    extra_title,
                )
            ]
        for href, title in extra_hrefs:
            filename = Path(href).name
            if filename.lower() in seen:
                continue
            seen.add(filename.lower())
            items.append(
                {
                    "filename": filename,
                    "title": title or extra_title,
                    "url": abs_url(href),
                    "rel_path": f"data/raw/pdf/{filename}",
                    "chapter": "Распоряжение",
                }
            )
    except Exception as exc:  # noqa: BLE001
        print("order download metadata failed:", exc, file=sys.stderr)

    print(f"files to download: {len(items)}")

    def one(item: dict) -> dict:
        dest = ROOT / item["rel_path"]
        if dest.exists() and dest.stat().st_size > 1000:
            data = dest.read_bytes()
        else:
            data = fetch(item["url"], dest)
        item["bytes"] = len(data)
        item["sha256"] = hashlib.sha256(data).hexdigest()
        item["ok"] = data[:5] == b"%PDF-" or dest.suffix.lower() != ".pdf"
        print(f"{'OK' if item['ok'] else 'BAD':3} {item['bytes']:10}  {item['filename']}")
        return item

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(one, item) for item in items]
        for fut in as_completed(futs):
            results.append(fut.result())

    results.sort(key=lambda x: x["filename"])
    manifest = {
        "source_page": SOURCE_URL,
        "source_api": API_URL,
        "title": doc.get("title"),
        "date_published": doc.get("date_published"),
        "price_level": "01.07.2026",
        "order": "Распоряжение ДЭПР от 25.06.2026 № ДПР-Р-15/26",
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": results,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    bad = [r for r in results if not r.get("ok")]
    print(f"done {len(results)} files, bad={len(bad)}, total_mb={sum(r['bytes'] for r in results)/1048576:.1f}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
