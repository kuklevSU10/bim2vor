import duckdb
conn = duckdb.connect(r'C:\Users\kuklev.d.s\PycharmProjects\bim2vor\bim_warehouse.db')
print("Floors:")
res_floors = conn.execute("SELECT DISTINCT type_name FROM v_expert_analysis WHERE category='OST_Floors' LIMIT 10").fetchall()
for r in res_floors: print(r[0])

print("Walls:")
res_walls = conn.execute("SELECT DISTINCT type_name FROM v_expert_analysis WHERE category='OST_Walls' LIMIT 10").fetchall()
for r in res_walls: print(r[0])
