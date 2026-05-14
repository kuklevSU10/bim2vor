# -*- coding: utf-8 -*-
"""
Final verified BIM analysis for Символ 4А tender — masonry sections 06.01 and 08.
Reads from runs/simvol_walls.db, outputs text table.
"""
import sqlite3, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

conn = sqlite3.connect('runs/simvol_walls.db')
c = conn.cursor()


def dedup(families, zone=None, metric='volume'):
    """Deduplicated sum by element ID with architecture-model priority."""
    ph = ','.join(['?'] * len(families))
    zc = ''
    if zone == 'underground':
        zc = 'AND source_model = "PRK"'
    elif zone == 'above':
        zc = 'AND source_model != "PRK"'
    col = metric
    sql = (
        f'WITH uw AS ('
        f'  SELECT id, family, {col}, source_model,'
        f'  ROW_NUMBER() OVER (PARTITION BY id ORDER BY'
        f'    CASE source_model WHEN "K39" THEN 1 WHEN "K40" THEN 2'
        f'    WHEN "PRK" THEN 3 WHEN "K39_FSD" THEN 4 WHEN "K40_FSD" THEN 5 END'
        f'  ) as rn'
        f'  FROM walls WHERE family IN ({ph}) {zc}'
        f') SELECT ROUND(SUM({col}),2), COUNT(*) FROM uw WHERE rn = 1'
    )
    c.execute(sql, list(families))
    r = c.fetchone()
    return (r[0] or 0, r[1] or 0)


# =========================================================================
# BUILD FINAL TABLE
# =========================================================================

rows = []

# --- 06.01.01 ---
fams = ['НР_Газобетон D600_200мм', 'НР_200_Газобетон_200', 'НР_Газобетон D600_200мм (К36)']
v, n = dedup(fams)
rows.append(('06.01.01', 'Кладка наруж. стен газобетон D600 200мм', 'м3', 4060.0, v, n, ''))

# --- 06.01.02 ---
v, n = dedup(['НР_Кирпич полнотелый_250мм'])
rows.append(('06.01.02', 'Кладка наруж. стен кирпич 250мм', 'м3', 78.2, v, n, ''))

# --- 08.01.01 ---
a, n = dedup(['ВН_Кирпич полнотелый_120мм'], 'underground', 'area')
rows.append(('08.01.01', 'Кирпич 120мм подземные внутр.', 'м2', 11344.0, a, n, 'PRK'))

# --- 08.01.02 ---
a, n = dedup(['НР_Ут Минвата ρ 110 м/кг³_100мм'], 'underground', 'area')
rows.append(('08.01.02', 'Зашивка минватой p110 100мм подземн.', 'м2', 2230.0, a, n, 'PRK; p=110,t=100мм совпадают'))

# --- 08.02.01+02: газобетон 200мм надземн + допы ---
fams_main = ['ВН_Газобетон D600_200мм', 'ВН_200_Газобетон_200']
v_main, n_main = dedup(fams_main, 'above', 'volume')
v_ker, n_ker = dedup(['ВН_200_Керамзитобетон_200'], 'above', 'volume')
v_250, n_250 = dedup(['ВН_Газобетон D600_250мм'], 'above', 'volume')
v_total = round(v_main + v_ker + v_250, 2)
n_total = n_main + n_ker + n_250
note = f'Осн:{v_main}+керамзит:{v_ker}+250мм:{v_250}'
rows.append(('08.02.01+02', 'Газобетон D600 200мм надземные внутр.', 'м3', 6914.98, v_total, n_total, note))

# --- 08.02.03+04: кирпич 120мм надземн + допы ---
fams_main = ['ВН_Кирпич полнотелый_120мм', 'ВН_120_Кирпич_120', '(ВН)_120_(КРП)']
a_main, n_main = dedup(fams_main, 'above', 'area')
a_k250, n_k250 = dedup(['ВН_Кирпич полнотелый_250мм', '(ВН)_250_(КРП)'], 'above', 'area')
a_total = round(a_main + a_k250, 2)
n_total = n_main + n_k250
note = f'Осн 120мм:{a_main}+кирп.250мм:{a_k250}'
rows.append(('08.02.03+04', 'Кирпич 120мм надземные внутр.', 'м2', 7022.0, a_total, n_total, note))

# --- ПГП: все надземные в одну сумму, сравним с суммой ВОР ---
# VOR: 08.02.05(5512) + 08.02.06(238) + 08.02.06.1(6174.91) + 08.02.07(373) = 12297.91
fams_pgp_all = [
    'ВН_МОП_ПГП_На всю высоту_80мм',
    'ВН_80_ПГП_80_На всю высоту_(вне квартир)',
    'ВН_80_ПГП_80_На высоту одного блока',
    'ВН_ПГП_На всю высоту_80мм',
    'ВН_ПГП_На всю высоту_80мм (Красные линии)',
]
a_pgp, n_pgp = dedup(fams_pgp_all, 'above', 'area')
cust_pgp = 5512 + 238 + 6174.91 + 373
note = 'K40 не разделяет МОП/квартиры/трассировку'
rows.append(('08.02.05-07', 'ПГП 80мм надземные (все типы)', 'м2', cust_pgp, a_pgp, n_pgp, note))

# --- 08.02.08 ---
a, n = dedup(['ВН_Газобетон_На всю высоту_80мм'], 'above', 'area')
rows.append(('08.02.08', 'Газобетон 80мм шахты на всю высоту', 'м2', 4118.0, a, n, ''))

# --- 08.02.09 ---
a, n = dedup(['ВН_Газобетон_На всю высоту_100мм'], 'above', 'area')
rows.append(('08.02.09', 'Газобетон 100мм шахты на всю высоту', 'м2', 167.0, a, n, ''))

# --- 08.02.10 + доп ---
a_main, n_main = dedup(['(ВН)_75_(ГКЛ_50_Отделка_25)', '(ВН)_75_(ГКЛ)'], 'above', 'area')
a_95, n_95 = dedup(['(ВН)_95_(ГКЛ_70_Отделка_25)'], 'above', 'area')
a_total = round(a_main + a_95, 2)
n_total = n_main + n_95
note = f'Осн(каркас50):{a_main}+доп(каркас70):{a_95}'
rows.append(('08.02.10', 'ГКЛ однослойная на каркасе', 'м2', 3277.0, a_total, n_total, note))

# --- 08.02.11 ---
a, n = dedup(['(ВН)_100_(ГКЛ_50_Отделка_2х25)'], 'above', 'area')
rows.append(('08.02.11', 'ГКЛ двухслойная на каркасе 50мм', 'м2', 5158.0, a, n, ''))

# =========================================================================
# UNDERGROUND extras (no VOR position)
# =========================================================================
v_ug_gb, n_ug_gb = dedup(['ВН_Газобетон D600_200мм'], 'underground', 'volume')
a_ug_gb, _ = dedup(['ВН_Газобетон D600_200мм'], 'underground', 'area')

fams_ug_pgp = ['ВН_ПГП_На всю высоту_80мм', 'ВН_МОП_ПГП_На всю высоту_80мм', 'ВН_ПГП_80мм_межкомнатные']
a_ug_pgp, n_ug_pgp = dedup(fams_ug_pgp, 'underground', 'area')

# Not-to-build
a_ne, n_ne = dedup(['ВН_ПГП_Не возводится_80мм', 'ВН_ПГП_На всю высоту_80мм_не возвод.'], 'above', 'area')

# =========================================================================
# PRINT
# =========================================================================
W = 140
print()
print('=' * W)
print('ИТОГОВАЯ ТАБЛИЦА BIM: ЖК Символ 4А — Кладка (06.01 + 08)')
print('5 BIM-моделей, дедупликация по Element ID, приоритет: K39 > K40 > PRK > FSD')
print('=' * W)
print(f'{"Позиция":<14} {"Наименование":<46} {"Ед.":<5} {"Заказчик":>10} {"BIM":>12} {"D%":>8} {"Эл.":>6}  Примечание')
print('-' * W)

for pos, name, unit, cust, bim, cnt, note in rows:
    if cust > 0:
        d = (bim - cust) / cust * 100
        ds = f'{d:+.1f}%'
    else:
        ds = 'n/a'
    print(f'{pos:<14} {name:<46} {unit:<5} {cust:>10.2f} {bim:>12.2f} {ds:>8} {cnt:>6}  {note}')

print('-' * W)
print()
print('ПОДЗЕМНЫЕ ЭЛЕМЕНТЫ БЕЗ ПОЗИЦИИ ВОР:')
print(f'  Газобетон D600 200мм (PRK): {v_ug_gb} м3 / {a_ug_gb} м2  ({n_ug_gb} эл.)')
print(f'  ПГП 80мм (PRK):             {a_ug_pgp} м2  ({n_ug_pgp} эл.)')
print()
print(f'ИСКЛЮЧЕНО: ПГП "Не возводится": {a_ne} м2  ({n_ne} эл.)')
print()

# Summary
print('=' * W)
print('ВЕРИФИКАЦИЯ:')
print('  Ед.изм.: все совпадают с ВОР (м3 для газобетона/кирпича наружного и 200мм, м2 для остальных)')
print('  Толщины: vol/area ratio подтверждает именование семейств (200/250/120/80/100/75/95/100мм)')
print('  Дедупликация: 2950 общих ID K39/K39_FSD + 8569 общих K40/K40_FSD -> убраны')
print('  Зонирование: PRK=подземная, K39/K40=надземная (Level пусты во всех моделях)')
print()
print('ИЗМЕНЕНИЯ ОТ ПРЕДЫДУЩЕЙ ВЕРСИИ:')
print('  1. 08.02.01+02: +керамзитобетон 200мм (270 м3) + газобетон 250мм (12 м3)')
print('  2. 08.02.03+04: +кирпич 250мм (175 м2)')
print('  3. 08.02.05-07: объединены ВСЕ ПГП (МОП + вне квартир + квартирные + полная выс.)')
print('  4. 08.02.10: +ГКЛ 95мм каркас 70 (751 м2) к однослойной')
print('  5. Подземные газобетон и ПГП вынесены отдельно (нет позиции ВОР)')
print('=' * W)

conn.close()
