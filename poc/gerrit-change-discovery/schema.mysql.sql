-- SkillHub Gerrit Change Discovery POC - MySQL tables
-- Recommended: MySQL 5.7+ / 8.0+, InnoDB, utf8mb4.
-- This script ONLY creates tables in the database configured by config.json.
-- Create the database first, for example:
--   CREATE DATABASE skillhub_security CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS skill_source (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    repository VARCHAR(255) NOT NULL,
    skill_path VARCHAR(1024) NOT NULL,
    skill_name VARCHAR(255) NOT NULL,
    source_key VARCHAR(700) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    first_seen_at DATETIME(3) NOT NULL,
    last_seen_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_skill_source_key (source_key),
    KEY idx_skill_source_repo_path (repository, skill_path(191)),
    KEY idx_skill_source_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS content_version (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    skill_digest CHAR(64) NOT NULL,
    hash_algorithm VARCHAR(32) NOT NULL DEFAULT 'SHA-256',
    file_count INT UNSIGNED NULL,
    package_size BIGINT UNSIGNED NULL,
    manifest_json LONGTEXT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_content_version_digest (skill_digest)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS gerrit_patchset (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    repository VARCHAR(255) NOT NULL,
    change_number BIGINT UNSIGNED NOT NULL,
    patchset INT UNSIGNED NOT NULL,
    revision_sha VARCHAR(64) NOT NULL,
    branch VARCHAR(255) NULL,
    subject VARCHAR(1024) NULL,
    status VARCHAR(32) NULL,
    changed_files_json LONGTEXT NULL,
    raw_result_json LONGTEXT NULL,
    observed_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_gerrit_patchset (repository, change_number, patchset),
    KEY idx_gerrit_patchset_revision (revision_sha),
    KEY idx_gerrit_patchset_observed (observed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS source_revision (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_id BIGINT UNSIGNED NOT NULL,
    revision_sha VARCHAR(64) NOT NULL,
    change_number BIGINT UNSIGNED NULL,
    patchset INT UNSIGNED NULL,
    branch VARCHAR(255) NULL,
    content_version_id BIGINT UNSIGNED NULL,
    observed_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_source_revision (source_id, revision_sha),
    KEY idx_source_revision_content (content_version_id),
    KEY idx_source_revision_change (change_number, patchset),
    CONSTRAINT fk_source_revision_source
      FOREIGN KEY (source_id) REFERENCES skill_source(id),
    CONSTRAINT fk_source_revision_content
      FOREIGN KEY (content_version_id) REFERENCES content_version(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS change_skill_event (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    patchset_id BIGINT UNSIGNED NOT NULL,
    source_id BIGINT UNSIGNED NULL,
    source_key VARCHAR(700) NULL,
    action VARCHAR(32) NOT NULL,
    trigger_file VARCHAR(1024) NULL,
    reason TEXT NULL,
    event_json LONGTEXT NULL,
    created_at DATETIME(3) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_change_skill_event_patchset (patchset_id),
    KEY idx_change_skill_event_source (source_id),
    KEY idx_change_skill_event_action (action),
    CONSTRAINT fk_change_skill_event_patchset
      FOREIGN KEY (patchset_id) REFERENCES gerrit_patchset(id),
    CONSTRAINT fk_change_skill_event_source
      FOREIGN KEY (source_id) REFERENCES skill_source(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
