"""Download the CSAS match data for local exploration."""

import argparse
import os
from pathlib import Path

from skillcorner_data import (
    EVENT_TABLES,
    SkillCornerHTTPError,
    connect_skillcorner,
    get_competition_editions,
    get_matches,
    save_match_file,
)


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
SETTINGS = {
    "SKILLCORNER_API_TOKEN", "SKILLCORNER_USERNAME", "SKILLCORNER_PASSWORD",
    "NWSL_EDITION_ID", "FAWSL_EDITION_ID",
}


def load_local_settings(path):
    """Read the literal KEY=value entries used by .env.example."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.strip().removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if key not in SETTINGS or os.getenv(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ[key] = value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-editions", action="store_true", help="Show accessible edition IDs.")
    parser.add_argument("--league", choices=("nwsl", "fawsl"), default="nwsl")
    parser.add_argument("--limit", type=int, help="Download at most this many matches.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive.")

    load_local_settings(PROJECT_DIR / ".env")
    use_token = bool(os.getenv("SKILLCORNER_API_TOKEN"))
    if not use_token and not all(
        os.getenv(key) for key in ("SKILLCORNER_USERNAME", "SKILLCORNER_PASSWORD")
    ):
        parser.error("Set SKILLCORNER_API_TOKEN or SKILLCORNER_USERNAME and SKILLCORNER_PASSWORD in .env.")

    if not args.list_editions:
        league_name = args.league.upper()
        edition_key = f"{league_name}_EDITION_ID"
        try:
            edition = int(os.environ[edition_key])
        except (KeyError, ValueError):
            parser.error(f"Set {edition_key} to the numeric edition ID.")

    with connect_skillcorner(use_token=use_token) as client:
        if args.list_editions:
            columns = ["id", "competition.name", "season.name"]
            print(get_competition_editions(client).reindex(columns=columns).to_string(index=False))
            return

        DATA_DIR.mkdir(mode=0o700, exist_ok=True)
        matches = get_matches(client, edition)
        if matches.empty:
            print(f"{league_name} edition {edition}: no matches listed.")
            return
        match_ids = matches["id"].drop_duplicates().tolist()[:args.limit]
        for index, match_id in enumerate(match_ids, start=1):
            folder = DATA_DIR / args.league / str(edition) / str(int(match_id))
            print(f"{league_name} match {match_id} ({index}/{len(match_ids)})", flush=True)
            files = {
                "metadata": "match.json",
                "tracking": "tracking.jsonl",
                **{table: f"{table}.csv" for table in EVENT_TABLES},
            }
            for dataset, filename in files.items():
                try:
                    path = save_match_file(client, match_id, dataset, folder / filename)
                except SkillCornerHTTPError as error:
                    if error.status_code not in {403, 404}:
                        raise
                    print(f"  {dataset} unavailable (HTTP {error.status_code}).")
                else:
                    if path is None:
                        print(f"  {dataset} returned an empty file.")


if __name__ == "__main__":
    main()
