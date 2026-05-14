import duckdb
import json

def extract_boq_targets():
    conn = duckdb.connect("bim_warehouse.db")
    
    query = """
    SELECT 
        "Номер позиции" as code,
        "Наименование" as name,
        "Ед. изм." as unit
    FROM boq_raw
    WHERE 
        "Номер позиции" LIKE '05.%' OR 
        "Номер позиции" LIKE '06.%' OR 
        "Номер позиции" LIKE '07.%' OR 
        "Номер позиции" LIKE '10.%'
    """
    
    df = conn.execute(query).df()
    
    # Сгруппируем по разделам для удобства агентов
    targets = {
        "monolith": df[df['code'].str.startswith('05.') | df['code'].str.startswith('06.')].to_dict(orient='records'),
        "masonry": df[df['code'].str.startswith('07.')].to_dict(orient='records'),
        "facades": df[df['code'].str.startswith('10.')].to_dict(orient='records')
    }
    
    with open("results/Event_6_1/boq_targets.json", "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False, indent=2)
        
    print(f"Extracted {len(df)} target BOQ positions.")
    conn.close()

if __name__ == "__main__":
    extract_boq_targets()
