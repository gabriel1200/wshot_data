"""Pure transforms. Unknown coverage never becomes an invented lineup or assist."""
import math
import re

import pandas as pd


def identifier(value, width=0):
    if pd.isna(value):
        raise ValueError("Missing identifier")
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if not text.isdigit():
        raise ValueError(f"Invalid identifier: {value!r}")
    return text.zfill(width)


def period_length(year, period):
    periods, seconds = (2, 1200) if year < 2006 else (4, 600)
    return seconds if period <= periods else 300


def elapsed(year, period, minutes, seconds):
    period = int(period)
    remaining = float(minutes) * 60 + float(seconds)
    duration = period_length(year, period)
    if period < 1 or not math.isfinite(remaining) or not 0 <= remaining <= duration:
        raise ValueError(f"Invalid {year} period {period} clock {minutes}:{seconds}")
    start = sum(period_length(year, p) for p in range(1, period))
    return round((start + duration - remaining) * 10)


def normalize_shots(frame, year, playoffs):
    required = {"GAME_ID", "GAME_EVENT_ID", "PLAYER_ID", "PLAYER_NAME", "TEAM_ID",
                "TEAM_NAME", "PERIOD", "MINUTES_REMAINING", "SECONDS_REMAINING",
                "SHOT_MADE_FLAG", "GAME_DATE", "HTM", "VTM"}
    if not required.issubset(frame):
        raise ValueError(f"Missing shot columns: {sorted(required - set(frame))}")
    df = frame.copy()
    for col in ["GAME_ID", "GAME_EVENT_ID", "PLAYER_ID", "TEAM_ID"]:
        df[col] = df[col].map(lambda v: identifier(v, 10 if col == "GAME_ID" else 0))
    prefix = ("104" if playoffs else "102") + str(year)[-2:]
    if not df.GAME_ID.str.startswith(prefix).all() or not df.GAME_ID.str.len().eq(10).all():
        raise ValueError(f"Wrong league/season/type: expected game IDs starting {prefix}")
    df["SHOT_ID"] = df.GAME_ID + df.GAME_EVENT_ID
    df["SEASON"] = str(year)
    if df.duplicated(["GAME_ID", "GAME_EVENT_ID"]).any():
        raise ValueError("Duplicate shot events")
    df["time"] = [elapsed(year, p, m, s) for p, m, s in
                  zip(df.PERIOD, df.MINUTES_REMAINING, df.SECONDS_REMAINING)]
    return df


def pbp_frame(frame, game, year, expected_score=None):
    required = {"GAME_ID", "EVENTNUM", "EVENTMSGTYPE", "PERIOD", "PCTIMESTRING",
                "PLAYER1_ID", "PLAYER1_TEAM_ID", "PLAYER2_ID", "PLAYER2_TEAM_ID"}
    if frame.empty or not required.issubset(frame):
        raise ValueError("Missing play-by-play rows/columns")
    # Some feeds repeat an identical event verbatim. Remove only exact copies;
    # conflicting versions of the same event remain an error.
    df = frame.drop_duplicates().copy()
    df.attrs["duplicate_rows_removed"] = len(frame) - len(df)
    df["GAME_ID"] = df.GAME_ID.map(lambda v: identifier(v, 10))
    df["EVENTNUM"] = df.EVENTNUM.map(identifier)
    if set(df.GAME_ID) != {game} or df.EVENTNUM.duplicated().any():
        raise ValueError("Wrong game or duplicate play-by-play events")
    for col in [c for c in df if re.fullmatch(r"PLAYER[123]_(ID|TEAM_ID)", c)]:
        df[col] = df[col].map(lambda v: identifier(v) if pd.notna(v) else "0")
    clocks = df.PCTIMESTRING.astype(str).tolist()
    # Historical feeds sometimes initialize quarter-start markers and the
    # opening tip-off with the NBA's 12:00 default. Their event semantics fix
    # them at the period start. Preserve raw clocks; never adjust live plays.
    seen_periods, normalized = set(), 0
    for i, row in enumerate(df.itertuples()):
        first_marker = row.PERIOD not in seen_periods and row.EVENTMSGTYPE == 12
        opening_tip = (i == 1 and row.PERIOD == 1 and row.EVENTMSGTYPE == 10
                       and df.iloc[0].PERIOD == 1 and df.iloc[0].EVENTMSGTYPE == 12
                       and re.fullmatch(r"12:00(?:\.0+)?", str(df.iloc[0].PCTIMESTRING)))
        if (year >= 2006 and 1 <= row.PERIOD <= 4
                and re.fullmatch(r"12:00(?:\.0+)?", clocks[i])
                and (first_marker or opening_tip)):
            clocks[i] = "10:00"
            normalized += 1
        seen_periods.add(row.PERIOD)
    df.attrs["period_start_clocks_normalized"] = normalized
    df["time"] = [elapsed(year, p, *clock.split(":")) for p, clock in zip(df.PERIOD, clocks)]
    regulation = 2 if year < 2006 else 4
    last_period = int(df.PERIOD.max())
    ends = df[(df.EVENTMSGTYPE == 13) & (df.PERIOD == last_period)]
    if last_period < regulation or ends.empty or ends.time.iloc[-1] != elapsed(year, last_period, 0, 0):
        score = None
        if "SCORE" in df:
            scored = df.SCORE.dropna().astype(str)
            if not scored.empty:
                match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", scored.iloc[-1])
                if match:
                    score = tuple(map(int, match.groups()))
        at_horn = last_period >= regulation and int(df.PERIOD.iloc[-1]) == last_period and df.time.iloc[-1] == elapsed(year, last_period, 0, 0)
        verified_score = expected_score is not None and expected_score[0] != expected_score[1] and score == expected_score
        terminal_marker = last_period >= regulation and int(df.PERIOD.iloc[-1]) == last_period and int(df.EVENTMSGTYPE.iloc[-1]) == 13
        if terminal_marker and verified_score:
            df.loc[df.index[-1], "time"] = elapsed(year, last_period, 0, 0)
            df.attrs["terminal_marker_score_verified"] = True
        elif ends.empty and at_horn and verified_score:
            df.attrs["final_horn_score_verified"] = True
        else:
            raise ValueError("Play-by-play does not contain a completed final period")
    df["SHOT_ID"] = df.GAME_ID + df.EVENTNUM
    if "PBP_SOURCE" not in df:
        df["PBP_SOURCE"] = "playbyplayv2"
    return df


def game_roster(payload, game):
    data = payload["boxScoreTraditional"]
    if identifier(data["gameId"], 10) != game:
        raise ValueError("Wrong box-score game")
    roster = []
    for side in ("homeTeam", "awayTeam"):
        team = data[side]
        if not team.get("players"):
            raise ValueError("Empty game roster")
        for p in team["players"]:
            first, last = p.get("firstName", "").strip(), p.get("familyName", "").strip()
            roster.append((identifier(team["teamId"]), identifier(p["personId"]),
                           [first, last, f"{first} {last}", f"{last} {first}", p.get("nameI", "")]))
    return roster


def pbp_v3_frame(payload, game, year, roster=None):
    """Adapt V3 without fuzzy or cross-game name matching.

    Primary actor IDs are native. Secondary names must identify exactly one
    native actor ID for this team in this game's own feed; ambiguous names stay
    unresolved. Auxiliary block/steal rows share the original event number.
    """
    data = payload["game"]
    if identifier(data["gameId"], 10) != game or not data.get("actions"):
        raise ValueError("Wrong/empty V3 game")
    actions = data["actions"]
    names = {}
    for team, person, aliases in roster or []:
        for name in aliases:
            if name.strip():
                names.setdefault((team, name.strip()), set()).add(person)
    for action in actions:
        team, person = identifier(action.get("teamId", 0)), identifier(action.get("personId", 0))
        if team == "0" or person == "0" or int(person) >= 1_000_000_000:
            continue
        for key in ("playerName", "playerNameI"):
            name = action.get(key, "").strip()
            if name:
                names.setdefault((team, name), set()).add(person)
    def resolve(team, name):
        found = names.get((team, name.strip()), set())
        return next(iter(found)) if len(found) == 1 else "0"
    kinds = {"made shot": 1, "missed shot": 2, "free throw": 3, "rebound": 4,
             "turnover": 5, "foul": 6, "violation": 7, "substitution": 8,
             "timeout": 9, "jump ball": 10, "ejection": 11, "instant replay": 18}
    auxiliary = {}
    for action in actions:
        if not action.get("actionType"):
            auxiliary.setdefault(str(action["actionNumber"]), []).append(action)
    rows = []
    unresolved = 0
    for action in actions:
        kind = action.get("actionType", "").lower()
        if not kind:
            continue
        msg = kinds.get(kind, 0)
        if kind == "period":
            msg = 12 if action["subType"] == "start" else 13
        clock = re.fullmatch(r"PT(\d+)M([\d.]+)S", action["clock"])
        if not clock:
            raise ValueError("Unrecognized V3 clock")
        team, person = identifier(action.get("teamId", 0)), identifier(action.get("personId", 0))
        event_num = identifier(action["actionNumber"])
        description = action.get("description", "")
        row = dict(GAME_ID=game, EVENTNUM=event_num, EVENTMSGTYPE=msg,
                   EVENTMSGACTIONTYPE=0, PERIOD=action["period"],
                   PCTIMESTRING=f"{clock[1]}:{clock[2]}", NEUTRALDESCRIPTION=description,
                   PLAYER1_ID=person, PLAYER1_TEAM_ID=team,
                   PLAYER1_TEAM_ABBREVIATION=action.get("teamTricode", ""),
                   PLAYER2_ID="0", PLAYER2_TEAM_ID="0", PLAYER3_ID="0", PLAYER3_TEAM_ID="0",
                   ASSIST_UNRESOLVED=False, PBP_SOURCE="playbyplayv3")
        if msg == 8:
            match = re.fullmatch(r"SUB: (.+) FOR (.+)", description)
            incoming = resolve(team, match[1]) if match else "0"
            row.update(PLAYER2_ID=incoming, PLAYER2_TEAM_ID=team)
            unresolved += incoming == "0"
        if msg == 1 and " AST)" in description:
            match = re.search(r"\(([^()]+) \d+ AST\)", description)
            assister = resolve(team, match[1]) if match else "0"
            row.update(PLAYER2_ID=assister, PLAYER2_TEAM_ID=team,
                       ASSIST_UNRESOLVED=assister == "0")
            unresolved += assister == "0"
        for other in auxiliary.get(event_num, []):
            text = other.get("description", "")
            slot = 3 if msg == 2 and " BLOCK " in text else (2 if msg == 5 and " STEAL " in text else None)
            if slot:
                row[f"PLAYER{slot}_ID"] = identifier(other.get("personId", 0))
                row[f"PLAYER{slot}_TEAM_ID"] = identifier(other.get("teamId", 0))
        rows.append(row)
    df = pbp_frame(pd.DataFrame(rows), game, year)
    df.attrs["unresolved_secondary_names"] = unresolved
    return df


def rotation_frame(frame, game, team_ids):
    required = {"GAME_ID", "TEAM_ID", "PERSON_ID", "IN_TIME_REAL", "OUT_TIME_REAL"}
    if frame.empty or not required.issubset(frame):
        raise ValueError("Missing rotation rows/columns")
    df = frame.copy()
    for col in ["GAME_ID", "TEAM_ID", "PERSON_ID"]:
        df[col] = df[col].map(lambda v: identifier(v, 10 if col == "GAME_ID" else 0))
    if set(df.GAME_ID) != {game} or set(df.TEAM_ID) != set(team_ids):
        raise ValueError("Rotation game/team mismatch")
    for col in ["IN_TIME_REAL", "OUT_TIME_REAL"]:
        df[col] = pd.to_numeric(df[col], errors="raise")
    if not (df.IN_TIME_REAL.ge(0) & df.OUT_TIME_REAL.gt(df.IN_TIME_REAL)).all():
        raise ValueError("Invalid rotation intervals")
    if not df[["IN_TIME_REAL", "OUT_TIME_REAL"]].applymap(math.isfinite).all().all():
        raise ValueError("Nonfinite rotation intervals")
    if df.duplicated(["TEAM_ID", "PERSON_ID", "IN_TIME_REAL", "OUT_TIME_REAL"]).any():
        raise ValueError("Duplicate rotation intervals")
    df["SOURCE"] = "gamerotation"
    return df


def actors(row):
    """Only events that establish on-court participation; exclude bench fouls/techs."""
    kind = int(row["EVENTMSGTYPE"])
    slots = {1: (1, 2), 2: (1, 3), 4: (1,), 5: (1, 2), 10: (1, 2, 3)}.get(kind, ())
    if kind == 3 and "technical" not in str(row).lower():
        slots = (1,)
    for n in slots:
        player, team = row.get(f"PLAYER{n}_ID", "0"), row.get(f"PLAYER{n}_TEAM_ID", "0")
        if player != "0" and team != "0" and int(player) < 1_000_000_000:
            yield team, player


def infer_lineups(pbp, year, team_ids):
    """Infer each period independently from participation and ordered substitutions.

    If starters or any transition cannot be established, discard that team's
    entire period. Event order, not clock alone, resolves same-clock substitutions.
    """
    snapshots, stints, issues = {}, [], []
    source = "playbyplay_v3_inferred" if "PBP_SOURCE" in pbp and pbp.PBP_SOURCE.iloc[0] == "playbyplayv3" else "playbyplay_inferred"
    for period, group in pbp.groupby("PERIOD", sort=False):
        rows = group.to_dict("records")
        for team in team_ids:
            entered, starters = set(), set()
            for row in rows:
                if int(row["EVENTMSGTYPE"]) == 8 and row["PLAYER1_TEAM_ID"] == team:
                    if row["PLAYER1_ID"] not in entered:
                        starters.add(row["PLAYER1_ID"])
                    entered.add(row["PLAYER2_ID"])
                for actor_team, player in actors(row):
                    if actor_team == team and player not in entered:
                        starters.add(player)
            if len(starters) != 5:
                issues.append(f"{team} period {period}: {len(starters)} inferred starters")
                continue
            lineup = starters.copy()
            start = elapsed(year, period, period_length(year, period) / 60, 0)
            opened = {player: start for player in lineup}
            period_stints, period_shots = [], {}
            valid = True
            for row in rows:
                if any(t == team and p not in lineup for t, p in actors(row)):
                    valid = False
                    break
                if int(row["EVENTMSGTYPE"]) == 8 and row["PLAYER1_TEAM_ID"] == team:
                    out, incoming = row["PLAYER1_ID"], row["PLAYER2_ID"]
                    if out not in lineup or incoming in lineup or incoming == "0" or row["PLAYER2_TEAM_ID"] != team:
                        valid = False
                        break
                    period_stints.append((out, opened.pop(out), row["time"]))
                    lineup.remove(out)
                    lineup.add(incoming)
                    opened[incoming] = row["time"]
                if int(row["EVENTMSGTYPE"]) in (1, 2):
                    period_shots[(row["SHOT_ID"], team)] = "|".join(sorted(lineup, key=int))
            if not valid:
                issues.append(f"{team} period {period}: inconsistent participation/substitutions")
                continue
            end = elapsed(year, period, 0, 0)
            period_stints.extend((p, begin, end) for p, begin in opened.items())
            snapshots.update(period_shots)
            stints.extend({"GAME_ID": pbp.GAME_ID.iloc[0], "TEAM_ID": team,
                           "PERSON_ID": p, "IN_TIME_REAL": begin, "OUT_TIME_REAL": finish,
                           "PERIOD": int(period), "SOURCE": source}
                          for p, begin, finish in period_stints if finish > begin)
    return snapshots, pd.DataFrame(stints), issues


def reconcile_shot_clocks(shots, pbp, year):
    """Repair the halves-era (pre-2006) halftime encoding defect using native event identity."""
    df = shots.copy()
    if year >= 2006:
        return df
    for col in ("PERIOD", "MINUTES_REMAINING", "SECONDS_REMAINING"):
        if "RAW_" + col not in df:
            df["RAW_" + col] = df[col]
    if "SHOT_CLOCK_STATUS" not in df:
        df["SHOT_CLOCK_STATUS"] = "source"
    if pbp is None:
        return df
    events = pbp.set_index("SHOT_ID").to_dict("index")
    for idx, row in df.iterrows():
        e = events.get(row.SHOT_ID)
        if (e is None or e["PLAYER1_ID"] != row.PLAYER_ID
                or e["PLAYER1_TEAM_ID"] != row.TEAM_ID
                or int(e["EVENTMSGTYPE"]) != (1 if row.SHOT_MADE_FLAG else 2)
                or int(row.PERIOD) != 1 or int(e["PERIOD"]) != 2):
            continue
        remaining = float(row.MINUTES_REMAINING) * 60 + float(row.SECONDS_REMAINING)
        # The affected first 3:20 of half two has 16:40 subtracted from its
        # remaining clock and is mislabeled half one. No near-match tolerance.
        if 0 <= remaining <= 200 and e["time"] - row.time == 2000:
            minutes, seconds = str(e["PCTIMESTRING"]).split(":")
            if float(minutes) * 60 + float(seconds) != remaining + 1000:
                continue
            df.loc[idx, ["PERIOD", "MINUTES_REMAINING", "SECONDS_REMAINING", "time"]] = [2, int(minutes), float(seconds), e["time"]]
            df.loc[idx, "SHOT_CLOCK_STATUS"] = "pbp_verified_halftime"
    return df


def attach_game(shots, pbp, rotations, year, team_ids, allow_inference=True):
    df = reconcile_shot_clocks(shots, pbp, year)
    events = {} if pbp is None else pbp.set_index("SHOT_ID").to_dict("index")
    inferred, inferred_rotations, issues = ({}, pd.DataFrame(), [])
    if pbp is not None and allow_inference:
        inferred, inferred_rotations, issues = infer_lineups(pbp, year, team_ids)
    if "SHOT_CLOCK_STATUS" in df:
        corrected = int(df.SHOT_CLOCK_STATUS.eq("pbp_verified_halftime").sum())
        if corrected:
            issues.append(f"Reconciled {corrected} halftime shot clocks against exact PBP event/player/team/outcome identities; raw clocks preserved")
    values = []
    for row in df.to_dict("records"):
        event = events.get(row["SHOT_ID"])
        matched = event is not None and event["PLAYER1_ID"] == row["PLAYER_ID"] and event["PLAYER1_TEAM_ID"] == row["TEAM_ID"] and int(event["EVENTMSGTYPE"]) == (1 if int(row["SHOT_MADE_FLAG"]) else 2) and int(event["PERIOD"]) == int(row["PERIOD"]) and abs(event["time"] - row["time"]) <= 10
        assist = None
        assisted = None
        assist_status = "missing_pbp" if pbp is None else "event_mismatch"
        if matched:
            assist = event["PLAYER2_ID"] if int(event["EVENTMSGTYPE"]) == 1 and event["PLAYER2_ID"] != "0" and event["PLAYER2_TEAM_ID"] == row["TEAM_ID"] else None
            assisted = int(assist is not None)
            assist_status = "matched"
            if event.get("ASSIST_UNRESOLVED", False):
                assisted, assist_status = None, "unresolved_assister"
            elif assist and event.get("PBP_SOURCE") == "playbyplayv3":
                assist_status = "matched_name_resolved"
        result = {"ASSIST_ID": assist, "assisted": assisted, "ASSIST_STATUS": assist_status,
                  "PBP_SOURCE": event.get("PBP_SOURCE", "playbyplayv2") if event is not None else None}
        opponent = next((t for t in team_ids if t != row["TEAM_ID"]), None)
        result["OPP_TEAM_ID"] = opponent
        for team, prefix in [(row["TEAM_ID"], ""), (opponent, "OPP_")]:
            lineup, status, source = None, "missing_rotation", None
            if rotations is not None:
                active = rotations[(rotations.TEAM_ID == team) & (rotations.IN_TIME_REAL < row["time"]) & (rotations.OUT_TIME_REAL >= row["time"])]
                persons = active.PERSON_ID.tolist()
                status = "invalid_count"
                if len(persons) == len(set(persons)) == 5:
                    lineup = "|".join(sorted(persons, key=int))
                    status, source = "complete", "gamerotation"
                    if not prefix and row["PLAYER_ID"] not in persons:
                        lineup, status, source = None, "shooter_missing", None
                    if not prefix and assist is not None and assist not in persons:
                        lineup, status, source = None, "assister_missing", None
                    # At an exact substitution clock, use event order if available.
                    # Shot-chart clocks have whole-second precision; rotations
                    # include tenths. Treat the surrounding second as ambiguous.
                    boundary = rotations[(rotations.TEAM_ID == team) & ((rotations.IN_TIME_REAL.sub(row["time"]).abs() < 10) | (rotations.OUT_TIME_REAL.sub(row["time"]).abs() < 10))]
                    if not boundary.empty:
                        lineup, status, source = None, "clock_boundary", None
            evidence = inferred.get((row["SHOT_ID"], team)) if matched else None
            if lineup is not None and evidence is not None and lineup != evidence:
                lineup, status, source = None, "lineup_conflict", None
            if lineup is None and status != "lineup_conflict" and evidence is not None:
                source = "playbyplay_v3_inferred" if event.get("PBP_SOURCE") == "playbyplayv3" else "playbyplay_inferred"
                lineup, status = inferred[(row["SHOT_ID"], team)], "complete"
            result[prefix + "PLAYERS_ON"] = lineup
            result[prefix + "LINEUP_STATUS"] = status
            result[prefix + "LINEUP_SOURCE"] = source
        values.append(result)
    # Drop previously derived columns when rebuilding existing enriched files.
    extra = pd.DataFrame(values, index=df.index)
    df = df.drop(columns=[c for c in extra if c in df]).join(extra)
    df["assisted"] = df["assisted"].astype("Int64")
    return df, inferred_rotations, issues
