import os
import duckdb
import pandas as pd
import glob

# Настройки
DB_PATH = "bim_warehouse.db"
EXTRACT_DIR = r"C:\Users\kuklev.d.s\PycharmProjects\bim2vor\Выгрузка 6.1"
BOQ_FILE = r"C:\Users\kuklev.d.s\PycharmProjects\bim2vor\runs\demo_run\Расчет ПЗ_Событие 6.1_Версия 4.xlsx"

def setup_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    print(f"Connecting to {DB_PATH}...")
    conn = duckdb.connect(DB_PATH)
    return conn

def ingest_revit_exports(conn):
    print("Ingesting Revit exports...")
    files = glob.glob(os.path.join(EXTRACT_DIR, "*.xlsx"))
    
    for f in files:
        file_name = os.path.basename(f)
        if file_name.startswith("~$"): continue
        
        print(f"  Processing {file_name}...")
        try:
            # Читаем только необходимые колонки (категория, семейство, тип, объем, площадь, уровень и т.д.)
            # Но так как мы не знаем точно их названия во всех файлах, 
            # мы сначала прочитаем 1 строку, чтобы понять схему.
            df_sample = pd.read_excel(f, nrows=1)
            
            # Добавляем метаданные
            df = pd.read_excel(f)
            df['source_file'] = file_name
            df['run_tag'] = "Event 6.1"
            df = df.astype(str)
            
            # Для каждого файла создаем свою таблицу, чтобы не мучаться с колонками
            table_name = file_name.replace(".xlsx", "").replace("-", "_").replace(".", "_")
            # DuckDB не любит точки и тире в именах таблиц
            safe_table_name = "".join([c if c.isalnum() else "_" for c in table_name])
            
            conn.execute(f"CREATE TABLE {safe_table_name} AS SELECT * FROM df")
            print(f"  Done: {len(df)} rows in table {safe_table_name}")
        except Exception as e:
            print(f"  Error processing {file_name}: {e}")

def ingest_boq(conn):
    print("Ingesting BOQ structure...")
    try:
        df_boq = pd.read_excel(BOQ_FILE)
        df_boq.columns = [str(c).strip() for c in df_boq.columns]
        df_boq = df_boq.astype(str)
        conn.execute("CREATE TABLE boq_raw AS SELECT * FROM df_boq")
        print(f"  Done: {len(df_boq)} rows")
    except Exception as e:
        print(f"  Error processing BOQ: {e}")

if __name__ == "__main__":
    db_conn = setup_db()
    ingest_revit_exports(db_conn)
    ingest_boq(db_conn)
    
    # Проверка списка таблиц
    tables = db_conn.execute("SHOW TABLES").fetchall()
    print("\nTables in Database:")
    for t in tables:
        count = db_conn.execute(f"SELECT count(*) FROM {t[0]}").fetchone()[0]
        print(f"  {t[0]}: {count} rows")
        
    db_conn.close()
    print("\nWarehouse initialization complete.")
