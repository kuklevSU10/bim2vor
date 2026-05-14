# -*- coding: utf-8 -*-
"""
OST-таксономия: классификация Revit-категорий на физические/мусорные.
Источник истины: recipes/ost_dictionary.yaml
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
OST_YAML = REPO_ROOT / "recipes" / "ost_dictionary.yaml"


@dataclass(frozen=True)
class CategoryInfo:
    """Информация о категории Revit."""
    raw: str                          # как пришло из Revit ("OST_Walls" или "3M_88-108 м2")
    canonical: str                    # каноническое имя ("walls"/"apartment_type"/"unknown")
    russian: str                      # человеко-читаемо
    is_physical: bool                 # имеет ли физический объём
    is_excluded: bool                 # мусор
    excluded_reason: str | None
    default_unit: str | None
    note: str | None


# Паттерны user-defined "квартирных" типов (приходят в Category, не в Family)
# Примеры: "3M_88-108 м2", "2M_62-74 м2", "Ст 28-32 м2", "ПХ_140-250 м2"
APARTMENT_TYPE_RE = re.compile(
    r'^(?:'
    r'(?P<rooms>\d+)(?P<class>[MSK])_(?P<area_min>\d+)-(?P<area_max>\d+)\s*м2'
    r'|(?P<studio>Ст)\s+(?P<sa_min>\d+)-(?P<sa_max>\d+)\s*м2'
    r'|(?P<ph>ПХ)_(?P<pa_min>\d+)-(?P<pa_max>\d+)\s*м2'
    r')'
)

ROOM_GROUP_NAMES = {
    'Холлы и лестницы',
    'Технические помещения',
    'Места общего пользования',
    'МОП',
}


class OstTaxonomy:
    """Загружает и применяет ost-словарь."""

    def __init__(self, yaml_path: Path = OST_YAML):
        self._yaml_path = yaml_path
        self._raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        self._table: dict[str, dict[str, Any]] = self._raw.get("categories", {})

    def classify(self, category_raw: str | None) -> CategoryInfo:
        """Классифицирует одну категорию из выгрузки."""
        if not category_raw:
            return CategoryInfo(
                raw="", canonical="unknown", russian="(пусто)",
                is_physical=False, is_excluded=True,
                excluded_reason="empty_category",
                default_unit=None, note=None,
            )

        s = str(category_raw).strip()

        # 1. Прямой OST_-маппинг
        if s.startswith("OST_"):
            entry = self._table.get(s)
            if entry:
                return CategoryInfo(
                    raw=s,
                    canonical=entry.get("canonical", "unknown"),
                    russian=entry.get("russian", s),
                    is_physical=bool(entry.get("is_physical", False)),
                    is_excluded=bool(entry.get("excluded", False)),
                    excluded_reason=entry.get("reason"),
                    default_unit=entry.get("default_unit"),
                    note=entry.get("note"),
                )
            # Неизвестная OST_-категория → не мусор, но требует доразметки
            return CategoryInfo(
                raw=s, canonical="unknown_ost", russian=s,
                is_physical=False, is_excluded=False,
                excluded_reason=None, default_unit=None,
                note=f"Unknown OST category: {s}",
            )

        # 2. Apartment-типы ("3M_88-108 м2")
        if APARTMENT_TYPE_RE.match(s):
            return CategoryInfo(
                raw=s, canonical="apartment_type", russian=f"Тип квартиры: {s}",
                is_physical=False, is_excluded=False,
                excluded_reason=None, default_unit="m2",
                note="User-defined apartment type — for zoning, not a physical element",
            )

        # 3. Имена групп помещений
        if s in ROOM_GROUP_NAMES:
            return CategoryInfo(
                raw=s, canonical="room_group", russian=s,
                is_physical=False, is_excluded=False,
                excluded_reason=None, default_unit="m2",
                note="Room group / zone marker",
            )

        # 4. Прочее — неизвестно, не мусор
        return CategoryInfo(
            raw=s, canonical="unknown", russian=s,
            is_physical=False, is_excluded=False,
            excluded_reason=None, default_unit=None,
            note="Unrecognized category — needs manual mapping",
        )


def main():
    """Прогоняем словарь по реальной выгрузке для проверки покрытия."""
    import json
    profile = json.loads((REPO_ROOT / "data" / "revit_profile.json").read_text(encoding="utf-8"))
    tax = OstTaxonomy()

    physical_count = 0
    excluded_count = 0
    unknown_count = 0
    apartment_count = 0
    total_elements = 0

    issues = []
    for cat_raw, stats in profile["categories"].items():
        info = tax.classify(cat_raw)
        n = stats["count"]
        total_elements += n
        if info.is_excluded:
            excluded_count += n
        elif info.is_physical:
            physical_count += n
        elif info.canonical == "apartment_type":
            apartment_count += n
        else:
            unknown_count += n
            issues.append((cat_raw, n, info.canonical))

    print(f"Всего элементов: {total_elements}")
    print(f"Физические:      {physical_count:>8} ({100 * physical_count / total_elements:5.1f}%)")
    print(f"Apartment-типы:  {apartment_count:>8} ({100 * apartment_count / total_elements:5.1f}%)")
    print(f"Исключённые:     {excluded_count:>8} ({100 * excluded_count / total_elements:5.1f}%)")
    print(f"Неклассифициров: {unknown_count:>8} ({100 * unknown_count / total_elements:5.1f}%)")

    if issues:
        print(f"\nТребуют доразметки ({len(issues)}):")
        for cat, n, canon in issues[:20]:
            print(f"  [{canon}] {n:>6}  {cat}")


if __name__ == "__main__":
    main()
