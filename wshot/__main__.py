import argparse
import fcntl
import logging
from pathlib import Path

from .client import StatsClient
from .pipeline import run_season
from .transform import identifier

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="Build validated WNBA shot, assist and lineup CSVs.")
    parser.add_argument("--season", type=int, nargs="+", required=True, help="Calendar year(s), e.g. 2025 2026")
    parser.add_argument("--season-type", choices=["regular", "playoffs", "both"], default="regular")
    parser.add_argument("--shots", choices=["auto", "local", "live"], default="auto", help="auto: reuse local shots when box-score totals match, fetch missing/incomplete games")
    parser.add_argument("--input", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "wnba")
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache" / "wshot")
    parser.add_argument("--game-id", action="append", type=lambda v: identifier(v, 10), help="Limit to a game; repeat for multiple games; use separate sample output")
    parser.add_argument("--lineups", choices=["auto", "api", "pbp"], default="auto", help="auto: rotations with conservative play-by-play fallback")
    parser.add_argument("--rotation-failure-limit", type=int, default=3, help="auto mode pauses uncached rotations after this many failures; probes every 50 games")
    parser.add_argument("--pbp-source", choices=["auto", "v2", "v3"], default="auto", help="auto: native-ID V2 first, V3 fallback with unique same-game secondary-name resolution")
    parser.add_argument("--stats-host", choices=["stats.wnba.com", "stats.nba.com"], default="stats.wnba.com")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--retries", type=int, default=3, help="Maximum attempts per endpoint")
    parser.add_argument("--delay", type=float, default=1.5, help="Minimum seconds between requests")
    parser.add_argument("--offline", action="store_true", help="Use only local files and validated cached responses")
    parser.add_argument("--refresh", action="store_true", help="Refetch cached games; combine with --game-id for targeted repairs")
    parser.add_argument("--strict-shot-totals", action="store_true", help="Withhold season exports if official shot charts and game logs disagree on FGA/FGM")
    parser.add_argument("--allow-missing-games", action="store_true", help="Export available games with partial status and an explicit missing-game list")
    args = parser.parse_args()
    if min(args.season) < 1997 or args.timeout <= 0 or args.retries < 1 or args.delay < 0 or args.rotation_failure_limit < 1:
        parser.error("Seasons must be >=1997; timeout/retries positive; delay nonnegative")
    if args.game_id and (len(args.season) != 1 or args.season_type == "both"):
        parser.error("A game sample must select one season and one season type")
    for name in ("input", "output", "cache"):
        setattr(args, name, getattr(args, name).resolve())
    # Candidate builds remain separate from the existing source data.
    if args.output == args.input:
        parser.error("--output must differ from --input; review a candidate build before replacing source data")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = StatsClient(args.cache, args.stats_host, args.timeout, args.retries, args.delay, args.offline, args.refresh)
    args.output.mkdir(parents=True, exist_ok=True)
    lock = (args.output / ".run.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error(f"Another pipeline is writing to {args.output}")
    bad = False
    modes = [False, True] if args.season_type == "both" else [args.season_type == "playoffs"]
    for year in args.season:
        for playoffs in modes:
            report = run_season(args, client, year, playoffs)
            logging.info("%s%s: %s %s", year, "ps" if playoffs else "", report["status"], report.get("totals", {}))
            bad |= report["status"] not in ("complete", "no_games")
    return 2 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
