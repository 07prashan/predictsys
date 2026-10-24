"""The world leagues' one-file-per-country loader: the new-format column layout is
normalized to the frame every model is keyed on, seasons are read from the file's own
labels, and a country's cup competition never leaks into its league.
Offline - the CSV is written to a temp folder and served from the cache, no network.

Run from the project root:  python -m unittest discover -s tests -t .
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import fetch_data  # noqa: E402

NEW_FORMAT_HEADER = "Country,League,Season,Date,Time,Home,Away,HG,AG,Res,PSCH,PSCD,PSCA"


def write_raw(tmp_dir: Path, code: str, rows: str, header: str = NEW_FORMAT_HEADER) -> Path:
    """The file exactly where fetch_world_league looks for its cache - a fresh copy is
    served from there without touching the network."""
    raw_dir = tmp_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{code}.csv"
    path.write_text(header + "\n" + rows, encoding="utf-8")
    return path


class WorldLeagueLoaderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(fetch_data, "RAW_DIR", self.tmp / "raw")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_new_format_columns_are_normalized_to_the_models_frame(self):
        write_raw(
            self.tmp,
            "BRA",
            "Brazil,Serie A,2026,12/07/2026,22:30,Flamengo RJ,Palmeiras,2,1,H,1.9,3.4,4.2\n"
            "Brazil,Serie A,2026,13/07/2026,20:00,Santos,Corinthians,0,0,D,2.4,3.1,3.2\n",
        )
        df = fetch_data.load_world_league("BRA")
        self.assertEqual(list(df.columns), ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "season"])
        row = df.iloc[0]
        self.assertEqual((row["HomeTeam"], row["AwayTeam"], row["FTHG"], row["FTAG"], row["FTR"]), ("Flamengo RJ", "Palmeiras", 2, 1, "H"))
        self.assertEqual(df["season"].tolist(), [2026, 2026])
        self.assertEqual(row["Date"].date().isoformat(), "2026-07-12")

    def test_both_season_label_styles_read_as_the_year_they_start_in(self):
        write_raw(
            self.tmp,
            "JPN",
            "Japan,J1 League,2026,01/03/2026,06:00,Urawa Reds,Gamba Osaka,1,0,H\n"
            "Japan,J1 League,2026/2027,02/03/2026,06:00,Kashima Antlers,Nagoya,2,2,D\n",
        )
        self.assertEqual(fetch_data.load_world_league("JPN")["season"].tolist(), [2026, 2026])

    def test_a_second_competition_in_the_same_file_stays_out(self):
        # Argentina's file carries the Copa de la Liga alongside the league - a cup result
        # must never enter the league's Elo or form history
        write_raw(
            self.tmp,
            "ARG",
            "Argentina,Liga Profesional,2026,01/03/2026,23:00,River Plate,Boca Juniors,1,0,H\n"
            "Argentina,Copa De La Liga Profesional,2026,05/03/2026,23:00,River Plate,Boca Juniors,0,3,A\n",
        )
        df = fetch_data.load_world_league("ARG")
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["FTR"], "H")

    def test_seasons_before_the_modeled_window_are_dropped(self):
        write_raw(
            self.tmp,
            "CHN",
            "China,Super League,2014,01/03/2014,06:00,Guangzhou,Shanghai,1,0,H\n"
            "China,Super League,2017,01/03/2017,06:00,Shandong,BEIJING,2,1,H\n",
        )
        self.assertEqual(fetch_data.load_world_league("CHN")["season"].tolist(), [2017])

    def test_every_world_league_is_in_the_all_leagues_catalog(self):
        for code, (name, country, _league_in_csv) in fetch_data.WORLD_LEAGUES.items():
            self.assertEqual(fetch_data.ALL_LEAGUES[code], f"{name} ({country})")
        self.assertIn("E0", fetch_data.ALL_LEAGUES)  # the European leagues come along too
        self.assertEqual(len(fetch_data.ALL_LEAGUES), len(fetch_data.LEAGUES) + len(fetch_data.WORLD_LEAGUES))

    def test_an_unavailable_country_returns_an_empty_frame_not_a_crash(self):
        with mock.patch.object(fetch_data, "fetch_world_league", return_value=None):
            df = fetch_data.load_world_league("BRA")
        self.assertTrue(df.empty)
        self.assertEqual(list(df.columns), ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "season"])


if __name__ == "__main__":
    unittest.main()
