from .audit import write


class Grants:
    def __init__(self, connection):
        self.connection = connection

    def issue(self, grant_id, source_id, recipient, purpose, valid_until, at, fields=None):
        """登记合作许可。

        许可必须列出被授权字段（名称或 ``(名称, 定义版本)``）；空许可不
        授权任何披露，避免“整来源放行”。字段记录许可签发时的定义版本，
        来源字段定义升级后旧许可自动失效，需要重新登记。
        """
        source = self.connection.execute("SELECT owner FROM sources WHERE id=?", (source_id,)).fetchone()
        if source is None:
            raise KeyError(source_id)
        if fields is None:
            raise ValueError("合作许可必须明确列出字段")
        scopes = self._normalise(source_id, fields)
        if not scopes:
            raise ValueError("合作许可必须明确列出字段")
        with self.connection:
            self.connection.execute(
                "INSERT INTO grants(id,source_id,recipient,purpose,valid_until) VALUES(?,?,?,?,?)",
                (grant_id, source_id, recipient, purpose, valid_until),
            )
            self.connection.executemany(
                "INSERT INTO grant_fields(grant_id,source_id,field_name,definition_version) VALUES(?,?,?,?)",
                [(grant_id, source_id, name, version) for name, version in scopes.items()],
            )
            write(
                self.connection,
                at,
                source["owner"],
                "grant.issue",
                grant_id,
                {"source": source_id, "recipient": recipient, "fields": scopes},
            )

    def _normalise(self, source_id, fields):
        catalogued = {
            row["name"]: row["definition_version"]
            for row in self.connection.execute(
                "SELECT name,definition_version FROM fields WHERE source_id=? AND retired=0",
                (source_id,),
            )
        }
        if fields is None:
            return {}
        if isinstance(fields, (str, bytes)):
            raise ValueError("许可字段必须逐个列出")
        entries = list(fields)
        scopes = {}
        for field in entries:
            name, version = (field, None) if isinstance(field, str) else field
            if name not in catalogued:
                raise ValueError("许可字段不在来源目录中")
            version = catalogued[name] if version is None else int(version)
            if version != catalogued[name]:
                raise ValueError("许可字段版本与来源目录不一致")
            if name in scopes:
                raise ValueError("许可字段重复")
            scopes[name] = version
        return scopes

    def scopes(self, grant_id):
        rows = self.connection.execute(
            "SELECT source_id,field_name,definition_version FROM grant_fields "
            "WHERE grant_id=? ORDER BY source_id,field_name",
            (grant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def active(self, source_id, recipient, purpose, at):
        row = self.connection.execute(
            "SELECT id FROM grants WHERE source_id=? AND recipient=? AND purpose=? "
            "AND status='active' AND valid_until>=? ORDER BY id LIMIT 1",
            (source_id, recipient, purpose, at),
        ).fetchone()
        return row["id"] if row else None

    def permits(self, source_id, recipient, purpose, at):
        """兼容旧接口：来源级许可是否存在，不代表任何字段已授权。"""
        return self.active(source_id, recipient, purpose, at)

    def allowed(self, source_id, recipient, purpose, at, fields):
        """返回每个字段实际命中的许可编号。

        ``fields`` 是 ``{字段名: 当前定义版本}``。同一接收方可能持有多份
        许可，因此逐份累加覆盖范围，而不是只看第一条；某字段只有在某份
        有效许可记录了当前定义版本时才会出现在结果里。
        """
        rows = self.connection.execute(
            "SELECT g.id AS grant_id,gf.field_name,gf.definition_version FROM grants g "
            "JOIN grant_fields gf ON gf.grant_id=g.id "
            "WHERE g.source_id=? AND g.recipient=? AND g.purpose=? AND g.status='active' "
            "AND g.valid_until>=? ORDER BY g.id",
            (source_id, recipient, purpose, at),
        ).fetchall()
        allowed = {}
        for row in rows:
            name = row["field_name"]
            if name in allowed:
                continue
            if fields.get(name) == row["definition_version"]:
                allowed[name] = row["grant_id"]
        return allowed
