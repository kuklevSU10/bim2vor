import duckdb
import json
import os

def extract_final_data():
    conn = duckdb.connect("bim_warehouse.db")
    results = {}

    # 1. МОНОЛИТ (Секции 5 и 6)
    print("Extracting Monolith data...")
    monolith_query = """
    SELECT 
        category,
        type_name,
        count(*) as count,
        sum(volume_m3) as total_volume
    FROM v_expert_analysis 
    WHERE category IN ('OST_StructuralFoundation', 'OST_Floors', 'OST_StructuralColumns', 'OST_StructuralFraming', 'OST_Walls')
      AND (type_name LIKE '%Бетон%' OR type_name LIKE '%ЖБ%' OR type_name LIKE '%КЦ_%' OR type_name LIKE '%ПЕР-%')
    GROUP BY 1, 2
    HAVING total_volume > 0
    """
    results['monolith'] = conn.execute(monolith_query).df().to_dict(orient='records')

    # 2. ФАСАДЫ (Секция 10)
    print("Extracting Facade data...")
    facade_query = """
    SELECT 
        category,
        type_name,
        count(*) as count,
        sum(area_m2) as total_area
    FROM v_expert_analysis 
    WHERE category IN ('OST_CurtainWallPanels', 'OST_CurtainWallMullions', 'OST_Windows')
       OR (category = 'OST_Walls' AND (type_name LIKE '%СН-%' OR type_name LIKE '%Фасад%'))
    GROUP BY 1, 2
    """
    results['facades'] = conn.execute(facade_query).df().to_dict(orient='records')

    # 3. ПОЗИЦИИ ВОР (для маппинга)
    print("Extracting BOQ structure...")
    boq_query = """
    SELECT 
        "Номер позиции" as code,
        "Наименование" as name,
        "Ед. изм." as unit
    FROM boq_raw
    """
    results['boq_structure'] = conn.execute(boq_query).df().to_dict(orient='records')

    os.makedirs("results/Event_6_1", exist_ok=True)
    with open("results/Event_6_1/final_raw_data.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print("Final raw data extracted to results/Event_6_1/final_raw_data.json")
    conn.close()

if __name__ == "__main__":
    extract_final_data()
