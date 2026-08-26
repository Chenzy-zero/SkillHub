#!/usr/bin/env python3
import argparse
import html
import json
from pathlib import Path
from string import Template

from database import Database


def esc(value):
    return html.escape("" if value is None else str(value))


def short(value, length=14):
    value = "" if value is None else str(value)
    return value if len(value) <= length else value[:length] + "..."


def generate_report(db_path, report_dir, logger=None):
    db = Database(db_path, logger=logger)
    db.init_schema()
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    with db.connect() as conn:
        summary = db.summary()
        sources = conn.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM source_revision r WHERE r.source_id=s.id) AS revision_count, "
            "(SELECT r.revision_sha FROM source_revision r WHERE r.source_id=s.id ORDER BY r.id DESC LIMIT 1) AS latest_revision, "
            "(SELECT cv.skill_digest FROM source_revision r LEFT JOIN content_version cv ON cv.id=r.content_version_id "
            " WHERE r.source_id=s.id ORDER BY r.id DESC LIMIT 1) AS latest_digest "
            "FROM skill_source s ORDER BY s.last_seen_at DESC"
        ).fetchall()
        changes = conn.execute(
            "SELECT p.*, COUNT(e.id) AS affected_count "
            "FROM gerrit_patchset p LEFT JOIN change_skill_event e ON e.patchset_id=p.id "
            "GROUP BY p.id ORDER BY p.id DESC LIMIT 100"
        ).fetchall()
        events = conn.execute(
            "SELECT e.*, p.repository, p.change_number, p.patchset "
            "FROM change_skill_event e JOIN gerrit_patchset p ON p.id=e.patchset_id "
            "ORDER BY e.id DESC LIMIT 200"
        ).fetchall()

    cards = [
        ("Skill Source", summary["skill_sources"]),
        ("Active Source", summary["active_sources"]),
        ("Content Version", summary["content_versions"]),
        ("Source Revision", summary["source_revisions"]),
        ("Gerrit Patchset", summary["patchsets"]),
        ("Skill Event", summary["skill_events"]),
    ]

    card_html = "".join(
        '<div class="card"><div class="num">{}</div><div class="label">{}</div></div>'.format(v, esc(k))
        for k, v in cards
    )

    source_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}</td><td><code>{}</code></td><td><code>{}</code></td></tr>".format(
            esc(row["skill_name"]),
            esc(row["repository"]),
            esc(row["skill_path"]),
            esc(row["status"]),
            row["revision_count"],
            esc(short(row["latest_revision"])),
            esc(short(row["latest_digest"])),
        )
        for row in sources
    ) or '<tr><td colspan="7">暂无数据</td></tr>'

    change_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            esc(row["change_number"]),
            esc(row["patchset"]),
            esc(row["repository"]),
            esc(row["branch"]),
            esc(row["subject"]),
            row["affected_count"],
            esc(row["observed_at"]),
        )
        for row in changes
    ) or '<tr><td colspan="7">暂无数据</td></tr>'

    event_rows = "".join(
        "<tr><td>{}/{}</td><td>{}</td><td>{}</td><td><code>{}</code></td><td>{}</td></tr>".format(
            esc(row["change_number"]),
            esc(row["patchset"]),
            esc(row["repository"]),
            esc(row["action"]),
            esc(row["source_key"] or ""),
            esc(row["trigger_file"] or ""),
        )
        for row in events
    ) or '<tr><td colspan="5">暂无数据</td></tr>'

    # Use string.Template instead of str.format so normal CSS braces are not
    # interpreted as Python formatting placeholders (e.g. {font-family:...}).
    page_template = Template("""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SkillHub Gerrit Discovery Dashboard</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f5f7fa;color:#1f2937}.wrap{max-width:1500px;margin:auto;padding:24px}
h1{margin:0 0 6px}.sub{color:#6b7280;margin-bottom:22px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:24px}
.card{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:18px;box-shadow:0 1px 2px rgba(0,0,0,.04)}.num{font-size:30px;font-weight:700}.label{color:#6b7280;margin-top:4px}
section{background:white;border:1px solid #e5e7eb;border-radius:12px;margin:16px 0;padding:18px;overflow:auto}h2{margin:0 0 14px;font-size:18px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:10px;border-bottom:1px solid #eee;white-space:nowrap}th{background:#fafafa;position:sticky;top:0}code{font-family:Consolas,monospace;font-size:12px}
.badge{display:inline-block;padding:2px 7px;border-radius:10px;background:#eef2ff}.foot{color:#9ca3af;font-size:12px;margin-top:18px}
</style>
</head><body><div class="wrap">
<h1>SkillHub Gerrit Discovery Dashboard</h1>
<div class="sub">SQLite 为事实存档；JSON 为原始证据；本页由数据库自动生成。</div>
<div class="cards">$cards</div>
<section><h2>Skill Sources</h2><table><thead><tr><th>Skill</th><th>Repository</th><th>Path</th><th>Status</th><th>Revisions</th><th>Latest Revision</th><th>Latest Digest</th></tr></thead><tbody>$sources</tbody></table></section>
<section><h2>Recent Gerrit Patchsets</h2><table><thead><tr><th>Change</th><th>PS</th><th>Repository</th><th>Branch</th><th>Subject</th><th>Affected Skills</th><th>Observed</th></tr></thead><tbody>$changes</tbody></table></section>
<section><h2>Recent Skill Events</h2><table><thead><tr><th>Change/PS</th><th>Repository</th><th>Action</th><th>Source Key</th><th>Trigger File</th></tr></thead><tbody>$events</tbody></table></section>
<div class="foot">Generated by gerrit-change-discovery POC</div>
</div></body></html>""")
    page = page_template.substitute(
        cards=card_html,
        sources=source_rows,
        changes=change_rows,
        events=event_rows,
    )

    output = report_dir / "index.html"
    output.write_text(page, encoding="utf-8")
    if logger:
        logger.info("HTML Dashboard 已生成: %s", output)
    return output


def load_config(path):
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8-sig") as fh:
        config = json.load(fh)
    base = config_path.parent
    db_path = Path(config.get("database_path", "./data/skillhub-poc.db"))
    report_dir = Path(config.get("report_dir", "./output/dashboard"))
    if not db_path.is_absolute():
        db_path = (base / db_path).resolve()
    if not report_dir.is_absolute():
        report_dir = (base / report_dir).resolve()
    return str(db_path), str(report_dir)


def main():
    parser = argparse.ArgumentParser(description="Generate SkillHub POC HTML dashboard from SQLite")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    db_path, report_dir = load_config(args.config)
    output = generate_report(db_path, report_dir)
    print("Dashboard: {}".format(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
