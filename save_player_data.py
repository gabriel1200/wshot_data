import os
import glob
import pandas as pd

def process_local_shot_data(base_team_dir="team", base_player_dir="player", target_folders=None):
    """
    Reads local team shot charts and generates player-specific shot charts.
    Can process all history, or target specific seasons for daily updates.
    Now includes team_id in the player indices for better relational mapping.
    """
    if not os.path.exists(base_team_dir):
        print(f"Error: Could not find '{base_team_dir}'. Please run the scraper first.")
        return

    # Identify all available year directories
    all_folders = [d for d in os.listdir(base_team_dir) if os.path.isdir(os.path.join(base_team_dir, d))]
    
    # Filter for targeted folders if requested
    if target_folders:
        if isinstance(target_folders, str):
            target_folders = [target_folders]
        folders = [f for f in all_folders if f in target_folders]
        print(f"Targeted Update Mode: Processing {len(folders)} specific folder(s): {folders}")
    else:
        folders = all_folders
        print(f"Full Run Mode: Processing all {len(folders)} historical folders...")

    # --- Helper to load existing indices ---
    def load_existing_index(filename):
        if os.path.exists(filename):
            # Read CSV and ensure 'year' is treated as a string for exact matching
            return pd.read_csv(filename, dtype={'year': str}).to_dict('records')
        return []

    # Load existing indices
    team_index_rs = load_existing_index("team_index.csv")
    player_index_rs = load_existing_index("player_index.csv")
    team_index_ps = load_existing_index("team_index_ps.csv")
    player_index_ps = load_existing_index("player_index_ps.csv")

    # --- CRITICAL: Wipe out existing data for the targeted folders ---
    if target_folders:
        team_index_rs = [r for r in team_index_rs if r['year'] not in target_folders]
        player_index_rs = [r for r in player_index_rs if r['year'] not in target_folders]
        team_index_ps = [r for r in team_index_ps if r['year'] not in target_folders]
        player_index_ps = [r for r in player_index_ps if r['year'] not in target_folders]
    else:
        # If doing a full run, just start completely fresh
        team_index_rs, player_index_rs, team_index_ps, player_index_ps = [], [], [], []

    # ---------------------------------------------------------
    # Main Processing Loop
    # ---------------------------------------------------------
    for folder in sorted(folders):
        is_playoffs = folder.endswith('ps')
        segment_name = "Playoffs" if is_playoffs else "Regular Season"
        
        print(f"\n--- Processing {folder} ({segment_name}) ---")
        
        year_dir = os.path.join(base_team_dir, folder)
        all_csvs = glob.glob(os.path.join(year_dir, "*.csv"))
        team_files = [f for f in all_csvs if not f.endswith("avg.csv")]
        
        if not team_files:
            print(f"  No valid team CSV files found in {year_dir}. Skipping.")
            continue
            
        year_dfs = []
        
        for file in team_files:
            try:
                df = pd.read_csv(file)
                if not df.empty:
                    year_dfs.append(df)
                    
                    record = {
                        'team_name': df['TEAM_NAME'].iloc[0],
                        'team_id': df['TEAM_ID'].iloc[0],
                        'year': str(folder)
                    }
                    
                    if is_playoffs:
                        team_index_ps.append(record)
                    else:
                        team_index_rs.append(record)
                        
            except Exception as e:
                print(f"  [ERROR] Failed to read {file}: {e}")
        
        if not year_dfs:
            continue
            
        segment_shots_df = pd.concat(year_dfs, ignore_index=True)
        player_year_dir = os.path.join(base_player_dir, folder)
        os.makedirs(player_year_dir, exist_ok=True)
        
        grouped_players = segment_shots_df.groupby('PLAYER_ID')
        print(f"  Found {len(grouped_players)} unique players. Saving files...")
        
        for player_id, player_shots in grouped_players:
            player_name = player_shots['PLAYER_NAME'].iloc[0]
            
            # --- UPDATED: Catch every team the player took a shot for ---
            unique_teams = player_shots['TEAM_ID'].unique()
            
            for t_id in unique_teams:
                record = {
                    'player_name': player_name,
                    'player_id': player_id,
                    'team_id': t_id,
                    'year': str(folder)
                }
                
                if is_playoffs:
                    player_index_ps.append(record)
                else:
                    player_index_rs.append(record)
            
            # Save all the player's shots for the year into their single CSV
            player_file_path = os.path.join(player_year_dir, f"{player_id}.csv")
            player_shots.to_csv(player_file_path, index=False)

    # ---------------------------------------------------------
    # Create and export the master indices
    # ---------------------------------------------------------
    print("\n--- Generating Master Indices ---")
    
    def export_index(data_list, filename, sort_cols):
        if not data_list:
            print(f" - {filename}: No data generated.")
            return
            
        df = pd.DataFrame(data_list).drop_duplicates().sort_values(by=sort_cols)
        df.to_csv(filename, index=False)
        print(f" - {filename} ({len(df)} rows)")

    export_index(team_index_rs, "wteam_index.csv", ['year', 'team_name'])
    export_index(team_index_ps, "wteam_index_ps.csv", ['year', 'team_name'])
    
    # Updated sort to group multi-team players neatly
    export_index(player_index_rs, "wplayer_index.csv", ['year', 'player_name', 'team_id'])
    export_index(player_index_ps, "wplayer_index_ps.csv", ['year', 'player_name', 'team_id'])
    
    print("\nData processing complete!")

if __name__ == "__main__":
    process_local_shot_data()