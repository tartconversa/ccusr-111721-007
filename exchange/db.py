import json
import sqlite3


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, jurisdiction TEXT NOT NULL,
    classification TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS fields (
    source_id TEXT NOT NULL REFERENCES sources(id), name TEXT NOT NULL,
    meaning TEXT NOT NULL, kind TEXT NOT NULL, sensitive INTEGER NOT NULL,
    definition_version INTEGER NOT NULL DEFAULT 1,
    retired INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(source_id,name)
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    schema_version INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
    received_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS asset_fields (
    asset_id TEXT NOT NULL REFERENCES assets(id), field_name TEXT NOT NULL,
    definition_version INTEGER NOT NULL, sensitive INTEGER NOT NULL,
    PRIMARY KEY(asset_id,field_name)
);
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
    recipient TEXT NOT NULL, purpose TEXT NOT NULL, valid_until TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS grant_fields (
    grant_id TEXT NOT NULL REFERENCES grants(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    field_name TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    PRIMARY KEY(grant_id,source_id,field_name)
);
CREATE TABLE IF NOT EXISTS policies (
    jurisdiction TEXT NOT NULL, classification TEXT NOT NULL,
    sensitivity TEXT NOT NULL DEFAULT 'standard', purpose TEXT NOT NULL,
    decision TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY(jurisdiction,classification,sensitivity,purpose)
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT NOT NULL, revision INTEGER NOT NULL, name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
    PRIMARY KEY(id,revision)
);
CREATE TABLE IF NOT EXISTS product_inputs (
    product_id TEXT NOT NULL, product_revision INTEGER NOT NULL,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    PRIMARY KEY(product_id,product_revision,asset_id),
    FOREIGN KEY(product_id,product_revision) REFERENCES products(id,revision)
);
CREATE TABLE IF NOT EXISTS product_fields (
    product_id TEXT NOT NULL, product_revision INTEGER NOT NULL,
    asset_id TEXT NOT NULL REFERENCES assets(id), field_name TEXT NOT NULL,
    definition_version INTEGER NOT NULL,
    PRIMARY KEY(product_id,product_revision,asset_id,field_name),
    FOREIGN KEY(product_id,product_revision) REFERENCES products(id,revision)
);
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY, product_id TEXT NOT NULL, product_revision INTEGER NOT NULL,
    recipient TEXT NOT NULL, purpose TEXT NOT NULL, created_at TEXT NOT NULL,
    input_digest TEXT NOT NULL, policy_revisions TEXT NOT NULL,
    included_fields TEXT NOT NULL DEFAULT '[]',
    excluded_fields TEXT NOT NULL DEFAULT '[]',
    source_versions TEXT NOT NULL DEFAULT '{}',
    field_versions TEXT NOT NULL DEFAULT '{}',
    policy_scopes TEXT NOT NULL DEFAULT '{}',
    grant_scopes TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(product_id,product_revision) REFERENCES products(id,revision)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, subject TEXT NOT NULL, details TEXT NOT NULL
);
"""


def _columns(connection, table):
    return {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}


def _migrate(connection):
    """把旧版按来源授权的台账补齐为字段级模型。

    旧库中的策略没有敏感度维度，迁移时视为通配规则，保持原有行为；
    旧字段定义一律从版本 1 开始。
    """
    field_columns = _columns(connection, "fields")
    if "definition_version" not in field_columns:
        connection.execute("ALTER TABLE fields ADD COLUMN definition_version INTEGER NOT NULL DEFAULT 1")
    if "retired" not in field_columns:
        connection.execute("ALTER TABLE fields ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")

    policy_columns = _columns(connection, "policies")
    if "sensitivity" not in policy_columns:
        connection.executescript(
            """
            ALTER TABLE policies RENAME TO policies_legacy;
            CREATE TABLE policies (
                jurisdiction TEXT NOT NULL, classification TEXT NOT NULL,
                sensitivity TEXT NOT NULL DEFAULT 'standard', purpose TEXT NOT NULL,
                decision TEXT NOT NULL, revision INTEGER NOT NULL,
                PRIMARY KEY(jurisdiction,classification,sensitivity,purpose)
            );
            INSERT INTO policies(jurisdiction,classification,sensitivity,purpose,decision,revision)
                SELECT jurisdiction,classification,'*',purpose,decision,revision FROM policies_legacy;
            DROP TABLE policies_legacy;
            """
        )

    delivery_columns = _columns(connection, "deliveries")
    additions = {
        "included_fields": "TEXT NOT NULL DEFAULT '[]'",
        "excluded_fields": "TEXT NOT NULL DEFAULT '[]'",
        "source_versions": "TEXT NOT NULL DEFAULT '{}'",
        "field_versions": "TEXT NOT NULL DEFAULT '{}'",
        "policy_scopes": "TEXT NOT NULL DEFAULT '{}'",
        "grant_scopes": "TEXT NOT NULL DEFAULT '{}'",
    }
    for name, definition in additions.items():
        if name not in delivery_columns:
            connection.execute("ALTER TABLE deliveries ADD COLUMN " + name + " " + definition)

    # 旧资产按当时来源目录回填字段版本，保证历史记录仍可追溯。
    missing = connection.execute(
        "SELECT a.id,a.source_id,a.payload FROM assets a "
        "WHERE NOT EXISTS (SELECT 1 FROM asset_fields f WHERE f.asset_id=a.id)"
    ).fetchall()
    for asset in missing:
        payload = json.loads(asset["payload"])
        for name in payload:
            field = connection.execute(
                "SELECT definition_version,sensitive FROM fields WHERE source_id=? AND name=?",
                (asset["source_id"], name),
            ).fetchone()
            if field is None:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO asset_fields(asset_id,field_name,definition_version,sensitive) VALUES(?,?,?,?)",
                (asset["id"], name, field["definition_version"], field["sensitive"]),
            )
    connection.commit()


def connect(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection
