# -*- coding: utf-8 -*-
"""
Multi-file loader: сканирует папку выгрузок, инжестит все Excel в SQLite.

Стриминг: файлы читаются по одному через RevitReader (read_only=True),
элементы пишутся в БД батчами. Это позволяет обрабатывать 700k+ строк
без загрузки в память.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from bim2vor.ingest.revit import RevitReader, Element
from bim2vor.storage.schema import init_db
from bim2vor.taxonomy.ost import OstTaxonomy


FILE_DISCIPLINES = {
    "BA_AR":       {"discipline": "AR", "part": "башня",       "description": "Архитектура башни"},
    "FC_S1_AR":    {"discipline": "AR", "part": "фасад_с1",    "description": "Архитектура фасада секция 1"},
    "FC_S2-S3_AR": {"discipline": "AR", "part": "фасад_с2-с3", "description": "Архитектура фасада секции 2-3"},
    "PR_AR":       {"discipline": "AR", "part": "паркинг",     "description": "Архитектура паркинга"},
    "KR":          {"discipline": "KR", "part": "конструктив", "description": "Несущие конструкции"},
    "OV":          {"discipline": "OV", "part": "ов",          "description": "Отопление и вентиляция"},
    "VK":          {"discipline": "VK", "part": "вк",          "description": "Водоснабжение и канализация"},
    "EOM":         {"discipline": "EOM", "part": "эом",        "description": "Электрооборудование"},
    "SS":          {"discipline": "SS", "part": "сс",          "description": "Слаботочные системы"},
}


def parse_source_model(filename: str) -> tuple[str, str, str]:
    """
    Парсит имя файла выгрузки → (source_model, discipline, part).
    'SOB_ATR_PD_K1_BA_AR_2022_rvt.xlsx' → ('BA_AR', 'AR', 'башня')
    'SOB_STR_PD_K1_KR_2022_rvt.xlsx' → ('KR', 'KR', 'конструктив')
    """
    stem = Path(filename).stem
    for key, info in FILE_DISCIPLINES.items():
        if key in stem:
            return key, info["discipline"], info["part"]
    return stem, "UNKNOWN", "unknown"


INSERT_SQL = """
INSERT OR IGNORE INTO elements (
    run_id, source_model, discipline, element_id, unique_id,
    category, category_raw, family, type_name,
    level_raw, level_floor, level_zone, workset,
    volume_m3, area_m2, length_m, width_m, cost,
    is_physical, is_excluded, excluded_reason,
    family_parsed_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _element_to_row(elem: Element, run_id: str, source_model: str, discipline: str) -> tuple:
    return (
        run_id, source_model, discipline,
        elem.element_id, elem.unique_id,
        elem.category_canonical, elem.category_raw,
        elem.family, elem.type_name,
        elem.level_raw, elem.level_floor, elem.level_zone,
        elem.workset,
        elem.volume_m3, elem.area_m2, elem.length_m, elem.width_m, elem.cost,
        int(elem.is_physical), int(elem.is_excluded), elem.excluded_reason,
        json.dumps(elem.family_parsed, ensure_ascii=False) if elem.family_parsed else None,
    )


def ingest_file(
    conn: sqlite3.Connection,
    xlsx_path: Path,
    run_id: str,
    taxonomy: OstTaxonomy | None = None,
    batch_size: int = 5000,
) -> dict:
    """Инжестит один Excel-файл в SQLite. Возвращает статистику."""
    source_model, discipline, part = parse_source_model(xlsx_path.name)
    taxonomy = taxonomy or OstTaxonomy()
    reader = RevitReader(xlsx_path, taxonomy)

    stats = Counter()
    batch: list[tuple] = []
    t0 = time.time()

    for elem in reader.iter_elements():
        row = _element_to_row(elem, run_id, source_model, discipline)
        batch.append(row)
        stats["total"] += 1
        if elem.is_physical and not elem.is_excluded:
            stats["physical"] += 1
        if elem.is_excluded:
            stats["excluded"] += 1

        if len(batch) >= batch_size:
            conn.executemany(INSERT_SQL, batch)
            batch.clear()

    if batch:
        conn.executemany(INSERT_SQL, batch)
    conn.commit()

    elapsed = time.time() - t0
    return {
        "source_model": source_model,
        "discipline": discipline,
        "part": part,
        "file": xlsx_path.name,
        "total": stats["total"],
        "physical": stats["physical"],
        "excluded": stats["excluded"],
        "elapsed_sec": round(elapsed, 1),
    }


def ingest_directory(
    conn: sqlite3.Connection,
    directory: Path,
    run_id: str,
    taxonomy: OstTaxonomy | None = None,
) -> list[dict]:
    """Инжестит все xlsx из папки. Возвращает список статистик по файлам."""
    taxonomy = taxonomy or OstTaxonomy()
    xlsx_files = sorted(directory.glob("*.xlsx"))
    if not xlsx_files:
        raise FileNotFoundError(f"Нет xlsx-файлов в {directory}")

    results = []
    total_t0 = time.time()

    for i, f in enumerate(xlsx_files, 1):
        if f.name.startswith("~$"):
            continue
        print(f"[{i}/{len(xlsx_files)}] {f.name} ...", flush=True)
        stats = ingest_file(conn, f, run_id, taxonomy)
        print(f"  → {stats['total']} строк ({stats['physical']} физ.), {stats['elapsed_sec']}с")
        results.append(stats)

    total_elapsed = time.time() - total_t0
    total_rows = sum(r["total"] for r in results)
    total_phys = sum(r["physical"] for r in results)
    print(f"\nИтого: {total_rows} строк, {total_phys} физических, {round(total_elapsed, 1)}с")
    return results


def main():
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]

    revit_dir = repo / "Выгрузка 6.1"
    db_path = repo / "runs" / "bim2vor.db"
    run_id = "event_6_1_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print(f"Инжест из: {revit_dir}")
    print(f"БД: {db_path}")
    print(f"Run ID: {run_id}")
    print()

    conn = init_db(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")

    results = ingest_directory(conn, revit_dir, run_id)

    print("\n=== Проверка в БД ===")
    for row in conn.execute(
        "SELECT source_model, discipline, COUNT(*), SUM(is_physical), SUM(is_excluded) "
        "FROM elements WHERE run_id=? GROUP BY source_model ORDER BY COUNT(*) DESC",
        (run_id,),
    ):
        print(f"  {row[0]:15s} disc={row[1]:4s}  total={row[2]:>7}  phys={row[3]:>7}  excl={row[4]:>7}")

    conn.close()
    print("\nГотово.")


if __name__ == "__main__":
    main()
