#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now_db():
    # Store UTC as a timezone-naive DATETIME for portability across SQLite/MySQL.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Database:
    def __init__(self, backend="sqlite", db_path=None, mysql_config=None, schema_path=None, logger=None):
        self.backend = (backend or "sqlite").lower()
        self.db_path = Path(db_path) if db_path else None
        self.mysql_config = dict(mysql_config or {})
        self.logger = logger

        if schema_path:
            self.schema_path = Path(schema_path)
        elif self.backend == "mysql":
            self.schema_path = Path(__file__).with_name("schema.mysql.sql")
        else:
            self.schema_path = Path(__file__).with_name("schema.sql")

        if self.backend == "sqlite":
            if self.db_path is None:
                raise ValueError("SQLite database_path 未配置")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        elif self.backend != "mysql":
            raise ValueError("不支持的 database.type: {}".format(self.backend))

    @classmethod
    def from_config(cls, config, logger=None):
        db_cfg = dict(config.get("database") or {})
        backend = (db_cfg.get("type") or "sqlite").lower()
        if backend == "mysql":
            return cls(backend="mysql", mysql_config=db_cfg, logger=logger)
        return cls(backend="sqlite", db_path=config.get("database_path", "./data/skillhub-poc.db"), logger=logger)

    def description(self):
        if self.backend == "mysql":
            cfg = self.mysql_config
            return "mysql://{}@{}:{}/{}".format(
                cfg.get("username") or "",
                cfg.get("host") or "127.0.0.1",
                int(cfg.get("port") or 3306),
                cfg.get("database") or "",
            )
        return "sqlite:///{}".format(self.db_path)

    def _mysql_password(self):
        cfg = self.mysql_config
        env_name = cfg.get("password_env", "SKILLHUB_DB_PASSWORD")
        return cfg.get("password") or os.environ.get(env_name, "")

    def connect(self):
        if self.backend == "sqlite":
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:
            raise RuntimeError(
                "MySQL 模式需要 PyMySQL。请执行: python -m pip install -r requirements.txt"
            ) from exc

        cfg = self.mysql_config
        kwargs = {
            "host": cfg.get("host", "127.0.0.1"),
            "port": int(cfg.get("port", 3306)),
            "user": cfg.get("username") or "",
            "password": self._mysql_password(),
            "database": cfg.get("database") or "",
            "charset": cfg.get("charset", "utf8mb4"),
            "connect_timeout": int(cfg.get("connect_timeout_seconds", 10)),
            "cursorclass": DictCursor,
            "autocommit": False,
        }
        if bool(cfg.get("ssl", False)):
            ssl_options = {}
            if cfg.get("ssl_ca"):
                ssl_options["ca"] = cfg.get("ssl_ca")
            if cfg.get("ssl_cert"):
                ssl_options["cert"] = cfg.get("ssl_cert")
            if cfg.get("ssl_key"):
                ssl_options["key"] = cfg.get("ssl_key")
            kwargs["ssl"] = ssl_options or {}
        return pymysql.connect(**kwargs)

    def _adapt_sql(self, sql):
        return sql.replace("?", "%s") if self.backend == "mysql" else sql

    def _execute(self, conn, sql, params=()):
        sql = self._adapt_sql(sql)
        if self.backend == "sqlite":
            return conn.execute(sql, params)
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur

    def _fetchone(self, conn, sql, params=()):
        cur = self._execute(conn, sql, params)
        try:
            return cur.fetchone()
        finally:
            if self.backend == "mysql":
                cur.close()

    def _fetchall(self, conn, sql, params=()):
        cur = self._execute(conn, sql, params)
        try:
            return cur.fetchall()
        finally:
            if self.backend == "mysql":
                cur.close()

    def query_all(self, sql, params=()):
        conn = self.connect()
        try:
            rows = self._fetchall(conn, sql, params)
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def query_one(self, sql, params=()):
        conn = self.connect()
        try:
            row = self._fetchone(conn, sql, params)
            return dict(row) if row else None
        finally:
            conn.close()

    @staticmethod
    def _split_mysql_statements(sql):
        cleaned = []
        for line in sql.splitlines():
            if line.lstrip().startswith("--"):
                continue
            cleaned.append(line)
        return [stmt.strip() for stmt in "\n".join(cleaned).split(";") if stmt.strip()]

    def init_schema(self):
        sql = self.schema_path.read_text(encoding="utf-8")
        conn = self.connect()
        try:
            if self.backend == "sqlite":
                conn.executescript(sql)
            else:
                for statement in self._split_mysql_statements(sql):
                    cur = conn.cursor()
                    try:
                        cur.execute(statement)
                    finally:
                        cur.close()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if self.logger:
            self.logger.info("数据库 Schema 已初始化: %s", self.description())

    def inventory_rows(self):
        self.init_schema()
        return self.query_all(
            "SELECT repository, skill_path, skill_name, source_key "
            "FROM skill_source WHERE status = 'ACTIVE' ORDER BY repository, skill_path"
        )

    def _upsert_source(self, conn, repository, skill_path, skill_name, source_key, status="ACTIVE"):
        ts = now_db()
        row = self._fetchone(conn, "SELECT id FROM skill_source WHERE source_key = ?", (source_key,))
        if row:
            cur = self._execute(
                conn,
                "UPDATE skill_source SET repository=?, skill_path=?, skill_name=?, status=?, last_seen_at=? WHERE id=?",
                (repository, skill_path, skill_name, status, ts, row["id"]),
            )
            if self.backend == "mysql":
                cur.close()
            return row["id"]
        cur = self._execute(
            conn,
            "INSERT INTO skill_source(repository, skill_path, skill_name, source_key, status, first_seen_at, last_seen_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (repository, skill_path, skill_name, source_key, status, ts, ts),
        )
        source_id = cur.lastrowid
        if self.backend == "mysql":
            cur.close()
        return source_id

    def _set_source_status(self, conn, source_key, status):
        if not source_key:
            return
        cur = self._execute(
            conn,
            "UPDATE skill_source SET status=?, last_seen_at=? WHERE source_key=?",
            (status, now_db(), source_key),
        )
        if self.backend == "mysql":
            cur.close()

    def _upsert_content_version(self, conn, item):
        digest = item.get("skill_digest")
        if not digest:
            return None
        row = self._fetchone(conn, "SELECT id FROM content_version WHERE skill_digest=?", (digest,))
        if row:
            return row["id"]
        cur = self._execute(
            conn,
            "INSERT INTO content_version(skill_digest, hash_algorithm, file_count, package_size, manifest_json, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                digest,
                item.get("digest_algorithm") or "SHA-256",
                item.get("file_count"),
                item.get("package_size"),
                json.dumps(item.get("manifest", []), ensure_ascii=False),
                now_db(),
            ),
        )
        content_id = cur.lastrowid
        if self.backend == "mysql":
            cur.close()
        return content_id

    def persist_analysis(self, payload):
        self.init_schema()
        change = payload.get("change", {})
        repository = change.get("project")
        change_number = change.get("number")
        patchset = change.get("patchset")
        revision = change.get("revision")
        if repository is None or change_number is None or patchset is None or not revision:
            raise ValueError("分析结果缺少 project/change_number/patchset/revision，无法入库")

        ts = now_db()
        conn = self.connect()
        try:
            row = self._fetchone(
                conn,
                "SELECT id FROM gerrit_patchset WHERE repository=? AND change_number=? AND patchset=?",
                (repository, change_number, patchset),
            )
            changed_files_json = json.dumps(payload.get("changed_files", []), ensure_ascii=False)
            raw_result_json = json.dumps(payload, ensure_ascii=False)
            if row:
                patchset_id = row["id"]
                cur = self._execute(
                    conn,
                    "UPDATE gerrit_patchset SET revision_sha=?, branch=?, subject=?, status=?, "
                    "changed_files_json=?, raw_result_json=?, observed_at=? WHERE id=?",
                    (
                        revision,
                        change.get("branch"),
                        change.get("subject"),
                        change.get("status"),
                        changed_files_json,
                        raw_result_json,
                        ts,
                        patchset_id,
                    ),
                )
                if self.backend == "mysql":
                    cur.close()
                cur = self._execute(conn, "DELETE FROM change_skill_event WHERE patchset_id=?", (patchset_id,))
                if self.backend == "mysql":
                    cur.close()
            else:
                cur = self._execute(
                    conn,
                    "INSERT INTO gerrit_patchset(repository, change_number, patchset, revision_sha, branch, subject, status, "
                    "changed_files_json, raw_result_json, observed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        repository,
                        change_number,
                        patchset,
                        revision,
                        change.get("branch"),
                        change.get("subject"),
                        change.get("status"),
                        changed_files_json,
                        raw_result_json,
                        ts,
                    ),
                )
                patchset_id = cur.lastrowid
                if self.backend == "mysql":
                    cur.close()

            for item in payload.get("affected_skills", []):
                action = item.get("action") or "UNKNOWN"
                source_key = item.get("source_key")
                source_id = None
                if action == "DELETED_SKILL":
                    self._set_source_status(conn, source_key, "DELETED")
                    if source_key:
                        source_row = self._fetchone(conn, "SELECT id FROM skill_source WHERE source_key=?", (source_key,))
                        source_id = source_row["id"] if source_row else None
                elif source_key and item.get("skill_name") is not None:
                    source_id = self._upsert_source(
                        conn,
                        repository,
                        item.get("skill_path") or "",
                        item.get("skill_name"),
                        source_key,
                        "ACTIVE",
                    )
                    previous = item.get("previous_sources") or []
                    if action == "RENAMED_SKILL":
                        for old_key in previous:
                            self._set_source_status(conn, old_key, "MOVED")
                    elif action == "NEW_SKILL" and previous:
                        for old_key in previous:
                            self._set_source_status(conn, old_key, "INACTIVE")

                content_version_id = None
                if source_id and item.get("digest_status") == "SUCCESS":
                    content_version_id = self._upsert_content_version(conn, item)

                if source_id and action != "DELETED_SKILL":
                    rev_row = self._fetchone(
                        conn,
                        "SELECT id FROM source_revision WHERE source_id=? AND revision_sha=?",
                        (source_id, revision),
                    )
                    if rev_row:
                        cur = self._execute(
                            conn,
                            "UPDATE source_revision SET change_number=?, patchset=?, branch=?, content_version_id=?, observed_at=? WHERE id=?",
                            (change_number, patchset, change.get("branch"), content_version_id, ts, rev_row["id"]),
                        )
                    else:
                        cur = self._execute(
                            conn,
                            "INSERT INTO source_revision(source_id, revision_sha, change_number, patchset, branch, content_version_id, observed_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (source_id, revision, change_number, patchset, change.get("branch"), content_version_id, ts),
                        )
                    if self.backend == "mysql":
                        cur.close()

                cur = self._execute(
                    conn,
                    "INSERT INTO change_skill_event(patchset_id, source_id, source_key, action, trigger_file, reason, event_json, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        patchset_id,
                        source_id,
                        source_key,
                        action,
                        item.get("trigger_file"),
                        item.get("reason"),
                        json.dumps(item, ensure_ascii=False),
                        ts,
                    ),
                )
                if self.backend == "mysql":
                    cur.close()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        if self.logger:
            self.logger.info(
                "数据库存档完成: backend=%s change=%s patchset=%s affected_skills=%s",
                self.backend,
                change_number,
                patchset,
                len(payload.get("affected_skills", [])),
            )

    def summary(self):
        self.init_schema()
        row = self.query_one(
            "SELECT "
            "(SELECT COUNT(*) FROM skill_source) AS skill_sources, "
            "(SELECT COUNT(*) FROM skill_source WHERE status='ACTIVE') AS active_sources, "
            "(SELECT COUNT(*) FROM content_version) AS content_versions, "
            "(SELECT COUNT(*) FROM source_revision) AS source_revisions, "
            "(SELECT COUNT(*) FROM gerrit_patchset) AS patchsets, "
            "(SELECT COUNT(*) FROM change_skill_event) AS skill_events"
        )
        return row or {
            "skill_sources": 0,
            "active_sources": 0,
            "content_versions": 0,
            "source_revisions": 0,
            "patchsets": 0,
            "skill_events": 0,
        }

    def dashboard_sources(self):
        return self.query_all(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM source_revision r WHERE r.source_id=s.id) AS revision_count, "
            "(SELECT r.revision_sha FROM source_revision r WHERE r.source_id=s.id ORDER BY r.id DESC LIMIT 1) AS latest_revision, "
            "(SELECT cv.skill_digest FROM source_revision r LEFT JOIN content_version cv ON cv.id=r.content_version_id "
            " WHERE r.source_id=s.id ORDER BY r.id DESC LIMIT 1) AS latest_digest "
            "FROM skill_source s ORDER BY s.last_seen_at DESC"
        )

    def dashboard_changes(self):
        return self.query_all(
            "SELECT p.*, COUNT(e.id) AS affected_count "
            "FROM gerrit_patchset p LEFT JOIN change_skill_event e ON e.patchset_id=p.id "
            "GROUP BY p.id ORDER BY p.id DESC LIMIT 100"
        )

    def dashboard_events(self):
        return self.query_all(
            "SELECT e.*, p.repository, p.change_number, p.patchset "
            "FROM change_skill_event e JOIN gerrit_patchset p ON p.id=e.patchset_id "
            "ORDER BY e.id DESC LIMIT 200"
        )


def load_config(path):
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8-sig") as fh:
        config = json.load(fh)
    db_path = config.get("database_path", "./data/skillhub-poc.db")
    if not Path(db_path).is_absolute():
        config["database_path"] = str((config_path.parent / db_path).resolve())
    return config


def main():
    parser = argparse.ArgumentParser(description="Initialize or inspect SkillHub POC database")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    db = Database.from_config(config)
    if args.check:
        conn = db.connect()
        conn.close()
        print("Database connection OK: {}".format(db.description()))
    if args.init:
        db.init_schema()
        print("Database schema initialized: {}".format(db.description()))
    if args.summary:
        print(json.dumps(db.summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
