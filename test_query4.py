import duckdb
conn = duckdb.connect(r'C:\Users\kuklev.d.s\bim_warehouse.db')
print("Floors:")
res_floors = conn.execute("SELECT DISTINCT type_name FROM v_expert_analysis WHERE category='OST_Floors' LIMIT 10").fetchall()
for r in res_floors: print(r[0])

print("Columns:")
res_cols = conn.execute("SELECT DISTINCT type_name FROM v_expert_analysis WHERE category='OST_StructuralColumns' LIMIT 10").fetchall()
for r in res_cols: print(r[0])
