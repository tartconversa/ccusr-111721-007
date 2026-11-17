import hashlib
import json

from .audit import write


def _normalise_fields(fields):
    rows = []
    for field in fields:
        if len(field) < 4:
            raise ValueError("来源字段定义不完整")
        name, meaning, kind, sensitive = field[:4]
        rows.append((name, meaning, kind, bool(sensitive)))
    if not rows or len({row[0] for row in rows}) != len(rows):
        raise ValueError("来源字段缺失或重复")
    return rows


class Catalog:
    def __init__(self, connection):
        self.connection = connection

    def register(self, source_id, owner, jurisdiction, classification, fields, at):
        rows = _normalise_fields(fields)
        with self.connection:
            self.connection.execute(
                "INSERT INTO sources(id,owner,jurisdiction,classification) VALUES(?,?,?,?)",
                (source_id, owner, jurisdiction, classification),
            )
            self.connection.executemany(
                "INSERT INTO fields(source_id,name,meaning,kind,sensitive) VALUES(?,?,?,?,?)",
                [(source_id, name, meaning, kind, int(sensitive)) for name, meaning, kind, sensitive in rows],
            )
            write(self.connection, at, owner, "source.register", source_id, {"jurisdiction": jurisdiction})

    def define(self, source_id, fields, at, actor=None):
        """新增或更新来源字段定义。

        只有定义真正变化时才递增 ``schema_version``；单个字段的
        ``definition_version`` 也随其定义变化递增，供产品、许可和交付
        对照，避免历史版本被直接沿用。
        """
        rows = _normalise_fields(fields)
        source = self.connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if source is None:
            raise KeyError(source_id)
        changes = []
        with self.connection:
            for name, meaning, kind, sensitive in rows:
                existing = self.connection.execute(
                    "SELECT meaning,kind,sensitive,definition_version,retired FROM fields WHERE source_id=? AND name=?",
                    (source_id, name),
                ).fetchone()
                version = 1
                if existing is None:
                    self.connection.execute(
                        "INSERT INTO fields(source_id,name,meaning,kind,sensitive) VALUES(?,?,?,?,?)",
                        (source_id, name, meaning, kind, int(sensitive)),
                    )
                else:
                    changed = (
                        existing["meaning"] != meaning
                        or existing["kind"] != kind
                        or bool(existing["sensitive"]) != sensitive
                        or bool(existing["retired"])
                    )
                    if not changed:
                        continue
                    version = existing["definition_version"] + 1
                    self.connection.execute(
                        "UPDATE fields SET meaning=?,kind=?,sensitive=?,definition_version=?,retired=0 "
                        "WHERE source_id=? AND name=?",
                        (meaning, kind, int(sensitive), version, source_id, name),
                    )
                changes.append({"field": name, "definition_version": version, "sensitive": sensitive})
            if changes:
                self.connection.execute(
                    "UPDATE sources SET schema_version=schema_version+1 WHERE id=?", (source_id,)
                )
            schema_version = self.connection.execute(
                "SELECT schema_version FROM sources WHERE id=?", (source_id,)
            ).fetchone()[0]
            write(
                self.connection,
                at,
                actor or source["owner"],
                "source.define",
                source_id,
                {"changes": changes, "schema_version": schema_version},
            )
        return schema_version

    def retire(self, source_id, names, at, actor=None):
        """停用字段；停用会同时提高来源版本和字段定义版本。"""
        names = list(dict.fromkeys(names))
        if not names:
            raise ValueError("未指定要停用的字段")
        source = self.connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if source is None:
            raise KeyError(source_id)
        retired = []
        with self.connection:
            for name in names:
                field = self.connection.execute(
                    "SELECT definition_version,retired FROM fields WHERE source_id=? AND name=?",
                    (source_id, name),
                ).fetchone()
                if field is None:
                    raise ValueError("来源字段不存在")
                if field["retired"]:
                    continue
                version = field["definition_version"] + 1
                self.connection.execute(
                    "UPDATE fields SET retired=1,definition_version=? WHERE source_id=? AND name=?",
                    (version, source_id, name),
                )
                retired.append({"field": name, "definition_version": version})
            if retired:
                self.connection.execute(
                    "UPDATE sources SET schema_version=schema_version+1 WHERE id=?", (source_id,)
                )
            schema_version = self.connection.execute(
                "SELECT schema_version FROM sources WHERE id=?", (source_id,)
            ).fetchone()[0]
            write(
                self.connection,
                at,
                actor or source["owner"],
                "source.retire",
                source_id,
                {"fields": retired, "schema_version": schema_version},
            )
        return schema_version

    def ingest(self, asset_id, source_id, payload, at):
        source = self.connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if source is None or not source["active"]:
            raise ValueError("来源不可用")
        catalogued = {
            row["name"]: row
            for row in self.connection.execute("SELECT * FROM fields WHERE source_id=?", (source_id,))
        }
        if not isinstance(payload, dict) or not payload or set(payload) - set(catalogued):
            raise ValueError("记录字段不符合来源目录")
        if any(catalogued[name]["retired"] for name in payload):
            raise ValueError("记录包含已停用字段")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.connection:
            self.connection.execute(
                "INSERT INTO assets(id,source_id,schema_version,payload,digest,received_at) VALUES(?,?,?,?,?,?)",
                (asset_id, source_id, source["schema_version"], canonical, digest, at),
            )
            self.connection.executemany(
                "INSERT INTO asset_fields(asset_id,field_name,definition_version,sensitive) VALUES(?,?,?,?)",
                [
                    (asset_id, name, catalogued[name]["definition_version"], int(catalogued[name]["sensitive"]))
                    for name in payload
                ],
            )
            write(
                self.connection,
                at,
                source["owner"],
                "asset.ingest",
                asset_id,
                {
                    "source": source_id,
                    "digest": digest,
                    "schema_version": source["schema_version"],
                    "fields": {
                        name: catalogued[name]["definition_version"] for name in sorted(payload)
                    },
                },
            )
        return digest

    def field(self, source_id, name):
        row = self.connection.execute(
            "SELECT * FROM fields WHERE source_id=? AND name=?", (source_id, name)
        ).fetchone()
        return dict(row) if row else None

    def fields(self, source_id):
        rows = self.connection.execute(
            "SELECT * FROM fields WHERE source_id=? ORDER BY name", (source_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get(self, asset_id):
        row = self.connection.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise KeyError(asset_id)
        return dict(row)
