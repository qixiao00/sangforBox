from datetime import datetime, timezone
from sqlalchemy import String, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session


class Base(DeclarativeBase):
    pass


class SchemaVersion(Base):
    __tablename__ = "schema_versions"
    version: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40))


class Database:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = create_engine("sqlite:///" + path.as_posix(), connect_args={"check_same_thread": False})

    def initialize(self):
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            if session.get(SchemaVersion, 1) is None:
                session.add(SchemaVersion(version=1, created_at=datetime.now(timezone.utc).isoformat()))
                session.commit()
        with self.engine.begin() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    doc_type TEXT NOT NULL,
                    vendor TEXT,
                    product TEXT,
                    version TEXT,
                    source_path TEXT,
                    sha256 TEXT NOT NULL,
                    page_count INTEGER NOT NULL,
                    processed_at TEXT,
                    raw_json TEXT NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
                    doc_type TEXT NOT NULL,
                    product TEXT,
                    version TEXT,
                    title TEXT,
                    title_path_json TEXT NOT NULL,
                    page_start INTEGER NOT NULL,
                    page_end INTEGER NOT NULL,
                    content_markdown TEXT NOT NULL,
                    content_plain TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    entities_json TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    table_ids_json TEXT NOT NULL,
                    has_code INTEGER NOT NULL,
                    prev_chunk_id TEXT,
                    next_chunk_id TEXT,
                    text_sha256 TEXT NOT NULL
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_chunks_doc_page ON chunks(doc_id, page_start)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_chunks_text_sha ON chunks(text_sha256)"))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS api_endpoints (
                    endpoint_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
                    title TEXT,
                    method TEXT,
                    path TEXT,
                    summary TEXT,
                    page_start INTEGER,
                    page_end INTEGER,
                    source_chunk_ids_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_api_method_path ON api_endpoints(method, path)"))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS cli_commands (
                    command_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
                    command TEXT NOT NULL,
                    syntax TEXT,
                    mode TEXT,
                    summary TEXT,
                    page_start INTEGER,
                    page_end INTEGER,
                    source_chunk_ids_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_cli_command ON cli_commands(command)"))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS knowledge_imports (
                    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_manifest_sha256 TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    documents INTEGER NOT NULL,
                    chunks INTEGER NOT NULL,
                    api_endpoints INTEGER NOT NULL,
                    cli_commands INTEGER NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                    chunk_id UNINDEXED,
                    title_tokens,
                    content_tokens,
                    keyword_tokens,
                    tokenize='unicode61 remove_diacritics 2'
                )
            """))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS device_profiles (
                    device_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password_ciphertext TEXT NOT NULL,
                    verify_tls INTEGER NOT NULL DEFAULT 1,
                    ca_bundle TEXT,
                    timeout_seconds REAL NOT NULL DEFAULT 15,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_device_profiles_name ON device_profiles(name)"))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS device_connection_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL REFERENCES device_profiles(device_id) ON DELETE CASCADE,
                    success INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    tested_at TEXT NOT NULL
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_device_audit_device ON device_connection_audit(device_id, audit_id DESC)"))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS operation_plans (
                    plan_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES device_profiles(device_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    request TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    snapshots_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    applied_at TEXT,
                    result_json TEXT
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_operation_plans_device ON operation_plans(device_id, created_at DESC)"))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS initialization_cases (
                    case_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES device_profiles(device_id) ON DELETE CASCADE,
                    design_json TEXT NOT NULL,
                    design_revision INTEGER NOT NULL,
                    confirmed_revision INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS initialization_case_plans (
                    item_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES initialization_cases(case_id) ON DELETE CASCADE,
                    plan_id TEXT NOT NULL UNIQUE REFERENCES operation_plans(plan_id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    design_revision INTEGER NOT NULL,
                    implementation_json TEXT,
                    implementation_status TEXT,
                    verified_at TEXT
                )
            """))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS initialization_feedback (
                    item_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES initialization_cases(case_id) ON DELETE CASCADE,
                    review_hash TEXT NOT NULL,
                    satisfaction TEXT NOT NULL,
                    difference_confirmed TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    submitted_at TEXT NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE IF NOT EXISTS initialization_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL REFERENCES initialization_cases(case_id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    stage TEXT,
                    plan_id TEXT,
                    status TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_initialization_events_case ON initialization_events(case_id,event_id)"))
        with Session(self.engine) as session:
            if session.get(SchemaVersion, 2) is None:
                session.add(SchemaVersion(version=2, created_at=datetime.now(timezone.utc).isoformat()))
                session.commit()
            if session.get(SchemaVersion, 3) is None:
                session.add(SchemaVersion(version=3, created_at=datetime.now(timezone.utc).isoformat()))
                session.commit()
            if session.get(SchemaVersion, 4) is None:
                session.add(SchemaVersion(version=4, created_at=datetime.now(timezone.utc).isoformat()))
                session.commit()
            if session.get(SchemaVersion, 5) is None:
                session.add(SchemaVersion(version=5, created_at=datetime.now(timezone.utc).isoformat()))
                session.commit()

    def probe(self):
        with Session(self.engine) as session:
            return session.scalar(select(SchemaVersion.version).order_by(SchemaVersion.version.desc()).limit(1))
