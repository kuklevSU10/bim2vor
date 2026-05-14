# -*- coding: utf-8 -*-
"""
SQLite-схема bim2vor — knowledge base + run history + audit trail.

Главные принципы:
1. Run-snapshots immutable — после прогона ничего не правим
2. Каждое число имеет provenance (rule_id или llm_call_id)
3. Knowledge base (recipes, taxonomy) живёт в YAML, в БД только результаты
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- ============================================================
-- Прогоны (runs)
-- ============================================================
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,                  -- uuid
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,                 -- running|completed|failed
    project_id TEXT NOT NULL,
    revit_file_path TEXT,
    revit_file_sha256 TEXT,
    boq_template_path TEXT,
    boq_template_sha256 TEXT,
    recipes_snapshot_path TEXT,           -- путь к zip-снимку recipes/
    llm_model_version TEXT,               -- claude-sonnet-4-7@2026-04-15
    config_json TEXT,                     -- сериализованный конфиг прогона
    summary_json TEXT,                    -- финальная сводка (coverage, deltas)
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

-- ============================================================
-- Элементы Revit (нормализованные)
-- ============================================================
CREATE TABLE IF NOT EXISTS elements (
    run_id TEXT NOT NULL,
    source_model TEXT NOT NULL DEFAULT '',  -- имя файла-источника: AR_BA, KR, OV, ...
    discipline TEXT NOT NULL DEFAULT '',    -- дисциплина: AR, KR, OV, VK, EOM, SS
    element_id TEXT NOT NULL,             -- Revit ID + UniqueId
    unique_id TEXT,                       -- Revit UniqueId
    category TEXT,                        -- canonical: walls/floors/doors/...
    category_raw TEXT,                    -- original: OST_Walls / "3M_88-108 м2"
    family TEXT,                          -- raw family name
    type_name TEXT,
    level TEXT,                           -- canonical level
    level_raw TEXT,                       -- "6. Этаж"
    level_floor INTEGER,                  -- 6
    level_zone TEXT,                      -- typical/basement/technical/penthouse
    workset TEXT,
    volume_m3 REAL,
    area_m2 REAL,
    length_m REAL,
    width_m REAL,
    cost REAL,
    is_physical INTEGER NOT NULL,         -- 1 если категория несёт физический объём
    is_excluded INTEGER NOT NULL DEFAULT 0,  -- 1 если это мусор (SketchLines)
    excluded_reason TEXT,
    family_parsed_json TEXT,              -- JSON: {layers, zone, rei, ...}
    raw_extra_json TEXT,                  -- остальные параметры (sparse)
    PRIMARY KEY (run_id, source_model, element_id)
);

CREATE INDEX IF NOT EXISTS idx_elem_cat ON elements(run_id, category);
CREATE INDEX IF NOT EXISTS idx_elem_excluded ON elements(run_id, is_excluded);
CREATE INDEX IF NOT EXISTS idx_elem_physical ON elements(run_id, is_physical);
CREATE INDEX IF NOT EXISTS idx_elem_source ON elements(run_id, source_model);
CREATE INDEX IF NOT EXISTS idx_elem_discipline ON elements(run_id, discipline);

-- ============================================================
-- Позиции ВОР (нормализованные)
-- ============================================================
CREATE TABLE IF NOT EXISTS boq_positions (
    run_id TEXT NOT NULL,
    position_id TEXT NOT NULL,            -- "1.1.3"
    parent_id TEXT,                       -- "1.1"
    seq_num INTEGER,                      -- "№ п/п" из шаблона
    code_classifier TEXT,                 -- "Позиция по классификатору" (ГЭСН/ФЕР)
    code_airos TEXT,                      -- "Код Айроса"
    code_contractor TEXT,
    name TEXT NOT NULL,
    unit TEXT,                            -- м3/м2/шт/тн/...
    qty_planned REAL,                     -- плановое из шаблона
    qty_calculated REAL,                  -- наше посчитанное
    qty_confidence REAL,                  -- 0..1
    is_section_header INTEGER DEFAULT 0,  -- 1 = заголовок раздела (без расчёта)
    excel_row INTEGER,                    -- номер строки в исходном Excel
    excel_sheet TEXT,
    raw_data_json TEXT,                   -- остальные колонки шаблона
    PRIMARY KEY (run_id, position_id)
);

CREATE INDEX IF NOT EXISTS idx_pos_parent ON boq_positions(run_id, parent_id);

-- ============================================================
-- Маппинг позиция ↔ элементы (с учётом 1:N и shares)
-- ============================================================
CREATE TABLE IF NOT EXISTS mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    element_id TEXT NOT NULL,
    share REAL NOT NULL DEFAULT 1.0,      -- доля элемента, идущая в позицию
    quantity_contribution REAL,           -- объёмный вклад элемента в позицию
    confidence REAL NOT NULL,
    method TEXT NOT NULL,                 -- rule|fuzzy|llm|manual
    rule_id TEXT,                         -- ID использованного правила
    llm_call_id TEXT,                     -- ID llm-вызова если method=llm
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_map_pos ON mappings(run_id, position_id);
CREATE INDEX IF NOT EXISTS idx_map_elem ON mappings(run_id, element_id);

-- ============================================================
-- Audit-cells: чем подкреплено каждое число в результате
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_cells (
    run_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    field TEXT NOT NULL,                  -- "qty_calculated", "qty_share_concrete"
    value TEXT,
    method TEXT NOT NULL,                 -- sum|count|llm_allocate|formula|manual
    formula TEXT,                         -- "SUM(volume_m3) WHERE category=walls AND ..."
    elements_used_json TEXT,              -- список element_id
    rule_id TEXT,
    llm_call_id TEXT,
    confidence REAL,
    PRIMARY KEY (run_id, position_id, field)
);

-- ============================================================
-- Reasoning cells (LLM calls) — полный аудит
-- ============================================================
CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,                  -- uuid
    run_id TEXT,
    cell_type TEXT NOT NULL,              -- family_parse|allocate|verify|допник_propose
    prompt_hash TEXT NOT NULL,            -- sha256 для cache
    model_version TEXT NOT NULL,
    prompt_full TEXT NOT NULL,            -- JSON
    reasoning_trace TEXT,                 -- thinking content
    response_full TEXT NOT NULL,
    self_verify_passed INTEGER,
    constraint_check_passed INTEGER,
    confidence REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    thinking_tokens INTEGER,
    latency_ms INTEGER,
    cost_usd REAL,
    cached_from_id TEXT,                  -- если результат взят из кеша — id оригинала
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_run ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_hash ON llm_calls(prompt_hash);

-- ============================================================
-- Reasoning cache (для детерминизма повторных прогонов)
-- ============================================================
CREATE TABLE IF NOT EXISTS reasoning_cache (
    prompt_hash TEXT PRIMARY KEY,         -- sha256(prompt + model_version + recipe_versions)
    response_json TEXT NOT NULL,
    model_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    invalidated_at TEXT,
    hit_count INTEGER DEFAULT 0
);

-- ============================================================
-- Discrepancies (расхождения план/факт + допники)
-- ============================================================
CREATE TABLE IF NOT EXISTS discrepancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- delta|missing_in_model|missing_in_boq|допник|coverage
    position_id TEXT,
    element_ids_json TEXT,                -- для допников
    severity TEXT,                        -- info|warn|alarm
    metric REAL,                          -- например, % отклонения
    description TEXT NOT NULL,
    suggested_action TEXT
);

CREATE INDEX IF NOT EXISTS idx_disc_run ON discrepancies(run_id);
CREATE INDEX IF NOT EXISTS idx_disc_kind ON discrepancies(run_id, kind);

-- ============================================================
-- Manual overrides (правки человеком)
-- ============================================================
CREATE TABLE IF NOT EXISTS manual_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,             -- глобально на проект, не на прогон
    position_id TEXT,
    element_id TEXT,
    field TEXT NOT NULL,
    value TEXT,
    author TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    active INTEGER DEFAULT 1
);

-- ============================================================
-- Rule usage stats (для мониторинга knowledge base)
-- ============================================================
CREATE TABLE IF NOT EXISTS rule_usage (
    rule_id TEXT PRIMARY KEY,
    times_fired INTEGER DEFAULT 0,
    times_overridden INTEGER DEFAULT 0,
    last_used_at TEXT,
    avg_confidence REAL
);
"""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Миграция: добавляет source_model/discipline в elements если их нет (v1→v2)."""
    cur = conn.execute("PRAGMA table_info(elements)")
    cols = {row[1] for row in cur.fetchall()}
    if "source_model" not in cols:
        conn.execute("ALTER TABLE elements ADD COLUMN source_model TEXT NOT NULL DEFAULT ''")
    if "discipline" not in cols:
        conn.execute("ALTER TABLE elements ADD COLUMN discipline TEXT NOT NULL DEFAULT ''")
    conn.commit()


def init_db(db_path: Path) -> sqlite3.Connection:
    """Создаёт БД и применяет схему. Идемпотентно."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Проверяем, нужна ли миграция v1→v2
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='elements'")
        if cur.fetchone():
            _migrate_v1_to_v2(conn)
    except Exception:
        pass

    conn.executescript(DDL)
    cur = conn.execute("SELECT version FROM schema_version")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
    else:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()
    return conn


if __name__ == "__main__":
    db = Path(__file__).resolve().parents[2] / "runs" / "bim2vor.db"
    conn = init_db(db)
    print(f"DB initialized: {db}")
    conn.close()
