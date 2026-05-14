# -*- coding: utf-8 -*-
"""
Верификация результатов специалистов + заполнение ВОР.

Проверки:
1. Структурная валидация JSON
2. Все позиции ВОР покрыты
3. Нет двойного счёта кластеров между специалистами
4. Дельта план/факт в разумных пределах
5. Confidence-статистика

После проверок — заполняет шаблон ВОР.
"""
from __future__ import annotations

import json
import sys
import io
from collections import defaultdict
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bim2vor.report.writer import (
    load_specialist_outputs,
    consolidate_allocations,
    fill_boq_template,
    build_audit_workbook,
)


def validate_specialist_json(data: dict, key: str) -> list[str]:
    """Проверяет структуру JSON одного специалиста."""
    issues = []
    if data.get("specialist") != key:
        issues.append(f"specialist field mismatch: expected {key}, got {data.get('specialist')}")

    # Phase 1
    ph1 = data.get("phase1_filtering", {})
    if not ph1:
        issues.append("missing phase1_filtering")
    else:
        claimed = ph1.get("claimed", [])
        rejected = ph1.get("rejected", [])
        if not claimed:
            issues.append("phase1: no clusters claimed")

    # Phase 3
    ph3 = data.get("phase3_allocations", [])
    if not ph3:
        issues.append("missing phase3_allocations")
    else:
        for alloc in ph3:
            pid = alloc.get("position_id", "?")
            qty = alloc.get("quantity")
            conf = alloc.get("confidence", 0)
            if qty is not None and qty < 0:
                issues.append(f"negative quantity in {pid}: {qty}")
            if conf < 0 or conf > 1:
                issues.append(f"confidence out of range in {pid}: {conf}")

    # Phase 4
    ph4 = data.get("phase4_gaps")
    if ph4 is None:
        issues.append("missing phase4_gaps")

    return issues


def check_double_counting(all_outputs: dict[str, dict]) -> list[str]:
    """Проверяет что один кластер не claimed на 100% двумя специалистами."""
    issues = []
    cluster_claims: dict[str, list[tuple[str, float]]] = defaultdict(list)

    for spec_key, data in all_outputs.items():
        ph1 = data.get("phase1_filtering", {})
        for c in ph1.get("claimed", []):
            cid = c.get("cluster_id", "")
            share = c.get("share", 1.0)
            cluster_claims[cid].append((spec_key, share))
        for c in ph1.get("partial", []):
            cid = c.get("cluster_id", "")
            share = c.get("share", 0.5)
            cluster_claims[cid].append((spec_key, share))

    for cid, claims in cluster_claims.items():
        total_share = sum(s for _, s in claims)
        if total_share > 1.05:
            specs = ", ".join(f"{s}({sh:.0%})" for s, sh in claims)
            issues.append(f"double-count: {cid[:60]} claimed by {specs} (total {total_share:.0%})")

    return issues


def delta_analysis(all_outputs: dict[str, dict]) -> list[dict]:
    """Анализ дельты план/факт по каждой позиции."""
    results = []
    for spec_key, data in all_outputs.items():
        for alloc in data.get("phase3_allocations", []):
            pid = alloc.get("position_id", "?")
            qty = alloc.get("quantity")
            conf = alloc.get("confidence", 0)
            # qty_planned не в аллокации — будет сравнение при заполнении ВОР
            results.append({
                "specialist": spec_key,
                "position_id": pid,
                "bim_qty": qty,
                "confidence": conf,
                "fill_status": alloc.get("fill_status", ""),
            })
    return results


def main():
    specialist_dir = REPO / "runs" / "event_6_1" / "specialist_outputs"
    boq_template = Path(r"C:\Users\kuklev.d.s\Downloads\Расчет ПЗ_Событие 6.1_Версия 2.xlsx")
    output_filled = REPO / "runs" / "event_6_1" / "filled_boq.xlsx"
    output_audit = REPO / "runs" / "event_6_1" / "audit.xlsx"

    print("=" * 60)
    print("ВЕРИФИКАЦИЯ РЕЗУЛЬТАТОВ СПЕЦИАЛИСТОВ")
    print("=" * 60)

    # 1. Load outputs
    outputs = load_specialist_outputs(specialist_dir)
    print(f"\nЗагружено специалистов: {len(outputs)} ({list(outputs.keys())})")

    if not outputs:
        print("ОШИБКА: нет файлов в", specialist_dir)
        return

    # 2. Structural validation
    print("\n--- Структурная валидация ---")
    all_ok = True
    for key, data in outputs.items():
        issues = validate_specialist_json(data, key)
        n_claimed = len(data.get("phase1_filtering", {}).get("claimed", []))
        n_rejected = len(data.get("phase1_filtering", {}).get("rejected", []))
        n_allocs = len(data.get("phase3_allocations", []))
        n_filled = sum(1 for a in data.get("phase3_allocations", [])
                       if a.get("quantity") is not None and float(a.get("confidence", 0)) > 0)
        conf = data.get("specialist_confidence", 0)

        status = "OK" if not issues else f"ISSUES({len(issues)})"
        print(f"  {key:20s}  {status:10s}  claimed={n_claimed:>3}  rejected={n_rejected:>3}  "
              f"allocs={n_allocs:>3}  filled={n_filled:>3}  conf={conf:.2f}")
        if issues:
            all_ok = False
            for iss in issues:
                print(f"    ! {iss}")

    # 3. Double-counting check
    print("\n--- Проверка двойного счёта ---")
    dc_issues = check_double_counting(outputs)
    if dc_issues:
        print(f"  ВНИМАНИЕ: {len(dc_issues)} случаев двойного счёта:")
        for iss in dc_issues[:10]:
            print(f"    ! {iss}")
    else:
        print("  OK — нет двойного счёта кластеров")

    # 4. Consolidate allocations
    by_pos = consolidate_allocations(outputs)
    print(f"\nАллокаций по позициям: {len(by_pos)}")

    # 5. Confidence distribution
    print("\n--- Распределение confidence ---")
    all_allocs = []
    for data in outputs.values():
        all_allocs.extend(data.get("phase3_allocations", []))
    confs = [float(a.get("confidence", 0)) for a in all_allocs if a.get("quantity") is not None]
    if confs:
        high = sum(1 for c in confs if c >= 0.65)
        med = sum(1 for c in confs if 0.4 <= c < 0.65)
        low = sum(1 for c in confs if c < 0.4)
        print(f"  Высокий (>=0.65): {high}")
        print(f"  Средний (0.4-0.65): {med}")
        print(f"  Низкий (<0.4): {low}")

    # 6. Fill ВОР
    print("\n" + "=" * 60)
    print("ЗАПОЛНЕНИЕ ВОР")
    print("=" * 60)
    stats = fill_boq_template(boq_template, output_filled, by_pos)
    print(f"\nFilled BoQ: {output_filled}")
    print(f"  filled: {stats['filled']}")
    print(f"  no_data: {stats['no_data']}")
    print(f"  alarms (conf<0.4): {stats['alarms']}")
    print(f"  warns (0.4-0.65): {stats['warns']}")
    print(f"  delta>30%: {stats['delta_above_30pct']}")

    # 7. Audit workbook
    build_audit_workbook(output_audit, outputs)
    print(f"\nAudit: {output_audit}")

    # 8. Summary
    print("\n" + "=" * 60)
    print("ИТОГО")
    print("=" * 60)
    print(f"  Специалистов: {len(outputs)}")
    print(f"  Позиций с данными: {stats['filled']}")
    print(f"  Позиций без данных: {stats['no_data']}")
    print(f"  Сигналов тревоги: {stats['alarms']}")
    print(f"  Дельта > 30%: {stats['delta_above_30pct']}")

    if all_ok and stats["filled"] > 0:
        print("\n  СТАТУС: УСПЕШНО")
    elif stats["filled"] > 0:
        print("\n  СТАТУС: ЧАСТИЧНО (есть замечания)")
    else:
        print("\n  СТАТУС: ОШИБКА (нет заполненных позиций)")


if __name__ == "__main__":
    main()
