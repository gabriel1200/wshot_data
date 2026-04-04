import pandas as pd
import glob
import os

def check_for_playoff_bleed(year="1997"):
    folder_path = os.path.join("team", year)
    
    # Grab all the regular season CSVs for that year
    files = glob.glob(os.path.join(folder_path, "*.csv"))
    # Exclude the avg.csv so we are just looking at team shots
    files = [f for f in files if not f.endswith("avg.csv")]
    
    if not files:
        print(f"No regular season data found in {folder_path}")
        return

    # Combine them all into one dataframe
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    
    # Ensure GAME_ID is a zero-padded 10-digit string
    df['GAME_ID'] = df['GAME_ID'].astype(str).str.zfill(10)
    
    # Extract the 3-digit prefix
    df['PREFIX'] = df['GAME_ID'].str[:3]
    
    # See what we have!
    print(f"--- Data Audit for {year} Regular Season Pull ---")
    
    prefixes_found = df['PREFIX'].unique()
    
    for prefix in prefixes_found:
        count = len(df[df['PREFIX'] == prefix])
        if prefix == '102':
            print(f"Regular Season (102): {count} shots")
        elif prefix == '104':
            print(f"Playoffs (104): {count} shots found! They rolled over.")
        else:
            print(f"Unknown Prefix ({prefix}): {count} shots")
            
    # Also check the dates as a secondary confirmation
    # Early WNBA regular seasons ended in late August
    print(f"\nLatest Game Date in Dataset: {df['GAME_DATE'].max()}")

# Run it on 1997
check_for_playoff_bleed("1997")