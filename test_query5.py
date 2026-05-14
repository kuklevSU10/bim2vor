import duckdb
conn = duckdb.connect(r'C:\Users\kuklev.d.s\bim_warehouse.db')
print(conn.execute("SELECT category, count(*) FROM v_expert_analysis WHERE type_name IS NOT NULL GROUP BY 1 LIMIT 20").fetchall())
