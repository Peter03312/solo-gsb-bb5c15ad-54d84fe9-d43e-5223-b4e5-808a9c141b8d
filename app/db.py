"""PostgreSQL 连接与模式初始化。"""
import os
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.pool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://strat:strat@localhost:5432/stratigraphy",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS strata (
    stratum_id  TEXT PRIMARY KEY,
    site        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 断言只增不删：撤回仅置状态，保证历史版本可回溯、证据编号永久唯一
CREATE TABLE IF NOT EXISTS assertions (
    evidence_id        TEXT PRIMARY KEY,
    stratum_a          TEXT NOT NULL REFERENCES strata(stratum_id),
    stratum_b          TEXT NOT NULL REFERENCES strata(stratum_id),
    relation           TEXT NOT NULL CHECK (relation IN ('same_period', 'earlier_than')),
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
    added_revision     INTEGER NOT NULL,
    retracted_revision INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS revision_counter (
    id       BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    revision INTEGER NOT NULL DEFAULT 0
);
INSERT INTO revision_counter (id, revision) VALUES (TRUE, 0) ON CONFLICT DO NOTHING;

-- 每次成功批次的审计记录
CREATE TABLE IF NOT EXISTS revisions (
    revision   INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    batch      JSONB NOT NULL
);

-- 定版：不可变关系快照。结论行一旦写入永不修改、永不删除
CREATE TABLE IF NOT EXISTS snapshots (
    version    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision   INTEGER NOT NULL,
    label      TEXT,
    assertions JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS snapshot_conclusions (
    version   INTEGER NOT NULL REFERENCES snapshots(version),
    stratum_a TEXT NOT NULL,
    stratum_b TEXT NOT NULL,
    relation  TEXT NOT NULL CHECK (relation IN ('same_period', 'earlier_than', 'later_than')),
    path      JSONB NOT NULL,
    PRIMARY KEY (version, stratum_a, stratum_b)
);

-- 推演（证据修订沙盘）：创建时固定正式修订号、已登记地层与生效断言快照，
-- 不触碰正式状态；并入（merge）时才把相对基线的净变更原子应用到最新正式状态。
CREATE TABLE IF NOT EXISTS projections (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label                  TEXT,
    base_revision          INTEGER NOT NULL,
    base_strata            JSONB NOT NULL,
    base_assertions        JSONB NOT NULL,
    projection_revision    INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL DEFAULT 'open'
                               CHECK (status IN ('open', 'merged', 'discarded')),
    merged_into_revision   INTEGER,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at              TIMESTAMPTZ
);

-- 推演内新增的断言（编号在该推演内唯一；与正式断言表无外键关系，
-- 因为这些编号尚未进入正式历史，并入核验通过后才写入正式 assertions）。
CREATE TABLE IF NOT EXISTS projection_assertions (
    projection_id      BIGINT NOT NULL REFERENCES projections(id),
    evidence_id        TEXT NOT NULL,
    stratum_a          TEXT NOT NULL,
    stratum_b          TEXT NOT NULL,
    relation           TEXT NOT NULL CHECK (relation IN ('same_period', 'earlier_than')),
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retracted')),
    added_revision     INTEGER NOT NULL,
    retracted_revision INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted_at       TIMESTAMPTZ,
    PRIMARY KEY (projection_id, evidence_id)
);

-- 推演内对基线断言的撤回（撤回只增不删，同一基线证据在同一推演内仅可撤回一次）。
CREATE TABLE IF NOT EXISTS projection_retractions (
    projection_id      BIGINT NOT NULL REFERENCES projections(id),
    evidence_id        TEXT NOT NULL,
    retracted_revision INTEGER NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (projection_id, evidence_id)
);

-- 推演内每次成功批次的审计记录（修订号为推演自身修订号）。
CREATE TABLE IF NOT EXISTS projection_revisions (
    projection_id BIGINT NOT NULL REFERENCES projections(id),
    revision      INTEGER NOT NULL,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    batch         JSONB NOT NULL,
    PRIMARY KEY (projection_id, revision)
);

-- 正式修订关系变化追踪：仅成功的正式批次与产生修订号的推演并入在此落记录，
-- 与断言落库、修订号递增同事务原子写入（失败批次 / 推演内编辑 / 无净变更并入
-- 不产生记录）。同一地层对按编号固定顺序（stratum_a < stratum_b）只列一次。
CREATE TABLE IF NOT EXISTS revision_changes (
    revision     INTEGER NOT NULL,
    seq          INTEGER NOT NULL,                       -- 页内稳定次序：地层对固定顺序
    stratum_a    TEXT NOT NULL CHECK (stratum_a < stratum_b),
    stratum_b    TEXT NOT NULL,
    old_relation TEXT NOT NULL CHECK (old_relation IN ('same_period', 'earlier_than', 'later_than', 'undetermined')),
    new_relation TEXT NOT NULL CHECK (new_relation IN ('same_period', 'earlier_than', 'later_than', 'undetermined')),
    -- 当时的最优支撑证据路径；关系未定（含变为未定）时为空数组 []
    old_path     JSONB NOT NULL,
    new_path     JSONB NOT NULL,
    -- formal_batch=正式断言批次；projection_merge=推演并入；field_ingest=野外终端整包汇入
    source       TEXT NOT NULL CHECK (source IN ('formal_batch', 'projection_merge', 'field_ingest')),
    PRIMARY KEY (revision, seq),
    UNIQUE (revision, stratum_a, stratum_b)
);
CREATE INDEX IF NOT EXISTS idx_revision_changes_pair
    ON revision_changes (stratum_a, stratum_b, revision);

-- 修订封口标记：在线提交与启动回填都对已处理修订封口（含零变化修订），
-- 保证回填幂等、不重复。
CREATE TABLE IF NOT EXISTS revision_changes_sealed (
    revision   INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,
    sealed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 专题成果包：把某一不可变定版中的一组已登记地层封存为可转交复核的成果包。
-- 内容（pairs/evidence/transit_strata/content_hash）在封存时刻一次性写入，
-- 之后永不修改、永不删除：正式断言撤回、后续修订、新定版均不影响包内容。
-- 同一定版与同一地层集合（排序去重后取 strata_key）无论输入顺序只生成一份，
-- 由 (version, strata_key) 唯一约束保证，重复请求幂等返回已封存包。
CREATE TABLE IF NOT EXISTS thematic_packages (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version        INTEGER NOT NULL REFERENCES snapshots(version),
    revision       INTEGER NOT NULL,                       -- 定版对应的正式修订号（冗余留痕）
    label          TEXT,
    strata         JSONB NOT NULL,                         -- 排序去重后的入选地层编号
    strata_key     TEXT NOT NULL,                          -- 入选集合的稳定指纹（集合身份，与顺序无关）
    pairs          JSONB NOT NULL,                         -- 入选地层按编号排序的无序两两关系（冻结）
    evidence       JSONB NOT NULL,                         -- 路径上按序去重的原始断言（定版冻结副本）
    transit_strata JSONB NOT NULL,                         -- 路径途经但未入选的地层（仅随证据保留）
    content_hash   TEXT NOT NULL,                          -- 包内容的 sha256 指纹，供转交复核校验
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (version, strata_key)
);

-- 野外终端短号绑定：同一终端的同一短号跨包稳定绑定同一全库地层，不可改绑。
-- 绑定随首个成功汇入的包建立；任一汇入失败整体回滚，不留半绑定。
CREATE TABLE IF NOT EXISTS terminal_short_codes (
    terminal_id  TEXT NOT NULL,
    short_code  TEXT NOT NULL,
    stratum_id  TEXT NOT NULL REFERENCES strata(stratum_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (terminal_id, short_code)
);

-- 野外终端整包汇入登记：同一 (终端, 包号) 只成功落库一次。
-- content_hash 为规范化请求内容指纹：同内容重试幂等返回原结果（即使正式修订
-- 已前进），异内容复用拒绝。只有成功汇入才登记，失败请求不占用包号。
CREATE TABLE IF NOT EXISTS field_ingest_packages (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    terminal_id  TEXT NOT NULL,
    package_no   TEXT NOT NULL,
    site         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    request      JSONB NOT NULL,
    revision     INTEGER NOT NULL,                       -- 本包落库的正式修订号
    result       JSONB NOT NULL,                         -- 首次汇入的响应内容（重试原样返回）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (terminal_id, package_no)
);
"""

# 既有部署的幂等迁移：revision_changes.source 检查约束扩展 field_ingest 来源。
# CREATE TABLE IF NOT EXISTS 不会更新已存在表的约束，故在此显式替换（新库重放无害）。
MIGRATIONS = """
ALTER TABLE revision_changes DROP CONSTRAINT IF EXISTS revision_changes_source_check;
ALTER TABLE revision_changes ADD CONSTRAINT revision_changes_source_check
    CHECK (source IN ('formal_batch', 'projection_merge', 'field_ingest'));
"""

_pool: "psycopg2.pool.ThreadedConnectionPool | None" = None


def init_pool(retries: int = 30, delay: float = 1.0) -> None:
    """启动时初始化连接池并建表（幂等），等待数据库就绪。"""
    global _pool
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
            conn = pool.getconn()
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(SCHEMA)
                        cur.execute(MIGRATIONS)
                        # 启动幂等回填部署前历史修订的变化记录（“已有历史修订也应可追溯”）
                        from .changes import backfill_revision_changes

                        backfill_revision_changes(cur)
            finally:
                pool.putconn(conn)
            _pool = pool
            return
        except Exception as exc:  # noqa: BLE001 - 启动重试阶段需兜底
            last_err = exc
            time.sleep(delay)
    raise RuntimeError(f"无法连接数据库（{DATABASE_URL}）: {last_err}")


@contextmanager
def db_conn():
    """事务上下文：正常退出提交，异常回滚，连接归还连接池。"""
    assert _pool is not None, "连接池未初始化"
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
