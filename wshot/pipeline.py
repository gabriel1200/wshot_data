"""Fetch, validate, enrich and export a selected WNBA season."""
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from nba_api.stats.endpoints import ShotChartDetail, leaguegamelog, boxscoretraditionalv3

from .client import atomic_json, frames
from .transform import attach_game, identifier, normalize_shots, pbp_frame, pbp_v3_frame, rotation_frame, game_roster

LOG = logging.getLogger(__name__)

# Explicit, independently verified exceptions; zero totals alone are not proof
# that an arbitrary game was unplayed.
UNPLAYED_GAMES = {
    "1021800162": dict(
        reason="Las Vegas forfeited the August 3, 2018 game at Washington without playing",
        source="https://www.wnba.com/news/las-vegas-aces-forfeit-game-vs-washington-mystics",
        teams={"1611661319", "1611661322"}),
}


def verified_unplayed(schedule):
    excluded = []
    for game, info in UNPLAYED_GAMES.items():
        rows = schedule[schedule.GAME_ID == game]
        if rows.empty:
            continue
        fields = ["MIN", "FGA", "FGM", "PTS"]
        if (len(rows) != 2 or set(rows.TEAM_ID) != info["teams"]
                or not set(fields).issubset(rows)
                or not rows[fields].apply(pd.to_numeric, errors="raise").eq(0).all().all()):
            raise ValueError(f"Game log contradicts verified unplayed game {game}")
        excluded.append(dict(game_id=game, reason=info["reason"], source=info["source"]))
    return excluded


def atomic_csv(path, df):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            df.to_csv(f, index=False)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)


def read_csv(path):
    return pd.read_csv(path, dtype={c: str for c in ["GAME_ID", "GAME_EVENT_ID", "SHOT_ID", "PLAYER_ID", "TEAM_ID", "PERSON_ID", "ASSIST_ID", "OPP_TEAM_ID", "SHOOTING_TEAM_ID"]})


def load_local(root, folder, year, playoffs):
    paths = sorted(p for p in (root / "team" / folder).glob("*.csv") if p.stem.isdigit())
    if not paths:
        raise ValueError(f"No local team shot files in {root / 'team' / folder}")
    parts = []
    for path in paths:
        df = read_csv(path)
        if not df.empty:
            normalized = normalize_shots(df, year, playoffs)
            if set(normalized.TEAM_ID) != {path.stem}:
                raise ValueError(f"Team ID does not match filename: {path}")
            parts.append(normalized)
    if not parts:
        raise ValueError("No local shots")
    return normalize_shots(pd.concat(parts, ignore_index=True), year, playoffs)


def fetch_schedule(client, year, playoffs):
    params = leaguegamelog.LeagueGameLog(get_request=False, season=str(year), league_id="10",
                                      season_type_all_star="Playoffs" if playoffs else "Regular Season").parameters
    def validate(payload):
        df = frames(payload)["LeagueGameLog"]
        if not {"GAME_ID", "TEAM_ID", "TEAM_ABBREVIATION", "FGA", "FGM"}.issubset(df):
            raise ValueError("Invalid game log schema")
    payload = client.get("leaguegamelog", params, str(year) + ("ps" if playoffs else ""), validate, refresh=True)
    df = frames(payload)["LeagueGameLog"].copy()
    df["GAME_ID"] = df.GAME_ID.map(lambda v: identifier(v, 10))
    df["TEAM_ID"] = df.TEAM_ID.map(identifier)
    expected = ("104" if playoffs else "102") + str(year)[-2:]
    if not df.GAME_ID.str.startswith(expected).all():
        raise ValueError("Game log contains a different season/type")
    if df.duplicated(["GAME_ID", "TEAM_ID"]).any():
        raise ValueError("Duplicate game log team rows")
    return df


def boxscore_game_log(payload, game):
    """Verify a locally discovered game missing from the league season index."""
    game_roster(payload, game)
    box = payload["boxScoreTraditional"]
    home, away = box["homeTeam"], box["awayTeam"]
    if identifier(home["teamId"]) == identifier(away["teamId"]):
        raise ValueError("Box score has duplicate teams")
    rows = []
    for team, opponent, separator in ((home, away, " vs. "), (away, home, " @ ")):
        stats = team["statistics"]
        fga, fgm, pts = (stats[k] for k in ("fieldGoalsAttempted", "fieldGoalsMade", "points"))
        if any(not isinstance(v, (int, float)) or v != int(v) or v < 0 for v in (fga, fgm, pts)) or fgm > fga:
            raise ValueError("Invalid box-score totals")
        abbr, opp = team["teamTricode"], opponent["teamTricode"]
        if not abbr or not opp or abbr == opp:
            raise ValueError("Invalid box-score team abbreviations")
        rows.append(dict(GAME_ID=game, TEAM_ID=identifier(team["teamId"]), TEAM_ABBREVIATION=abbr,
                         MATCHUP=abbr + separator + opp, FGA=int(fga), FGM=int(fgm), PTS=int(pts)))
    return pd.DataFrame(rows)


def fetch_shots(client, game, year, playoffs, expected):
    params = ShotChartDetail(get_request=False, team_id=0, player_id=0, league_id="10",
                             context_measure_simple="FGA", season_nullable=str(year),
                             season_type_all_star="Playoffs" if playoffs else "Regular Season",
                             game_id_nullable=game).parameters
    def validate(payload):
        df = normalize_shots(frames(payload)["Shot_Chart_Detail"], year, playoffs)
        # Cache the source's valid response even when two official sources
        # disagree on totals; the run reports that disagreement separately.
        validate_shots(df, game)
    try:
        try:
            payload = client.get("shotchartdetail", params, game, validate)
        except ValueError:
            # Historical indexes can disagree with season filters. The exact
            # game, year/type prefix, and both teams are still validated.
            payload = client.get("shotchartdetail", dict(params, Season="", SeasonType=""), game, validate)
        return normalize_shots(frames(payload)["Shot_Chart_Detail"], year, playoffs), frames(payload).get("LeagueAverages")
    except ValueError:
        if expected is None or len(expected) != 2:
            raise
        # The original scraper's team-season query can remain available when
        # game-filtered requests fail. Preserve the actual raw team responses.
        parts, avg = [], None
        for team in expected.TEAM_ID:
            team_params = ShotChartDetail(get_request=False, team_id=team, player_id=0, league_id="10",
                                         context_measure_simple="FGA", season_nullable=f"{year}-{str(year+1)[-2:]}",
                                         season_type_all_star="Playoffs" if playoffs else "Regular Season").parameters
            def validate_team(payload):
                df = normalize_shots(frames(payload)["Shot_Chart_Detail"], year, playoffs)
                if df.empty or set(df.TEAM_ID) != {team}:
                    raise ValueError("Team-season shot response has wrong/empty team")
            key = f"{year}{'ps' if playoffs else ''}_team_{team}"
            payload = client.get("shotchartdetail", team_params, key, validate_team)
            df = normalize_shots(frames(payload)["Shot_Chart_Detail"], year, playoffs)
            if game not in set(df.GAME_ID) and not client.offline:
                payload = client.get("shotchartdetail", team_params, key, validate_team, refresh=True)
                df = normalize_shots(frames(payload)["Shot_Chart_Detail"], year, playoffs)
            parts.append(df[df.GAME_ID == game])
            avg = frames(payload).get("LeagueAverages")
        shots = pd.concat(parts, ignore_index=True)
        validate_shots(shots, game)
        return shots, avg


def validate_shots(shots, game, expected=None):
    if shots.empty or set(shots.GAME_ID) != {game} or shots.TEAM_ID.nunique() != 2:
        raise ValueError("Shot log must contain both teams for exactly one game")
    if not shots.SHOT_MADE_FLAG.isin([0, 1]).all():
        raise ValueError("Invalid shot outcome")
    if expected is not None:
        if len(expected) != 2 or set(expected.TEAM_ID) != set(shots.TEAM_ID):
            raise ValueError("Shot teams do not match game log")
        for row in expected.to_dict("records"):
            team_shots = shots[shots.TEAM_ID == row["TEAM_ID"]]
            if len(team_shots) != int(row["FGA"]) or int(team_shots.SHOT_MADE_FLAG.sum()) != int(row["FGM"]):
                raise ValueError(f"Shot totals differ from game log for {row['TEAM_ID']}: {len(team_shots)} FGA vs {row['FGA']}")


def replace_index(path, new, year, playoffs=None):
    if path.exists():
        old = pd.read_csv(path, dtype=str)
        key = "season" if playoffs is not None else "year"
        mask = old[key].astype(str).eq(str(year))
        if playoffs is not None:
            mask &= old["playoffs"].str.lower().eq(str(playoffs).lower())
        new = pd.concat([old[~mask], new], ignore_index=True)
    atomic_csv(path, new.drop_duplicates())


def export(root, folder, year, playoffs, shots, rotations, abbreviations):
    suffix = "_ps" if playoffs else ""
    team_index, player_index, dates = [], [], []
    for team, df in shots.groupby("TEAM_ID"):
        df = df.sort_values(["GAME_DATE", "GAME_ID", "time", "GAME_EVENT_ID"])
        atomic_csv(root / "team" / folder / f"{team}.csv", df)
        opponent = shots[shots.OPP_TEAM_ID == team].copy()
        opponent["SHOOTING_TEAM_ID"] = opponent.TEAM_ID
        opponent["SHOOTING_TEAM_NAME"] = opponent.TEAM_NAME
        opponent["TEAM_ID"] = team
        opponent["TEAM_NAME"] = df.TEAM_NAME.iloc[-1]
        opponent["OPP_TEAM_ID"] = opponent.SHOOTING_TEAM_ID
        # In a vs file PLAYERS_ON describes the file's defending team.
        for col in ["PLAYERS_ON", "LINEUP_STATUS", "LINEUP_SOURCE"]:
            offense = opponent[col].copy()
            opponent[col] = opponent["OPP_" + col]
            opponent["OPP_" + col] = offense
        atomic_csv(root / "team" / folder / f"{team}vs.csv", opponent)
        team_index.append(dict(team_id=team, team_name=df.TEAM_NAME.iloc[-1], year=year))
        for game, game_shots in df.groupby("GAME_ID"):
            row = game_shots.iloc[0]
            abbrev = abbreviations.get((game, team))
            opp = abbreviations.get((game, row.OPP_TEAM_ID))
            dates.append(dict(GAME_ID=game, TEAM_ID=team, HTM=row.HTM, VTM=row.VTM,
                              date=row.GAME_DATE, season=year, playoffs=playoffs, team=abbrev,
                              opp_team=opp, TEAM_STATUS="matched" if abbrev and opp else "unknown"))
    for player, df in shots.groupby("PLAYER_ID"):
        atomic_csv(root / "player" / folder / f"{player}.csv", df)
        for team, group in df.groupby("TEAM_ID"):
            player_index.append(dict(player_name=group.PLAYER_NAME.iloc[-1], player_id=player, team_id=team, year=year))
    if not rotations.empty:
        for team, df in rotations.groupby("TEAM_ID"):
            atomic_csv(root / "rotations" / folder / f"{team}.csv", df)
    assists = shots[shots.assisted == 1][["GAME_ID", "GAME_EVENT_ID", "PLAYER_ID", "ASSIST_ID", "TEAM_ID", "SHOT_ID"]].rename(columns={"GAME_EVENT_ID": "EVENTNUM"})
    atomic_csv(root / "assists" / folder / "ast.csv", assists)
    replace_index(root / f"wteam_index{suffix}.csv", pd.DataFrame(team_index), year)
    replace_index(root / f"wplayer_index{suffix}.csv", pd.DataFrame(player_index), year)
    replace_index(root / "wgame_dates.csv", pd.DataFrame(dates), year, playoffs)


def run_season(args, client, year, playoffs):
    folder = str(year) + ("ps" if playoffs else "")
    output = args.output
    report_path = output / "reports" / f"{folder}.json"
    report = dict(season=year, playoffs=playoffs, started=datetime.now(timezone.utc).isoformat(),
                  status="running", shots_source=args.shots, stats_host=client.host,
                  requested_games=args.game_id, games=[], errors=[], warnings=[])
    atomic_json(report_path, report)
    try:
        schedule = None if args.offline and args.shots != "live" and not (client.cache / "leaguegamelog" / f"{folder}.json").exists() else fetch_schedule(client, year, playoffs)
        local = None
        if args.shots != "live":
            try:
                local = load_local(args.input, folder, year, playoffs)
            except ValueError:
                if args.shots == "local":
                    raise
        if schedule is None and local is None:
            raise ValueError("No cached game log or local shots available")
        report["supplemental_games"] = []
        if schedule is not None and local is not None:
            for game in sorted(set(local.GAME_ID) - set(schedule.GAME_ID)):
                params = boxscoretraditionalv3.BoxScoreTraditionalV3(get_request=False, game_id=game).parameters
                box = client.get("boxscoretraditionalv3", params, game, lambda p: boxscore_game_log(p, game))
                supplement = boxscore_game_log(box, game)
                if set(supplement.TEAM_ID) != set(local.loc[local.GAME_ID == game, "TEAM_ID"]):
                    raise ValueError(f"Supplemental box score disagrees with local shot teams: {game}")
                schedule = pd.concat([schedule, supplement], ignore_index=True)
                report["supplemental_games"].append(dict(game_id=game, source="boxscoretraditionalv3",
                                                        reason="Present in local shot data but absent from league game log"))
        games = sorted(schedule.GAME_ID.unique() if schedule is not None else local.GAME_ID.unique())
        report["unplayed_games"] = verified_unplayed(schedule) if schedule is not None else []
        unplayed_ids = {g["game_id"] for g in report["unplayed_games"]}
        if local is not None and local.GAME_ID.isin(unplayed_ids).any():
            raise ValueError("Local shots contradict a verified unplayed game")
        games = [g for g in games if g not in unplayed_ids]
        if args.game_id:
            missing = set(args.game_id) - set(games)
            if missing:
                raise ValueError(f"Requested games not present in season: {sorted(missing)}")
            games = [g for g in games if g in args.game_id]
            # A sample must never replace season files with a subset of their games.
            for path in (output / "team" / folder).glob("*.csv"):
                if path.stem.isdigit() and not set(read_csv(path).GAME_ID).issubset(games):
                    raise ValueError("Use a separate --output directory for a game sample")
        if not games:
            if local is not None and not local.empty:
                raise ValueError("Game log is empty despite existing local shots; refusing to treat this as an empty season")
            if any(p.stem.isdigit() for p in (output / "team" / folder).glob("*.csv")):
                raise ValueError("Game log is empty despite previous candidate exports")
            report["status"] = "no_games"
            report["finished"] = datetime.now(timezone.utc).isoformat()
            report["requests"] = client.requests
            report["cache_hits"] = client.cache_hits
            atomic_json(report_path, report)
            return report
        enriched, rotation_parts, abbreviations = [], [], {}
        if schedule is not None:
            abbreviations.update({(r.GAME_ID, r.TEAM_ID): r.TEAM_ABBREVIATION for r in schedule.itertuples()})
        else:
            report["warnings"].append("Offline local run: game completeness cannot be checked against league game logs")
        avg = None
        rotation_failures = 0
        pbp_failures = 0
        for number, game in enumerate(games, 1):
            LOG.info("%s game %d/%d: %s", folder, number, len(games), game)
            entry = {"game_id": game, "errors": [], "warnings": []}
            report["games"].append(entry)
            try:
                expected = schedule[schedule.GAME_ID == game] if schedule is not None else None
                shots = None
                fallback = None
                if args.shots == "auto" and (client.cache / "shotchartdetail" / f"{game}.json").exists():
                    shots, game_avg = fetch_shots(client, game, year, playoffs, expected)
                    entry["shots_source"] = "shotchartdetail"
                    if game_avg is not None:
                        avg = game_avg
                if shots is None and local is not None and not (args.refresh and args.shots == "auto"):
                    candidate = local[local.GAME_ID == game].copy()
                    try:
                        validate_shots(candidate, game)
                        fallback = candidate
                        validate_shots(candidate, game, expected)
                        shots = candidate
                        entry["shots_source"] = "local"
                    except ValueError:
                        if args.shots == "local":
                            if fallback is None:
                                raise
                            shots = fallback
                            entry["shots_source"] = "local"
                if shots is None:
                    try:
                        shots, game_avg = fetch_shots(client, game, year, playoffs, expected)
                        entry["shots_source"] = "shotchartdetail"
                        if game_avg is not None:
                            avg = game_avg
                    except ValueError:
                        if fallback is None or args.refresh:
                            raise
                        shots = fallback
                        entry["shots_source"] = "local"
                        entry["errors"].append("Could not refresh local shots with discrepant totals")
                validate_shots(shots, game)
                if expected is not None and (len(expected) != 2 or set(expected.TEAM_ID) != set(shots.TEAM_ID)):
                    raise ValueError("Shot teams do not match game log")
                if expected is not None and "MATCHUP" in expected:
                    home = expected[expected.MATCHUP.str.contains(" vs. ", regex=False)]
                    away = expected[expected.MATCHUP.str.contains(" @ ", regex=False)]
                    if len(home) == len(away) == 1:
                        htm, vtm = home.TEAM_ABBREVIATION.iloc[0], away.TEAM_ABBREVIATION.iloc[0]
                        if not (shots.HTM.eq(htm) & shots.VTM.eq(vtm)).all():
                            shots["RAW_HTM"], shots["RAW_VTM"] = shots.HTM, shots.VTM
                            shots["HTM"], shots["VTM"] = htm, vtm
                            entry["warnings"].append("Corrected shot home/away abbreviations from verified matchup; raw values preserved")
                shots["SHOT_DATA_STATUS"] = "unverified" if expected is None else "validated"
                try:
                    validate_shots(shots, game, expected)
                except ValueError as exc:
                    if args.strict_shot_totals:
                        raise
                    shots["SHOT_DATA_STATUS"] = "boxscore_mismatch"
                    entry["errors"].append(str(exc))
                team_ids = sorted(shots.TEAM_ID.unique())
                pbp, rotations = None, None
                try:
                    expected_score = None
                    if expected is not None and {"MATCHUP", "PTS"}.issubset(expected):
                        home = expected[expected.MATCHUP.str.contains(" vs. ", regex=False)]
                        away = expected[expected.MATCHUP.str.contains(" @ ", regex=False)]
                        if len(home) == len(away) == 1:
                            expected_score = (int(away.PTS.iloc[0]), int(home.PTS.iloc[0]))
                    def validate_pbp(payload):
                        pbp_frame(frames(payload)["PlayByPlay"], game, year, expected_score)
                    mode = getattr(args, "pbp_source", "auto")
                    v2_cached = (client.cache / "playbyplayv2" / f"{game}.json").exists()
                    v3_cached = (client.cache / "playbyplayv3" / f"{game}.json").exists()
                    use_v3 = mode == "v3" or (mode == "auto" and not v2_cached and (v3_cached or (pbp_failures >= 3 and number % 50 != 0)))
                    if not use_v3:
                        try:
                            payload = client.get("playbyplayv2", dict(GameID=game, StartPeriod=0, EndPeriod=0), game, validate_pbp)
                            pbp = pbp_frame(frames(payload)["PlayByPlay"], game, year, expected_score)
                            if not v2_cached:
                                pbp_failures = 0
                        except ValueError as exc:
                            if mode == "v2":
                                raise
                            pbp_failures += 1
                            entry["warnings"].append(str(exc))
                            use_v3 = True
                    if use_v3:
                        roster = None
                        try:
                            params = boxscoretraditionalv3.BoxScoreTraditionalV3(get_request=False, game_id=game).parameters
                            box = client.get("boxscoretraditionalv3", params, game, lambda p: game_roster(p, game))
                            roster = game_roster(box, game)
                        except ValueError as exc:
                            entry["warnings"].append(f"V3 roster unavailable: {exc}")
                        payload = client.get("playbyplayv3", dict(GameID=game, StartPeriod=0, EndPeriod=0), game,
                                             lambda p: pbp_v3_frame(p, game, year, roster))
                        pbp = pbp_v3_frame(payload, game, year, roster)
                        entry["warnings"].append("V3 fallback: secondary names resolved only against unique native IDs on the same team in this game")
                        if pbp.attrs.get("unresolved_secondary_names"):
                            entry["warnings"].append(f"{pbp.attrs['unresolved_secondary_names']} unresolved V3 secondary names")
                    entry["pbp_source"] = pbp.PBP_SOURCE.iloc[0]
                    if pbp.attrs.get("duplicate_rows_removed"):
                        entry["warnings"].append(f"Removed {pbp.attrs['duplicate_rows_removed']} identical play-by-play duplicate row(s)")
                    if pbp.attrs.get("period_start_clocks_normalized"):
                        entry["warnings"].append(f"Normalized {pbp.attrs['period_start_clocks_normalized']} quarter-start/tip-off clocks from NBA-default 12:00 to WNBA 10:00; raw clocks preserved")
                    if pbp.attrs.get("final_horn_score_verified"):
                        entry["warnings"].append("End marker absent: final 0:00 event and non-tied score verified against official game log")
                    if pbp.attrs.get("terminal_marker_score_verified"):
                        entry["warnings"].append("Final period-end marker has stale clock: non-tied final score verified against game log; raw clock preserved")
                    for n in (1, 2, 3):
                        for r in pbp.to_dict("records"):
                            abbr = r.get(f"PLAYER{n}_TEAM_ABBREVIATION")
                            tid = r.get(f"PLAYER{n}_TEAM_ID")
                            if tid in team_ids and pd.notna(abbr) and abbr:
                                abbreviations[(game, tid)] = abbr
                except ValueError as exc:
                    entry["errors"].append(str(exc))
                rotation_cached = (client.cache / "gamerotation" / f"{game}.json").exists()
                rotation_paused = args.lineups == "auto" and rotation_failures >= args.rotation_failure_limit and not rotation_cached and number % 50 != 0
                if rotation_paused:
                    entry["warnings"].append("Rotation endpoint paused after repeated failures; using play-by-play, retrying every 50 games")
                if args.lineups != "pbp" and not rotation_paused:
                    try:
                        def validate_rotation(payload):
                            rotation_frame(pd.concat(frames(payload).values(), ignore_index=True), game, team_ids)
                        payload = client.get("gamerotation", dict(GameID=game, LeagueID="10"), game, validate_rotation)
                        rotations = rotation_frame(pd.concat(frames(payload).values(), ignore_index=True), game, team_ids)
                        rotation_parts.append(rotations)
                        if not rotation_cached:
                            rotation_failures = 0
                    except ValueError as exc:
                        rotation_failures += 1
                        entry["warnings"].append(str(exc))
                result, inferred_rotations, issues = attach_game(shots, pbp, rotations, year, team_ids, args.lineups != "api")
                if rotations is None and not inferred_rotations.empty:
                    rotation_parts.append(inferred_rotations)
                entry["warnings"].extend(issues)
                entry.update(shots=len(result), assists=int(result.assisted.sum()),
                             unknown_assists=int(result.assisted.isna().sum()),
                             incomplete_offense=int(result.PLAYERS_ON.isna().sum()),
                             incomplete_defense=int(result.OPP_PLAYERS_ON.isna().sum()),
                             lineup_sources=result.LINEUP_SOURCE.fillna("missing").value_counts().to_dict())
                if expected is not None:
                    entry["shot_totals"] = [{"team_id": r["TEAM_ID"], "expected_fga": int(r["FGA"]),
                                             "actual_fga": int((result.TEAM_ID == r["TEAM_ID"]).sum()),
                                             "expected_fgm": int(r["FGM"]),
                                             "actual_fgm": int(result.loc[result.TEAM_ID == r["TEAM_ID"], "SHOT_MADE_FLAG"].sum())}
                                            for r in expected.to_dict("records")]
                if entry["unknown_assists"] or entry["incomplete_offense"] or entry["incomplete_defense"]:
                    entry["errors"].append("Incomplete shot enrichment; see coverage counts")
                enriched.append(result)
            except (ValueError, KeyError, TypeError) as exc:
                entry["errors"].append(str(exc))
            atomic_json(report_path, report)
        # Missing shot games block exports: never silently publish a shortened season.
        if len(enriched) != len(games) and (not getattr(args, "allow_missing_games", False) or not enriched):
            raise ValueError(f"Only {len(enriched)}/{len(games)} games have valid shots; exports withheld")
        all_shots = pd.concat(enriched, ignore_index=True)
        rotation_df = pd.concat(rotation_parts, ignore_index=True) if rotation_parts else pd.DataFrame()
        report["status"] = "partial" if any(g["errors"] for g in report["games"]) else "complete"
        report["missing_games"] = [g["game_id"] for g in report["games"] if "shots" not in g]
        report["totals"] = dict(games=int(all_shots.GAME_ID.nunique()), expected_games=len(games), shots=len(all_shots),
                                offense_complete=int(all_shots.PLAYERS_ON.notna().sum()),
                                defense_complete=int(all_shots.OPP_PLAYERS_ON.notna().sum()),
                                assists_known=int(all_shots.assisted.notna().sum()))
        issue_rows = all_shots[all_shots.PLAYERS_ON.isna() | all_shots.OPP_PLAYERS_ON.isna() |
                               all_shots.assisted.isna() | all_shots.SHOT_DATA_STATUS.eq("boxscore_mismatch")]
        issue_cols = ["GAME_ID", "GAME_EVENT_ID", "SHOT_ID", "PLAYER_ID", "TEAM_ID", "OPP_TEAM_ID",
                      "SHOT_DATA_STATUS", "ASSIST_STATUS", "LINEUP_STATUS", "OPP_LINEUP_STATUS"]
        atomic_csv(output / "reports" / f"{folder}_shot_issues.csv", issue_rows[issue_cols])
        export(output, folder, year, playoffs, all_shots, rotation_df, abbreviations)
        if avg is None and local is not None and (args.input / "team" / folder / "avg.csv").exists():
            avg = pd.read_csv(args.input / "team" / folder / "avg.csv")
        if avg is not None:
            atomic_csv(output / "team" / folder / "avg.csv", avg)
    except Exception as exc:
        LOG.exception("Season %s failed", folder)
        report["status"] = "failed"
        report["errors"].append(str(exc))
    report["finished"] = datetime.now(timezone.utc).isoformat()
    report["requests"] = client.requests
    report["cache_hits"] = client.cache_hits
    atomic_json(report_path, report)
    return report
