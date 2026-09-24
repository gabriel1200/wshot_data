# WNBA shot-data pipeline

`wshot` builds team shots, opponent shots, player shots, rotation stints, assists,
and game/player/team indexes. It uses league `10` and calendar seasons (`2025`
means the 2025 WNBA season). Original scraped files remain in `team/` and `player/`;
candidate enriched output goes to `build/wnba/` by default.

## Run

Python 3.10+ on Linux/macOS:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m wshot --season 2025 2026 --season-type both
```

In the existing workspace, the tested interpreter is
`/home/gaber/basketball/web_app/venv/bin/python`.

The CLI works from the repository directory; input, cache, and output defaults
are resolved from the source location, not the current working directory. From
elsewhere, set `PYTHONPATH=/absolute/path/to/wshot_data` before `python -m wshot`.

Useful controls:

```bash
# Daily update: refresh game discovery, reuse validated completed-game caches.
python -m wshot --season 2026 --season-type both

# Historical backfill, including playoffs.
python -m wshot --season 2021 2022 2023 2024 --season-type both
python -m wshot --season 2016 2017 2018 2019 2020 --season-type both
python -m wshot --season 2005 2006 2007 2008 2009 2010 2011 2012 2013 2014 2015 --season-type both

# Test or repair one game in a separate candidate directory.
python -m wshot --season 2025 --game-id 1022500005 --refresh --output build/sample

# Rebuild from existing validated responses without networking.
python -m wshot --season 2025 2026 --season-type both --offline

# Re-fetch all source responses (slower); use for upstream corrections.
python -m wshot --season 2025 --refresh

# Require official rotation intervals, or explicitly use inferred PBP lineups.
python -m wshot --season 2025 --lineups api --output build/official-only
python -m wshot --season 2025 --lineups pbp --output build/pbp-only
```

`--shots auto` (default) checks local shot totals against the game log for each
team/game and fetches missing or mismatching games. `--shots live` fetches shot
charts for every game, using caches unless `--refresh` is supplied. `--shots local`
requires existing team CSVs and does not repair them. Only numeric team filenames
are read; `avg.csv` and `*vs.csv` cannot leak into the shot source.
Previously fetched game shot charts take precedence over older local team files,
so a targeted refresh is included in subsequent full builds.

`--season-type` accepts `regular`, `playoffs`, or `both`. `--game-id` can repeat,
but a sample must use one season/type and cannot overwrite season files containing
other games. `--input`, `--output`, and `--cache` accept absolute paths. Output
must differ from input so a candidate build cannot overwrite the original data.

## Sources and headers

The HTTP client imports `NBA_STATS_HEADERS` directly from `shotscrape.py`, so
header fixes there apply to this pipeline too. For `stats.wnba.com`, it adapts only
`Host`, `Origin`, and `Referer`. Use `--stats-host stats.nba.com` to try the NBA
host with the original header values. `--timeout`, `--retries` (maximum attempts),
and `--delay` bound requests and pacing; defaults are 30 seconds, 3, and 1.5 seconds.
In `--lineups auto`, `--rotation-failure-limit` (default 3) pauses uncached rotation
requests after repeated failures. The pipeline probes again every 50 games and
resumes if the endpoint recovers. Existing rotation caches remain usable.

Sources:

- `leaguegamelog`: discovers completed game logs, season-specific teams and
  abbreviations, and expected FGA/FGM. No static list of current franchises.
- `shotchartdetail`: shooter IDs/names, event IDs, coordinates, zones, outcomes.
- `gamerotation`: official `PERSON_ID` intervals in tenths of elapsed seconds.
- `playbyplayv2`: shot-event matching, assister IDs, and substitution evidence.
- `playbyplayv3` and `boxscoretraditionalv3`: fallback for games unavailable through
  V2. The game roster supplies native IDs for unambiguous secondary names.

The 2018 Las Vegas–Washington game `1021800162` was
[forfeited without play](https://www.wnba.com/news/las-vegas-aces-forfeit-game-vs-washington-mystics).
It remains in the raw game log and is listed in the report's `unplayed_games`,
but has no shot or game-date exports. The exception requires both expected teams
and zero minutes, attempts, makes, and points. Other zero-stat games are not
automatically excluded. `expected_games` counts games requiring shot coverage.

Locally scraped games absent from the league game log are verified against
`boxscoretraditionalv3` before inclusion, and listed in `supplemental_games`.
This recovered `1020700070`, omitted by the 2007 league index. Verified matchup
data also repairs inconsistent `HTM`/`VTM` labels while preserving `RAW_HTM`
and `RAW_VTM` on affected rows.

Live verification found the WNBA stats host usable with these headers. The NBA
stats host timed out in initial probes. WNBA CDN play-by-play returned HTML and
the NBA CDN returned 403, so the pipeline uses the verified stats PBP endpoint.
Endpoint availability can vary by game and run.
During the 2021–2024 backfill, switching to `--stats-host stats.nba.com`
recovered play-by-play requests that timed out on the WNBA host. Validated
caches are shared between hosts, so a host switch can resume existing work.

`--pbp-source auto` prefers cached/native-ID V2 and falls back to V3. After three
V2 failures it tries V3 first for uncached games, probing V2 every 50 games.
`--pbp-source v2` or `v3` explicitly selects one. V3's primary actors have native
IDs, but assists and incoming substitutes can appear only in descriptions. These
names are matched exactly to a unique native ID on the same team's official
game roster or in that same game's primary-actor records. Ambiguous names are
left unresolved; no fuzzy or cross-season matching is used. The roster also
covers participants who never attempted a shot or otherwise appeared as an actor.

If a game-filtered shot-chart request fails, the pipeline also tries the original
scraper's team-season query and selects the requested game from that response.

Raw successful JSON responses are cached under `.cache/wshot/{endpoint}/`.
Game-log discovery is refreshed on online runs. Responses must pass schema and
content checks before being cached. Timeouts, HTML responses, invalid JSON,
empty rotation data, and incomplete play-by-play are retried, never cached as
successful empty results. Existing valid caches survive unsuccessful refreshes,
but that run reports the failed refresh rather than silently using stale data.
JSON responses rejected by validation are retained separately under
`.cache/wshot/rejected/` for diagnosis; they are never used as successful cache hits.

Historical feeds occasionally label quarter-start markers and the opening
tip-off with the NBA-default `12:00`. For seasons with ten-minute quarters,
only the first marker in each regulation quarter and the tip-off immediately
after the opening marker may be assigned the proper period-start time. Raw
clocks are preserved and normalization is reported as a warning. All live
plays, including shots and later jump balls, retain strict clock validation.

The halves-era (1997–2005) shot charts have a distinct halftime encoding defect: some shots in
the first 3:20 of half two are labeled half one with 16:40 removed from the
remaining clock. The pipeline reconciles only this exact offset when native
game/event IDs, shooter, team, and outcome all match play-by-play. Corrected
rows use the PBP period and clock, retain `RAW_PERIOD`, `RAW_MINUTES_REMAINING`,
and `RAW_SECONDS_REMAINING`, and carry
`SHOT_CLOCK_STATUS=pbp_verified_halftime`. Other clocks remain unchanged;
unresolved discrepancies still prevent assist/lineup matching.

When a historical V2 feed omits its final period-end marker, it is accepted only
if its last event reaches 0:00 of regulation or overtime and its last score
exactly matches the game log's non-tied final score. This is reported explicitly;
a tied score or missing independent score remains invalid. A terminal period-end
marker with a stale nonzero clock can likewise be placed at the final horn only
when its non-tied score matches the game log; its raw clock is retained and the
correction reported. A live event before the horn does not satisfy either check.

Endpoint references: [ShotChartDetail](https://github.com/swar/nba_api/blob/master/docs/nba_api/stats/endpoints/shotchartdetail.md),
[GameRotation](https://github.com/swar/nba_api/blob/master/docs/nba_api/stats/endpoints/gamerotation.md),
[LeagueGameLog](https://github.com/swar/nba_api/blob/master/docs/nba_api/stats/endpoints/leaguegamelog.md).

## Outputs

Within the output directory, `{season}` is `2025`, `2025ps`, etc.:

| Path | Meaning |
| --- | --- |
| `team/{season}/{team_id}.csv` | Own shots, offensive and defensive lineups, assists |
| `team/{season}/{team_id}vs.csv` | Opponent shots, with the file's team as defense |
| `player/{season}/{player_id}.csv` | Shooter's attempts across all teams, including enrichment |
| `rotations/{season}/{team_id}.csv` | Official stints, or inferred stints if the official endpoint failed |
| `assists/{season}/ast.csv` | Assisted shots with scorer and assister IDs |
| `wgame_dates.csv` | One row per game/team, including season/type and opponent |
| `wplayer_index.csv`, `wplayer_index_ps.csv` | Player/team/season memberships derived from shots |
| `wteam_index.csv`, `wteam_index_ps.csv` | Teams with shots in each season |
| `reports/{season}.json` | Run status, game errors, coverage, and source information |
| `reports/{season}_shot_issues.csv` | Shot keys and quality flags requiring review |

`avg.csv` is retained from local data when using local charts; live chart responses
can supply league averages. This file is not the validation source for game totals.

IDs are digit strings; `GAME_ID` is 10 characters and `SHOT_ID` is game ID plus
event ID for compatibility. Read CSV identifier columns as strings. Joins use
explicit game/event/player/team identities. `time` is tenths of elapsed game time:
two 20-minute halves before 2006, four 10-minute quarters from 2006, and five-minute
overtimes. Higher overtimes are handled without special-case arithmetic.

### Lineup and assist fields

- `PLAYER_ID` always identifies the shooter.
- `PLAYERS_ON` contains exactly five unique IDs for the row's `TEAM_ID`, or is blank.
- `OPP_PLAYERS_ON` describes the opposing team's lineup.
- `LINEUP_STATUS` / `OPP_LINEUP_STATUS` explain missing or complete coverage.
- `LINEUP_SOURCE` / `OPP_LINEUP_SOURCE` distinguish `gamerotation` from
  `playbyplay_inferred` (V2) and `playbyplay_v3_inferred`.
- `ASSIST_ID` identifies the passer; `assisted` is 1/0 only for matched shot events.
  Missing or mismatched PBP leaves `assisted` blank, with an `ASSIST_STATUS`.
- `SHOT_DATA_STATUS` is `validated`, `unverified` (no schedule comparison), or
  `boxscore_mismatch` when the shot chart and game log disagree on FGA/FGM.
- `PBP_SOURCE` identifies V2/V3. V3 assists resolved through the game-name map
  have `ASSIST_STATUS=matched_name_resolved`; unresolved passers remain unknown.

In `*vs.csv`, `TEAM_ID` **and `TEAM_NAME`** describe the defending/file team.
`SHOOTING_TEAM_ID` and `SHOOTING_TEAM_NAME` preserve the attacking team;
`OPP_TEAM_ID` is the attacking team. `PLAYERS_ON` is defensive and
`OPP_PLAYERS_ON` offensive. This avoids the NBA scraper's mismatched team ID/name.

The official interval convention is `IN_TIME_REAL < time <= OUT_TIME_REAL`.
Whole-second shot clocks near a substitution are ambiguous; PBP event order can
resolve them. PBP fallback infers period starters only when five players can be
established from participation/substitution evidence, then checks every observed
transition. Unresolved periods stay blank. It does not substitute the shooter
for an unknown lineup. Official and inferred lineups that disagree away from a
clock boundary are flagged as conflicts. Inferred lineups are evidence-based
reconstructions, not an official source guarantee.

## Validation and reruns

Exit code 0 means complete enrichment (or no games); 2 means partial coverage or
failure. Check `reports/` before consuming a build. Partial enrichment may be
exported, with explicit missing fields. Missing/invalid shot games withhold season
exports completely. Existing candidate files may remain after a failed rerun;
the current report is authoritative and must say `complete` before normal use.
For an intentionally incomplete snapshot, `--allow-missing-games` exports available
games with partial status, `expected_games`, and an explicit `missing_games` list.

If fresh official shot charts still disagree with official game-log totals, the
pipeline preserves the chart rows, marks the game `boxscore_mismatch`, records
both teams' expected/actual counts, and returns partial status (exit 2). It does
not invent coordinates, drop shots, or silently adjust totals. Add
`--strict-shot-totals` to withhold season exports on these disagreements as well.

Writes replace individual files atomically, and a process lock prevents concurrent
writers to one output directory. A season is **not** a transactional multi-file
snapshot; do not consume its files while its report says `running` or `failed`.
Index updates replace only the selected season/type and retain other seasons.
Runs do not delete pre-existing player/team files, so removed upstream entities
can leave stale unindexed files; use a fresh output directory for clean snapshots.

Legacy `shotscrape.py`, `save_player_data.py`, and `gen_dates.py` remain available;
the new command replaces their role for enriched builds. Do not run legacy
processing scripts inside candidate output directories.

```bash
python -m unittest discover -s tests -v
```

Tests cover halves/quarters/overtime, identity and season validation, same-clock
substitutions, incomplete lineups, unknown assists, rotation conflicts, opponent
file semantics, box-score totals, cache validation, offline requests, and V3
secondary-name ambiguity and auxiliary events.
