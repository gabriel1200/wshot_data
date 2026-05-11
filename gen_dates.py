
import pandas as pd
import os
from collections import defaultdict

def generate_wgame_dates(base_path='team/'):
    data_rows = []
    # Keyed by (team_id, season) to handle relocations/rebranding
    seasonal_team_sets = defaultdict(list)

    # 1. Collect all game metadata
    subdirs = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d))]
    
    print("Reading files and collecting seasonal game sets...")
    for folder in subdirs:
        is_playoffs = folder.endswith('ps')
        year = folder.replace('ps', '')
        folder_path = os.path.join(base_path, folder)
        
        files = [f for f in os.listdir(folder_path) if f.endswith('.csv') and f != 'avg.csv']
        for file in files:
            team_id = int(file.replace('.csv', ''))
            try:
                df = pd.read_csv(os.path.join(folder_path, file))
                cols = ['GAME_ID', 'TEAM_ID', 'HTM', 'VTM', 'GAME_DATE']
                if not all(c in df.columns for c in cols):
                    continue
                
                unique_games = df[cols].drop_duplicates()
                for _, row in unique_games.iterrows():
                    data_rows.append({
                        'GAME_ID': row['GAME_ID'],
                        'TEAM_ID': row['TEAM_ID'],
                        'HTM': row['HTM'],
                        'VTM': row['VTM'],
                        'date': row['GAME_DATE'],
                        'season': year,
                        'playoffs': is_playoffs
                    })
                    # Track acronym sets per team per season
                    seasonal_team_sets[(team_id, year)].append({row['HTM'], row['VTM']})
            except Exception as e:
                print(f"Skipping {file} due to error: {e}")

    # 2. Infer Team Acronyms Per Season
    print("Inferring team acronyms per season...")
    seasonal_map = {}
    for (tid, year), game_sets in seasonal_team_sets.items():
        # Find acronym appearing in every game this team played this season
        common = set.intersection(*game_sets)
        
        if len(common) >= 1:
            # If intersection has multiple (e.g., only 1 game played), 
            # we'll resolve it in the final pass.
            seasonal_map[(tid, year)] = list(common)[0] if len(common) == 1 else common
        else:
            print(f"Warning: No common acronym for Team {tid} in {year}. Using ID.")
            seasonal_map[(tid, year)] = str(tid)

    # 3. Build Final DataFrame
    print("Building final wgame_dates.csv...")
    final_df = pd.DataFrame(data_rows)
    
    def resolve_team(row):
        val = seasonal_map.get((row['TEAM_ID'], row['season']))
        if isinstance(val, set):
            # If ambiguous (set of 2), the team acronym is the one that is NOT 
            # the opponent listed in HTM/VTM. 
            # We can't know for sure without a second game, so we pick the first
            # but usually, teams play more than one game.
            return list(val)[0]
        return val

    final_df['team'] = final_df.apply(resolve_team, axis=1)
    
    # Calculate opp_team
    final_df['opp_team'] = final_df.apply(
        lambda r: r['VTM'] if r['team'] == r['HTM'] else r['HTM'], axis=1
    )

    final_df.sort_values(by=['date', 'GAME_ID'], inplace=True)
    final_df.to_csv('wgame_dates.csv', index=False)
    print(f"Successfully created wgame_dates.csv with {len(final_df)} rows.")

if __name__ == "__main__":
    generate_wgame_dates()