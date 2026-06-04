-- BUER 数据层 schema. 对照 BUER_Design_v2.0.md §4.2.
-- 注意: 设计文本里 incidents.details 标 JSONB(PostgreSQL 类型),
-- SQLite 无 JSONB, 落地为 TEXT 存 JSON 字符串.
-- P5 CHECK 约束仅对全新库生效 (IF NOT EXISTS 对存量表是 no-op, SQLite 无法 ALTER 加 CHECK).
-- 应用层守卫是主防线; CHECK 是新库额外的数据库层校验。

-- 项目
CREATE TABLE IF NOT EXISTS projects (
    id                  INTEGER PRIMARY KEY,
    root_path           TEXT NOT NULL,           -- 项目根 (boundary_breach, §2.4)
    lifecycle_phase     TEXT DEFAULT 'growth',   -- growth / stable (§4.8)
    test_report_path    TEXT,                    -- JUnit XML 位置 (§4.5, NULL=常见位置启发)
    no_define_count     INTEGER DEFAULT 0,       -- 全量 ingest 时统计的无 define 文件数（覆盖率分母用）
    branch              TEXT,                    -- git 分支名; NULL = 非 git 项目
    created_at_commit   TEXT,                    -- HEAD commit hash at first creation
    created_at          TIMESTAMP
);

-- idx_projects_root_branch unique index is created in store._init_schema migration
-- (after ALTER TABLE migration ensures branch column exists on old DBs)

-- 判定记录 (agent 每次编辑 = 一个判定), 版本链基础
CREATE TABLE IF NOT EXISTS determinations (
    id               INTEGER PRIMARY KEY,
    project_id       INTEGER REFERENCES projects(id),
    seq              INTEGER NOT NULL,        -- 全局顺序 (≺ 偏序实现, §3.2)
    file_path        TEXT NOT NULL,
    define_name      TEXT,                    -- 函数/类名 (节点标识)
    node_fingerprint TEXT,                    -- 结构指纹 (define_loop / 等价类)
    content_hash     TEXT NOT NULL DEFAULT '', -- body content hash for change detection (decoupled from coarse/fine)
    return_type      TEXT DEFAULT '',         -- 显式 -> Type 标注（L3 推断用）
    git_commit       TEXT,                    -- HEAD commit at determination time
    edit_type        TEXT CHECK(edit_type IN ('create', 'modify', 'delete')),
    created_at       TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_det_node ON determinations(project_id, file_path, define_name, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_det_seq_unique ON determinations(project_id, seq);

-- 调用/引用图 (caller → callee, 静态分析, entity-theoretic).
-- 双重角色: (1) 喂 G_D 跨 define 边识别 (§4.2a); (2) 单独可查 (谁调用 A).
CREATE TABLE IF NOT EXISTS call_edges (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    caller      TEXT NOT NULL,
    callee      TEXT NOT NULL,
    edge_kind   TEXT,                         -- call / import
    source_file TEXT,                         -- absolute path of the file that owns this edge
    UNIQUE(project_id, caller, callee)
);

-- Re-export 穿透表 (barrel 重定向, §4.2a batch 1).
-- barrel_module: module_name_of(barrel 文件); exported_name: 对外暴露的名字
-- (aliased 时是别名 B); target_module/target_name: 真实定义所在模块和原名.
-- star re-export 第二批 (预留 exported_name='*', target_name='*').
-- IF NOT EXISTS: 已有库自动建表，无需 ALTER.
CREATE TABLE IF NOT EXISTS reexport_edges (
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER REFERENCES projects(id),
    barrel_module  TEXT NOT NULL,
    exported_name  TEXT NOT NULL,
    target_module  TEXT NOT NULL,
    target_name    TEXT NOT NULL,
    UNIQUE(project_id, barrel_module, exported_name)
);

-- 判定历史 G_D 边 (§3.3, 横向关系 omega/Gamma_R/连通分量基础).
-- 节点 = determinations.id. 边 u→v = 编辑 v 消耗编辑 u 的产物.
CREATE TABLE IF NOT EXISTS gd_edges (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    from_det    INTEGER REFERENCES determinations(id),  -- 上游 u (产物被消耗)
    to_det      INTEGER REFERENCES determinations(id),  -- 下游 v (消耗者)
    edge_class  TEXT NOT NULL,   -- cross_define_dataflow / cross_define_callgraph
    UNIQUE(project_id, from_det, to_det),
    CHECK(from_det != to_det)    -- 𝒢_D 必须是 DAG：禁自环 (新库生效; 应用层守卫是主防线)
);
CREATE INDEX IF NOT EXISTS idx_gd_to   ON gd_edges(project_id, to_det);
CREATE INDEX IF NOT EXISTS idx_gd_from ON gd_edges(project_id, from_det);

-- 等价类 (§2.4.4, define_loop / SymbolIndex 基础设施 / 同类传播)
CREATE TABLE IF NOT EXISTS node_equivalence_classes (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    class_key   TEXT NOT NULL,                -- 指纹归一化键
    member_node TEXT NOT NULL,
    UNIQUE(project_id, class_key, member_node)
);

-- 测试结果 (§4.5, debug_loop; §4.4 实时捕获)
-- source: 'junit_xml' (§4.5 XML ingestion) | 'stdout' (§4.4 Bash hook capture)
-- inserted_at: row creation time (UTC); used for recency dedup across both sources.
CREATE TABLE IF NOT EXISTS test_runs (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    seq         INTEGER,                      -- 关联最近判定 seq
    source_path TEXT,
    source_mtime TIMESTAMP,
    passed INTEGER, failed INTEGER, skipped INTEGER,
    source      TEXT DEFAULT 'junit_xml',
    inserted_at TIMESTAMP DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS test_cases (
    id          INTEGER PRIMARY KEY,
    test_run_id INTEGER REFERENCES test_runs(id),
    classname TEXT, name TEXT, file_path TEXT,
    status      TEXT CHECK(status IN ('passed', 'failed', 'skipped', 'error'))
);

-- 待重算队列 (§4.0 v2.1 hook 基础设施). post-read/post-edit 轻量入队; stop 异步出队.
-- status: 'pending' = 等待; 'done' = 已处理. 同 (project_id, file_path) pending 去重.
CREATE TABLE IF NOT EXISTS pending_recompute (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    file_path   TEXT NOT NULL,
    enqueued_at TIMESTAMP DEFAULT (datetime('now')),
    status      TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_recompute ON pending_recompute(project_id, status);

-- 覆盖率映射 (§4.5 精确档). 无覆盖率时空表, 退启发关联.
CREATE TABLE IF NOT EXISTS coverage_map (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    test_case   TEXT NOT NULL,                -- classname::name
    define_name TEXT NOT NULL,
    UNIQUE(project_id, test_case, define_name)
);
CREATE INDEX IF NOT EXISTS idx_cov_define ON coverage_map(project_id, define_name);

-- 任务范围 (§2.3 task_scope_breach, opt-in)
CREATE TABLE IF NOT EXISTS task_scopes (
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER REFERENCES projects(id),
    task_id        TEXT NOT NULL,
    allowed_glob   TEXT NOT NULL,             -- JSON array
    forbidden_glob TEXT,
    severity       TEXT,                      -- strict / warn
    state          TEXT DEFAULT 'active',     -- active / completed
    created_at     TIMESTAMP
);

-- incident 两步响应状态机 (§4.3)
-- (定义在 pending_deliveries 之前，满足外键拓扑序)
CREATE TABLE IF NOT EXISTS incidents (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER REFERENCES projects(id),
    signal            TEXT NOT NULL CHECK(signal IN (
                          'define_loop', 'stuck_region', 'debug_loop',
                          'regression', 'boundary_breach', 'task_scope_breach',
                          'test_tampering', 'token_waste', 'dangling_reference',
                          'ts_toolchain_missing', 'parse_skipped')),
    target_node       TEXT,
    state             TEXT DEFAULT 'open' CHECK(state IN (
                          'open', 'notified_agent', 'resolved', 'escalated_user')),
    agent_notified_at TIMESTAMP,
    post_notify_count INTEGER DEFAULT 0,      -- 提醒后又触发次数 (对比 theta_2)
    escalated_at      TIMESTAMP,
    resolved_by       TEXT,                   -- test_green / back_in_scope / no_more_equiv / ...
    details           TEXT,                   -- JSON 字符串 (设计标 JSONB, SQLite 用 TEXT)
    created_at TIMESTAMP, updated_at TIMESTAMP
);

-- 待送达队列 (§4.4 hook 集成). channel='agent'|'user'.
-- taken_at IS NULL = 尚未取走; 取走时打时间戳 (原子更新).
CREATE TABLE IF NOT EXISTS pending_deliveries (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id),
    incident_id INTEGER REFERENCES incidents(id),
    channel     TEXT NOT NULL CHECK(channel IN ('agent', 'user')),
    kind        TEXT NOT NULL DEFAULT 'alert' CHECK(kind IN ('alert', 'suggestion')),
    message     TEXT NOT NULL,
    created_at  TIMESTAMP,
    taken_at    TIMESTAMP          -- NULL until delivered
);
CREATE INDEX IF NOT EXISTS idx_del_project ON pending_deliveries(project_id, channel, taken_at);

-- 安全网关闭记录 (§2.9). dismissed_at IS NULL = 已提示未关闭; 非 NULL = 永久关闭.
CREATE TABLE IF NOT EXISTS safety_net_dismissals (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER REFERENCES projects(id),
    net_type     TEXT NOT NULL CHECK(net_type IN ('no_tests', 'no_git')),
    triggered_at TIMESTAMP,
    dismissed_at TIMESTAMP,       -- NULL = shown but not dismissed
    UNIQUE(project_id, net_type)
);

-- 辅助功能状态 (§4.9/§4.10/§4.11). 无 SDT 对位、纯工程辅助.
-- Tracks commit-timing assist state per project.
CREATE TABLE IF NOT EXISTS assist_state (
    project_id                     INTEGER PRIMARY KEY REFERENCES projects(id),
    last_commit_seq                INTEGER DEFAULT 0,   -- seq at last user-acknowledged commit
    last_commit_suggest_defines    TEXT DEFAULT '',     -- B-mechanism: defines present at last commit suggestion
    last_run_tests_suggest_defines TEXT DEFAULT ''      -- B-mechanism: defines present at last run-tests suggestion
);

-- 悬空引用观测记录 (§2.8 dangling_reference signal — pre-incident persistence tracking)
-- Each row = one determination of caller_define observed with callee_text still unresolved.
-- Signal fires only after THETA_1_DANGLING distinct det_ids accumulate per (file,caller,callee).
CREATE TABLE IF NOT EXISTS dangling_ref_observations (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER REFERENCES projects(id),
    file_path     TEXT NOT NULL,
    caller_define TEXT NOT NULL,
    callee_text   TEXT NOT NULL,
    det_id        INTEGER NOT NULL,
    created_at    TIMESTAMP,
    UNIQUE(project_id, file_path, caller_define, callee_text, det_id)
);
CREATE INDEX IF NOT EXISTS idx_dro ON dangling_ref_observations(project_id, file_path, caller_define, callee_text);

-- 会话边界表 (变更范围记忆体 Phase 1)
-- start_seq: session open 时的 max_seq; end_seq: session close 时的 max_seq.
-- 变更范围 = (start_seq, end_seq], 含首不含尾.
-- INSERT OR IGNORE: resume 时 SessionStart 可能重触发, 幂等保护原始 start_seq.
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    project_id  INTEGER REFERENCES projects(id),
    start_seq   INTEGER NOT NULL,
    end_seq     INTEGER,
    started_at  TIMESTAMP,
    ended_at    TIMESTAMP,
    UNIQUE(project_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_pid ON sessions(project_id, session_id);

-- 崩溃路径记录 (影响锥 ∩ stack trace 精确嫌疑，§ Thm 11.10)
-- stack_fqns: JSON array of FQN strings parsed from crash/test output
-- seq: max determination seq at time of crash (links to session for recency)
-- error_signature: normalized error type|pattern, e.g. "AssertionError|to_equal"
CREATE TABLE IF NOT EXISTS crash_stacks (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id),
    seq             INTEGER,
    stack_fqns      TEXT NOT NULL,   -- JSON array
    command         TEXT,
    error_signature TEXT,            -- normalized signature (NULL if no parseable error)
    created_at      TIMESTAMP DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_crash_stacks_pid ON crash_stacks(project_id, id DESC);

-- OTel 成本样本 (telemetry cost tracking, savings report)
-- project_id 经 session_id → sessions 表桥接 (可 NULL: 未知 session 数据不丢)
-- Privacy: only numeric metrics — no conversation content, no file paths.
CREATE TABLE IF NOT EXISTS cost_samples (
    id                    INTEGER PRIMARY KEY,
    project_id            INTEGER REFERENCES projects(id),
    session_id            TEXT,
    model                 TEXT,
    cost_usd              REAL,
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    sample_at             TIMESTAMP DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cost_samples_project ON cost_samples(project_id, sample_at);
CREATE INDEX IF NOT EXISTS idx_cost_samples_session ON cost_samples(session_id);

-- git 快照表 (batch 2) — append-only: 同 (project_id, commit_hash) 可多条
-- snapshot_at_seq: 建快照时 determinations 的 max(seq) — 标记式,不复制数据
-- 查"commit X 时的图" = seq ≤ snapshot_at_seq 的最新 determinations
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER REFERENCES projects(id),
    commit_hash     TEXT NOT NULL,
    branch          TEXT,
    snapshot_at_seq INTEGER NOT NULL,
    parent_commit   TEXT,
    reason          TEXT,            -- 'commit'/'rollback'/'branch_switch'/'session_start'/'initial'
    taken_at        TIMESTAMP DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshots_proj_commit ON snapshots(project_id, commit_hash);
CREATE INDEX IF NOT EXISTS idx_snapshots_proj_taken  ON snapshots(project_id, taken_at);

CREATE TABLE IF NOT EXISTS dir_mtimes (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    dir_path    TEXT    NOT NULL,
    mtime       REAL,
    PRIMARY KEY (project_id, dir_path)
);

