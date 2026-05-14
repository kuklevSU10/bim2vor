# -*- coding: utf-8 -*-
"""
SQL-based кластеризация: работает через БД вместо загрузки 700k+ элементов в память.

Результат идентичен cluster.py: список Cluster с агрегатами,
но вычисляется SQL-запросом за доли секунды.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from bim2vor.ingest.cluster import Cluster


CLUSTER_SQL = """
SELECT
    category,
    family,
    type_name,
    COUNT(*) as cnt,
    SUM(COALESCE(volume_m3, 0)) as vol,
    SUM(COALESCE(area_m2, 0)) as area,
    SUM(COALESCE(length_m, 0)) as len,
    GROUP_CONCAT(DISTINCT level_zone) as zones,
    GROUP_CONCAT(DISTINCT source_model) as sources,
    GROUP_CONCAT(DISTINCT discipline) as disciplines,
    MIN(level_floor) as min_floor,
    MAX(level_floor) as max_floor,
    -- Для family_parsed берём первый непустой
    (SELECT family_parsed_json FROM elements e2
     WHERE e2.run_id = elements.run_id
       AND e2.category = elements.category
       AND COALESCE(e2.family, '') = COALESCE(elements.family, '')
       AND COALESCE(e2.type_name, '') = COALESCE(elements.type_name, '')
       AND e2.family_parsed_json IS NOT NULL
     LIMIT 1) as family_parsed_json
FROM elements
WHERE run_id = ?
  AND is_excluded = 0
  AND (is_physical = 1 OR category IN ('rooms', 'apartment_type', 'room_group'))
GROUP BY category, COALESCE(family, ''), COALESCE(type_name, '')
ORDER BY COUNT(*) DESC
"""


def cluster_from_sql(conn: sqlite3.Connection, run_id: str) -> list[Cluster]:
    """Кластеризация через SQL. Возвращает list[Cluster] совместимый с остальным pipeline."""
    clusters: list[Cluster] = []

    for row in conn.execute(CLUSTER_SQL, (run_id,)):
        cat, family, type_name, cnt, vol, area, length, zones_str, sources_str, disc_str, min_fl, max_fl, fp_json = row

        family = family or None
        type_name = type_name or None
        zones = zones_str.split(",") if zones_str else []
        sources = sources_str.split(",") if sources_str else []

        family_parsed = None
        primary_material = None
        rei = None
        is_ug = False
        zone_marker = None
        if fp_json:
            try:
                family_parsed = json.loads(fp_json)
                layers = family_parsed.get("layers", []) or []
                non_vent = [l for l in layers if l.get("material") != "ventilation_gap"]
                if non_vent:
                    primary_material = max(non_vent, key=lambda l: l.get("thickness_mm") or 0).get("material")
                rei = family_parsed.get("rei_minutes")
                is_ug = bool(family_parsed.get("is_underground"))
                zone_marker = family_parsed.get("zone")
            except (json.JSONDecodeError, TypeError):
                pass

        floors = []
        if min_fl is not None:
            floors = list(range(min_fl, (max_fl or min_fl) + 1))

        level_summary = _summarize_levels(min_fl, max_fl, zones)

        cluster_id = "::".join(str(p) if p is not None else "_" for p in (cat, family, type_name))
        c = Cluster(
            cluster_id=cluster_id,
            category=cat or "",
            category_raw=cat or "",
            family=family,
            type_name=type_name,
            level_zone_summary=level_summary,
            level_floors=floors,
            count=cnt,
            volume_sum=vol or 0.0,
            area_sum=area or 0.0,
            length_sum=length or 0.0,
            family_parsed=family_parsed,
            sample_element_ids=[],
            workset_top=[],
            is_underground=is_ug,
            primary_material=primary_material,
            rei_minutes=rei,
            zone_marker=zone_marker,
        )
        clusters.append(c)

    return clusters


def _summarize_levels(min_fl: int | None, max_fl: int | None, zones: list[str]) -> str:
    zones_s = sorted(set(z for z in zones if z and z != "unknown"))
    if min_fl is None:
        return ",".join(zones_s) if zones_s else "—"
    base = f"{min_fl}-{max_fl}" if min_fl != max_fl else str(min_fl)
    if zones_s:
        base = f"{','.join(zones_s)} ({base})"
    return base


def main():
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    repo = Path(__file__).resolve().parents[2]
    db_path = repo / "runs" / "bim2vor.db"

    conn = sqlite3.connect(str(db_path))

    # Найти последний run_id
    cur = conn.execute(
        "SELECT DISTINCT run_id FROM elements ORDER BY run_id DESC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        print("Нет данных в БД. Сначала запустите loader.py")
        return
    run_id = row[0]
    print(f"Run ID: {run_id}")

    clusters = cluster_from_sql(conn, run_id)
    print(f"Кластеров: {len(clusters)}")

    print(f"\n=== Топ-30 кластеров ===")
    for c in clusters[:30]:
        mat = f" mat={c.primary_material}" if c.primary_material else ""
        print(f"  {c.count:>6}  V={c.volume_sum:>8.0f}м³  A={c.area_sum:>8.0f}м²  | {c.category:15s}  {(c.family or '-')[:50]}{mat}")

    # Статистика по категориям
    print(f"\n=== По категориям ===")
    cat_stats: dict[str, tuple[int, int, float, float]] = defaultdict(lambda: (0, 0, 0.0, 0.0))
    for c in clusters:
        n_cl, n_el, v, a = cat_stats[c.category]
        cat_stats[c.category] = (n_cl + 1, n_el + c.count, v + c.volume_sum, a + c.area_sum)
    for cat in sorted(cat_stats, key=lambda k: -cat_stats[k][1]):
        n_cl, n_el, v, a = cat_stats[cat]
        print(f"  {cat:20s}  clusters={n_cl:>4}  elements={n_el:>7}  V={v:>10.0f}м³  A={a:>10.0f}м²")

    conn.close()


if __name__ == "__main__":
    main()
