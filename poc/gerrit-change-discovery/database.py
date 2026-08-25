#!/usr/bin/env python3
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, db_path, schema_path=None, logger=None):
        self.db_path = Path(db_path)
        self.schema_path = Path(schema_path) if schema_path else Path(__file__).with_name("schema.sql")
        self.logger = logger
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self):
        sql = self.schema_path.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(sql)
        if self.logger:
            self.logger.info("SQLite 已初始化: %s", self.db_path)

    def inventory_rows(self):
        self.init_schema()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT repository, skill_path, skill_name, source_key "
                "FROM skill_source WHERE status = 'ACTIVE' ORDER BY repository, skill_path"
            ).fetchall()
        return [dict(row) for row in rows]

    def _upsert_source(self, conn, repository, skill_path, skill_name, source_key, status="ACTIVE"):
        ts = now_iso()
        row = conn.execute("SELECT id FROM skill_source WHERE source_key = ?", (source_key,)).fetchone()
        if row:
            conn.execute(
                "UPDATE skill_source SET repository=?, skill_path=?, skill_name=?, status=?, last_seen_at=? WHERE id=?",
                (repository, skill_path, skill_name, status, ts, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO skill_source(repository, skill_path, skill_name, source_key, status, first_seen_at, last_seen_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (repository, skill_path, skill_name, source_key, status, ts, ts),
        )
        return cur.lastrowid

    def _set_source_status(self, conn, source_key, status):
        if not source_key:
            return
        conn.execute(
            "UPDATE skill_source SET status=?, last_seen_at=? WHERE source_key=?",
            (status, now_iso(), source_key),
        )

    def _upsert_content_version(self, conn, item):
        digest = item.get("skill_digest")
        if not digest:
            return None
        row = conn.execute("SELECT id FROM content_version WHERE skill_digest=?", (digest,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO content_version(skill_digest, hash_algorithm, file_count, package_size, manifest_json, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                digest,
                item.get("digest_algorithm") or "SHA-256",
                item.get("file_count"),
                item.get("package_size"),
                json.dumps(item.get("manifest", []), ensure_ascii=False),
                now_iso(),
            ),
        )
        return cur.lastrowid

    def persist_analysis(self, payload):
        self.init_schema()
        change = payload.get("change", {})
        repository = change.get("project")
        change_number = change.get("number")
        patchset = change.get("patchset")
        revision = change.get("revision")
        if repository is None or change_number is None or patchset is None or not revision:
            raise ValueError("分析结果缺少 project/change_number/patchset/revision，无法入库")

        ts = now_iso()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM gerrit_patchset WHERE repository=? AND change_number=? AND patchset=?",
                (repository, change_number, patchset),
            ).fetchone()
            changed_files_json = json.dumps(payload.get("changed_files", []), ensure_ascii=False)
            raw_result_json = json.dumps(payload, ensure_ascii=False)
            if row:
                patchset_id = row["id"]
                conn.execute(
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
                conn.execute("DELETE FROM change_skill_event WHERE patchset_id=?", (patchset_id,))
            else:
                cur = conn.execute(
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

            for item in payload.get("affected_skills", []):
                action = item.get("action") or "UNKNOWN"
                source_key = item.get("source_key")
                source_id = None

                if action == "DELETED_SKILL":
                    self._set_source_status(conn, source_key, "DELETED")
                    if source_key:
                        source_row = conn.execute("SELECT id FROM skill_source WHERE source_key=?", (source_key,)).fetchone()
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
                    rev_row = conn.execute(
                        "SELECT id FROM source_revision WHERE source_id=? AND revision_sha=?",
                        (source_id, revision),
                    ).fetchone()
                    if rev_row:
                        conn.execute(
                            "UPDATE source_revision SET change_number=?, patchset=?, branch=?, content_version_id=?, observed_at=? WHERE id=?",
                            (change_number, patchset, change.get("branch"), content_version_id, ts, rev_row["id"]),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO source_revision(source_id, revision_sha, change_number, patchset, branch, content_version_id, observed_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (source_id, revision, change_number, patchset, change.get("branch"), content_version_id, ts),
                        )

                conn.execute(
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

        if self.logger:
            self.logger.info(
                "数据库存档完成: change=%s patchset=%s affected_skills=%s",
                change_number,
                patchset,
                len(payload.get("affected_skills", [])),
            )

    def summary(self):
        self.init_schema()
        with self.connect() as conn:
            return {
                "skill_sources": conn.execute("SELECT COUNT(*) FROM skill_source").fetchone()[0],
                "active_sources": conn.execute("SELECT COUNT(*) FROM skill_source WHERE status='ACTIVE'").fetchone()[0],
                "content_versions": conn.execute("SELECT COUNT(*) FROM content_version").fetchone()[0],
                "source_revisions": conn.execute("SELECT COUNT(*) FROM source_revision").fetchone()[0],
                "patchsets": conn.execute("SELECT COUNT(*) FROM gerrit_patchset").fetchone()[0],
                "skill_events": conn.execute("SELECT COUNT(*) FROM change_skill_event").fetchone()[0],
            }


def load_config(path):
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8-sig") as fh:
        config = json.load(fh)
    db_path = config.get("database_path", "./data/skillhub-poc.db")
    if not Path(db_path).is_absolute():
        db_path = str((config_path.parent / db_path).resolve())
    return config, db_path


def main():
    parser = argparse.ArgumentParser(description="Initialize or inspect SkillHub POC SQLite database")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    _, db_path = load_config(args.config)
    db = Database(db_path)
    if args.init or not Path(db_path).exists():
        db.init_schema()
        print("SQLite initialized: {}".format(db_path))
    if args.summary:
        print(json.dumps(db.summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
