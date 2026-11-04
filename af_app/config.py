import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_state_directory() -> Path:
    """Per-user runtime state directory, kept out of the source tree.

    Device profiles, knowledge indexes, review records, exports and logs all
    live here so the repository stays free of runtime state. Override with
    AF_STATE_DIR when a caller needs an isolated directory (for example when
    building the portable seed database).
    """
    override = os.environ.get("AF_STATE_DIR")
    if override:
        return Path(override).resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return (Path(base) / "SangforBox").resolve()
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return (Path(base) / "SangforBox").resolve()


@dataclass(frozen=True)
class Settings:
    database_path: Path
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*")
    output_directory: Path = Path("results")

    @classmethod
    def from_env(cls):
        hosts = tuple(x.strip() for x in os.environ.get("AF_ALLOWED_HOSTS", "127.0.0.1,127.0.0.1:*,localhost,localhost:*").split(",") if x.strip())
        state = default_state_directory()
        database = os.environ.get("AF_DATABASE_PATH")
        output = os.environ.get("AF_OUTPUT_DIR")
        return cls(
            Path(database).resolve() if database else (state / "af.db"),
            hosts,
            Path(output).resolve() if output else (state / "results"),
        )
