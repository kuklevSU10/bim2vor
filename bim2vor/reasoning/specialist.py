# -*- coding: utf-8 -*-
"""
SpecialistCell — эксперт по предметной области.

Принцип: один эксперт = один LLM-вызов на проект для своего раздела ВОР.
Эксперт работает в 4 фазы (всё в одном вызове, через extended thinking):

  Phase 1 — FILTERING:
    Из переданных кластеров элементов отбирает СВОИ (relevant к домену).
    Для каждого кластера: claim/reject/partial с обоснованием.

  Phase 2 — COMPLETENESS CHECK:
    Проверяет что не упустил очевидное:
      - есть ли все слои стен/ перекрытий
      - нет ли пропусков по уровням
      - все ли элементы по своему ВОР-разделу учтены

  Phase 3 — ALLOCATION (главная):
    Для каждой позиции своей секции ВОР:
      - какие кластеры вносят вклад
      - какая доля кластера идёт в эту позицию
      - какое количество в единицах позиции (м³/м²/шт)
      - confidence + обоснование
      - формула расчёта

  Phase 4 — DOPNIKI / GAPS:
    Кластеры которые claimed, но не попали ни в одну позицию ВОР
      → предложить новую позицию (допник)
    Позиции ВОР, для которых не нашлось элементов
      → объяснить (не моделируется / другая стадия / реально отсутствует)

Output: структурированный JSON со всеми 4 фазами.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bim2vor.reasoning.cell import ReasoningCell, DEFAULT_MODEL


REPO_ROOT = Path(__file__).resolve().parents[2]
SPECIALISTS_YAML = REPO_ROOT / "recipes" / "specialists.yaml"


@dataclass
class SpecialistConfig:
    key: str
    name: str
    short_name: str
    boq_sections: list[int]
    boq_section_names: list[str]
    domain_description: str
    candidate_revit_categories: list[str]
    family_signal_words: dict[str, Any]
    boq_subsections: list[str] | None = None
    preferred_disciplines: list[str] | None = None


def load_specialists() -> dict[str, SpecialistConfig]:
    raw = yaml.safe_load(SPECIALISTS_YAML.read_text(encoding="utf-8"))
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
# Promt template
# ---------------------------------------------------------------------
PROMPT_TEMPLATE = """\
Ты — {name} в крупной строительной компании. Ты специалист по СВОЕМУ разделу ВОР.

ТВОЯ ОБЛАСТЬ ОТВЕТСТВЕННОСТИ:
{domain_description}

Разделы ВОР, которые ты ведёшь: {sections_str}

================================================================
ВХОДНЫЕ ДАННЫЕ
================================================================

== БРИФИНГ ПО МОДЕЛИ ==
Получены данные BIM-модели жилого комплекса. Элементы агрегированы в КЛАСТЕРЫ
(один кластер = одно семейство Revit с одинаковым типом). Всего кластеров в проекте: {total_clusters}.
Каждый кластер описывает много отдельных элементов.

ИЗВЕСТНЫЕ ОСОБЕННОСТИ ИМЕНОВАНИЯ СЕМЕЙСТВ СТЕН:
- Префикс СВ — стена внутренняя/самонесущая
- Префикс СН — стена наружная
- Префикс УН — узловая (обычно монолитная бетонная)
- Префикс _Ф_ — фундамент/подвал
- Зона МОП — места общего пользования (холлы, лифт холлы, лестничные клетки)
- (REI XXX) — огнестойкость в минутах
- (шахты) — стены лифтовых/вентиляционных шахт
- (эркеры) — эркерные стены
- Имя содержит слои: материал+толщина_материал+толщина_материал+толщина ОБЩАЯ_ТОЛЩИНА
  Пример: "_Ф_СН-Блок200_Изоляция160_Продух90_Штукатурка20_МОП 470"
  → подвальная наружная стена МОП: 200мм блок + 160мм изоляция + 90мм продух + 20мм штукатурка, общая 470мм

== КЛАСТЕРЫ-КАНДИДАТЫ (предварительно отфильтрованные под твой домен) ==
{clusters_json}

== ПОЗИЦИИ ВОР ТВОЕГО РАЗДЕЛА (которые ты должен заполнить) ==
{boq_positions_json}

================================================================
ЗАДАНИЕ — ВЫПОЛНИ 4 ФАЗЫ
================================================================

PHASE 1: FILTERING (фильтрация)
Для каждого кластера в кандидатах решить:
  - "claim" — кластер мой, входит в мою область
  - "reject" — это не моё (объяснить кому это передать)
  - "partial" — часть кластера моя, часть нет (с долей)

PHASE 2: COMPLETENESS (полнота)
Проверь свой набор. Возможные дыры:
  - Если у меня есть бетон стен → должна быть и арматура (если в моей области)
  - Если у меня есть ЖБ перекрытие → должна быть опалубка (рассчитываемая)
  - Покрыт ли я по всем уровням здания (подвал/типовой/верх)
Записать список замечаний с серьёзностью info|warn|alarm.

PHASE 3: ALLOCATION (распределение по позициям ВОР)
Для каждой позиции из списка ВОР: рассчитать количество в её единице измерения.
Формат для каждой позиции:
  - position_id: "5.1.2"
  - quantity: число
  - unit: ед.изм. позиции
  - confidence: 0..1
  - reasoning: цепочка размышления (как пришёл к числу)
  - formula: формула расчёта в виде строки
  - source_clusters: список cluster_id с долями (share) и вкладами (contribution)

ВАЖНО про математику:
- Для м³: суммировать volume_m3 кластеров (с учётом share)
- Для м² (стены): обычно area_m2 СНАРУЖИ, но проверить контекст позиции
- Для слоёв стен: брать ПРОПОРЦИОНАЛЬНО volume = total_volume × thickness_layer / total_thickness
- Для шт: count
- Использовать reasoning чтобы не ошибаться в логике

PHASE 4: GAPS & DOPNIKI (расхождения)
- "claimed_but_unallocated": кластеры которые я взял, но они не попали ни в одну позицию ВОР → возможные допники
- "missing_in_model": позиции ВОР, для которых нет данных → объяснить причину
- "overall_concerns": общие вопросы или предупреждения

================================================================
ВЫХОДНОЙ ФОРМАТ — строго JSON, без преамбулы
================================================================
{{
  "specialist": "{key}",
  "phase1_filtering": {{
    "claimed": [
      {{"cluster_id": "...", "share": 1.0, "reason": "..."}}
    ],
    "rejected": [
      {{"cluster_id": "...", "reason": "...", "delegate_to": "specialist_key"}}
    ],
    "partial": [
      {{"cluster_id": "...", "share": 0.6, "reason": "..."}}
    ]
  }},
  "phase2_completeness": [
    {{"severity": "warn", "issue": "..."}}
  ],
  "phase3_allocations": [
    {{
      "position_id": "5.1.2",
      "quantity": 156.5,
      "unit": "м3",
      "confidence": 0.85,
      "reasoning": "...",
      "formula": "SUM(volume_m3 × share) WHERE primary_material='concrete' AND zone='МОП'",
      "source_clusters": [
        {{"cluster_id": "...", "share": 1.0, "contribution": 100.0}}
      ]
    }}
  ],
  "phase4_gaps": {{
    "claimed_but_unallocated": [
      {{"cluster_id": "...", "suggested_dopnik": "новая позиция: ...", "estimated_qty": 0, "unit": "..."}}
    ],
    "missing_in_model": [
      {{"position_id": "...", "reason": "..."}}
    ],
    "overall_concerns": ["..."]
  }},
  "specialist_confidence": 0.0_to_1.0,
  "summary": "1-2 предложения о результатах работы"
}}

Думай тщательно. Это профессиональная работа, от качества которой зависят финансовые расчёты.
Не торопись с числами. Если не уверен — пиши более низкий confidence и описывай сомнение в reasoning.
"""


class SpecialistCell(ReasoningCell):
    """Одна клетка = один эксперт = один LLM вызов под целый раздел ВОР."""

    cell_type = "specialist"
    prompt_version = "v2"
    use_thinking = True
    thinking_budget = 16000
    max_tokens = 32000
    self_verify = False

    def __init__(self, specialist_config: SpecialistConfig, **kw):
        super().__init__(**kw)
        self.config = specialist_config
        self.cell_type = f"specialist:{specialist_config.key}"

    def render_prompt(self, input_data: dict) -> str:
        cfg = self.config
        clusters_json = json.dumps(
            input_data["candidate_clusters"], ensure_ascii=False, indent=2,
        )
        boq_json = json.dumps(
            input_data["boq_positions"], ensure_ascii=False, indent=2,
        )
        sections_str = ", ".join(
            f"раздел {s} ({n})" for s, n in zip(cfg.boq_sections, cfg.boq_section_names)
        )
        return PROMPT_TEMPLATE.format(
            key=cfg.key,
            name=cfg.name,
            domain_description=cfg.domain_description.strip(),
            sections_str=sections_str,
            total_clusters=input_data.get("total_clusters_in_project", "?"),
            clusters_json=clusters_json,
            boq_positions_json=boq_json,
        )

    def get_confidence(self, output: dict) -> float:
        return float(output.get("specialist_confidence", 0.5))

    def check_constraints(self, input_data: dict, output: dict) -> tuple[bool, str]:
        # Базовые проверки
        if "phase3_allocations" not in output:
            return False, "no phase3_allocations"
        # Quantity должен быть >= 0
        for alloc in output.get("phase3_allocations", []):
            q = alloc.get("quantity")
            if q is not None and q < 0:
                return False, f"negative quantity in {alloc.get('position_id')}"
        # Cluster shares в кооперации должны быть в [0,1]
        for cl in output.get("phase1_filtering", {}).get("partial", []):
            sh = cl.get("share", 1.0)
            if not (0 < sh <= 1):
                return False, f"invalid share {sh}"
        return True, ""
