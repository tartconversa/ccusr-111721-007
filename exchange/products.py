from .audit import write


class Products:
    def __init__(self, connection):
        self.connection = connection

    def _requested(self, asset_ids, fields, inventories):
        """把 ``fields`` 规范为 ``{asset_id: [字段名]}``。

        接受 ``{asset_id: [字段名]}`` 或 ``(asset_id, field_name)`` 序列。
        每条输入记录必须至少贡献一个字段，且字段必须出现在输入记录中。
        """
        if isinstance(fields, dict):
            grouped = {
                asset_id: [names] if isinstance(names, str) else list(names)
                for asset_id, names in fields.items()
            }
        else:
            grouped = {}
            for asset_id, name in fields:
                grouped.setdefault(asset_id, []).append(name)
        unknown = set(grouped) - set(asset_ids)
        if unknown:
            raise ValueError("产品引用了未声明的输入记录")
        requested = {}
        for asset_id in asset_ids:
            names = grouped.get(asset_id, [])
            if len(set(names)) != len(names):
                raise ValueError("产品字段重复")
            if not names:
                raise ValueError("每条输入记录都必须明确引用字段")
            missing = set(names) - inventories[asset_id]
            if missing:
                raise ValueError("产品字段不在输入记录中")
            requested[asset_id] = sorted(names)
        return requested

    def assemble(self, product_id, name, asset_ids, at, actor, fields=None):
        """登记数据产品修订，并固定每个被引用字段的定义版本。

        ``fields`` 必须明确列出每个字段（映射或 ``(asset_id, field_name)``
        序列）；省略或留空会被拒绝，产品不会默认继承来源或输入记录的
        全部字段。字段必须仍与来源目录当前定义一致；来源字段定义更新
        后，旧修订不会被自动沿用，必须重新登记产品修订。
        """
        if fields is None:
            raise ValueError("数据产品必须明确引用字段")
        asset_ids = list(dict.fromkeys(asset_ids))
        if not asset_ids:
            raise ValueError("数据产品缺少输入")
        placeholders = ",".join("?" for _ in asset_ids)
        found = self.connection.execute(
            "SELECT count(*) FROM assets WHERE id IN (" + placeholders + ") AND state='active'",
            asset_ids,
        ).fetchone()[0]
        if found != len(asset_ids):
            raise ValueError("输入记录不可用")

        inventories, versions = {}, {}
        for asset_id in asset_ids:
            rows = self.connection.execute(
                "SELECT af.field_name,af.definition_version,af.sensitive,"
                "f.definition_version AS current_version,f.retired "
                "FROM asset_fields af JOIN assets a ON a.id=af.asset_id "
                "LEFT JOIN fields f ON f.source_id=a.source_id AND f.name=af.field_name "
                "WHERE af.asset_id=?",
                (asset_id,),
            ).fetchall()
            inventories[asset_id] = {row["field_name"] for row in rows}
            versions[asset_id] = {}
            for row in rows:
                if row["current_version"] is None or row["retired"]:
                    raise ValueError("输入记录引用了已停用的字段定义")
                if row["current_version"] != row["definition_version"]:
                    raise ValueError("来源字段定义已更新，请重新登记产品")
                versions[asset_id][row["field_name"]] = row["definition_version"]
        requested = self._requested(asset_ids, fields, inventories)

        revision = self.connection.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM products WHERE id=?", (product_id,)
        ).fetchone()[0]
        scopes = {
            asset_id: {field: versions[asset_id][field] for field in requested[asset_id]}
            for asset_id in asset_ids
        }
        with self.connection:
            self.connection.execute(
                "INSERT INTO products(id,revision,name,created_at) VALUES(?,?,?,?)",
                (product_id, revision, name, at),
            )
            self.connection.executemany(
                "INSERT INTO product_inputs(product_id,product_revision,asset_id) VALUES(?,?,?)",
                [(product_id, revision, asset_id) for asset_id in asset_ids],
            )
            self.connection.executemany(
                "INSERT INTO product_fields(product_id,product_revision,asset_id,field_name,definition_version) "
                "VALUES(?,?,?,?,?)",
                [
                    (product_id, revision, asset_id, field, version)
                    for asset_id in asset_ids
                    for field, version in scopes[asset_id].items()
                ],
            )
            write(
                self.connection,
                at,
                actor,
                "product.assemble",
                product_id,
                {"revision": revision, "inputs": asset_ids, "fields": scopes},
            )
        return revision

    def inputs(self, product_id, revision):
        rows = self.connection.execute(
            "SELECT a.*,s.owner,s.jurisdiction,s.classification,s.active AS source_active,"
            "s.schema_version AS current_schema_version "
            "FROM product_inputs p "
            "JOIN assets a ON a.id=p.asset_id JOIN sources s ON s.id=a.source_id "
            "WHERE p.product_id=? AND p.product_revision=? ORDER BY a.id",
            (product_id, revision),
        ).fetchall()
        return [dict(row) for row in rows]

    def fields(self, product_id, revision):
        """返回产品修订引用的字段及其来源版本、当前目录版本。"""
        rows = self.connection.execute(
            "SELECT pf.asset_id,pf.field_name,pf.definition_version,a.source_id,a.state AS asset_state,"
            "a.schema_version AS asset_schema_version,s.jurisdiction,s.classification,"
            "s.active AS source_active,s.schema_version AS current_schema_version,"
            "f.definition_version AS current_version,f.sensitive,f.retired,f.meaning "
            "FROM product_fields pf "
            "JOIN assets a ON a.id=pf.asset_id JOIN sources s ON s.id=a.source_id "
            "LEFT JOIN fields f ON f.source_id=a.source_id AND f.name=pf.field_name "
            "WHERE pf.product_id=? AND pf.product_revision=? "
            "ORDER BY pf.asset_id,pf.field_name",
            (product_id, revision),
        ).fetchall()
        return [dict(row) for row in rows]

    def stale(self, product_id, revision):
        """列出因来源字段定义变化而需要重新判断的依赖。"""
        stale = []
        for row in self.fields(product_id, revision):
            if (
                row["current_version"] is None
                or row["retired"]
                or row["current_version"] != row["definition_version"]
            ):
                stale.append(
                    {
                        "source": row["source_id"],
                        "asset": row["asset_id"],
                        "field": row["field_name"],
                        "product_version": row["definition_version"],
                        "current_version": row["current_version"],
                    }
                )
        return stale
