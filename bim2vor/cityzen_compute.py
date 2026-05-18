# -*- coding: utf-8 -*-
"""
Cityzen deterministic compute — без LLM, через keyword matching.

Для каждой BoQ position B3:
  1. Определяет cluster-фильтр по title + parent_path
  2. Применяет rule (full_cluster / layer_split / zone_filter)
  3. Считает qty per source S1 (AR), S2 (KR), S3 (merged dedup)
  4. Convergence check (abs_tol)
  5. Записывает в source_qtys + final_values

Honest: где соответствия не уверены → fill_status='needs_llm_classification',
qty=NULL. Не делаем wild guesses.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Abs tolerances
ABS_TOL = {
    "м³": 0.1, "м3": 0.1,
    "м²": 0.5, "м2": 0.5,
    "тн": 0.01, "т": 0.01,
    "шт": 0, "компл": 0,
    "пог.м": 0.1, "пог.м.": 0.1, "м": 0.1, "м.п.": 0.1, "м.п": 0.1, "м/п": 0.1,
    "кг": 1,
}

# Density steel kg/m³
DENSITY_STEEL = 7850

# Rebar norms (kg / m³ concrete) for S4 normative back-calc
REBAR_NORMS = {
    "foundation_slab": (80, 120),
    "walls_underground": (60, 100),
    "walls_above_ground": (40, 80),
    "columns": (100, 150),
    "slabs_floors": (60, 100),
    "lintels": (50, 80),
    "ростверк": (100, 150),
}


# =============================================================================
# BoQ position → cluster filter
# =============================================================================
@dataclass
class PositionFilter:
    """Описывает как position преобразуется в выборку clusters."""
    boq_row: int
    rule_kind: str  # full_cluster | layer_split | zone_filter | count_only | needs_llm | not_in_bim_scope
    categories: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    family_includes: list[str] = field(default_factory=list)
    family_excludes: list[str] = field(default_factory=list)
    layer_material: str | None = None
    zone_filter: str | None = None  # underground | above_ground | mixed
    fill_status_hint: str | None = None
    rule_notes: str = ""


def classify_position(p: dict) -> PositionFilter:
    """Определяет filter для BoQ position на основе title + parent_path."""
    title = (p["name"] or "")
    parent = p.get("parent_path") or ""
    unit = (p["unit"] or "").strip()
    spec = p.get("specialist_key")
    full_text = f"{title} | {parent}"
    pf = PositionFilter(boq_row=p["row"], rule_kind="needs_llm")

    # Zone hints from parent_path/title
    if re.search(r"подзем|подвал|фундамент", full_text, re.I):
        pf.zone_filter = "underground"
    elif re.search(r"надзем|выше|кровл", full_text, re.I):
        pf.zone_filter = "above_ground"

    # === Monolith family ===
    if spec == "monolith":
        # Concrete material requirement
        pf.materials = ["concrete"]
        pf.fill_status_hint = "monolith_compute"

        # Fundamental plate
        if re.search(r"фундаментн.{0,5}плит", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["foundation"]
            pf.family_includes = [r"фплит", r"плит\s*фунд"]
            pf.rule_notes = "Фундаментная плита"
        # Ростверк
        elif re.search(r"ростверк", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["foundation"]
            pf.family_includes = [r"ростверк"]
            pf.rule_notes = "Ростверк"
        # Plates перекрытия
        elif re.search(r"плит.+перекр|перекрыти|покрыти", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["floors"]
            pf.family_includes = [r"перекрыт", r"покрыт", r"межэтаж"]
            pf.family_excludes = [r"подготовк", r"щебень", r"тс", r"цпс", r"стяжк"]
            pf.rule_notes = "Плита перекрытия монолитная"
        # Walls
        elif re.search(r"\bстен", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["walls"]
            pf.family_includes = [r"стена", r"стен-"]
            pf.family_excludes = [r"парапет", r"блок", r"кирпич"]
            pf.rule_notes = "Стены монолитные ЖБ"
            # Match concrete grade — HARD: must be present
            m = re.search(r"\bB(\d{2})\b", title)
            if m:
                grade = m.group(1)
                # Need to find clusters whose family contains B{grade}
                # Both Latin B and Cyrillic В look identical — match both
                pf.family_includes = pf.family_includes + [f"b{grade}", f"в{grade}"]
                pf.rule_notes += f" grade B{grade}"
            else:
                pf.fill_status_hint = "wall_no_grade_in_title"
            # Zone hint: 4.1 → underground, 4.2.X → above_ground
            if "4.1" in parent:
                pf.zone_filter = "underground"
            elif "4.2" in parent:
                pf.zone_filter = "above_ground"
        # Колонны
        elif re.search(r"колон", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["structural_columns"]
            pf.rule_notes = "Колонны монолитные"
            m = re.search(r"\bB(\d{2})\b", title)
            if m:
                grade = m.group(1)
                pf.family_includes = [f"b{grade}", f"в{grade}"]
                pf.rule_notes += f" grade B{grade}"
            if "4.1" in parent:
                pf.zone_filter = "underground"
            elif "4.2" in parent:
                pf.zone_filter = "above_ground"
        # Пилоны
        elif re.search(r"пилон", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["structural_columns"]
            pf.family_includes = [r"пилон"]
            pf.rule_notes = "Пилоны"
        # Балки / ригели
        elif re.search(r"\bбалк|ригел", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["structural_framing"]
            pf.family_includes = [r"балк"]
            pf.rule_notes = "Балки/ригели"
        # Лестничные марши/площадки
        elif re.search(r"лестничн.{0,5}(марш|площадк)", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["stairs", "stairs_landings", "floors"]
            pf.family_includes = [r"лпл", r"лм", r"лестниц", r"марш", r"площадк"]
            pf.rule_notes = "Лестничные марши/площадки монолит"
        # Рампа
        elif re.search(r"рампа", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["floors"]
            pf.family_includes = [r"рамп"]
            pf.rule_notes = "Рампа монолит"
        # Парапет
        elif re.search(r"парапет", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["walls"]
            pf.family_includes = [r"парапет"]
            pf.rule_notes = "Парапет"
        # Капитель
        elif re.search(r"капител", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["structural_framing", "floors"]
            pf.family_includes = [r"капител"]
            pf.rule_notes = "Капитель"
        # Подбетонка
        elif re.search(r"подбетонк|бет\.?\s*подгот", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["floors", "foundation"]
            pf.family_includes = [r"подбетонк", r"бетон.{0,5}подгот"]
            pf.rule_notes = "Бетонная подготовка"
        # Сваи / срезка
        elif re.search(r"свая|оголов", title, re.I):
            pf.rule_kind = "count_only"
            pf.categories = ["structural_columns", "structural_framing", "generic", "foundation"]
            pf.family_includes = [r"свая", r"оголов"]
            pf.rule_notes = "Сваи / срезка оголовков"
        # Гидроизоляция / стяжки / мембраны / прочие "monolith-attached" но не монолит
        elif re.search(r"гидроизол|мембран|техноэласт|техниколь|герметик|шпонка|шланг|штуцер|ппс|пенополистирол|утеплит|щебень|цпс|stuk|стяжка|шланг|пена|лента", title, re.I):
            pf.rule_kind = "needs_llm"
            pf.fill_status_hint = "section_3_waterproofing_aux"
            pf.rule_notes = "Гидроизоляция/материалы — auxiliary, requires LLM classification or manual"
        else:
            pf.rule_kind = "needs_llm"
            pf.rule_notes = f"monolith position unclassified: '{title[:60]}'"

    # === Masonry ===
    elif spec == "masonry":
        pf.materials = ["block", "brick", "aerated_block"]
        if re.search(r"перемычк", title, re.I):
            pf.rule_kind = "count_only"
            pf.categories = ["walls", "generic"]
            pf.family_includes = [r"перемычк"]
            pf.rule_notes = "Перемычка"
        elif re.search(r"кладк|блок|кирпич", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["walls"]
            pf.rule_notes = "Кладка"
        else:
            pf.rule_kind = "needs_llm"

    # === Roofing ===
    elif spec == "roofing":
        if re.search(r"кровл|покрыти", title, re.I):
            pf.rule_kind = "layer_split"
            pf.categories = ["roofs", "floors"]
            pf.rule_notes = "Кровельный пирог (нужен layer split)"
            pf.fill_status_hint = "roofing_layered"
        elif re.search(r"парапет", title, re.I):
            pf.rule_kind = "full_cluster"
            pf.categories = ["walls"]
            pf.family_includes = [r"парапет"]
            pf.rule_notes = "Парапет"
        else:
            pf.rule_kind = "needs_llm"

    # === Facades ===
    elif spec == "facades":
        if re.search(r"\bокн[ао]|подоконник|витраж", title, re.I):
            pf.rule_kind = "count_only"
            pf.categories = ["windows", "curtain_panels"]
            pf.rule_notes = "Окна"
        elif re.search(r"утепл|изоляция", title, re.I):
            pf.rule_kind = "layer_split"
            pf.categories = ["walls"]
            pf.layer_material = "insulation"
            pf.rule_notes = "Утепление слоя стены"
        elif re.search(r"штукатурк|отделк", title, re.I):
            pf.rule_kind = "layer_split"
            pf.categories = ["walls"]
            pf.layer_material = "plaster"
            pf.rule_notes = "Штукатурка слоя стены"
        else:
            pf.rule_kind = "needs_llm"

    # === Doors ===
    elif spec == "doors":
        pf.rule_kind = "count_only"
        pf.categories = ["doors"]
        pf.rule_notes = "Двери"

    # === Elevators ===
    elif spec == "elevators":
        pf.rule_kind = "count_only"
        pf.categories = ["generic", "elevator"]
        pf.family_includes = [r"лифт", r"elevator"]
        pf.rule_notes = "Лифт комплект"

    # === Metal stairs ===
    elif spec == "metal_stairs":
        if re.search(r"перил|огражд", title, re.I):
            pf.rule_kind = "count_only"
            pf.categories = ["railings"]
            pf.rule_notes = "Ограждения"
        elif re.search(r"лестниц", title, re.I):
            pf.rule_kind = "count_only"
            pf.categories = ["stairs", "railings"]
            pf.rule_notes = "Лестницы металлические"
        else:
            pf.rule_kind = "needs_llm"

    # === Отделка (МОП, паркинг, квартиры) ===
    elif spec in ("finishing_mop", "finishing_parking", "finishing_apartments"):
        if unit in ("м2", "м²"):
            if re.search(r"пол|стяжк|плитка|линолеум|керамогр", title, re.I):
                pf.rule_kind = "needs_llm"
                pf.rule_notes = "Полы отделка"
            elif re.search(r"стен|штукатурк|шпатлёвк|шпатлёвка|окраск|краск|обои|потолок", title, re.I):
                pf.rule_kind = "needs_llm"
                pf.rule_notes = "Стены/потолок отделка"
            else:
                pf.rule_kind = "needs_llm"
        else:
            pf.rule_kind = "needs_llm"

    return pf


# =============================================================================
# Compute qty per source
# =============================================================================
@dataclass
class SourceQty:
    source: str       # S1_AR_only / S2_KR_only / S3_merged
    qty: float
    n_clusters: int
    n_elements: int
    notes: str = ""


def compute_qty_for_position(conn: sqlite3.Connection, p: dict, pf: PositionFilter) -> dict:
    """Compute qty per source for one BoQ position."""
    cur = conn.cursor()
    unit = (p["unit"] or "").strip()
    spec = p.get("specialist_key")
    abs_tol = ABS_TOL.get(unit, 0.1)

    sources: list[SourceQty] = []
    result = {
        "boq_row": p["row"],
        "rule": {
            "kind": pf.rule_kind,
            "categories": pf.categories,
            "materials": pf.materials,
            "family_includes": pf.family_includes,
            "family_excludes": pf.family_excludes,
            "layer_material": pf.layer_material,
            "zone_filter": pf.zone_filter,
            "notes": pf.rule_notes,
        },
        "unit": unit,
        "abs_tol": abs_tol,
        "sources": [],
        "qty_final": None,
        "delta_abs": None,
        "zone": None,
        "fill_status": pf.fill_status_hint or "computed",
        "preferred_source": None,
    }

    if pf.rule_kind in ("needs_llm", "not_in_bim_scope"):
        result["fill_status"] = pf.fill_status_hint or "needs_llm_classification"
        return result

    # Build SQL filter
    where_parts = []
    params = []
    if pf.categories:
        placeholders = ",".join(["?"] * len(pf.categories))
        where_parts.append(f"category IN ({placeholders})")
        params.extend(pf.categories)
    if pf.materials:
        # primary_material check OR family contains ЖБ/Бетон/B40 etc
        if "concrete" in pf.materials:
            where_parts.append("(primary_material='concrete' OR family LIKE '%ЖБ%' OR family LIKE '%Бетон%' OR family LIKE '%B40%' OR family LIKE '%B30%' OR family LIKE '%B25%' OR family LIKE '%B50%' OR family LIKE '%B60%' OR family LIKE '%B70%')")
        elif any(m in pf.materials for m in ("block", "brick", "aerated_block")):
            placeholders = ",".join(["?"] * len(pf.materials))
            where_parts.append(f"primary_material IN ({placeholders})")
            params.extend(pf.materials)

    if pf.family_includes:
        inc_clauses = []
        for pat in pf.family_includes:
            inc_clauses.append("(pylower(family) LIKE ? OR pylower(type_name) LIKE ?)")
            pat_sql = f"%{pat.lower().lstrip('^').rstrip('$')}%"
            params.extend([pat_sql, pat_sql])
        where_parts.append("(" + " OR ".join(inc_clauses) + ")")
    if pf.family_excludes:
        for pat in pf.family_excludes:
            where_parts.append("(pylower(family) NOT LIKE ? AND (pylower(type_name) IS NULL OR pylower(type_name) NOT LIKE ?))")
            pat_sql = f"%{pat.lower()}%"
            params.extend([pat_sql, pat_sql])
    # Zone filter through level_floor (Cityzen: подземная = level<0 OR 0 для 4.1; надземная = level>0)
    if pf.zone_filter == "underground":
        where_parts.append("(level_floor < 1 OR level_floor IS NULL)")
    elif pf.zone_filter == "above_ground":
        where_parts.append("level_floor >= 1")

    where_clause = " AND ".join(where_parts) if where_parts else "1=1"

    # Aggregate metric depending on unit
    if unit in ("м³", "м3"):
        agg = "COALESCE(SUM(volume_m3), 0)"
    elif unit in ("м²", "м2"):
        agg = "COALESCE(SUM(area_m2), 0)"
    elif unit in ("шт", "компл"):
        agg = "COUNT(*)"
    elif unit in ("пог.м", "пог.м.", "м", "м.п.", "м.п", "м/п"):
        agg = "COALESCE(SUM(length_m), 0)"
    elif unit in ("тн", "т"):
        # Steel mass via density
        if "concrete" in pf.materials:
            # рассчёт через S4 normative_backcalc — skip for now
            result["fill_status"] = "s4_normative_required"
            return result
        agg = f"COALESCE(SUM(volume_m3), 0) * {DENSITY_STEEL} / 1000.0"  # tons
    elif unit == "кг":
        agg = f"COALESCE(SUM(volume_m3), 0) * {DENSITY_STEEL}"
    else:
        result["fill_status"] = f"unsupported_unit:{unit}"
        return result

    # S1 = AR-only
    sql_s1 = f"SELECT {agg}, COUNT(DISTINCT cluster_id), COUNT(*) FROM elements WHERE source_discipline='AR' AND {where_clause}"
    s1 = cur.execute(sql_s1, params).fetchone()
    if s1 and s1[1] > 0:
        sources.append(SourceQty("S1_AR_only", round(s1[0] or 0, 3), s1[1], s1[2]))

    # S2 = KR-only
    sql_s2 = f"SELECT {agg}, COUNT(DISTINCT cluster_id), COUNT(*) FROM elements WHERE source_discipline='KR' AND {where_clause}"
    s2 = cur.execute(sql_s2, params).fetchone()
    if s2 and s2[1] > 0:
        sources.append(SourceQty("S2_KR_only", round(s2[0] or 0, 3), s2[1], s2[2]))

    # S3 = merged dedup — per cluster_id берём max(sum_AR_per_cluster, sum_KR_per_cluster)
    # Это: для каждого кластера агрегируем AR vol и KR vol отдельно, потом max
    if unit in ("м³", "м3"):
        sql_s3 = f"""
        SELECT SUM(max_vol), COUNT(*), SUM(n_elem) FROM (
            SELECT cluster_id,
                MAX(
                    SUM(CASE WHEN source_discipline='AR' THEN COALESCE(volume_m3,0) ELSE 0 END),
                    SUM(CASE WHEN source_discipline='KR' THEN COALESCE(volume_m3,0) ELSE 0 END)
                ) AS max_vol,
                COUNT(*) AS n_elem
            FROM elements
            WHERE {where_clause}
            GROUP BY cluster_id
        )
        """
        # SQLite не поддерживает MAX(aggregate1, aggregate2) — обход через max() column-wise
        # Используем subquery с двумя aggregates
        sql_s3 = f"""
        SELECT SUM(CASE WHEN ar_v > kr_v THEN ar_v ELSE kr_v END), COUNT(*), SUM(n_elem)
        FROM (
            SELECT cluster_id,
                SUM(CASE WHEN source_discipline='AR' THEN COALESCE(volume_m3,0) ELSE 0 END) AS ar_v,
                SUM(CASE WHEN source_discipline='KR' THEN COALESCE(volume_m3,0) ELSE 0 END) AS kr_v,
                COUNT(*) AS n_elem
            FROM elements WHERE {where_clause} GROUP BY cluster_id
        )
        """
        try:
            s3 = cur.execute(sql_s3, params).fetchone()
            if s3 and s3[0]:
                sources.append(SourceQty("S3_merged", round(s3[0], 3), s3[1] or 0, s3[2] or 0, notes="max(sum_AR, sum_KR) per cluster"))
        except sqlite3.Error as e:
            pass

    # Convergence check
    result["sources"] = [{"source": s.source, "qty": s.qty, "n_clusters": s.n_clusters, "n_elements": s.n_elements, "notes": s.notes} for s in sources]
    if len(sources) >= 2:
        qtys = [s.qty for s in sources]
        delta = max(qtys) - min(qtys)
        result["delta_abs"] = round(delta, 3)
        if delta <= abs_tol:
            # Green — pick median (or average)
            result["qty_final"] = round(sum(qtys) / len(qtys), 3)
            result["zone"] = "green"
            result["fill_status"] = "ok_converged"
            # Prefer S2 if available
            result["preferred_source"] = "S2_KR_only" if any(s.source == "S2_KR_only" for s in sources) else sources[0].source
        else:
            # Red — divergent
            result["qty_final"] = None
            result["zone"] = "red"
            result["fill_status"] = "divergent_sources"
            result["preferred_source"] = None
    elif len(sources) == 1:
        result["qty_final"] = sources[0].qty
        result["zone"] = "yellow"
        result["fill_status"] = "single_method_only"
        result["preferred_source"] = sources[0].source
        result["delta_abs"] = 0
    else:
        # Zero matches
        result["qty_final"] = 0 if unit in ("шт", "компл") and pf.rule_kind == "count_only" else None
        result["zone"] = "red" if result["qty_final"] is None else "yellow"
        result["fill_status"] = "no_matches_in_bim"
        result["preferred_source"] = None

    return result


# =============================================================================
# Main runner
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Cityzen det compute")
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()

    db_path = args.run_dir / "bim2vor.sqlite"
    conn = sqlite3.connect(db_path)
    # SQLite LOWER не поддерживает Cyrillic — регистрируем Python str.lower
    conn.create_function('pylower', 1, lambda s: s.lower() if s else None)
    cur = conn.cursor()

    print(f"=== Cityzen det compute ===")
    print(f"DB: {db_path}")

    # Load all BoQ positions
    positions = []
    for r in cur.execute("SELECT row, code, name, unit, qty_planned, parent_path, block_assignment, specialist_key FROM boq_positions ORDER BY row"):
        positions.append({
            "row": r[0], "code": r[1], "name": r[2], "unit": r[3], "qty_planned": r[4],
            "parent_path": r[5], "block_assignment": r[6], "specialist_key": r[7],
        })
    print(f"BoQ positions: {len(positions)}")

    # Process each
    n_green = n_yellow = n_red = n_needs_llm = 0
    results = []
    for p in positions:
        pf = classify_position(p)
        result = compute_qty_for_position(conn, p, pf)
        results.append(result)
        z = result.get("zone")
        if z == "green": n_green += 1
        elif z == "yellow": n_yellow += 1
        elif z == "red": n_red += 1
        if result["fill_status"] in ("needs_llm_classification", "section_3_waterproofing_aux"):
            n_needs_llm += 1

    print(f"\nResults distribution:")
    print(f"  🟢 green:    {n_green}")
    print(f"  🟡 yellow:   {n_yellow}")
    print(f"  🔴 red:      {n_red}")
    print(f"  💭 needs LLM: {n_needs_llm}")
    print(f"  ─ untouched: {len(positions) - n_green - n_yellow - n_red - n_needs_llm}")

    # Store source_qtys + final_values
    cur.execute("DELETE FROM source_qtys")
    cur.execute("DELETE FROM final_values")
    for r in results:
        for s in r["sources"]:
            cur.execute("INSERT INTO source_qtys VALUES (?,?,?,?,?,?)",
                        (r["boq_row"], s["source"], s["qty"], s["n_clusters"], s["n_elements"], s.get("notes", "")))
        cur.execute("INSERT INTO final_values VALUES (?,?,?,?,?,?,?,?)",
                    (r["boq_row"], r["qty_final"], r["delta_abs"], r["abs_tol"],
                     r["zone"], r["fill_status"], r["preferred_source"],
                     len(r["sources"])))
    conn.commit()

    # Dump full results JSON
    (args.run_dir / "compute_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nResults: {args.run_dir / 'compute_results.json'}")

    conn.close()


if __name__ == "__main__":
    main()
