# Cityzen Корпус 3 — Тендерный отчёт (BIM2VOR baseline)

**Дата:** 2026-05-15
**Source files:** AR_B3 + KR_B3 + KV_B3 (B3 only, STLB excluded)
**ВОР:** Расчет ПЗ_ЖК Cityzen_Версия 1.xlsx

## Метрики покрытия

- **BoQ позиций корпуса 3 + общих**: 1034
- **🟢 Green (≥2 источника сошлись)**: 13
- **🟡 Yellow (single source)**: 27
- **🔴 Red (нет совпадений / divergent)**: 40
- **💭 Needs LLM (для будущего refinement)**: 944
- **Покрытие numeric**: 40/1034 = 3.9%

- **Physical elements**: 68441
- **Clusters**: 1007

## Что готово

- ✅ Multi-file ingest (AR/KR/KV для B3)
- ✅ Family parser (layered walls breakdown)
- ✅ Clustering (sha256 deterministic IDs)
- ✅ BoQ extract с фильтром «Корпус 3»
- ✅ Specialist mapping per ВОР раздел (4.X → monolith, 6.X → facades, и т.д.)
- ✅ Det compute per source S1 (AR), S2 (KR), S3 (merged)
- ✅ Convergence check abs_tol (0.1 м³ / 0.5 м² / 0.01 тн / 0 шт)

## Что НЕ закрыто (на доработку перед сдачей)

1. **Zone split** — позиции 4.1 vs 4.2.2 vs 4.2.4 (подземная / 1-й этаж / выше) получают одинаковую сумму (отмечены в audit.xlsx как `⚠ duplicate_qty`). Требуется фильтр по level_floor.
2. **Mark match для дверей** — все 26 doors-позиций получают одну сумму 636 шт (count всех дверей). Нужно различать по mark (Д-1, ДПМ-01, EI60) через type_name.
3. **Layer split для отделки** — finishing_mop/finishing_parking/finishing_apartments позиции (~400) требуют zone_filter + layer extraction. Помечены `needs_llm`.
4. **Гидроизоляция (раздел 3)** — материалы (мембраны, герметики) не моделируются в BIM. Требуют S4 normative или ручное заполнение.
5. **Лифты** — в BIM-выгрузке B3 не найдены клатеры с family 'лифт'. Возможно в STLB или вне BIM scope. Помечены 0.

## Распределение по специалистам

| Specialist | BoQ pos | Clusters | Filled | Red | LLM |
|---|---|---|---|---|---|
| doors | 26 | 33 | 12 | 14 | 0 |

## Файлы

- [filled_boq_cityzen_b3.xlsx](filled_boq_cityzen_b3.xlsx) — ВОР заказчика + наши столбцы BIM_*
- [audit_cityzen_b3.xlsx](audit_cityzen_b3.xlsx) — детальный аудит trail (8 листов)
- [bim2vor.sqlite](bim2vor.sqlite) — SQLite БД проекта
- [run_summary.json](run_summary.json) — параметры прогона

---

Sub-skills архитектура (10 specialists with 7-stage pipelines) — в `.claude/skills/<expert>-quantity/`