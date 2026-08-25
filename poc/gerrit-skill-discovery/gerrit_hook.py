#!/usr/bin/env python3
"""POC adapter for Gerrit's hooks plugin `patchset-created` event.

The hooks plugin invokes this program once for every created patchset. The program scans
that commit from Gerrit's bare repository and appends discovered Skill Source / Content
Version candidates to JSONL.
Compatible with Python 3.8+.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from skill_scan import GitError, scan_revision


def project_repo_path(repo_base, project):
    candidate = Path(repo_base) / (project if project.endswith(".git") else project + ".git")
    return str(candidate)


def main():
    parser = argparse.ArgumentParser(description="Gerrit patchset-created -> Skill discovery POC")
    parser.add_argument(
        "--repo-base",
        default=os.environ.get("SKILL_POC_GIT_BASE"),
        help="Gerrit git repository base directory",
    )
    parser.add_argument(
        "--output-file",
        default=os.environ.get("SKILL_POC_OUTPUT"),
        help="Append JSONL records to this file",
    )

    # Gerrit hooks plugin patchset-created arguments. Keep unknown args for version compatibility.
    parser.add_argument("--change")
    parser.add_argument("--kind")
    parser.add_argument("--change-url")
    parser.add_argument("--change-owner")
    parser.add_argument("--change-owner-username")
    parser.add_argument("--project", required=True)
    parser.add_argument("--branch")
    parser.add_argument("--topic")
    parser.add_argument("--uploader")
    parser.add_argument("--uploader-username")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--patchset")
    args, unknown = parser.parse_known_args()

    if not args.repo_base:
        print("missing --repo-base or SKILL_POC_GIT_BASE", file=sys.stderr)
        return 2

    repo = project_repo_path(args.repo_base, args.project)
    if not os.path.exists(repo):
        print("repository not found: {}".format(repo), file=sys.stderr)
        return 2

    try:
        records = scan_revision(repo, args.commit, args.project)
    except GitError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    event = {
        "event_type": "patchset-created",
        "project": args.project,
        "branch": args.branch,
        "change": args.change,
        "patchset": args.patchset,
        "commit": args.commit,
        "uploader": args.uploader_username or args.uploader,
        "unknown_args": unknown,
    }

    lines = []
    if not records:
        lines.append(
            json.dumps(
                {"event": event, "skills": [], "message": "no SKILL.md found in revision"},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        for record in records:
            lines.append(
                json.dumps(
                    {"event": event, "skill": asdict(record)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )

    text = "\n".join(lines) + "\n"
    if args.output_file:
        output = Path(args.output_file)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
