# -*- coding: utf-8 -*-
"""
Парсер имён семейств Revit (стен/перекрытий) → структурированное описание слоёв.

Формат имён, который часто встречается:
  ПРЕФИКС-Слой1Толщина_Слой2Толщина_ЗОНА Общая_толщина

Примеры:
  СВ-Блоки80 80                                  → внутренняя, 1 слой блок 80мм
  СН-Блок200_Изоляция160_Продух90 450            → наружная, 3 слоя
  УН-Бетон300_Штукатурка20_МОП 320               → монолит МОП
  _Ф_СН-Блок200_Изоляция160 360                  → подвальная наружная, 2 слоя
  СВ-Блок ячеистый 100 (REI 240)_Отделка50_МОП 150  → REI 240 + МОП

Стратегия:
1. Regex-парсинг (быстро, детерминистично, бесплатно)
2. Если не распарсилось — отправить в FamilyParserCell (LLM с reasoning)

Принцип: каждое имя парсится один раз и кешируется. На 22к стен с ~100 уникальных
семейств — это всего ~100 LLM-вызовов в худшем случае, обычно 5-15 (только unknown).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from typing import Any


# Префиксы стен
WALL_PREFIXES = {
    "СН": "external",         # стена наружная
    "СВ": "internal",         # стена внутренняя / самонесущая
    "УН": "monolith_node",    # узел/монолитный (бетон)
    "_Ф_": "underground",     # фундамент / подвал
}

# Опциональный модификатор префикса
UNDERGROUND_PREFIX_RE = re.compile(r"^_Ф_")

# Зоны
ZONE_KEYWORDS = {
    "МОП": "common_areas",
    "квартиры": "apartments",
    "паркинг": "parking",
    "тех": "technical",
    "шахты": "shafts",
    "эркеры": "bay_windows",
    "Кровля": "roof",
}

# Известные материалы слоёв (нормализация)
MATERIAL_PATTERNS: list[tuple[str, str]] = [
    # (regex pattern → canonical name)
    (r"(?:^|[\s_])Бетон\b", "concrete"),
    (r"(?:^|[\s_])Блок\s+ячеист", "aerated_block"),
    (r"(?:^|[\s_])Блоки?\b", "block"),
    (r"(?:^|[\s_])Кирпич", "brick"),
    (r"(?:^|[\s_])Изоляция", "insulation"),
    (r"(?:^|[\s_])Минеральная", "insulation_mineral"),
    (r"(?:^|[\s_])Утеплит", "insulation"),
    (r"(?:^|[\s_])Штукатурка", "plaster"),
    (r"(?:^|[\s_])Отделка", "finish"),
    (r"(?:^|[\s_])Продух", "ventilation_gap"),
    (r"(?:^|[\s_])Гипс", "gypsum"),
    (r"(?:^|[\s_])Пенобет", "foam_concrete"),
]


@dataclass
class WallLayer:
    raw: str                                  # исходный фрагмент имени ("Бетон300")
    material: str                             # canonical: concrete/block/brick/insulation/...
    thickness_mm: int | None                  # 300

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WallFamilyInfo:
    raw_family: str
    parsed: bool                              # успешно ли распарсилось
    parse_method: str                         # regex|llm|unknown
    prefix: str | None                        # СН/СВ/УН/_Ф_
    structure_kind: str | None                # external/internal/monolith_node/...
    is_underground: bool                      # начинается с _Ф_
    zone: str | None                          # common_areas/apartments/parking/...
    rei_minutes: int | None                   # огнестойкость REI
    layers: list[WallLayer] = field(default_factory=list)
    total_thickness_mm: int | None = None     # явно указано в конце имени
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["layers"] = [layer if isinstance(layer, dict) else layer for layer in d["layers"]]
        return d

    def primary_material(self) -> str | None:
        """Главный материал стены — самый толстый слой."""
        if not self.layers:
            return None
        # Игнорируем продух (ventilation_gap) при выборе главного
        main = [l for l in self.layers if l.material != "ventilation_gap"]
        if not main:
            return None
        main = sorted(main, key=lambda l: -(l.thickness_mm or 0))
        return main[0].material


def _detect_material(text: str) -> str:
    for pat, mat in MATERIAL_PATTERNS:
        if re.search(pat, text, flags=re.IGNORECASE):
            return mat
    return "unknown"


def _parse_layer_token(token: str) -> WallLayer | None:
    """
    Парсит один токен слоя: "Бетон300" → (concrete, 300мм)
    "Блок ячеистый 100" → (aerated_block, 100мм)
    "Отделка50" → (finish, 50мм)
    """
    if not token or not token.strip():
        return None
    raw = token.strip()
    # Удаляем (REI XXX) скобки — они обрабатываются отдельно
    cleaned = re.sub(r"\(REI[^)]*\)", "", raw).strip()
    # Берём число с конца
    m = re.search(r"(.+?)\s*(\d{2,4})\s*$", cleaned)
    if m:
        text, thickness = m.group(1).strip(), int(m.group(2))
        material = _detect_material(text)
        return WallLayer(raw=raw, material=material, thickness_mm=thickness)
    # Без числа — слой без указанной толщины (Изоляция_Минеральная Мягкая)
    material = _detect_material(cleaned)
    if material == "unknown":
        return None
    return WallLayer(raw=raw, material=material, thickness_mm=None)


def parse_wall_family(name: str) -> WallFamilyInfo:
    """
    Главная функция-парсер. Возвращает WallFamilyInfo с parsed=True/False.

    Алгоритм:
    1. Определить префикс (СН/СВ/УН/_Ф_)
    2. Найти REI XXX (огнестойкость)
    3. Найти зону (МОП/...)
    4. Извлечь финальное число — total_thickness
    5. Разбить остальное по '_' на слои
    6. Парсить каждый слой
    """
    if not name:
        return WallFamilyInfo(
            raw_family="", parsed=False, parse_method="empty",
            prefix=None, structure_kind=None, is_underground=False,
            zone=None, rei_minutes=None,
        )

    raw = name.strip()
    info = WallFamilyInfo(
        raw_family=raw, parsed=False, parse_method="regex",
        prefix=None, structure_kind=None, is_underground=False,
        zone=None, rei_minutes=None,
    )

    # 1. Underground modifier
    work = raw
    if UNDERGROUND_PREFIX_RE.match(work):
        info.is_underground = True
        work = UNDERGROUND_PREFIX_RE.sub("", work).strip()

    # 2. Префикс (СН/СВ/УН) с тире
    m = re.match(r"^(СН|СВ|УН)\s*-\s*(.*)", work)
    if m:
        info.prefix = m.group(1)
        info.structure_kind = WALL_PREFIXES[m.group(1)]
        if info.is_underground:
            info.structure_kind = f"underground_{info.structure_kind}"
        work = m.group(2).strip()
    elif info.is_underground:
        info.prefix = "_Ф_"
        info.structure_kind = "underground"

    # 3. REI
    m = re.search(r"\(REI\s*(\d+)\)", work)
    if m:
        info.rei_minutes = int(m.group(1))

    # Area-marker в скобках (шахты, эркеры, ...) → запоминаем как note и убираем
    for marker, zone in [("шахты", "shafts"), ("эркеры", "bay_windows"), ("балконы", "balconies")]:
        if re.search(rf"\(\s*{marker}\s*\)", work):
            if not info.zone:
                info.zone = zone
            else:
                info.notes = f"+{marker}"
    # Убираем все скобочные пометки (REI XX), (шахты), (эркеры), (380) — оставляем чистый текст для парсинга слоёв
    work = re.sub(r"\([^)]*\)", " ", work).strip()

    # 4. Total thickness — последнее число
    m = re.search(r"\s+(\d{2,4})\s*$", work)
    if m:
        info.total_thickness_mm = int(m.group(1))
        work = work[: m.start()].strip()

    # 5. Зона (МОП и др.) — обычно последний токен
    for kw, zone_canon in ZONE_KEYWORDS.items():
        if re.search(rf"(?:^|[_\s])({kw})(?:[_\s]|$)", work):
            info.zone = zone_canon
            # Удаляем зону из текста для дальнейшего парсинга слоёв
            work = re.sub(rf"(?:^|[_\s])({kw})(?:[_\s]|$)", "_", work).strip("_ ")
            break

    # 6. Слои разделены '_'
    layer_tokens = [t.strip() for t in work.split("_") if t.strip()]
    layers = []
    for tok in layer_tokens:
        # Скобки — модификатор, не отдельный слой
        if tok.startswith("("):
            continue
        layer = _parse_layer_token(tok)
        if layer:
            layers.append(layer)

    info.layers = layers

    # Считаем что распарсили если есть хотя бы prefix или 1 слой
    if info.prefix or info.layers:
        info.parsed = True

    return info


# ---------------------------------------------------------------------
# CLI: тест на реальной выгрузке
# ---------------------------------------------------------------------
def main():
    import sys, io, json
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    from pathlib import Path

    profile = json.loads(
        Path(r"C:\Users\kuklev.d.s\PycharmProjects\bim2vor\data\revit_profile.json")
        .read_text(encoding="utf-8")
    )
    walls = profile["categories"]["OST_Walls"]
    families = walls["top_families"]

    parsed_ok = 0
    parsed_fail = []
    for fam_name, count in families:
        info = parse_wall_family(fam_name)
        status = "OK" if info.parsed else "FAIL"
        layers_str = ", ".join(
            f"{l.material}={l.thickness_mm}мм" for l in info.layers
        ) if info.layers else "—"
        zone = info.zone or "-"
        prefix = info.prefix or "-"
        rei = f" REI{info.rei_minutes}" if info.rei_minutes else ""
        ug = " UG" if info.is_underground else ""
        total = f" total={info.total_thickness_mm}" if info.total_thickness_mm else ""
        print(f"[{status}] {count:>5}  {prefix:>3}{ug:<3}  zone={zone:12s}{rei:7s}{total:11s}  | {fam_name}")
        print(f"        слои: {layers_str}")
        if info.parsed:
            parsed_ok += count
        else:
            parsed_fail.append((fam_name, count))

    print(f"\nИтого: распарсено {parsed_ok} элементов")
    if parsed_fail:
        print(f"Не распарсено ({sum(c for _, c in parsed_fail)} элементов):")
        for f, c in parsed_fail:
            print(f"  {c:>5}  {f}")


if __name__ == "__main__":
    main()
