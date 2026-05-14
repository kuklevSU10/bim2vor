# -*- coding: utf-8 -*-
"""Глубокий анализ всех типов стен — что у нас есть и как они называются."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from pathlib import Path

profile = json.loads(Path(r'C:\Users\kuklev.d.s\PycharmProjects\bim2vor\data\revit_profile.json').read_text(encoding='utf-8'))

walls = profile['categories']['OST_Walls']
print(f"OST_Walls: {walls['count']} элементов, {walls['volume_sum']} м³, {walls['area_sum']} м²")
print(f"\n=== Все семейства стен (top 20 + остальные ≥10 шт) ===")
for fam, n in walls['top_families']:
    print(f"  {n:>5}  {fam}")

print(f"\n=== Worksets ===")
for w, n in walls['top_worksets']:
    print(f"  {n:>5}  {w}")

print(f"\n=== Levels ===")
for lvl, n in walls['top_levels'][:15]:
    print(f"  {n:>5}  {lvl}")

# Аналогично для Floors
print("\n" + "=" * 60)
floors = profile['categories']['OST_Floors']
print(f"OST_Floors: {floors['count']} элементов, {floors['volume_sum']} м³, {floors['area_sum']} м²")
print(f"\n=== Семейства полов ===")
for fam, n in floors['top_families']:
    print(f"  {n:>5}  {fam}")

# Doors
print("\n" + "=" * 60)
doors = profile['categories']['OST_Doors']
print(f"OST_Doors: {doors['count']} элементов")
print(f"\n=== Семейства дверей ===")
for fam, n in doors['top_families']:
    print(f"  {n:>5}  {fam}")
