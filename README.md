# bim2vor — система автозаполнения ВОР из BIM

Преобразует Revit Excel-выгрузки (DDC-формат) в заполненные позиции **ВОР** (Ведомость Объёмов Работ).

## Архитектура: Specialist-Expert pattern

```
Revit Excel (118k строк, 1199 cols)
    ↓ taxonomy + family parser (regex)
40k physical elements
    ↓ cluster aggregator
~200-3500 кластеров по семействам
    ↓ pre-filter per specialist
top-80 кандидатов на эксперта
    ↓ SpecialistCell (LLM с reasoning, 4 фазы)
filter → completeness → allocate → gaps
    ↓ consolidator
filled_boq.xlsx + audit.xlsx
```

## 11 специалистов (по разделам ВОР)

| # | Specialist | BoQ sections | Domain |
|---|------------|-------------|--------|
| 1 | monolith | 5, 6 | Монолитные конструкции (бетон, арматура, опалубка) |
| 2 | masonry | 7 | Каменная кладка (блоки, кирпич) |
| 3 | roofing | 8, 9 | Кровли корпусов и стилобата |
| 4 | facades | 10 | Фасады, утепление, светопрозрачные |
| 5 | finishing_mop | 11 | Отделка МОП |
| 6 | finishing_parking | 12 | Отделка паркинга, тех.помещений |
| 7 | finishing_apartments | 13 | Отделка квартир |
| 8 | doors | 14 | Двери |
| 9 | metal_stairs | 15 | Металлические лестницы, ограждения |
| 10 | elevators | 24 | Лифтовое оборудование |
| 11 | windows | 10, 13 | Окна (фасадные + квартирные) |

Каждый специалист = один LLM-вызов (Sonnet с extended thinking) на проект.

## Quality safeguards

1. **Reasoning Cell** — каждый LLM-вызов имеет cache (sha256 input), constraint check, provenance
2. **Honest failure** — если данных нет, `quantity=null, confidence=0`, явный `fill_status` (`missing_in_ar_model`, `not_in_bim_scope` и т.д.)
3. **Provenance** — для каждой цифры записывается formula, источник кластеров, share, contribution
4. **Audit trail** — отдельный xlsx с reasoning по каждой позиции
5. **Цветовая индикация** — зелёный/жёлтый/красный/серый по confidence и fill_status

## Структура проекта

```
bim2vor/
  bim2vor/
    storage/      — SQLite schema (runs, llm_calls, mappings, audit_cells)
    taxonomy/     — OST → канонические категории + dictionary
    parser/       — family-name → структура слоёв (regex)
    ingest/       — RevitReader, BoQReader, cluster_elements
    match/        — prefilter (signal words, score per cluster)
    reasoning/    — ReasoningCell, SpecialistCell, cache
    report/       — fill_boq_template, audit workbook
    orchestrator.py — pipeline runner
  recipes/
    ost_dictionary.yaml   — таксономия OST → categories
    specialists.yaml      — реестр 11 экспертов
  data/
    revit_profile.json    — профайл выгрузки
    boq_profile.json      — профайл шаблона
    clusters.json         — кластеры всей модели
  runs/<run_id>/
    briefings/            — briefing.json + prompt.md per specialist
    specialist_outputs/   — JSON-ответы экспертов
    filled_boq.xlsx       — заполненный шаблон ВОР
    audit.xlsx            — детальный аудит
    run_summary.json      — метрики прогона
```

## Run

```bash
cd C:/Users/kuklev.d.s/PycharmProjects/bim2vor
python -m bim2vor.orchestrator    # ingest + briefings
# Запуск специалистов (в этом MVP — через спавн агентов или ручную работу)
python -m bim2vor.report.writer   # consolidation + filled_boq.xlsx + audit
```

## Демонстрационный прогон (SKLNK ЖК + ВГК№5)

| Метрика | Значение |
|---------|----------|
| Revit-выгрузка | SKLNK_АР_ПД_К2.1_R25_rvt.xlsx (118213 строк) |
| ВОР-шаблон | Расчёт ПЗ_ВГК№5 (520 позиций) |
| Физических элементов | 40248 (34%, остальное мусор/неактивные категории) |
| Кластеров | 3548 |
| Запущено специалистов | 11 |
| Заполнено позиций | 79 / 520 (15%) |
| Уверенно (conf>0.6) | 17 |
| Сильное расхождение (delta>30%) | 11 |

### Ключевые выводы пилота
1. **АР-модель содержит ≤15% от физических объёмов ВОР** — несущие конструкции (стены, колонны, плита фундамента) живут в КР-модели; для полного заполнения необходимы данные обеих моделей.
2. **Семейства самодокументированы** — Revit-имена `СН-Блок200_Изоляция160_Продух90 450` парсятся в структуру слоёв и распределяются между специалистами автоматически.
3. **Площадь окон/дверей не выгружается DDC** — нужно либо доработать экспорт, либо считать в м² по count × средний размер.
4. **53% строк выгрузки — мусор** (SketchLines, RoomSeparationLines), фильтруются на этапе ingest.

## Дальнейшее развитие

- [ ] Подключить Anthropic API напрямую (сейчас pilot run = ручной spawn агентов)
- [ ] Добавить КР-модель в ingest для покрытия фундамента/стен
- [ ] Пол паркинга, кровля, потолки — обогатить выгрузку DDC
- [ ] Расширить таксономию unknown OST_-категорий
- [ ] Web UI для review специалистов и принятия решений
- [ ] Knowledge base правил для повторных проектов
- [ ] Smell tests / golden examples для регрессии
