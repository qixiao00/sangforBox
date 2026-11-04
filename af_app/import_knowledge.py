import argparse
import json
from pathlib import Path

from af_app.config import Settings
from af_app.database import Database
from af_app.knowledge import KnowledgeRepository


def main():
    parser = argparse.ArgumentParser(description="Import processed Sangfor AF knowledge into SQLite FTS5")
    parser.add_argument("--source", type=Path, default=Path("knowledge/processed"))
    args = parser.parse_args()
    database = Database(Settings.from_env().database_path)
    database.initialize()
    result = KnowledgeRepository(database).import_processed(args.source)
    database.engine.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
