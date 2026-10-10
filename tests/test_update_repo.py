import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from update_repo import promote


class RepositoryUpdateTests(unittest.TestCase):
    def fixture(self, root, status="complete", games=1, expected=1):
        build = root / "build"
        (build / "reports").mkdir(parents=True)
        (build / "reports/2026.json").write_text(json.dumps({
            "status": status, "season": 2026, "playoffs": False,
            "totals": {"games": games, "expected_games": expected},
        }))
        for category in ("team", "player", "assists"):
            path = Path(category) / "2026" / "1.csv"
            (build / path).parent.mkdir(parents=True)
            (build / path).write_text("fresh\n")
            (root / path).parent.mkdir(parents=True)
            (root / path).write_text("previous\n")
        for name in ("wteam_index.csv", "wplayer_index.csv"):
            (root / name).write_text("year,value\n2025,keep\n2026,old\n")
            (build / name).write_text("year,value\n2026,new\n")
        (root / "wgame_dates.csv").write_text(
            "season,playoffs,GAME_ID\n2025,False,1022500001\n2026,True,1042600001\n2026,False,1022600001\n")
        (build / "wgame_dates.csv").write_text(
            "season,playoffs,GAME_ID\n2026,False,1022600002\n")
        return build

    def test_promotion_preserves_other_seasons_and_backs_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = self.fixture(root)
            promote(root, build, 2026, False)
            dates = pd.read_csv(root / "wgame_dates.csv", dtype=str)
            self.assertEqual(set(dates.GAME_ID), {"1022500001", "1042600001", "1022600002"})
            self.assertEqual(pd.read_csv(root / "wteam_index.csv").value.tolist(), ["keep", "new"])
            self.assertEqual((root / "team/2026/1.csv").read_text(), "fresh\n")
            self.assertEqual((build / "backup/team/2026/1.csv").read_text(), "previous\n")

    def test_partial_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = self.fixture(root, status="partial")
            with self.assertRaises(ValueError):
                promote(root, build, 2026, False)
            self.assertEqual((root / "team/2026/1.csv").read_text(), "previous\n")
            promote(root, build, 2026, False, allow_partial=True)
            self.assertEqual((root / "team/2026/1.csv").read_text(), "fresh\n")

    def test_missing_games_never_promoted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = self.fixture(root, status="partial", expected=2)
            with self.assertRaises(ValueError):
                promote(root, build, 2026, False, allow_partial=True)
            self.assertEqual((root / "team/2026/1.csv").read_text(), "previous\n")


if __name__ == "__main__":
    unittest.main()
