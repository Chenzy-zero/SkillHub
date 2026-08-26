PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS skill_source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    skill_path TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skill_source_repo_path
ON skill_source(repository, skill_path);

CREATE TABLE IF NOT EXISTS content_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_digest TEXT NOT NULL UNIQUE,
    hash_algorithm TEXT NOT NULL DEFAULT 'SHA-256',
    file_count INTEGER,
    package_size INTEGER,
    manifest_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gerrit_patchset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    change_number INTEGER NOT NULL,
    patchset INTEGER NOT NULL,
    revision_sha TEXT NOT NULL,
    branch TEXT,
    subject TEXT,
    status TEXT,
    changed_files_json TEXT,
    raw_result_json TEXT,
    observed_at TEXT NOT NULL,
    UNIQUE(repository, change_number, patchset)
);

CREATE INDEX IF NOT EXISTS idx_gerrit_patchset_revision
ON gerrit_patchset(revision_sha);

CREATE TABLE IF NOT EXISTS source_revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    revision_sha TEXT NOT NULL,
    change_number INTEGER,
    patchset INTEGER,
    branch TEXT,
    content_version_id INTEGER,
    observed_at TEXT NOT NULL,
    UNIQUE(source_id, revision_sha),
    FOREIGN KEY(source_id) REFERENCES skill_source(id),
    FOREIGN KEY(content_version_id) REFERENCES content_version(id)
);

CREATE INDEX IF NOT EXISTS idx_source_revision_content
ON source_revision(content_version_id);

CREATE TABLE IF NOT EXISTS change_skill_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patchset_id INTEGER NOT NULL,
    source_id INTEGER,
    source_key TEXT,
    action TEXT NOT NULL,
    trigger_file TEXT,
    reason TEXT,
    event_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(patchset_id) REFERENCES gerrit_patchset(id),
    FOREIGN KEY(source_id) REFERENCES skill_source(id)
);

CREATE INDEX IF NOT EXISTS idx_change_skill_event_patchset
ON change_skill_event(patchset_id);

CREATE INDEX IF NOT EXISTS idx_change_skill_event_source
ON change_skill_event(source_id);
