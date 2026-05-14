# -*- coding: utf-8 -*-
"""
Pre-filter: для каждого специалиста выбрать его кандидатные кластеры по signal words.

Это удешевляет вызов LLM-эксперта: вместо 200 кластеров он получает ~30-50 релевантных.
Эксперт сам делает финальный filter (claim/reject), но не на пустом месте.

Pre-filter работает в "wide mode" — лучше чуть переборщить с кандидатами,
чем что-то упустить. Финальное решение — за экспертом.
"""
from __future__ import annotations

from dataclasses import dataclass

from bim2vor.ingest.cluster import Cluster
from bim2vor.reasoning.specialist import SpecialistConfig


@dataclass
class PrefilterMatch:
    cluster: Cluster
    score: float                      # 0..1 — насколько уверены что это кандидат
    matched_signals: list[str]


def _name_match(name: str | None, words: list[str]) -> list[str]:
    if not name:
        return []
    name_l = name.lower()
    return [w for w in words if w.lower() in name_l]


def prefilter_clusters_for_specialist(
    clusters: list[Cluster],
    spec: SpecialistConfig,
) -> list[PrefilterMatch]:
    """
    Возвращает список кандидатных кластеров для специалиста + score.

    Эвристика:
    1. Если category in candidate_revit_categories → +0.5
    2. Сигналы в family include words → +0.3..0.5
    3. Сигналы в family exclude words → -0.5
    4. Zone match → +0.2
    """
    candidates_canon = set()
    for ost_code in spec.candidate_revit_categories:
        # маппинг OST → canonical (через taxonomy если нужно)
        # для simplicity: вытаскиваем последнюю часть OST_X → x.lower()
        canon = ost_code.replace("OST_", "").lower()
        # стандартный канонический маппинг — должно совпадать с taxonomy
        manual_map = {
            "walls": "walls",
            "floors": "floors",
            "doors": "doors",
            "windows": "windows",
            "roofs": "roofs",
            "stairs": "stairs",
            "structuralcolumns": "structural_columns",
            "structuralframing": "structural_framing",
            "structuralfoundation": "foundation",
            "ceilings": "ceilings",
            "rooms": "rooms",
            "site": "site",
            "genericmodel": "generic",
            "stairsrailing": "railings",
            "curtainwallpanels": "curtain_panels",
            "curtainwallmullions": "curtain_mullions",
        }
        candidates_canon.add(manual_map.get(canon, canon))

    include_words = spec.family_signal_words.get("include", []) or []
    exclude_words = spec.family_signal_words.get("exclude", []) or []
    zone_filter = spec.family_signal_words.get("zone_filter")
    if isinstance(zone_filter, str):
        zone_filter = [zone_filter]
    elif zone_filter is None:
        zone_filter = []

    results: list[PrefilterMatch] = []
    for c in clusters:
        score = 0.0
        signals = []

        # Category match
        if c.category in candidates_canon:
            score += 0.5
            signals.append(f"category:{c.category}")
        else:
            # Если категория не в кандидатах — пропускаем
            continue

        # Family include
        matches = _name_match(c.family, include_words)
        if matches:
            score += 0.3 + min(0.2, len(matches) * 0.1)
            signals += [f"include:{m}" for m in matches]

        # Family exclude
        excl = _name_match(c.family, exclude_words)
        if excl:
            score -= 0.5
            signals += [f"exclude:{m}" for m in excl]

        # Zone filter
        if zone_filter:
            zone_matches = []
            zone_text = " ".join(filter(None, [c.family, c.zone_marker]))
            zone_matches = _name_match(zone_text, zone_filter)
            if zone_matches:
                score += 0.2
                signals += [f"zone:{m}" for m in zone_matches]

        # Material match (для monolith — нужен concrete или foundation)
        if c.primary_material:
            include_materials_for_specialist = {
                "monolith": ["concrete"],
                "masonry": ["block", "brick", "aerated_block"],
                "facades": ["insulation", "insulation_mineral", "finish", "plaster"],
                "finishing_mop": ["plaster", "finish"],
                "finishing_apartments": ["plaster", "finish"],
                "roofing": ["concrete"],  # плоская кровля = бетон + слои
            }.get(spec.key, [])
            if c.primary_material in include_materials_for_specialist:
                score += 0.2
                signals.append(f"material:{c.primary_material}")

        if score > 0.3:    # threshold
            results.append(PrefilterMatch(cluster=c, score=min(1.0, score), matched_signals=signals))

    results.sort(key=lambda m: -m.score)
    return results


def main():
    """Тест на реальных данных."""
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    from pathlib import Path
    from bim2vor.ingest.revit import RevitReader
    from bim2vor.ingest.cluster import cluster_elements
    from bim2vor.reasoning.specialist import load_specialists

    print("Загружаю элементы и кластеризую...")
    fp = Path(r"C:\Users\kuklev.d.s\Downloads\программа\SKLNK_АР_ПД_К2.1_R25_rvt.xlsx")
    elements = list(RevitReader(fp).iter_elements())
    clusters = cluster_elements(elements)
    print(f"Кластеров: {len(clusters)}")

    specs = load_specialists()
    print(f"Специалистов: {len(specs)}\n")

    for key, spec in specs.items():
        matches = prefilter_clusters_for_specialist(clusters, spec)
        total_v = sum(m.cluster.volume_sum for m in matches)
        total_a = sum(m.cluster.area_sum for m in matches)
        print(f"\n=== {spec.short_name.upper()} ({key}) ===")
        print(f"Кандидатных кластеров: {len(matches)}, V={total_v:.0f} м³, A={total_a:.0f} м²")
        for m in matches[:8]:
            print(f"  score={m.score:.2f}  count={m.cluster.count:>4}  "
                  f"V={m.cluster.volume_sum:.0f} A={m.cluster.area_sum:.0f}  "
                  f"| {(m.cluster.family or '-')[:60]}")
            print(f"          signals: {m.matched_signals}")


if __name__ == "__main__":
    main()
