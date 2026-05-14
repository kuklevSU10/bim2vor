# -*- coding: utf-8 -*-
"""
Кластеризатор: 40k элементов → ~200 кластеров.

Идея: эксперты не видят отдельные элементы, а работают с КЛАСТЕРАМИ
(одна family + один уровневый профиль = один кластер).
Каждый кластер несёт count, total_volume, total_area, levels, families_parsed.

Это даёт smart-data-handling:
- Sonnet за один вызов видит структуру всей модели в виде ~200 строк
- Каждая строка имеет всю агрегированную информацию для решения
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from bim2vor.ingest.revit import Element


@dataclass
class Cluster:
    """Группа схожих элементов. Один кластер = одна строка в брифе для эксперта."""
    cluster_id: str                              # детерминированный (cat::family::type::zone)
    category: str                                # canonical: walls/floors/...
    category_raw: str
    family: str | None
    type_name: str | None
    level_zone_summary: str                      # "regular 1-32" / "basement+regular" / "technical"
    level_floors: list[int] = field(default_factory=list)
    count: int = 0
    volume_sum: float = 0.0
    area_sum: float = 0.0
    length_sum: float = 0.0
    family_parsed: dict | None = None            # parsed family info (one-shot per family)
    sample_element_ids: list[str] = field(default_factory=list)
    workset_top: list[tuple[str, int]] = field(default_factory=list)
    is_underground: bool = False
    primary_material: str | None = None
    rei_minutes: int | None = None
    zone_marker: str | None = None               # МОП/shafts/bay_windows

    def to_dict(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "category": self.category,
            "family": self.family,
            "type_name": self.type_name,
            "count": self.count,
            "volume_m3": round(self.volume_sum, 2),
            "area_m2": round(self.area_sum, 2),
            "length_m": round(self.length_sum, 2),
            "level_zone": self.level_zone_summary,
            "level_floors": sorted(set(self.level_floors)) if self.level_floors else [],
            "primary_material": self.primary_material,
            "rei": self.rei_minutes,
            "zone_marker": self.zone_marker,
            "is_underground": self.is_underground,
            "family_parsed": self.family_parsed,
        }


def _cluster_key(elem: Element) -> tuple:
    """Ключ группировки. Чем грубее ключ, тем меньше кластеров."""
    return (
        elem.category_canonical,
        elem.family,
        elem.type_name,
    )


def _summarize_levels(floors: list[int | None], zones: list[str]) -> str:
    valid = [f for f in floors if f is not None]
    zones_s = sorted(set(z for z in zones if z and z != "unknown"))
    if not valid:
        return ",".join(zones_s) if zones_s else "—"
    lo, hi = min(valid), max(valid)
    base = f"{lo}-{hi}" if lo != hi else str(lo)
    if zones_s:
        base = f"{','.join(zones_s)} ({base})"
    return base


def cluster_elements(elements: Iterable[Element]) -> list[Cluster]:
    """Группирует элементы. Возвращает отсортированный по count список кластеров."""
    groups: dict[tuple, list[Element]] = defaultdict(list)
    for elem in elements:
        if elem.is_excluded:
            continue
        if not elem.is_physical and elem.category_canonical != "rooms":
            # Rooms оставляем — они дают контекст для отделки
            if elem.category_canonical not in ("apartment_type", "room_group"):
                continue
        groups[_cluster_key(elem)].append(elem)

    clusters: list[Cluster] = []
    for key, items in groups.items():
        cat, family, type_name = key
        zones = [e.level_zone for e in items]
        floors = [e.level_floor for e in items]
        worksets: dict[str, int] = defaultdict(int)
        for e in items:
            if e.workset:
                worksets[e.workset] += 1

        # Парсим family один раз (берём из первого элемента где он есть)
        fp = next((e.family_parsed for e in items if e.family_parsed), None)
        primary_material = None
        rei = None
        is_ug = False
        zone_marker = None
        if fp:
            # Извлекаем главный материал
            from bim2vor.parser.family import WallLayer
            layers = fp.get("layers", []) or []
            non_ventilation = [l for l in layers if l.get("material") != "ventilation_gap"]
            if non_ventilation:
                primary_material = max(
                    non_ventilation, key=lambda l: l.get("thickness_mm") or 0
                ).get("material")
            rei = fp.get("rei_minutes")
            is_ug = bool(fp.get("is_underground"))
            zone_marker = fp.get("zone")

        cluster_id = "::".join(str(p) if p is not None else "_" for p in key)
        c = Cluster(
            cluster_id=cluster_id,
            category=cat,
            category_raw=items[0].category_raw,
            family=family,
            type_name=type_name,
            level_zone_summary=_summarize_levels(floors, zones),
            level_floors=[f for f in floors if f is not None],
            count=len(items),
            volume_sum=sum(e.volume_m3 or 0 for e in items),
            area_sum=sum(e.area_m2 or 0 for e in items),
            length_sum=sum(e.length_m or 0 for e in items),
            family_parsed=fp,
            sample_element_ids=[e.element_id for e in items[:5]],
            workset_top=sorted(worksets.items(), key=lambda x: -x[1])[:3],
            is_underground=is_ug,
            primary_material=primary_material,
            rei_minutes=rei,
            zone_marker=zone_marker,
        )
        clusters.append(c)

    clusters.sort(key=lambda c: -c.count)
    return clusters


def main():
    import sys, io, json
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    from bim2vor.ingest.revit import RevitReader

    fp = Path(r"C:\Users\kuklev.d.s\Downloads\программа\SKLNK_АР_ПД_К2.1_R25_rvt.xlsx")
    print("Читаю элементы...")
    elements = list(RevitReader(fp).iter_elements())
    print(f"  всего: {len(elements)}")
    print(f"  физических не-excluded: {sum(1 for e in elements if e.is_physical and not e.is_excluded)}")

    print("\nКластеризация...")
    clusters = cluster_elements(elements)
    print(f"Кластеров: {len(clusters)}")

    print(f"\n=== Топ-30 кластеров ===")
    for c in clusters[:30]:
        material = f" mat={c.primary_material}" if c.primary_material else ""
        rei = f" REI{c.rei_minutes}" if c.rei_minutes else ""
        zone = f" zone={c.zone_marker}" if c.zone_marker else ""
        print(f"  {c.count:>5}  V={c.volume_sum:>7.0f}м³  A={c.area_sum:>7.0f}м²  | {c.category:10s}  {(c.family or '-')[:50]}")
        print(f"          floors={c.level_zone_summary}{material}{rei}{zone}")

    # Сохраняем для проверки
    out = Path(r"C:\Users\kuklev.d.s\PycharmProjects\bim2vor\data\clusters.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps([c.to_dict() for c in clusters], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nСохранено: {out}")


if __name__ == "__main__":
    main()
