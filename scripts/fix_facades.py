# -*- coding: utf-8 -*-
"""Fix known issues in facades.json output."""
import json
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

p = Path(r"C:\Users\kuklev.d.s\PycharmProjects\bim2vor\runs\event_6_1\specialist_outputs\facades.json")
data = json.loads(p.read_text(encoding="utf-8"))

fixed = 0

# Fix 1: quantity should match contribution when share < 1
for alloc in data.get("phase3_allocations", []):
    sources = alloc.get("source_clusters", [])
    if not sources:
        continue
    if len(sources) == 1:
        sc = sources[0]
        contrib = sc.get("contribution")
        share = float(sc.get("share", 1.0))
        qty = alloc.get("quantity")
        if contrib is not None and qty is not None and share < 1.0 and qty > 0:
            delta_pct = abs(qty - contrib) / max(contrib, 0.01) * 100
            if delta_pct > 50:
                pid = alloc.get("position_id", "?")
                print(f"  FIX {pid}: qty {qty} -> {contrib} (was full cluster, now share={share})")
                alloc["quantity"] = round(contrib, 2)
                fixed += 1

# Fix 2: add orphaned clusters to dopniki
ph1 = data.get("phase1_filtering", {})
claimed_ids = set(c.get("cluster_id", "") for c in ph1.get("claimed", []))
used_ids = set()
for alloc in data.get("phase3_allocations", []):
    for sc in alloc.get("source_clusters", []):
        used_ids.add(sc.get("cluster_id", ""))
dopnik_ids = set(d.get("cluster_id", "") for d in data.get("phase4_gaps", {}).get("claimed_but_unallocated", []))
orphaned = claimed_ids - used_ids - dopnik_ids

for cid in sorted(orphaned):
    print(f"  ADD DOPNIK: {cid[:80]}")
    data["phase4_gaps"]["claimed_but_unallocated"].append({
        "cluster_id": cid,
        "suggested_dopnik": "Кластер без позиции ВОР — требуется проверка",
        "estimated_qty": 0,
        "unit": "",
    })
    fixed += 1

p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nFixed {fixed} issues in facades.json")
