import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace

import pandas as pd

from wshot.client import StatsClient
from wshot.pipeline import export, read_csv, validate_shots, run_season, fetch_shots, verified_unplayed, boxscore_game_log
from wshot.transform import attach_game, elapsed, infer_lineups, normalize_shots, pbp_frame, pbp_v3_frame, rotation_frame, game_roster, reconcile_shot_clocks


GAME = "1022500001"
HOME, AWAY = "1611661313", "1611661319"


def shot(event=10, player="1", team=HOME, made=1, minutes=9, seconds=0):
    return dict(GAME_ID=GAME, GAME_EVENT_ID=event, PLAYER_ID=player, PLAYER_NAME=f"Player {player}",
                TEAM_ID=team, TEAM_NAME="Home" if team == HOME else "Away", PERIOD=1,
                MINUTES_REMAINING=minutes, SECONDS_REMAINING=seconds, SHOT_MADE_FLAG=made,
                GAME_DATE=20250501, HTM="NYL", VTM="LVA")


def event(number, kind=1, p1="1", p2="0", team=HOME, time=600, period=1):
    return dict(GAME_ID=GAME, EVENTNUM=str(number), SHOT_ID=GAME+str(number), EVENTMSGTYPE=kind,
                PLAYER1_ID=p1, PLAYER2_ID=p2, PLAYER3_ID="0", PLAYER1_TEAM_ID=team,
                PLAYER2_TEAM_ID=team if p2 != "0" else "0", PLAYER3_TEAM_ID="0", time=time, PERIOD=period)


def complete_pbp():
    rows = [event(i, 4, str(i), time=i*10) for i in range(1, 6)]
    rows += [event(i, 4, str(i), team=AWAY, time=i*10) for i in range(6, 11)]
    rows += [event(20, 1, "1", "2", time=600), event(21, 8, "1", "11", time=600), event(22, 1, "11", "2", time=600)]
    return pd.DataFrame(rows)


class TransformTests(unittest.TestCase):
    def test_2005_halftime_clock_requires_exact_identity_and_offset(self):
        raw = dict(shot(20, minutes=2, seconds=44), GAME_ID="1020500001")
        shots = normalize_shots(pd.DataFrame([raw]), 2005, False)
        pbp = complete_pbp()
        pbp.GAME_ID = "1020500001"
        pbp.SHOT_ID = pbp.GAME_ID + pbp.EVENTNUM
        pbp.PERIOD = 2
        pbp["PCTIMESTRING"] = "19:24"
        pbp.loc[pbp.EVENTNUM == "20", "time"] = 12360
        fixed, _, issues = attach_game(shots, pbp, None, 2005, [HOME, AWAY])
        self.assertEqual(fixed.PERIOD.iloc[0], 2)
        self.assertEqual(fixed.MINUTES_REMAINING.iloc[0], 19)
        self.assertEqual(fixed.time.iloc[0], 12360)
        self.assertEqual(fixed.RAW_PERIOD.iloc[0], 1)
        self.assertEqual(fixed.RAW_MINUTES_REMAINING.iloc[0], 2)
        self.assertEqual(fixed.assisted.iloc[0], 1)
        self.assertEqual(fixed.PLAYERS_ON.iloc[0], "1|2|3|4|5")
        self.assertTrue(any("halftime" in issue for issue in issues))
        for col, val in (("time", 12370), ("PLAYER1_ID", "9"), ("PLAYER1_TEAM_ID", AWAY),
                         ("EVENTMSGTYPE", 2), ("PERIOD", 1), ("PCTIMESTRING", "19:25")):
            bad = pbp.copy(); bad.loc[bad.EVENTNUM == "20", col] = val
            with self.subTest(column=col):
                self.assertEqual(reconcile_shot_clocks(shots, bad, 2005).PERIOD.iloc[0], 1)
        self.assertEqual(reconcile_shot_clocks(shots, pbp, 2006).PERIOD.iloc[0], 1)
        early = normalize_shots(pd.DataFrame([dict(raw, GAME_ID="1029700001")]), 1997, False)
        early_pbp = pbp.assign(GAME_ID="1029700001"); early_pbp.SHOT_ID = early_pbp.GAME_ID + early_pbp.EVENTNUM
        self.assertEqual(reconcile_shot_clocks(early, early_pbp, 1997).PERIOD.iloc[0], 2)

    def test_halves_quarters_and_multiple_overtimes(self):
        self.assertEqual(elapsed(2005, 2, 19, 0), 12600)
        self.assertEqual(elapsed(2005, 3, 4, 30), 24300)
        self.assertEqual(elapsed(2025, 4, 0, 0), 24000)
        self.assertEqual(elapsed(2025, 6, 4, 30), 27300)
        self.assertEqual(elapsed(2025, 8, 5, 0), 33000)
        with self.assertRaises(ValueError):
            elapsed(2025, 1, 12, 0)

    def test_ids_duplicate_and_season_contamination(self):
        row = shot()
        row["PLAYER_ID"] = 1.0
        df = normalize_shots(pd.DataFrame([row]), 2025, False)
        self.assertEqual(df.PLAYER_ID.iloc[0], "1")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            normalize_shots(pd.DataFrame([row, row]), 2025, False)
        with self.assertRaisesRegex(ValueError, "Wrong league"):
            normalize_shots(pd.DataFrame([row]), 2025, True)

    def test_same_clock_substitution_uses_event_order(self):
        snapshots, stints, issues = infer_lineups(complete_pbp(), 2025, [HOME, AWAY])
        self.assertFalse(issues)
        self.assertEqual(snapshots[(GAME+"20", HOME)], "1|2|3|4|5")
        self.assertEqual(snapshots[(GAME+"22", HOME)], "2|3|4|5|11")
        self.assertEqual(stints[stints.PERSON_ID == "1"].OUT_TIME_REAL.iloc[0], 600)

    def test_ambiguous_starters_and_inconsistent_subs_not_guessed(self):
        pbp = complete_pbp()
        pbp = pbp[~pbp.EVENTNUM.isin(["5"])]
        snapshots, _, issues = infer_lineups(pbp, 2025, [HOME, AWAY])
        self.assertNotIn((GAME+"20", HOME), snapshots)
        self.assertIn((GAME+"20", AWAY), snapshots)
        self.assertTrue(issues)
        pbp = complete_pbp()
        pbp.loc[pbp.EVENTNUM == "21", "PLAYER2_ID"] = "2"
        snapshots, _, issues = infer_lineups(pbp, 2025, [HOME])
        self.assertFalse(snapshots)
        self.assertTrue(issues)

    def test_assists_match_shooter_team_outcome_and_event(self):
        shots = normalize_shots(pd.DataFrame([shot(20), shot(22, "11"), shot(999)]), 2025, False)
        result, _, _ = attach_game(shots, complete_pbp(), None, 2025, [HOME, AWAY])
        self.assertEqual(result.ASSIST_ID.iloc[0], "2")
        self.assertEqual(result.assisted.iloc[0], 1)
        self.assertTrue(pd.isna(result.assisted.iloc[2]))
        self.assertTrue(pd.isna(result.PLAYERS_ON.iloc[2]))
        self.assertEqual(result.ASSIST_STATUS.iloc[2], "event_mismatch")
        wrong = complete_pbp()
        wrong.loc[wrong.EVENTNUM == "20", "PLAYER1_TEAM_ID"] = AWAY
        result, _, _ = attach_game(shots, wrong, None, 2025, [HOME, AWAY])
        self.assertTrue(pd.isna(result.assisted.iloc[0]))

    def test_missing_pbp_does_not_mean_unassisted(self):
        df = normalize_shots(pd.DataFrame([shot()]), 2025, False)
        result, _, _ = attach_game(df, None, None, 2025, [HOME, AWAY])
        self.assertTrue(pd.isna(result.assisted.iloc[0]))
        self.assertTrue(pd.isna(result.PLAYERS_ON.iloc[0]))
        self.assertTrue(pd.isna(result.OPP_PLAYERS_ON.iloc[0]))

    def test_api_lineup_requires_five_distinct_players_and_shooter(self):
        df = normalize_shots(pd.DataFrame([shot()]), 2025, False)
        rotations = pd.DataFrame([dict(TEAM_ID=HOME, PERSON_ID=str(p), IN_TIME_REAL=0, OUT_TIME_REAL=6000) for p in [1, 2, 3, 4, 4]])
        result, _, _ = attach_game(df, None, rotations, 2025, [HOME, AWAY])
        self.assertTrue(pd.isna(result.PLAYERS_ON.iloc[0]))
        rotations.PERSON_ID = ["2", "3", "4", "5", "6"]
        result, _, _ = attach_game(df, None, rotations, 2025, [HOME, AWAY])
        self.assertEqual(result.LINEUP_STATUS.iloc[0], "shooter_missing")

    def test_api_boundary_requires_event_evidence(self):
        df = normalize_shots(pd.DataFrame([shot(22, "11")]), 2025, False)
        rotations = pd.DataFrame([dict(TEAM_ID=HOME, PERSON_ID=str(p), IN_TIME_REAL=0, OUT_TIME_REAL=600) for p in range(1, 6)])
        result, _, _ = attach_game(df, complete_pbp(), rotations, 2025, [HOME, AWAY])
        self.assertEqual(result.PLAYERS_ON.iloc[0], "2|3|4|5|11")
        self.assertEqual(result.LINEUP_SOURCE.iloc[0], "playbyplay_inferred")

    def test_conflicting_lineups_are_not_silently_selected(self):
        df = normalize_shots(pd.DataFrame([shot(20)]), 2025, False)
        rotations = pd.DataFrame([dict(TEAM_ID=HOME, PERSON_ID=str(p), IN_TIME_REAL=0, OUT_TIME_REAL=6000) for p in [1, 2, 3, 4, 12]])
        result, _, _ = attach_game(df, complete_pbp(), rotations, 2025, [HOME, AWAY])
        self.assertTrue(pd.isna(result.PLAYERS_ON.iloc[0]))
        self.assertEqual(result.LINEUP_STATUS.iloc[0], "lineup_conflict")

    def test_wrong_or_partial_rotation_rejected(self):
        df = pd.DataFrame([dict(GAME_ID=GAME, TEAM_ID=HOME, PERSON_ID="1", IN_TIME_REAL=0, OUT_TIME_REAL=10)])
        with self.assertRaises(ValueError):
            rotation_frame(df, GAME, [HOME, AWAY])

    def test_shot_totals_checked_against_box_scores(self):
        shots = normalize_shots(pd.DataFrame([shot(), shot(11, "6", AWAY)]), 2025, False)
        expected = pd.DataFrame([dict(TEAM_ID=HOME, FGA=1, FGM=1), dict(TEAM_ID=AWAY, FGA=2, FGM=1)])
        with self.assertRaisesRegex(ValueError, "totals differ"):
            validate_shots(shots, GAME, expected)

    def test_only_verified_unplayed_game_can_be_excluded(self):
        rows = pd.DataFrame([dict(GAME_ID="1021800162", TEAM_ID=t, MIN=0, FGA=0, FGM=0, PTS=0)
                             for t in ("1611661319", "1611661322")])
        self.assertEqual(verified_unplayed(rows)[0]["game_id"], "1021800162")
        other = rows.copy(); other.GAME_ID = "1021800163"
        self.assertEqual(verified_unplayed(other), [])
        for bad in (rows.iloc[:1], rows.drop(columns="MIN"), rows.assign(FGA=1), rows.assign(TEAM_ID=HOME)):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "contradicts"):
                verified_unplayed(bad)

    def test_incomplete_pbp_is_rejected(self):
        row = event(1)
        row["PCTIMESTRING"] = "9:00"
        with self.assertRaisesRegex(ValueError, "completed final period"):
            pbp_frame(pd.DataFrame([row]), GAME, 2025)

    def test_missing_end_marker_requires_horn_and_verified_final_score(self):
        made = dict(event(1, period=4), PCTIMESTRING="0:23", SCORE="71 - 91")
        rebound = dict(event(2, 4, period=4), PCTIMESTRING="0:00", SCORE=None)
        df = pd.DataFrame([made, rebound])
        self.assertTrue(pbp_frame(df, GAME, 2025, (71, 91)).attrs["final_horn_score_verified"])
        stale = pd.DataFrame([made, dict(rebound, EVENTMSGTYPE=13, PCTIMESTRING="0:06")])
        fixed = pbp_frame(stale, GAME, 2025, (71, 91))
        self.assertTrue(fixed.attrs["terminal_marker_score_verified"])
        self.assertEqual(fixed.time.iloc[-1], 24000)
        self.assertEqual(fixed.PCTIMESTRING.iloc[-1], "0:06")
        for bad, expected in ((df, None), (df, (70, 91)), (df.assign(SCORE="71 - 71"), (71, 71)),
                              (df.assign(PCTIMESTRING="0:01"), (71, 91)),
                              (df.assign(PERIOD=3), (71, 91)), (stale, (71, 92)), (stale, None)):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "completed final period"):
                pbp_frame(bad, GAME, 2025, expected)

    def test_identical_pbp_duplicates_are_safe_but_conflicting_versions_fail(self):
        row = event(1); row["PCTIMESTRING"] = "9:00"
        end = event(99, 13, period=4); end["PCTIMESTRING"] = "0:00"
        df = pbp_frame(pd.DataFrame([row, row, end]), GAME, 2025)
        self.assertEqual(len(df), 2)
        self.assertEqual(df.attrs["duplicate_rows_removed"], 1)
        changed = dict(row, PLAYER1_ID="2")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            pbp_frame(pd.DataFrame([row, changed, end]), GAME, 2025)

    def test_nba_default_opening_clocks_only(self):
        start = dict(event(0, 12), PCTIMESTRING="12:00")
        jump = dict(event(1, 10), PCTIMESTRING="12:00.00")
        attempt = dict(event(2), PCTIMESTRING="9:45")
        end = dict(event(99, 13, period=4), PCTIMESTRING="0:00")
        df = pbp_frame(pd.DataFrame([start, jump, attempt, end]), GAME, 2025)
        self.assertEqual(df.time.tolist(), [0, 0, 150, 24000])
        self.assertEqual(df.PCTIMESTRING.iloc[:2].tolist(), ["12:00", "12:00.00"])
        self.assertEqual(df.attrs["period_start_clocks_normalized"], 2)
        later = dict(event(50, 12, period=2), PCTIMESTRING="12:00")
        df = pbp_frame(pd.DataFrame([start, jump, attempt, later, end]), GAME, 2025)
        self.assertEqual(df.time.iloc[3], 6000)
        self.assertEqual(df.attrs["period_start_clocks_normalized"], 3)
        for rows in ([start, dict(jump, EVENTMSGTYPE=1), attempt, end],
                     [start, jump, dict(attempt, PCTIMESTRING="12:00"), end],
                     [attempt, start, jump, end],
                     [dict(start, PERIOD=2), dict(jump, PERIOD=2), attempt, end]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "Invalid .* clock"):
                pbp_frame(pd.DataFrame(rows), GAME, 2025)


class ExportTests(unittest.TestCase):
    def test_vs_identity_and_indices_keep_seasons_separate(self):
        shots = normalize_shots(pd.DataFrame([shot(20), shot(30, "6", AWAY)]), 2025, False)
        pbp = pd.concat([complete_pbp(), pd.DataFrame([event(30, 1, "6", team=AWAY, time=600)])], ignore_index=True)
        enriched, rotations, _ = attach_game(shots, pbp, None, 2025, [HOME, AWAY])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pd.DataFrame([dict(player_name="Old", player_id="50", team_id=HOME, year=2024)]).to_csv(root / "wplayer_index.csv", index=False)
            export(root, "2025", 2025, False, enriched, rotations, {(GAME, HOME): "NYL", (GAME, AWAY): "LVA"})
            vs = read_csv(root / "team" / "2025" / f"{HOME}vs.csv")
            self.assertEqual(vs.TEAM_ID.iloc[0], HOME)
            self.assertEqual(vs.TEAM_NAME.iloc[0], "Home")
            self.assertEqual(vs.SHOOTING_TEAM_ID.iloc[0], AWAY)
            self.assertEqual(vs.PLAYER_ID.iloc[0], "6")
            self.assertEqual(vs.PLAYERS_ON.iloc[0], "2|3|4|5|11")
            self.assertEqual(vs.OPP_PLAYERS_ON.iloc[0], "6|7|8|9|10")
            self.assertEqual(set(pd.read_csv(root / "wplayer_index.csv").year), {2024, 2025})
            assists = read_csv(root / "assists" / "2025" / "ast.csv")
            self.assertEqual(assists.ASSIST_ID.tolist(), ["2"])
            export(root, "2025", 2025, False, enriched, rotations, {(GAME, HOME): "NYL", (GAME, AWAY): "LVA"})
            self.assertEqual(len(pd.read_csv(root / "wplayer_index.csv")), 3)


class V3Tests(unittest.TestCase):
    def action(self, number, kind, person, team=HOME, description="", clock="PT09M00.00S", period=1, subtype=""):
        return dict(actionNumber=number, actionType=kind, personId=int(person), teamId=int(team),
                    teamTricode="NYL", description=description, clock=clock, period=period,
                    subType=subtype, playerName="", playerNameI="")

    def payload(self, actions):
        end = self.action(99, "period", "0", team="0", clock="PT00M00.00S", period=4, subtype="end")
        return {"game": {"gameId": GAME, "actions": actions + [end]}}

    def test_unique_game_roster_names_resolve_secondary_ids(self):
        box = {"boxScoreTraditional": {"gameId": GAME,
               "homeTeam": {"teamId": HOME, "players": [dict(personId=1, firstName="One", familyName="Shooter"), dict(personId=2, firstName="Xu", familyName="Han"), dict(personId=3, firstName="Morgan", familyName="Maly")]},
               "awayTeam": {"teamId": AWAY, "players": [dict(personId=6, firstName="Other", familyName="Player")]}}}
        payload = self.payload([self.action(20, "Made Shot", "1", description="Shooter Shot (2 PTS) (Xu 1 AST)"),
                                self.action(21, "Substitution", "1", description="SUB: Maly FOR Shooter")])
        df = pbp_v3_frame(payload, GAME, 2025, game_roster(box, GAME))
        self.assertEqual(df[df.EVENTNUM == "20"].PLAYER2_ID.iloc[0], "2")
        self.assertEqual(df[df.EVENTNUM == "21"].PLAYER2_ID.iloc[0], "3")
        self.assertEqual(df.attrs["unresolved_secondary_names"], 0)
        shots = normalize_shots(pd.DataFrame([shot(20)]), 2025, False)
        result, _, _ = attach_game(shots, df, None, 2025, [HOME, AWAY])
        self.assertEqual(result.ASSIST_ID.iloc[0], "2")
        self.assertEqual(result.ASSIST_STATUS.iloc[0], "matched_name_resolved")
        self.assertEqual(result.PBP_SOURCE.iloc[0], "playbyplayv3")

    def test_ambiguous_name_does_not_become_unassisted_or_choose_a_player(self):
        roster = [(HOME, "2", ["Smith"]), (HOME, "3", ["Smith"])]
        payload = self.payload([self.action(20, "Made Shot", "1", description="Shot (2 PTS) (Smith 1 AST)")])
        df = pbp_v3_frame(payload, GAME, 2025, roster)
        shots = normalize_shots(pd.DataFrame([shot(20)]), 2025, False)
        result, _, _ = attach_game(shots, df, None, 2025, [HOME, AWAY])
        self.assertTrue(pd.isna(result.assisted.iloc[0]))
        self.assertEqual(result.ASSIST_STATUS.iloc[0], "unresolved_assister")

    def test_auxiliary_block_is_joined_to_the_shot_event(self):
        payload = self.payload([self.action(20, "Missed Shot", "1"),
                                self.action(20, "", "6", team=AWAY, description="Player BLOCK (1 BLK)")])
        df = pbp_v3_frame(payload, GAME, 2025)
        self.assertEqual(len(df[df.EVENTNUM == "20"]), 1)
        self.assertEqual(df[df.EVENTNUM == "20"].PLAYER3_ID.iloc[0], "6")


class ClientTests(unittest.TestCase):
    def test_invalid_success_response_not_cached_and_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            client = StatsClient(folder, retries=2, delay=0)
            client.session = Mock()
            invalid = Mock(); invalid.json.side_effect = ValueError("HTML not JSON")
            valid = Mock(); valid.json.return_value = {"ok": True}
            client.session.get.side_effect = [invalid, valid]
            payload = client.get("endpoint", {}, "sample", lambda p: p["ok"])
            self.assertEqual(payload, {"ok": True})
            self.assertEqual(client.session.get.call_count, 2)
            headers = client.session.get.call_args.kwargs["headers"]
            self.assertEqual(headers["Host"], "stats.wnba.com")
            self.assertIn("Chrome/131", headers["User-Agent"])
            client.offline = True
            client.refresh = True
            self.assertEqual(client.get("endpoint", {}, "sample", lambda p: p["ok"]), payload)
            self.assertEqual(client.session.get.call_count, 2)

    def test_cache_miss_offline_never_makes_network_request(self):
        with tempfile.TemporaryDirectory() as folder:
            client = StatsClient(folder, offline=True)
            client.session = Mock()
            with self.assertRaisesRegex(ValueError, "Offline cache miss"):
                client.get("endpoint", {}, "sample", lambda p: p)
            client.session.get.assert_not_called()


class PipelineTests(unittest.TestCase):
    def test_supplemental_boxscore_verifies_game_teams_and_totals(self):
        def team(tid, abbr):
            return dict(teamId=tid, teamTricode=abbr,
                        players=[dict(personId=1, firstName="A", familyName="B")],
                        statistics=dict(fieldGoalsAttempted=60, fieldGoalsMade=30, points=75))
        payload = dict(boxScoreTraditional=dict(gameId=GAME, homeTeam=team(HOME, "NYL"), awayTeam=team(AWAY, "LVA")))
        rows = boxscore_game_log(payload, GAME)
        self.assertEqual(rows.MATCHUP.tolist(), ["NYL vs. LVA", "LVA @ NYL"])
        self.assertEqual(rows.FGA.tolist(), [60, 60])
        with self.assertRaisesRegex(ValueError, "Wrong box-score"):
            boxscore_game_log(payload, "1022500002")
        payload['boxScoreTraditional']['awayTeam']['statistics']['fieldGoalsMade'] = 61
        with self.assertRaisesRegex(ValueError, "Invalid box-score"):
            boxscore_game_log(payload, GAME)

    def test_game_chart_can_retry_without_season_filters(self):
        calls = []
        def get(endpoint, params, key, validator):
            calls.append(params.copy())
            if params["Season"]:
                raise ValueError("Season index empty")
            df = pd.DataFrame([shot(), shot(11, "6", AWAY)])
            value = {"resultSets": [{"name": "Shot_Chart_Detail", "headers": list(df.columns), "rowSet": df.values.tolist()}]}
            validator(value)
            return value
        result, _ = fetch_shots(SimpleNamespace(get=get), GAME, 2025, False, None)
        self.assertEqual(len(result), 2)
        self.assertEqual(calls[1]["GameID"], GAME)
        self.assertEqual(calls[1]["SeasonType"], "")

    def test_team_season_fallback_refreshes_a_cache_missing_the_requested_game(self):
        calls = []
        def get(endpoint, params, key, validator, refresh=False):
            calls.append((key, refresh))
            if key == GAME:
                raise ValueError("Game-filtered endpoint unavailable")
            row = shot(team=str(params["TeamID"]))
            if not refresh:
                row["GAME_ID"] = "1022500002"
            df = pd.DataFrame([row])
            value = {"resultSets": [{"name": "Shot_Chart_Detail", "headers": list(df.columns), "rowSet": df.values.tolist()}]}
            validator(value)
            return value
        client = SimpleNamespace(get=get, offline=False)
        expected = pd.DataFrame([dict(TEAM_ID=HOME, FGA=1, FGM=1), dict(TEAM_ID=AWAY, FGA=1, FGM=1)])
        result, _ = fetch_shots(client, GAME, 2025, False, expected)
        self.assertEqual(set(result.GAME_ID), {GAME})
        self.assertEqual(set(result.TEAM_ID), {HOME, AWAY})
        self.assertEqual(sum(refresh for _, refresh in calls), 2)

    def test_cached_correction_wins_and_complete_run_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, output, cache = base / "input", base / "output", base / "cache"
            raw = pd.DataFrame([shot(20), shot(22, "11"), shot(30, "6", AWAY)])
            raw["LOC_X"] = 77
            (source / "team" / "2025").mkdir(parents=True)
            for team, df in raw.groupby("TEAM_ID"):
                df.to_csv(source / "team" / "2025" / f"{team}.csv", index=False)
            # Presence of this cache must cause auto mode to use the corrected
            # response even though the older local shot totals still match.
            (cache / "shotchartdetail").mkdir(parents=True)
            (cache / "shotchartdetail" / f"{GAME}.json").write_text("{}")
            corrected = raw.copy(); corrected["LOC_X"] = 99
            schedule = pd.DataFrame([dict(GAME_ID=GAME, TEAM_ID=HOME, TEAM_ABBREVIATION="NYL", FGA=2, FGM=2),
                                     dict(GAME_ID=GAME, TEAM_ID=AWAY, TEAM_ABBREVIATION="LVA", FGA=1, FGM=1)])
            pbp = pd.concat([complete_pbp(), pd.DataFrame([event(30, 1, "6", team=AWAY, time=600), event(99, 13, "0", team="0", period=4, time=24000)])], ignore_index=True)
            pbp["PCTIMESTRING"] = [f"{int(600-t/10)//60}:{int(600-t/10)%60:02d}" if p == 1 else "0:00" for t,p in zip(pbp.time,pbp.PERIOD)]
            def payload(name, df):
                return {"resultSets": [{"name": name, "headers": list(df.columns), "rowSet": df.values.tolist()}]}
            responses = {"leaguegamelog": payload("LeagueGameLog", schedule),
                         "shotchartdetail": payload("Shot_Chart_Detail", corrected),
                         "playbyplayv2": payload("PlayByPlay", pbp)}
            client = SimpleNamespace(cache=cache, host="fixture", requests=0, cache_hits=0)
            def get(endpoint, params, key, validator, **kwargs):
                value = responses[endpoint]; validator(value); return value
            client.get = get
            args = SimpleNamespace(output=output, input=source, offline=False, shots="auto", game_id=None,
                                   lineups="pbp", refresh=False, strict_shot_totals=False, rotation_failure_limit=1)
            first = run_season(args, client, 2025, False)
            self.assertEqual(first["status"], "complete", first)
            self.assertEqual(read_csv(output / "team" / "2025" / f"{HOME}.csv").LOC_X.tolist(), [99,99])
            second = run_season(args, client, 2025, False)
            self.assertEqual(second["totals"], first["totals"])
            self.assertEqual(len(pd.read_csv(output / "wplayer_index.csv")), 3)
            # Source disagreement is visible and preserves rows; strict mode
            # prevents a new season export instead of silently dropping a shot.
            schedule.loc[schedule.TEAM_ID == HOME, "FGA"] = 3
            responses["leaguegamelog"] = payload("LeagueGameLog", schedule)
            partial = run_season(args, client, 2025, False)
            self.assertEqual(partial["status"], "partial")
            self.assertEqual(set(read_csv(output / "team" / "2025" / f"{HOME}.csv").SHOT_DATA_STATUS), {"boxscore_mismatch"})
            args.output = base / "strict"; args.strict_shot_totals = True
            with self.assertLogs("wshot.pipeline", level="ERROR"):
                failed = run_season(args, client, 2025, False)
            self.assertEqual(failed["status"], "failed")
            self.assertFalse((args.output / "team").exists())


if __name__ == "__main__":
    unittest.main()
