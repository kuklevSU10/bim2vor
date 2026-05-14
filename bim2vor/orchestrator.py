# -*- coding: utf-8 -*-
"""
Orchestrator: главный pipeline.

Делает следующие шаги:
1. Загружает Revit Excel → элементы
2. Кластеризует
3. Загружает BoQ → позиции с разбивкой по разделам
4. Для каждого специалиста:
   - prefilter своих кластеров
   - готовит briefing (input для SpecialistCell)
   - сохраняет briefing на диск
5. Готовит сводный план запусков

Запуск специалистов выполняется отдельно (через Task-агента или Anthropic API).
Результаты сохраняются в data/specialist_outputs/{key}.json и обрабатываются consolidator'ом.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bim2vor.ingest.boq import BoQReader, BoQPosition
from bim2vor.ingest.cluster import cluster_elements, Cluster
from bim2vor.ingest.revit import RevitReader, Element
from bim2vor.match.prefilter import prefilter_clusters_for_specialist, PrefilterMatch
from bim2vor.reasoning.specialist import load_specialists, SpecialistConfig, SpecialistCell
from bim2vor.taxonomy.ost import OstTaxonomy


REPO_ROOT = Path(__file__).resolve().parents[1]


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(65536)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def trim_clusters_for_briefing(matches: list[PrefilterMatch], max_clusters: int = 80) -> tuple[list[PrefilterMatch], dict]:
    """
    Урезает список кластеров до max_clusters: берём top-N по score+volume.
    Остальные суммируются в footer.
    """
    if len(matches) <= max_clusters:
        return matches, {}
    matches_sorted = sorted(
        matches,
        key=lambda m: -(m.score * 100 + (m.cluster.volume_sum or 0) / 100 + m.cluster.count / 1000),
    )
    selected = matches_sorted[:max_clusters]
    rest = matches_sorted[max_clusters:]
    rest_summary = {
        "n_clusters_omitted": len(rest),
        "total_count": sum(m.cluster.count for m in rest),
        "total_volume_m3": round(sum(m.cluster.volume_sum for m in rest), 2),
        "total_area_m2": round(sum(m.cluster.area_sum for m in rest), 2),
        "note": "Кластеры с меньшим score были опущены для краткости.",
    }
    return selected, rest_summary


def cluster_to_brief(c: Cluster, score: float | None = None) -> dict:
    """Превращает кластер в компактное представление для брифинга специалиста."""
    out = {
        "cluster_id": c.cluster_id,
        "category": c.category,
        "family": c.family,
        "type_name": c.type_name,
        "count": c.count,
        "volume_m3": round(c.volume_sum, 2),
        "area_m2": round(c.area_sum, 2),
        "level_zone_summary": c.level_zone_summary,
    }
    if c.primary_material:
        out["primary_material"] = c.primary_material
    if c.zone_marker:
        out["zone"] = c.zone_marker
    if c.is_underground:
        out["is_underground"] = True
    if c.rei_minutes:
        out["rei"] = c.rei_minutes
    # Слои (для стен)
    if c.family_parsed:
        layers = c.family_parsed.get("layers", []) or []
        if layers:
            out["layers"] = [
                {"material": l.get("material"), "thickness_mm": l.get("thickness_mm")}
                for l in layers
                if l.get("material") != "ventilation_gap"
            ]
        if c.family_parsed.get("total_thickness_mm"):
            out["total_thickness_mm"] = c.family_parsed["total_thickness_mm"]
    if score is not None:
        out["prefilter_score"] = round(score, 2)
    return out


def position_to_brief(p: BoQPosition) -> dict:
    """Превращает BoQ-позицию в компактное представление."""
    return {
        "position_id": p.code,
        "section": p.section,
        "name": p.name,
        "unit": p.unit,
        "qty_planned": p.qty_planned,
        "depth": p.depth,
        "parent": p.parent_code,
    }


def prepare_briefing(
    spec: SpecialistConfig,
    all_clusters: list[Cluster],
    all_positions: list[BoQPosition],
    max_clusters: int = 80,
) -> dict:
    """Готовит briefing JSON для одного специалиста."""

    # 1. Prefilter
    matches = prefilter_clusters_for_specialist(all_clusters, spec)
    selected_matches, omitted_summary = trim_clusters_for_briefing(matches, max_clusters)

    # 2. BoQ позиции из своих разделов (только считаемые, без header'ов)
    # Если есть boq_subsections — фильтруем точнее по коду подраздела
    if spec.boq_subsections:
        prefixes = tuple(spec.boq_subsections)
        spec_positions = [
            p for p in all_positions
            if p.code and any(p.code.startswith(pfx) for pfx in prefixes)
            and not p.is_section_header
        ]
        section_headers = [
            p for p in all_positions
            if p.section in spec.boq_sections and p.is_section_header and p.depth <= 3
        ]
    else:
        spec_positions = [
            p for p in all_positions
            if p.section in spec.boq_sections and not p.is_section_header
        ]
        section_headers = [
            p for p in all_positions
            if p.section in spec.boq_sections and p.is_section_header and p.depth <= 3
        ]

    return {
        "specialist_key": spec.key,
        "specialist_name": spec.name,
        "candidate_clusters": [
            cluster_to_brief(m.cluster, score=m.score) for m in selected_matches
        ],
        "omitted_clusters_summary": omitted_summary if omitted_summary else None,
        "boq_positions": [position_to_brief(p) for p in spec_positions],
        "boq_section_context": [position_to_brief(p) for p in section_headers],
        "total_clusters_in_project": len(all_clusters),
        "candidate_clusters_count": len(matches),
    }


# ---------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------
def run_pipeline(
    revit_xlsx: Path,
    boq_xlsx: Path,
    out_dir: Path,
    project_id: str = "default",
) -> dict:
    """Полный pipeline до момента запуска специалистов."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    briefings_dir = out_dir / "briefings"
    briefings_dir.mkdir(exist_ok=True)

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    print(f"=== bim2vor pipeline run {run_id} ===")
    print(f"Revit: {revit_xlsx}")
    print(f"BoQ:   {boq_xlsx}")

    # 1. Ingest Revit
    print("\n[1/4] Загружаю Revit...")
    elements = list(RevitReader(revit_xlsx, OstTaxonomy()).iter_elements())
    n_physical = sum(1 for e in elements if e.is_physical and not e.is_excluded)
    print(f"  всего: {len(elements)}, физических: {n_physical}")

    # 2. Кластеризация
    print("\n[2/4] Кластеризую...")
    clusters = cluster_elements(elements)
    print(f"  кластеров: {len(clusters)}")

    # 3. Ingest BoQ
    print("\n[3/4] Загружаю BoQ...")
    boq_positions = list(BoQReader(boq_xlsx).iter_positions())
    print(f"  позиций: {len(boq_positions)}")

    # 4. Briefings per specialist
    print("\n[4/4] Готовлю briefings для специалистов...")
    specialists = load_specialists()
    briefings_summary = {}
    for key, spec in specialists.items():
        brief = prepare_briefing(spec, clusters, boq_positions)
        # Сохраняем на диск
        out_path = briefings_dir / f"{key}.json"
        out_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

        # Промпт для просмотра
        cell = SpecialistCell(spec)
        prompt_text = cell.render_prompt(brief)
        prompt_path = briefings_dir / f"{key}.prompt.md"
        prompt_path.write_text(prompt_text, encoding="utf-8")

        n_clusters = len(brief["candidate_clusters"])
        n_pos = len(brief["boq_positions"])
        n_chars = len(prompt_text)
        briefings_summary[key] = {
            "specialist": spec.short_name,
            "candidate_clusters": n_clusters,
            "boq_positions": n_pos,
            "prompt_chars": n_chars,
            "briefing_file": str(out_path.relative_to(REPO_ROOT)),
            "prompt_file": str(prompt_path.relative_to(REPO_ROOT)),
        }
        print(f"  {spec.short_name:25s}  clusters={n_clusters:>3}  positions={n_pos:>3}  prompt={n_chars} chars")

    # 5. Run summary
    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "project_id": project_id,
        "revit_file": str(revit_xlsx),
        "revit_sha256": file_sha256(revit_xlsx),
        "boq_file": str(boq_xlsx),
        "boq_sha256": file_sha256(boq_xlsx),
        "n_elements": len(elements),
        "n_physical_elements": n_physical,
        "n_clusters": len(clusters),
        "n_boq_positions": len(boq_positions),
        "specialists": briefings_summary,
    }
    summary_path = out_dir / "run_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSummary: {summary_path}")

    # Также сохраним кластеры и позиции для consolidator'а
    (out_dir / "all_clusters.json").write_text(
        json.dumps([c.to_dict() for c in clusters], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "all_boq_positions.json").write_text(
        json.dumps([p.to_dict() for p in boq_positions], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    return summary


def load_specialists_from_yaml(yaml_path: Path) -> dict[str, SpecialistConfig]:
    """Загружает специалистов из произвольного yaml."""
    import yaml
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    out = {}
    for key, body in raw.get("specialists", {}).items():
        out[key] = SpecialistConfig(
            key=key,
            name=body["name"],
            short_name=body["short_name"],
            boq_sections=body["boq_sections"],
            boq_section_names=body["boq_section_names"],
            domain_description=body["domain_description"],
            candidate_revit_categories=body.get("candidate_revit_categories", []),
            family_signal_words=body.get("family_signal_words", {}) or {},
            boq_subsections=body.get("boq_subsections"),
            preferred_disciplines=body.get("preferred_disciplines"),
        )
    return out


# ---------------------------------------------------------------------
# Pipeline v2: multi-model через SQLite
# ---------------------------------------------------------------------
def run_pipeline_v2(
    revit_dir: Path,
    boq_xlsx: Path,
    out_dir: Path,
    project_id: str = "default",
    specialists_yaml: Path | None = None,
) -> dict:
    """
    Multi-model pipeline: инжестит все xlsx из папки в SQLite,
    кластеризует через SQL, генерит briefings.
    """
    import sqlite3
    from bim2vor.ingest.loader import ingest_directory
    from bim2vor.ingest.cluster_sql import cluster_from_sql
    from bim2vor.storage.schema import init_db

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    briefings_dir = out_dir / "briefings"
    briefings_dir.mkdir(exist_ok=True)

    run_id = project_id + "_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    started_at = datetime.now(timezone.utc).isoformat()
    db_path = out_dir / "bim2vor.db"

    print(f"=== bim2vor pipeline v2 — run {run_id} ===")
    print(f"Revit dir: {revit_dir}")
    print(f"BoQ: {boq_xlsx}")
    print(f"DB: {db_path}")

    # 1. Init DB
    conn = init_db(db_path)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")

    # 2. Ingest all Revit files
    print("\n[1/4] Инжест выгрузок в SQLite...")
    ingest_results = ingest_directory(conn, revit_dir, run_id)
    n_total = sum(r["total"] for r in ingest_results)
    n_physical = sum(r["physical"] for r in ingest_results)

    # 3. Кластеризация через SQL
    print("\n[2/4] Кластеризую через SQL...")
    clusters = cluster_from_sql(conn, run_id)
    print(f"  кластеров: {len(clusters)}")

    # 4. Ingest BoQ
    print("\n[3/4] Загружаю BoQ...")
    boq_positions = list(BoQReader(boq_xlsx).iter_positions())
    print(f"  позиций: {len(boq_positions)}")

    # 5. Briefings per specialist
    print("\n[4/4] Готовлю briefings для специалистов...")
    if specialists_yaml:
        specialists = load_specialists_from_yaml(specialists_yaml)
    else:
        specialists = load_specialists()
    briefings_summary = {}
    for key, spec in specialists.items():
        brief = prepare_briefing(spec, clusters, boq_positions)
        out_path = briefings_dir / f"{key}.json"
        out_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

        cell = SpecialistCell(spec)
        prompt_text = cell.render_prompt(brief)
        prompt_path = briefings_dir / f"{key}.prompt.md"
        prompt_path.write_text(prompt_text, encoding="utf-8")

        n_cl = len(brief["candidate_clusters"])
        n_pos = len(brief["boq_positions"])
        n_chars = len(prompt_text)
        briefings_summary[key] = {
            "specialist": spec.short_name,
            "candidate_clusters": n_cl,
            "boq_positions": n_pos,
            "prompt_chars": n_chars,
            "briefing_file": str(out_path.relative_to(REPO_ROOT)),
            "prompt_file": str(prompt_path.relative_to(REPO_ROOT)),
        }
        print(f"  {spec.short_name:25s}  clusters={n_cl:>3}  positions={n_pos:>3}  prompt={n_chars} chars")

    # 6. Run summary
    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "project_id": project_id,
        "revit_dir": str(revit_dir),
        "boq_file": str(boq_xlsx),
        "boq_sha256": file_sha256(boq_xlsx),
        "ingest_stats": ingest_results,
        "n_elements": n_total,
        "n_physical_elements": n_physical,
        "n_clusters": len(clusters),
        "n_boq_positions": len(boq_positions),
        "specialists": briefings_summary,
        "db_path": str(db_path),
    }
    summary_path = out_dir / "run_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSummary: {summary_path}")

    (out_dir / "all_clusters.json").write_text(
        json.dumps([c.to_dict() for c in clusters], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "all_boq_positions.json").write_text(
        json.dumps([p.to_dict() for p in boq_positions], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    conn.close()
    return summary


def main():
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    # v1 — старый single-file pipeline (для обратной совместимости)
    revit = Path(r"C:\Users\kuklev.d.s\Downloads\программа\SKLNK_АР_ПД_К2.1_R25_rvt.xlsx")
    boq = Path(r"C:\Users\kuklev.d.s\Downloads\Расчет ПЗ_ВГК№5 (ЖК)_Версия 4.xlsx")
    out = REPO_ROOT / "runs" / "demo_run"
    summary = run_pipeline(revit, boq, out, project_id="SKLNK_demo")
    print(f"\nGot {len(summary['specialists'])} specialists ready to run.")


def main_v2():
    """Запуск multi-model pipeline для Событие 6.1."""
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    revit_dir = REPO_ROOT / "Выгрузка 6.1"
    boq = Path(r"C:\Users\kuklev.d.s\Downloads\Расчет ПЗ_Событие 6.1_Версия 2.xlsx")
    out = REPO_ROOT / "runs" / "event_6_1"
    specialists_yaml = REPO_ROOT / "recipes" / "specialists_6_1.yaml"

    summary = run_pipeline_v2(
        revit_dir, boq, out,
        project_id="SOB_event_6_1",
        specialists_yaml=specialists_yaml,
    )
    print(f"\nGot {len(summary['specialists'])} specialists ready to run.")


if __name__ == "__main__":
    main()
