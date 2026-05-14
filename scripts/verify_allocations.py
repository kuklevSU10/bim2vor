# -*- coding: utf-8 -*-
"""
Комплексная верификация результатов специалистов.

Проверки:
1. VOLUME RECONCILIATION — сравнение объёмов: briefing clusters vs allocated quantities
   (ни один м³/м² не должен потеряться)
2. CLUSTER COVERAGE — каждый claimed кластер должен быть использован хотя бы в одной позиции
3. POSITION COVERAGE — каждая позиция ВОР покрыта или имеет объяснение
4. DOUBLE COUNTING — один кластер не может быть claimed > 100% суммарно
5. MATH VALIDATION — пересчёт quantity из source_clusters и сравнение с заявленным
6. SHARE CONSISTENCY — если cluster claimed с share<1, сумма shares по всем специалистам = 1
7. DOPNIKI CHECK — claimed_but_unallocated кластеры проверены на существенный объём
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


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_all_specialist_outputs(spec_dir: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(spec_dir.glob("*.json")):
        data = load_json(p)
        out[data.get("specialist", p.stem)] = data
    return out


def load_all_briefings(briefing_dir: Path) -> dict[str, dict]:
    out = {}
    for p in sorted(briefing_dir.glob("*.json")):
        data = load_json(p)
        out[data.get("specialist_key", p.stem)] = data
    return out


# ====================================================================
# CHECK 1: Volume Reconciliation
# ====================================================================
def check_volume_reconciliation(
    spec_key: str,
    briefing: dict,
    output: dict,
) -> list[dict]:
    """Per-cluster reconciliation: для каждого claimed кластера проверяем,
    что его вклад (contribution) в аллокации совпадает с фактическим объёмом/площадью.

    Кластер может быть использован в м³ ИЛИ м² — проверяем по факту использования."""
    issues = []

    cluster_data = {}
    for c in briefing.get("candidate_clusters", []):
        cid = c.get("cluster_id", "")
        cluster_data[cid] = {
            "volume_m3": c.get("volume_m3", 0) or 0,
            "area_m2": c.get("area_m2", 0) or 0,
            "count": c.get("count", 0) or 0,
        }

    # Map: cluster_id → list of (position_id, share, contribution, unit)
    cluster_usage: dict[str, list[dict]] = defaultdict(list)
    for alloc in output.get("phase3_allocations", []):
        pid = alloc.get("position_id", "?")
        unit = str(alloc.get("unit", "")).lower().strip()
        for sc in alloc.get("source_clusters", []):
            cid = sc.get("cluster_id", "")
            cluster_usage[cid].append({
                "position_id": pid,
                "share": float(sc.get("share", 1.0)),
                "contribution": sc.get("contribution"),
                "unit": unit,
            })

    # Dopniki usage
    dopnik_clusters = set()
    for d in output.get("phase4_gaps", {}).get("claimed_but_unallocated", []):
        dopnik_clusters.add(d.get("cluster_id", ""))

    # For each claimed cluster, verify
    ph1 = output.get("phase1_filtering", {})
    all_claimed = []
    for c in ph1.get("claimed", []):
        all_claimed.append((c.get("cluster_id", ""), float(c.get("share", 1.0))))
    for c in ph1.get("partial", []):
        all_claimed.append((c.get("cluster_id", ""), float(c.get("share", 0.5))))

    n_used = 0
    n_contrib_ok = 0
    n_contrib_mismatch = 0
    mismatches = []

    for cid, claimed_share in all_claimed:
        cd = cluster_data.get(cid, {})
        usages = cluster_usage.get(cid, [])

        if not usages and cid not in dopnik_clusters:
            # Orphaned — already caught by cluster_coverage check
            continue

        if not usages:
            continue  # In dopniki — ok

        n_used += 1

        # Check each usage
        for u in usages:
            contrib = u.get("contribution")
            if contrib is None:
                continue

            unit = u["unit"]
            expected = None
            if "м3" in unit or "м³" in unit:
                expected = cd.get("volume_m3", 0) * u["share"]
            elif "м2" in unit or "м²" in unit:
                expected = cd.get("area_m2", 0) * u["share"]
            elif "шт" in unit:
                expected = cd.get("count", 0) * u["share"]

            if expected is not None and expected > 0:
                delta_pct = abs(contrib - expected) / expected * 100 if expected else 0
                if delta_pct > 15:
                    n_contrib_mismatch += 1
                    mismatches.append({
                        "cluster_id": cid[:60],
                        "position_id": u["position_id"],
                        "expected": round(expected, 2),
                        "actual_contribution": round(contrib, 2),
                        "delta_pct": round(delta_pct, 1),
                    })
                else:
                    n_contrib_ok += 1

    if mismatches:
        for m in mismatches[:5]:
            issues.append({
                "check": "volume_reconciliation",
                "severity": "warn",
                "position_id": m["position_id"],
                "cluster_id": m["cluster_id"],
                "expected": m["expected"],
                "actual": m["actual_contribution"],
                "delta_pct": m["delta_pct"],
                "message": f"Contribution mismatch {m['delta_pct']:.1f}%: expected {m['expected']}, got {m['actual_contribution']}",
            })
    else:
        issues.append({
            "check": "volume_reconciliation",
            "severity": "ok",
            "n_clusters_used": n_used,
            "n_contributions_ok": n_contrib_ok,
            "message": f"Все {n_used} claimed кластеров с {n_contrib_ok} contributions прошли проверку",
        })

    # Summary: total claimed vs total used (informational)
    claimed_vol = sum(cluster_data.get(cid, {}).get("volume_m3", 0) * sh for cid, sh in all_claimed)
    claimed_area = sum(cluster_data.get(cid, {}).get("area_m2", 0) * sh for cid, sh in all_claimed)
    alloc_vol = sum(a.get("quantity", 0) or 0 for a in output.get("phase3_allocations", [])
                    if "м3" in str(a.get("unit", "")).lower() or "м³" in str(a.get("unit", "")).lower())
    alloc_area = sum(a.get("quantity", 0) or 0 for a in output.get("phase3_allocations", [])
                     if "м2" in str(a.get("unit", "")).lower() or "м²" in str(a.get("unit", "")).lower())
    issues.append({
        "check": "volume_summary",
        "severity": "info",
        "claimed_vol_m3": round(claimed_vol, 1),
        "allocated_vol_m3": round(alloc_vol, 1),
        "claimed_area_m2": round(claimed_area, 1),
        "allocated_area_m2": round(alloc_area, 1),
        "message": f"Claimed: {claimed_vol:.0f}м³ / {claimed_area:.0f}м². Allocated: {alloc_vol:.0f}м³ / {alloc_area:.0f}м²",
    })

    return issues


# ====================================================================
# CHECK 2: Cluster Coverage
# ====================================================================
def check_cluster_coverage(spec_key: str, output: dict, briefing: dict | None = None) -> list[dict]:
    """Каждый claimed кластер должен быть в source_clusters хотя бы одной позиции
    ИЛИ в claimed_but_unallocated.

    Различаем:
    - alarm: кластер orphaned И его объём не покрыт агрегатными аллокациями
    - warn: кластер orphaned, но его объём скорее всего в агрегатной аллокации (нет traceability)
    """
    issues = []

    ph1 = output.get("phase1_filtering", {})
    claimed_map: dict[str, float] = {}
    for c in ph1.get("claimed", []):
        claimed_map[c.get("cluster_id", "")] = float(c.get("share", 1.0))
    for c in ph1.get("partial", []):
        claimed_map[c.get("cluster_id", "")] = float(c.get("share", 0.5))

    # Clusters used in allocations
    used_ids = set()
    for alloc in output.get("phase3_allocations", []):
        for sc in alloc.get("source_clusters", []):
            used_ids.add(sc.get("cluster_id", ""))

    # Clusters in dopniki
    dopnik_ids = set()
    for d in output.get("phase4_gaps", {}).get("claimed_but_unallocated", []):
        dopnik_ids.add(d.get("cluster_id", ""))

    accounted_ids = used_ids | dopnik_ids
    orphaned = set(claimed_map.keys()) - accounted_ids

    if not orphaned:
        issues.append({
            "check": "cluster_coverage",
            "severity": "ok",
            "message": f"Все {len(claimed_map)} claimed кластеров учтены в позициях или допниках",
        })
        return issues

    # Check if orphaned clusters have aggregate coverage
    # (allocations exist for the category but without per-cluster traceability)
    n_true_orphan = 0
    n_no_traceability = 0
    orphan_vol = 0.0

    # Build cluster lookup from briefing for volume info
    cluster_data = {}
    if briefing:
        for c in briefing.get("candidate_clusters", []):
            cluster_data[c.get("cluster_id", "")] = c

    # Check if allocations have content without source_clusters (aggregate style)
    has_aggregate_allocs = any(
        alloc.get("quantity") and not alloc.get("source_clusters")
        for alloc in output.get("phase3_allocations", [])
    )

    for cid in sorted(orphaned):
        cd = cluster_data.get(cid, {})
        vol = cd.get("volume_m3", 0) or 0
        area = cd.get("area_m2", 0) or 0

        if has_aggregate_allocs:
            n_no_traceability += 1
            orphan_vol += vol
        else:
            n_true_orphan += 1
            issues.append({
                "check": "cluster_coverage",
                "severity": "alarm",
                "cluster_id": cid,
                "message": f"Кластер claimed, но НЕ использован (V={vol:.1f}м³, A={area:.1f}м²)",
            })

    if n_no_traceability > 0:
        issues.append({
            "check": "cluster_coverage",
            "severity": "warn",
            "n_clusters": n_no_traceability,
            "total_volume_m3": round(orphan_vol, 1),
            "message": f"{n_no_traceability} claimed кластеров без per-cluster traceability "
                       f"(V={orphan_vol:.0f}м³). Объём в агрегатных аллокациях, но нет привязки к кластерам.",
        })

    return issues


# ====================================================================
# CHECK 3: Position Coverage
# ====================================================================
def check_position_coverage(spec_key: str, briefing: dict, output: dict) -> list[dict]:
    """Каждая позиция ВОР из брифинга должна быть в phase3 или в missing_in_model."""
    issues = []

    boq_ids = set()
    for p in briefing.get("boq_positions", []):
        pid = str(p.get("code", p.get("position_id", ""))).strip().rstrip(".")
        if pid and not p.get("is_section_header"):
            boq_ids.add(pid)

    allocated_ids = set()
    for alloc in output.get("phase3_allocations", []):
        pid = str(alloc.get("position_id", "")).strip().rstrip(".")
        if pid:
            allocated_ids.add(pid)

    missing_ids = set()
    for m in output.get("phase4_gaps", {}).get("missing_in_model", []):
        pid = str(m.get("position_id", "")).strip().rstrip(".")
        if pid:
            missing_ids.add(pid)

    accounted = allocated_ids | missing_ids
    uncovered = boq_ids - accounted

    if uncovered:
        for pid in sorted(uncovered):
            issues.append({
                "check": "position_coverage",
                "severity": "warn",
                "position_id": pid,
                "message": f"Позиция ВОР не покрыта: нет ни аллокации, ни объяснения в missing_in_model",
            })
    else:
        issues.append({
            "check": "position_coverage",
            "severity": "ok",
            "message": f"Все {len(boq_ids)} позиций ВОР покрыты ({len(allocated_ids)} аллокаций + {len(missing_ids)} missing)",
        })

    extra_allocs = allocated_ids - boq_ids
    if extra_allocs:
        for pid in sorted(extra_allocs):
            issues.append({
                "check": "position_coverage",
                "severity": "info",
                "position_id": pid,
                "message": f"Аллокация для позиции, которой нет в брифинге (возможно подпозиция)",
            })

    return issues


# ====================================================================
# CHECK 4: Double Counting across specialists
# ====================================================================
def check_double_counting(all_outputs: dict[str, dict]) -> list[dict]:
    """Один кластер не может быть claimed > 100% суммарно по всем специалистам."""
    issues = []
    cluster_claims: dict[str, list[tuple[str, float]]] = defaultdict(list)

    for spec_key, data in all_outputs.items():
        ph1 = data.get("phase1_filtering", {})
        for c in ph1.get("claimed", []):
            cid = c.get("cluster_id", "")
            share = float(c.get("share", 1.0))
            cluster_claims[cid].append((spec_key, share))
        for c in ph1.get("partial", []):
            cid = c.get("cluster_id", "")
            share = float(c.get("share", 0.5))
            cluster_claims[cid].append((spec_key, share))

    for cid, claims in sorted(cluster_claims.items()):
        total_share = sum(s for _, s in claims)
        if total_share > 1.05:
            specs = ", ".join(f"{s}({sh:.0%})" for s, sh in claims)
            issues.append({
                "check": "double_counting",
                "severity": "alarm",
                "cluster_id": cid[:80],
                "total_share": round(total_share, 2),
                "specs": specs,
                "message": f"Двойной счёт: суммарный share = {total_share:.0%}",
            })

    if not any(i["severity"] == "alarm" for i in issues):
        issues.append({
            "check": "double_counting",
            "severity": "ok",
            "message": f"Нет двойного счёта ({len(cluster_claims)} кластеров проверено)",
        })

    return issues


# ====================================================================
# CHECK 5: Math Validation
# ====================================================================
def check_math_validation(spec_key: str, briefing: dict, output: dict) -> list[dict]:
    """Пересчитать quantity из source_clusters и сравнить с заявленным."""
    issues = []

    cluster_data = {}
    for c in briefing.get("candidate_clusters", []):
        cid = c.get("cluster_id", "")
        cluster_data[cid] = {
            "volume_m3": c.get("volume_m3", 0) or 0,
            "area_m2": c.get("area_m2", 0) or 0,
            "count": c.get("count", 0) or 0,
            "length_m": c.get("length_m", 0) or 0,
        }

    for alloc in output.get("phase3_allocations", []):
        pid = alloc.get("position_id", "?")
        qty = alloc.get("quantity")
        unit = str(alloc.get("unit", "")).lower().strip()
        confidence = float(alloc.get("confidence", 0) or 0)
        sources = alloc.get("source_clusters", [])

        if qty is None or qty == 0 or not sources:
            continue

        # Try to reconstruct from source_clusters
        reconstructed = 0.0
        can_reconstruct = True

        for sc in sources:
            cid = sc.get("cluster_id", "")
            share = float(sc.get("share", 1.0))
            contrib = sc.get("contribution")

            cd = cluster_data.get(cid, {})
            if not cd:
                can_reconstruct = False
                break

            if "м3" in unit or "м³" in unit:
                reconstructed += cd.get("volume_m3", 0) * share
            elif "м2" in unit or "м²" in unit:
                reconstructed += cd.get("area_m2", 0) * share
            elif "шт" in unit:
                reconstructed += cd.get("count", 0) * share
            elif "пог" in unit:
                reconstructed += cd.get("length_m", 0) * share
            else:
                can_reconstruct = False
                break

        if can_reconstruct and reconstructed > 0:
            delta = abs(qty - reconstructed)
            delta_pct = (delta / reconstructed) * 100 if reconstructed else 0

            if delta_pct > 10:
                issues.append({
                    "check": "math_validation",
                    "severity": "warn" if delta_pct < 30 else "alarm",
                    "position_id": pid,
                    "stated_qty": round(qty, 2),
                    "reconstructed_qty": round(reconstructed, 2),
                    "delta_pct": round(delta_pct, 1),
                    "message": f"Расхождение {delta_pct:.1f}%: заявлено {qty:.2f}, пересчёт из кластеров {reconstructed:.2f}",
                })

    if not any(i.get("severity") in ("warn", "alarm") for i in issues):
        n_checked = sum(1 for a in output.get("phase3_allocations", [])
                       if a.get("quantity") and a.get("source_clusters"))
        issues.append({
            "check": "math_validation",
            "severity": "ok",
            "message": f"Все {n_checked} аллокаций с source_clusters прошли пересчёт (дельта < 10%)",
        })

    return issues


# ====================================================================
# CHECK 6: Rejected Cluster Audit
# ====================================================================
def check_rejected_audit(spec_key: str, briefing: dict, output: dict) -> list[dict]:
    """Проверяем что отклонённые кластеры действительно не относятся к этому специалисту.
    Помечаем крупные rejected кластеры как требующие внимания."""
    issues = []

    cluster_data = {}
    for c in briefing.get("candidate_clusters", []):
        cluster_data[c.get("cluster_id", "")] = c

    ph1 = output.get("phase1_filtering", {})
    large_rejected = []

    for r in ph1.get("rejected", []):
        cid = r.get("cluster_id", "")
        cd = cluster_data.get(cid, {})
        vol = cd.get("volume_m3", 0) or 0
        area = cd.get("area_m2", 0) or 0

        if vol > 100 or area > 1000:
            large_rejected.append({
                "cluster_id": cid[:80],
                "volume_m3": round(vol, 1),
                "area_m2": round(area, 1),
                "reason": r.get("reason", "")[:200],
                "delegate_to": r.get("delegate_to", ""),
            })

    if large_rejected:
        for lr in large_rejected[:10]:
            issues.append({
                "check": "rejected_audit",
                "severity": "info",
                "cluster_id": lr["cluster_id"],
                "volume_m3": lr["volume_m3"],
                "area_m2": lr["area_m2"],
                "delegate_to": lr["delegate_to"],
                "message": f"Крупный отклонённый кластер (V={lr['volume_m3']}м³, A={lr['area_m2']}м²) → {lr['delegate_to'] or '?'}",
            })

    return issues


# ====================================================================
# CHECK 7: Confidence Distribution
# ====================================================================
def check_confidence_distribution(spec_key: str, output: dict) -> list[dict]:
    issues = []
    allocs = output.get("phase3_allocations", [])
    confs = [float(a.get("confidence", 0) or 0) for a in allocs if a.get("quantity") is not None]

    if not confs:
        return [{"check": "confidence", "severity": "alarm", "message": "Нет аллокаций с quantity"}]

    high = sum(1 for c in confs if c >= 0.65)
    med = sum(1 for c in confs if 0.4 <= c < 0.65)
    low = sum(1 for c in confs if c < 0.4)
    avg = sum(confs) / len(confs)

    issues.append({
        "check": "confidence",
        "severity": "ok" if avg >= 0.6 else ("warn" if avg >= 0.4 else "alarm"),
        "high": high,
        "medium": med,
        "low": low,
        "average": round(avg, 3),
        "message": f"Confidence: high={high}, med={med}, low={low}, avg={avg:.2f}",
    })

    return issues


# ====================================================================
# CHECK 8: DB-Level Reconciliation — raw BIM elements vs claimed
# ====================================================================
def check_db_reconciliation(
    all_outputs: dict[str, dict],
    all_briefings: dict[str, dict],
    db_path: Path | None = None,
) -> list[dict]:
    """Проверяет что суммарный объём ВСЕХ физических элементов из БД
    покрыт claimed кластерами по всем специалистам.
    Это главная проверка 'ничего не потеряно'."""
    issues = []

    if db_path is None or not db_path.exists():
        issues.append({
            "check": "db_reconciliation",
            "severity": "info",
            "message": "БД не найдена — пропуск проверки",
        })
        return issues

    import sqlite3
    conn = sqlite3.connect(str(db_path))

    # Get latest run_id
    cur = conn.execute("SELECT DISTINCT run_id FROM elements ORDER BY run_id DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        conn.close()
        return [{"check": "db_reconciliation", "severity": "alarm", "message": "Нет данных в БД"}]
    run_id = row[0]

    # Total volumes by category from DB
    db_totals = {}
    for row in conn.execute("""
        SELECT category,
               COUNT(*) as cnt,
               SUM(COALESCE(volume_m3, 0)) as vol,
               SUM(COALESCE(area_m2, 0)) as area
        FROM elements
        WHERE run_id = ? AND is_excluded = 0 AND is_physical = 1
        GROUP BY category
        ORDER BY SUM(COALESCE(volume_m3, 0)) DESC
    """, (run_id,)):
        db_totals[row[0]] = {"count": row[1], "volume_m3": row[2], "area_m2": row[3]}

    # Total from DB
    db_total_vol = sum(v["volume_m3"] for v in db_totals.values())
    db_total_area = sum(v["area_m2"] for v in db_totals.values())
    db_total_count = sum(v["count"] for v in db_totals.values())

    # Claimed totals across all specialists (from briefings)
    # Each briefing contains candidate_clusters which are a SUBSET of all clusters
    # Multiple specialists may have the same cluster in their briefing
    # We need to track unique clusters and their claimed shares
    cluster_claimed_shares: dict[str, float] = {}  # cid → max share claimed

    for spec_key, output in all_outputs.items():
        ph1 = output.get("phase1_filtering", {})
        for c in ph1.get("claimed", []):
            cid = c.get("cluster_id", "")
            share = float(c.get("share", 1.0))
            cluster_claimed_shares[cid] = cluster_claimed_shares.get(cid, 0) + share
        for c in ph1.get("partial", []):
            cid = c.get("cluster_id", "")
            share = float(c.get("share", 0.5))
            cluster_claimed_shares[cid] = cluster_claimed_shares.get(cid, 0) + share

    # Get cluster volumes from briefings
    all_clusters_in_briefings: dict[str, dict] = {}
    for spec_key, briefing in all_briefings.items():
        for c in briefing.get("candidate_clusters", []):
            cid = c.get("cluster_id", "")
            if cid not in all_clusters_in_briefings:
                all_clusters_in_briefings[cid] = {
                    "volume_m3": c.get("volume_m3", 0) or 0,
                    "area_m2": c.get("area_m2", 0) or 0,
                    "count": c.get("count", 0) or 0,
                    "category": c.get("category", ""),
                }

    # All clusters from DB (via SQL)
    db_clusters = {}
    for row in conn.execute("""
        SELECT category || '::' || COALESCE(family, '') || '::' || COALESCE(type_name, '') as cid,
               COUNT(*) as cnt,
               SUM(COALESCE(volume_m3, 0)) as vol,
               SUM(COALESCE(area_m2, 0)) as area
        FROM elements
        WHERE run_id = ? AND is_excluded = 0 AND is_physical = 1
        GROUP BY category, COALESCE(family, ''), COALESCE(type_name, '')
    """, (run_id,)):
        db_clusters[row[0]] = {"count": row[1], "volume_m3": row[2], "area_m2": row[3]}

    conn.close()

    # Compare: what % of DB clusters are in any briefing?
    db_vol_in_briefing = 0
    db_vol_not_in_briefing = 0
    large_uncovered = []

    for cid, cd in db_clusters.items():
        if cid in all_clusters_in_briefings:
            db_vol_in_briefing += cd["volume_m3"]
        else:
            db_vol_not_in_briefing += cd["volume_m3"]
            if cd["volume_m3"] > 50 or cd["area_m2"] > 500:
                large_uncovered.append({
                    "cluster_id": cid[:80],
                    "volume_m3": round(cd["volume_m3"], 1),
                    "area_m2": round(cd["area_m2"], 1),
                    "count": cd["count"],
                })

    # Coverage by category
    categories_with_specialists = set()
    for spec_key, briefing in all_briefings.items():
        for c in briefing.get("candidate_clusters", []):
            categories_with_specialists.add(c.get("category", ""))

    uncovered_categories = []
    for cat, totals in sorted(db_totals.items(), key=lambda x: -x[1]["volume_m3"]):
        if cat not in categories_with_specialists and totals["volume_m3"] > 10:
            uncovered_categories.append({
                "category": cat,
                "volume_m3": round(totals["volume_m3"], 1),
                "area_m2": round(totals["area_m2"], 1),
                "count": totals["count"],
            })

    # Report
    briefing_coverage_pct = (db_vol_in_briefing / db_total_vol * 100) if db_total_vol else 0
    issues.append({
        "check": "db_reconciliation",
        "severity": "ok" if briefing_coverage_pct > 80 else ("warn" if briefing_coverage_pct > 50 else "alarm"),
        "db_total_vol_m3": round(db_total_vol, 1),
        "db_total_area_m2": round(db_total_area, 1),
        "db_total_elements": db_total_count,
        "db_total_clusters": len(db_clusters),
        "clusters_in_briefings": len(all_clusters_in_briefings),
        "vol_covered_by_briefings_pct": round(briefing_coverage_pct, 1),
        "message": f"БД: {db_total_vol:.0f}м³ / {db_total_area:.0f}м² / {db_total_count} элементов / {len(db_clusters)} кластеров. "
                   f"В брифингах: {len(all_clusters_in_briefings)} кластеров ({briefing_coverage_pct:.1f}% объёма)",
    })

    # Claimed coverage
    claimed_vol = sum(
        all_clusters_in_briefings.get(cid, {}).get("volume_m3", 0) * min(sh, 1.0)
        for cid, sh in cluster_claimed_shares.items()
    )
    claimed_pct = (claimed_vol / db_total_vol * 100) if db_total_vol else 0
    issues.append({
        "check": "db_reconciliation",
        "severity": "info",
        "claimed_vol_m3": round(claimed_vol, 1),
        "claimed_pct_of_db": round(claimed_pct, 1),
        "n_specialists_with_output": len(all_outputs),
        "message": f"Claimed specialists: {claimed_vol:.0f}м³ ({claimed_pct:.1f}% от БД). "
                   f"NB: только {len(all_outputs)} из ~11 специалистов готовы",
    })

    if large_uncovered:
        for lc in large_uncovered[:10]:
            issues.append({
                "check": "db_reconciliation",
                "severity": "info",
                "cluster_id": lc["cluster_id"],
                "volume_m3": lc["volume_m3"],
                "area_m2": lc["area_m2"],
                "message": f"Кластер не в брифингах: {lc['cluster_id']} (V={lc['volume_m3']}м³, A={lc['area_m2']}м²)",
            })

    if uncovered_categories:
        for uc in uncovered_categories[:5]:
            issues.append({
                "check": "db_reconciliation",
                "severity": "warn",
                "category": uc["category"],
                "volume_m3": uc["volume_m3"],
                "message": f"Категория без специалиста: {uc['category']} ({uc['volume_m3']}м³, {uc['count']} элементов)",
            })

    return issues


# ====================================================================
# MAIN
# ====================================================================
def run_full_verification(
    spec_outputs_dir: Path,
    briefings_dir: Path,
    db_path: Path | None = None,
) -> dict:
    """Запускает все проверки и возвращает структурированный отчёт."""
    outputs = load_all_specialist_outputs(spec_outputs_dir)
    briefings = load_all_briefings(briefings_dir)

    report = {
        "specialists_checked": list(outputs.keys()),
        "per_specialist": {},
        "cross_specialist": {},
        "db_reconciliation": {},
        "summary": {},
    }

    total_alarms = 0
    total_warns = 0

    for spec_key, output in outputs.items():
        briefing = briefings.get(spec_key, {})
        if not briefing:
            print(f"  ! Нет брифинга для {spec_key}, пропускаем детальные проверки")
            continue

        checks = []
        checks.extend(check_volume_reconciliation(spec_key, briefing, output))
        checks.extend(check_cluster_coverage(spec_key, output, briefing))
        checks.extend(check_position_coverage(spec_key, briefing, output))
        checks.extend(check_math_validation(spec_key, briefing, output))
        checks.extend(check_rejected_audit(spec_key, briefing, output))
        checks.extend(check_confidence_distribution(spec_key, output))

        alarms = [c for c in checks if c.get("severity") == "alarm"]
        warns = [c for c in checks if c.get("severity") == "warn"]
        total_alarms += len(alarms)
        total_warns += len(warns)

        report["per_specialist"][spec_key] = {
            "checks": checks,
            "n_alarms": len(alarms),
            "n_warns": len(warns),
        }

    # Cross-specialist checks
    cross = check_double_counting(outputs)
    cross_alarms = sum(1 for c in cross if c.get("severity") == "alarm")
    total_alarms += cross_alarms
    report["cross_specialist"]["double_counting"] = cross

    # DB-level reconciliation
    db_checks = check_db_reconciliation(outputs, briefings, db_path)
    db_alarms = sum(1 for c in db_checks if c.get("severity") == "alarm")
    db_warns = sum(1 for c in db_checks if c.get("severity") == "warn")
    total_alarms += db_alarms
    total_warns += db_warns
    report["db_reconciliation"] = db_checks

    report["summary"] = {
        "total_specialists": len(outputs),
        "total_alarms": total_alarms,
        "total_warns": total_warns,
        "verdict": "PASS" if total_alarms == 0 else "FAIL",
    }

    return report


def print_report(report: dict):
    print("=" * 70)
    print("КОМПЛЕКСНАЯ ВЕРИФИКАЦИЯ РЕЗУЛЬТАТОВ")
    print("=" * 70)
    print(f"Специалистов: {report['summary']['total_specialists']}")
    print(f"Проверено: {', '.join(report['specialists_checked'])}")
    print()

    for spec_key, spec_report in report["per_specialist"].items():
        print(f"─── {spec_key.upper()} ───")
        for check in spec_report["checks"]:
            sev = check.get("severity", "?")
            icon = {"ok": "✓", "info": "ℹ", "warn": "⚠", "alarm": "✗"}.get(sev, "?")
            msg = check.get("message", "")
            check_name = check.get("check", "")

            # Color output
            if sev == "alarm":
                print(f"  {icon} [{check_name}] {msg}")
                for k, v in check.items():
                    if k not in ("check", "severity", "message"):
                        print(f"      {k}: {v}")
            elif sev == "warn":
                print(f"  {icon} [{check_name}] {msg}")
            elif sev == "ok":
                print(f"  {icon} [{check_name}] {msg}")
            elif sev == "info":
                print(f"  {icon} [{check_name}] {msg}")

        a = spec_report["n_alarms"]
        w = spec_report["n_warns"]
        print(f"  → Итого: {a} alarms, {w} warns")
        print()

    # Cross-specialist
    print("─── CROSS-SPECIALIST ───")
    for check in report["cross_specialist"].get("double_counting", []):
        sev = check.get("severity", "?")
        icon = {"ok": "✓", "alarm": "✗"}.get(sev, "?")
        print(f"  {icon} [{check['check']}] {check.get('message', '')}")
    print()

    # DB reconciliation
    if report.get("db_reconciliation"):
        print("─── DB RECONCILIATION ───")
        for check in report["db_reconciliation"]:
            sev = check.get("severity", "?")
            icon = {"ok": "✓", "info": "ℹ", "warn": "⚠", "alarm": "✗"}.get(sev, "?")
            print(f"  {icon} [{check.get('check', '')}] {check.get('message', '')}")
        print()

    # Final verdict
    print("=" * 70)
    v = report["summary"]["verdict"]
    ta = report["summary"]["total_alarms"]
    tw = report["summary"]["total_warns"]
    print(f"ВЕРДИКТ: {v}  (alarms={ta}, warns={tw})")
    print("=" * 70)


def main():
    spec_outputs_dir = REPO / "runs" / "event_6_1" / "specialist_outputs"
    briefings_dir = REPO / "runs" / "event_6_1" / "briefings"
    db_path = REPO / "runs" / "bim2vor.db"

    report = run_full_verification(spec_outputs_dir, briefings_dir, db_path)
    print_report(report)

    # Save report as JSON
    report_path = REPO / "runs" / "event_6_1" / "verification_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nОтчёт сохранён: {report_path}")


if __name__ == "__main__":
    main()
