import duckdb
conn = duckdb.connect(r'C:\Users\kuklev.d.s\bim_warehouse.db')
res = conn.execute("SELECT category, sum(volume_m3) FROM v_expert_analysis WHERE category IN ('OST_Walls', 'OST_Floors', 'OST_StructuralColumns') GROUP BY 1").fetchall()
print(res)
