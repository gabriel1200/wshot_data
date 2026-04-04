import time
import os
import pandas as pd
from nba_api.stats.endpoints import ShotChartDetail, leaguedashteamstats

# ---------------------------------------------------------
# 1. Apply your custom NBA API Headers / Patch
# ---------------------------------------------------------
NBA_STATS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Host": "stats.nba.com",
    "Origin": "https://www.nba.com",
    "Pragma": "no-cache",
    "Referer": "https://www.nba.com/",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

try:
    from nba_api.stats.library import http as stats_http
    from nba_api.library import http as base_http

    stats_http.STATS_HEADERS = NBA_STATS_HEADERS
    stats_http.NBAStatsHTTP.headers = NBA_STATS_HEADERS

    stats_http.NBAStatsHTTP._session = None
    base_http.NBAHTTP._session = None
except Exception as e:
    print(f"Warning: Could not patch nba_api headers: {e}")

# ---------------------------------------------------------
# 2. Setup Scraper Parameters
# ---------------------------------------------------------
WNBA_LEAGUE_ID = "10"
CURRENT_SEASON_TYPE = "Playoffs" # Toggle to 'Playoffs' when needed

start_year = 2000
end_year = 2025
seasons = [f"{year}-{str(year+1)[-2:]}" for year in range(start_year, end_year + 1)]

# ---------------------------------------------------------
# 3. Main Scraping Loop (Dynamic Fetching)
# ---------------------------------------------------------
def scrape_wnba_team_shotcharts_dynamic(seasons_list, season_type="Regular Season"):
    
    is_playoffs = (season_type == "Playoffs")
    folder_suffix = "ps" if is_playoffs else ""
    
    for season in seasons_list:
        print(f"\n==========================================")
        print(f"--- Scraping Season: {season} ({season_type}) ---")
        print(f"==========================================")
        
        base_year = season.split('-')[0]
        folder_name = f"{base_year}{folder_suffix}"
        
        target_dir = os.path.join("team", folder_name)
        os.makedirs(target_dir, exist_ok=True)
        
        avg_file_path = os.path.join(target_dir, "avg.csv")
        avg_saved = os.path.exists(avg_file_path)
        
        # STEP A: Ask the API exactly who played in this specific season
        try:
            team_stats = leaguedashteamstats.LeagueDashTeamStats(
                season=season,
                league_id_nullable=WNBA_LEAGUE_ID,
                season_type_all_star=season_type
            )
            active_teams_df = team_stats.get_data_frames()[0]
            
            if active_teams_df.empty:
                print(f"  [INFO] No WNBA teams found for {season}. Skipping year.")
                time.sleep(1)
                continue
                
            print(f"  -> Found {len(active_teams_df)} active franchises for {season}.")
            
        except Exception as e:
            print(f"  [ERROR] Failed to fetch team roster for {season}: {e}")
            time.sleep(2)
            continue
            
        # STEP B: Loop strictly through the teams that actually existed
        for index, row in active_teams_df.iterrows():
            team_id = row['TEAM_ID']
            team_name = row['TEAM_NAME']
            
            file_path = os.path.join(target_dir, f"{team_id}.csv")
            empty_marker = os.path.join(target_dir, f"{team_id}_empty.txt")
            
            team_data_exists = os.path.exists(file_path) or os.path.exists(empty_marker)
            
            # --- UPDATED: Only skip if BOTH the team data AND the avg data exist ---
            if team_data_exists and avg_saved:
                print(f"  [SKIP] {team_name}: Data already downloaded.")
                continue
            
            if team_data_exists and not avg_saved:
                print(f"  [FETCHING AVG] Re-pinging {team_name} just to recover missing avg.csv...")
            
            try:
                sc = ShotChartDetail(
                    team_id=team_id,
                    player_id=0,
                    context_measure_simple='FGA',
                    season_nullable=season,
                    season_type_all_star=season_type,
                    league_id=WNBA_LEAGUE_ID
                )
                
                dfs = sc.get_data_frames()
                team_shots_df = dfs[0] 
                league_avg_df = dfs[1]
                
                # Save League Averages once per season
                if not avg_saved and not league_avg_df.empty:
                    league_avg_df['SEASON'] = season
                    league_avg_df.to_csv(avg_file_path, index=False)
                    print(f"  [SUCCESS] Saved missing League Averages to {avg_file_path}")
                    avg_saved = True  
                
                # Only save the team data if we actually needed it
                if not team_data_exists:
                    if not team_shots_df.empty:
                        team_shots_df['SEASON'] = season 
                        team_shots_df.to_csv(file_path, index=False)
                        print(f"  [SUCCESS] {team_name}: Saved {len(team_shots_df)} shots.")
                    else:
                        with open(empty_marker, 'w') as f:
                            f.write("No data available for this season.")
                        print(f"  [EMPTY] {team_name} had a record but 0 shots found.")
                    
            except Exception as e:
                print(f"  [ERROR] Failed to fetch {team_name}: {e}")
            
            time.sleep(1.5) 

    print(f"\n{season_type} scrape sequence complete!")

if __name__ == "__main__":
    scrape_wnba_team_shotcharts_dynamic(seasons, season_type=CURRENT_SEASON_TYPE)