import duckdb

def create_master_view():
    conn = duckdb.connect("bim_warehouse.db")
    
    tables = conn.execute("SHOW TABLES").fetchall()
    bim_tables = [t[0] for t in tables if t[0].startswith('SOB_')]
    
    print(f"Consolidating {len(bim_tables)} tables with corrected column mapping...")
    
    # Карта соответствия: наше имя -> возможные имена в выгрузке (DDC формат использует 'Name : Type')
    mapping = {
        'category': ['"Category : String"', '"Категория"', '"Category"'],
        'family': ['"Family : String"', '"Семейство"', '"Family"'],
        'type_name': ['"Type Name : String"', '"Тип"', '"Type Name"', '"Type"'],
        'volume': ['"Volume : Double"', '"Объем"', '"Volume"'],
        'area': ['"Area : Double"', '"Площадь"', '"Area"'],
        'length': ['"Length : Double"', '"Длина"', '"Length"'],
        'level': ['"Level : String"', '"Уровень"', '"Level"'],
        'mark': ['"Mark : String"', '"Марка"', '"Mark"']
    }
    
    queries = []
    for t in bim_tables:
        existing_cols = [c[1] for c in conn.execute(f"PRAGMA table_info('{t}')").fetchall()]
        select_parts = []
        
        for canonical, options in mapping.items():
            found = False
            for opt in options:
                clean_opt = opt.replace('"', '')
                if clean_opt in existing_cols:
                    select_parts.append(f"CAST({opt} AS VARCHAR) as {canonical}")
                    found = True
                    break
            if not found:
                select_parts.append(f"NULL as {canonical}")
        
        select_parts.append(f"'{t}' as source_file")
        queries.append(f"SELECT {', '.join(select_parts)} FROM {t}")
    
    union_query = " UNION ALL ".join(queries)
    
    print("Executing Union and creating bim_master...")
    conn.execute(f"CREATE OR REPLACE TABLE bim_master AS {union_query}")
    
    print("Creating Expert View...")
    conn.execute("""
    CREATE OR REPLACE VIEW v_expert_analysis AS 
    SELECT 
        source_file,
        category,
        family,
        type_name,
        TRY_CAST(REPLACE(REPLACE(volume, ',', '.'), ' ', '') AS DOUBLE) as volume_m3,
        TRY_CAST(REPLACE(REPLACE(area, ',', '.'), ' ', '') AS DOUBLE) as area_m2,
        TRY_CAST(REPLACE(REPLACE(length, ',', '.'), ' ', '') AS DOUBLE) as length_m,
        level,
        mark
    FROM bim_master
    """)
    
    print("\nData Summary (Top Categories by Volume):")
    res = conn.execute("SELECT category, count(*), sum(volume_m3) as v FROM v_expert_analysis GROUP BY 1 ORDER BY v DESC LIMIT 15").fetchall()
    for row in res:
        cat = row[0] if row[0] else "Unknown"
        vol = row[2] if row[2] else 0
        print(f"  {cat}: {row[1]} elements, {vol:.2f} m3")

    conn.close()

if __name__ == "__main__":
    create_master_view()
