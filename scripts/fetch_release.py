"""Download one SN-2012 release (all chapter PDFs) from mos.ru."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import mosru
from mosru import Release

DEFAULT_SINCE = datetime(2026, 6, 1)


def chapter_of(filename: str, title: str) -> str:
    m = re.search(r"SN_gl(\d+)_sb(\d+)", filename, re.I)
    if m:
        return f"Глава {m.group(1)}, сборник {m.group(2)}"
    m = re.search(r"sn21_(\d+)", filename, re.I)
    if m:
        return f"Глава 21, раздел {m.group(1)}"
    low = filename.lower()
    if low == "sn21.pdf":
        return "Глава 21, общие положения"
    if low == "sn_22.pdf":
        return "Глава 22"
    if low == "op.pdf":
        return "Общие положения"
    if "rasporyajenie" in low:
        return "Распоряжение"
    return title[:80]


def resolve_release(args: argparse.Namespace) -> Release | None:
    if args.doc_id:
        release = mosru.load_release(args.doc_id)
        if args.order_doc_id:
            order = mosru.api_document(args.order_doc_id)
            match = mosru.ORDER_TITLE.search(mosru.clean(order.get("title")))
            release.order_document_id = args.order_doc_id
            release.order_number = match.group(1) if match else ""
        return release

    state = mosru.read_releases()
    since = DEFAULT_SINCE
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d")
    elif last := mosru.last_published(state):
        since = last
    print(f"Ищу новый выпуск СН-2012 среди документов с {since:%d.%m.%Y}…")
    return mosru.latest_release(since, max_pages=args.max_pages)


def archive_previous(price_level: str) -> None:
    """Keep only the current quarter in the working tree; stash the rest."""
    manifest = mosru.MANIFEST
    if not manifest.exists():
        return
    old = json.loads(manifest.read_text(encoding="utf-8")).get("price_level")
    if not old or old == price_level:
        return
    target = mosru.ARCHIVE / mosru.price_level_to_slug(old)
    target.mkdir(parents=True, exist_ok=True)
    if mosru.PDF_DIR.exists():
        if (target / "pdf").exists():
            shutil.rmtree(target / "pdf")
        shutil.move(str(mosru.PDF_DIR), str(target / "pdf"))
    shutil.copy2(manifest, target / "manifest.json")
    print(f"Прошлый выпуск {old} убран в {target}")


def download_release(release: Release, workers: int = 6) -> dict:
    mosru.PDF_DIR.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    for url, title in release.files:
        filename = url.rsplit("/", 1)[-1]
        items.append(
            {
                "filename": filename,
                "title": title,
                "url": url,
                "rel_path": f"data/pdf/{filename}",
                "chapter": chapter_of(filename, title),
            }
        )

    if release.order_document_id:
        order = mosru.api_document(release.order_document_id)
        order_links = mosru.merge_links(
            mosru.attachment_links(order), mosru.pdf_links(order.get("text"))
        )
        for url, title in order_links[:1]:
            filename = url.rsplit("/", 1)[-1]
            if any(i["filename"].lower() == filename.lower() for i in items):
                continue
            items.append(
                {
                    "filename": filename,
                    "title": title or mosru.clean(order.get("title")),
                    "url": url,
                    "rel_path": f"data/pdf/{filename}",
                    "chapter": "Распоряжение",
                }
            )

    print(f"Файлов к загрузке: {len(items)}")

    def one(item: dict) -> dict:
        dest = mosru.ROOT / item["rel_path"]
        if dest.exists() and dest.stat().st_size > 1000:
            data = dest.read_bytes()
        else:
            data = mosru.fetch(item["url"], dest)
        item["bytes"] = len(data)
        item["sha256"] = hashlib.sha256(data).hexdigest()
        item["ok"] = data[:5] == b"%PDF-"
        print(f"{'OK' if item['ok'] else 'БИТЫЙ':6} {item['bytes']:9}  {item['filename']}", flush=True)
        return item

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda x: x["filename"])

    bad = [r for r in results if not r["ok"]]
    if bad:
        raise RuntimeError(f"Скачаны битые файлы: {', '.join(r['filename'] for r in bad)}")

    order_text = ""
    if release.order_number:
        order_text = f"Распоряжение ДЭПР № {release.order_number}"
    manifest = {
        "source_page": f"{mosru.SECTION_URL}-{release.price_level[-4:]}-god/view/{release.document_id}/",
        "source_api": f"{mosru.API_ROOT}/{release.document_id}",
        "document_id": release.document_id,
        "order_document_id": release.order_document_id,
        "title": release.title,
        "date_published": release.date_published,
        "price_level": release.price_level,
        "order": order_text,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": results,
    }
    mosru.MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def register(manifest: dict) -> None:
    state = mosru.read_releases()
    entry = {
        "price_level": manifest["price_level"],
        "document_id": manifest["document_id"],
        "order_document_id": manifest.get("order_document_id"),
        "order": manifest.get("order"),
        "date_published": manifest.get("date_published"),
        "files": len(manifest["files"]),
        "bytes": sum(f["bytes"] for f in manifest["files"]),
        "manifest_sha256": mosru.sha256_file(mosru.MANIFEST),
        "downloaded_at": manifest["downloaded_at"],
    }
    others = [r for r in state.get("releases", []) if r.get("price_level") != entry["price_level"]]
    state["releases"] = sorted(
        others + [entry],
        key=lambda r: mosru.price_level_to_slug(r["price_level"]),
    )
    state["current"] = entry["price_level"]
    mosru.write_releases(state)


def main() -> int:
    mosru.use_utf8_stdout()
    parser = argparse.ArgumentParser(description="Скачать выпуск СН-2012 с mos.ru")
    parser.add_argument("--doc-id", type=int, help="ID документа на mos.ru (иначе ищем свежий)")
    parser.add_argument("--order-doc-id", type=int, help="ID документа с распоряжением (к --doc-id)")
    parser.add_argument("--since", help="Искать документы, опубликованные с даты YYYY-MM-DD")
    parser.add_argument("--max-pages", type=int, default=400, help="Предел страниц API при поиске")
    parser.add_argument("--force", action="store_true", help="Перекачать, даже если выпуск уже известен")
    args = parser.parse_args()

    release = resolve_release(args)
    if release is None:
        print("Новых выпусков не найдено.")
        return 0
    print(release.describe())
    if not release.is_plausible:
        print("Структура документа не похожа на полный сборник — загрузка отменена.")
        return 2

    state = mosru.read_releases()
    if release.price_level in mosru.known_levels(state) and not args.force:
        print(f"Выпуск {release.price_level} уже загружен. Для перезагрузки добавьте --force.")
        return 0

    archive_previous(release.price_level)
    mosru.api_document(release.document_id, mosru.DOC_DUMP)
    manifest = download_release(release)
    register(manifest)
    total_mb = sum(f["bytes"] for f in manifest["files"]) / 1048576
    print(
        f"Готово: {len(manifest['files'])} файлов, {total_mb:.1f} МБ, "
        f"уровень цен {manifest['price_level']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
