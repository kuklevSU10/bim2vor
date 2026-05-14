import duckdb
import pandas as pd
import re

def normalize_text(text):
    if not isinstance(text, str): return ""
    text = text.lower()
    replacements = {'a':'а', 'b':'в', 'c':'с', 'e':'е', 'h':'н', 'k':'к', 'm':'м', 'o':'о', 'p':'р', 't':'т', 'x':'х', 'y':'у'}
    for eng, rus in replacements.items():
        text = text.replace(eng, rus)
    return text

conn = duckdb.connect(r'C:\Users\kuklev.d.s\bim_warehouse.db')
query = """
SELECT 
    source_file, category, type_name, mark, COUNT(*) as count, SUM(volume_m3) as volume_m3, SUM(area_m2) as area_m2
FROM v_expert_analysis
WHERE category IN ('OST_StructuralColumns', 'OST_Floors', 'OST_Walls')
GROUP BY 1, 2, 3, 4 LIMIT 20
"""
df_bim = conn.execute(query).df()

for idx, row in df_bim.iterrows():
    cat = str(row['category']).upper()
    t_name = str(row['type_name'])
    t_name_norm = normalize_text(t_name)
    vol = float(row['volume_m3'] or 0)
    
    print(f"Cat: {cat}, Name: {t_name_norm}, Vol: {vol}")
    
    if ('колонн' in t_name_norm) and ('OST_STRUCTURALCOLUMNS' in cat or 'OST_COLUMNS' in cat):
        print("  -> MATCHED COLUMN")
    elif ('перекрыт' in t_name_norm or 'пер-' in t_name_norm) and 'OST_FLOORS' in cat:
        print("  -> MATCHED FLOOR")
    elif 'стен' in t_name_norm and ('бетон' in t_name_norm or 'кц_' in t_name_norm) and 'OST_WALLS' in cat:
        print("  -> MATCHED MONOLITH WALL")
    elif ('блок' in t_name_norm or 'кирпич' in t_name_norm or 'газобетон' in t_name_norm) and 'OST_WALLS' in cat:
        print("  -> MATCHED MASONRY")
