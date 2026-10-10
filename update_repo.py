#!/usr/bin/env python3
"""Build one WNBA season and promote complete output into this repository."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pandas as pd

from wshot.pipeline import replace_index


ROOT = Path(__file__).resolve().parent


def promote(root, build, year, playoffs, allow_partial=False):
    """Prepare merged indexes and backups before replacing repository files."""
    folder = f"{year}{'ps' if playoffs else ''}"
    report_path = build / "reports" / f"{folder}.json"
    report = json.loads(report_path.read_text())
    statuses = ("complete", "partial") if allow_partial else ("complete",)
    totals = report.get("totals", {})
    if (report.get("status") not in statuses or report.get("season") != year
            or report.get("playoffs") != playoffs or report.get("requested_games")
            or report.get("missing_games") or not totals.get("games")
            or totals.get("games") != totals.get("expected_games")):
        raise ValueError(f"Build is not a complete full-season export: {report_path}")

    files = []
    for category in ("team", "player", "rotations", "assists"):
        sources = sorted((build / category / folder).glob("*.csv"))
        if category in ("team", "player", "assists") and not sources:
            raise ValueError(f"Missing {category} output in {build}")
        files.extend((source, source.relative_to(build)) for source in sources)

    # Merge only the requested season into the current repository's indexes.
    # A fresh candidate intentionally contains no other seasons.
    staged = build / "merged_indexes"
    staged.mkdir()
    suffix = "_ps" if playoffs else ""
    for name in (f"wteam_index{suffix}.csv", f"wplayer_index{suffix}.csv", "wgame_dates.csv"):
        new = pd.read_csv(build / name, dtype=str)
        key = "season" if name == "wgame_dates.csv" else "year"
        if new.empty or not new[key].eq(str(year)).all():
            raise ValueError(f"Unexpected season in {name}")
        if name == "wgame_dates.csv" and not new.playoffs.str.lower().eq(str(playoffs).lower()).all():
            raise ValueError(f"Unexpected season type in {name}")
        destination = staged / name
        if (root / name).exists():
            shutil.copy2(root / name, destination)
        replace_index(destination, new, year, playoffs if name == "wgame_dates.csv" else None)
        files.append((destination, Path(name)))

    backup = build / "backup"
    backup.mkdir()
    manifest = {"replaced": [], "created": []}
    for _, relative in files:
        destination = root / relative
        if destination.exists():
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, saved)
            manifest["replaced"].append(str(relative))
        else:
            manifest["created"].append(str(relative))
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    for source, relative in files:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(fd)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f"Updated {len(files)} repository files for {folder}.")
    print(f"Previous versions and file manifest: {backup}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True, help="Calendar year, e.g. 2026")
    parser.add_argument("--season-type", choices=("regular", "playoffs"), default="regular")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch cached source responses too")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Allow flagged lineup/assist gaps or shot-total disagreements; all games must still be exported")
    parser.add_argument("--stats-host", choices=("stats.wnba.com", "stats.nba.com"), default="stats.wnba.com")
    args = parser.parse_args()
    if args.season < 1997:
        parser.error("Season must be >= 1997")
    build_root = ROOT / "build"
    build_root.mkdir(exist_ok=True)
    # Keep two invocations of this updater from promoting concurrently.
    with (build_root / ".update-repo.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Another repository update is already running.\n")
        playoffs = args.season_type == "playoffs"
        folder = f"{args.season}{'ps' if playoffs else ''}"
        build = Path(tempfile.mkdtemp(prefix=f"wnba-update-{folder}-", dir=build_root))
        command = [sys.executable, "-m", "wshot", "--season", str(args.season),
                   "--season-type", args.season_type, "--output", str(build),
                   "--stats-host", args.stats_host]
        if args.refresh:
            command.append("--refresh")
        print(f"Building into {build}", flush=True)
        result = subprocess.run(command, cwd=ROOT, check=False)
        report_path = build / "reports" / f"{folder}.json"
        if result.returncode and not (result.returncode == 2 and args.allow_partial):
            print(f"Build exited with code {result.returncode}; repository data was not replaced.", file=sys.stderr)
            print(f"Review coverage and errors: {report_path}", file=sys.stderr)
            if report_path.exists():
                report = json.loads(report_path.read_text())
                print(json.dumps({key: report.get(key) for key in ("status", "totals", "errors")}, indent=2), file=sys.stderr)
            return result.returncode
        try:
            promote(ROOT, build, args.season, playoffs, args.allow_partial)
        except (ValueError, OSError, KeyError) as exc:
            print(f"Repository update stopped: {exc}", file=sys.stderr)
            print(f"Build and any backups: {build}", file=sys.stderr)
            return 1
        print(f"Build report: {report_path}")
        print("Done. Review with git diff --stat. No commit or push was performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
