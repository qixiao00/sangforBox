import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*")
    output_directory: Path = Path("results")

    @classmethod
    def from_env(cls):
        hosts = tuple(x.strip() for x in os.environ.get("AF_ALLOWED_HOSTS", "127.0.0.1,127.0.0.1:*,localhost,localhost:*").split(",") if x.strip())
        return cls(
            Path(os.environ.get("AF_DATABASE_PATH", "data/af.db")).resolve(), hosts,
            Path(os.environ.get("AF_OUTPUT_DIR", "results")).resolve(),
        )
