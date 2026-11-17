import hashlib
import json

from .audit import write
from .grants import Grants
from .policy import Policies
from .products import Products


class Delivery:
    def __init__(self, connection):
        self.connection = connection
        self.grants = Grants(connection)
        self.policies = Policies(connection)
        self.products = Products(connection)

    def send(self, delivery_id, product_id, revision, recipient, purpose, at):
        existing = self.connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if existing:
            if (existing["product_id"], existing["product_revision"], existing["recipient"], existing["purpose"]) != (product_id, revision, recipient, purpose):
                raise ValueError("交付编号已用于其他请求")
            return self.receipt(delivery_id)
        product = self.connection.execute("SELECT state FROM products WHERE id=? AND revision=?", (product_id, revision)).fetchone()
        if product is None or product["state"] != "draft":
            raise ValueError("数据产品修订不可交付")
        inputs = self.products.inputs(product_id, revision)
        if not inputs:
            raise ValueError("数据产品缺少输入")
        requested = self.products.fields(product_id, revision)
        if not requested:
            raise ValueError("数据产品未引用字段")
        fixed = {(row["asset_id"], row["field_name"]) for row in requested}

        reasons = []
        included, excluded = [], []
        revisions, scopes, grants, source_versions = {}, {}, {}, {}
        for asset in inputs:
            source_id = asset["source_id"]
            source_versions[source_id] = {
                "asset_schema_version": asset["schema_version"],
                "current_schema_version": asset["current_schema_version"],
            }
            # 输入记录中未被产品修订引用的字段不进入披露范围。
            for row in self.connection.execute(
                "SELECT field_name,definition_version,sensitive FROM asset_fields "
                "WHERE asset_id=? ORDER BY field_name",
                (asset["id"],),
            ).fetchall():
                if (asset["id"], row["field_name"]) not in fixed:
                    excluded.append(
                        {
                            "source": source_id,
                            "asset": asset["id"],
                            "field": row["field_name"],
                            "definition_version": row["definition_version"],
                            "sensitive": bool(row["sensitive"]),
                            "reason": "not_in_product",
                        }
                    )

        by_source = {}
        for row in requested:
            by_source.setdefault(row["source_id"], []).append(row)

        for source_id, rows in by_source.items():
            first = rows[0]
            source_block = None
            if not first["source_active"]:
                source_block = "inactive"
            for row in rows:
                if row["asset_state"] != "active":
                    source_block = source_block or "inactive"
            catalog_versions = {}
            for row in rows:
                if row["current_version"] is None or row["retired"]:
                    reasons.append(
                        {"source": source_id, "field": row["field_name"], "reason": "retired"}
                    )
                elif row["current_version"] != row["definition_version"]:
                    reasons.append(
                        {
                            "source": source_id,
                            "field": row["field_name"],
                            "reason": "stale",
                            "product_version": row["definition_version"],
                            "current_version": row["current_version"],
                        }
                    )
                catalog_versions[row["field_name"]] = row["current_version"]

            granted = self.grants.allowed(
                source_id, recipient, purpose, at, catalog_versions
            )
            grants[source_id] = {
                "grants": sorted(set(granted.values())),
                "fields": sorted(granted),
            }
            for row in rows:
                if source_block:
                    reasons.append(
                        {"source": source_id, "field": row["field_name"], "reason": source_block}
                    )
                elif row["field_name"] not in granted:
                    reasons.append(
                        {"source": source_id, "field": row["field_name"], "reason": "grant"}
                    )

            for sensitivity in sorted({("sensitive" if row["sensitive"] else "standard") for row in rows}):
                decision, policy_revision, scope = self.policies.decision(
                    first["jurisdiction"], first["classification"], purpose, sensitivity
                )
                revisions[f"{source_id}:{scope}"] = policy_revision
                scopes[f"{source_id}:{scope}"] = decision
                if decision != "allow":
                    for row in rows:
                        if ("sensitive" if row["sensitive"] else "standard") == sensitivity:
                            reasons.append(
                                {
                                    "source": source_id,
                                    "field": row["field_name"],
                                    "reason": "policy",
                                    "sensitivity": sensitivity,
                                    "scope": scope,
                                    "revision": policy_revision,
                                }
                            )
        if reasons:
            with self.connection:
                write(
                    self.connection,
                    at,
                    recipient,
                    "delivery.reject",
                    product_id,
                    {"revision": revision, "reasons": reasons},
                )
            raise PermissionError(reasons)

        for row in requested:
            included.append(
                {
                    "source": row["source_id"],
                    "asset": row["asset_id"],
                    "field": row["field_name"],
                    "definition_version": row["definition_version"],
                    "sensitive": bool(row["sensitive"]),
                }
            )
        included.sort(key=lambda item: (item["source"], item["asset"], item["field"]))
        excluded.sort(key=lambda item: (item["source"], item["asset"], item["field"]))
        field_versions = {}
        for item in included:
            field_versions.setdefault(item["source"], {})[item["field"]] = item["definition_version"]
        digest = hashlib.sha256(
            "|".join(
                asset["digest"] + ":" + ",".join(
                    sorted(f"{item['field']}@{item['definition_version']}" for item in included if item["asset"] == asset["id"])
                )
                for asset in inputs
            ).encode()
        ).hexdigest()
        with self.connection:
            self.connection.execute(
                "INSERT INTO deliveries(id,product_id,product_revision,recipient,purpose,created_at,"
                "input_digest,policy_revisions,included_fields,excluded_fields,source_versions,"
                "field_versions,policy_scopes,grant_scopes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delivery_id,
                    product_id,
                    revision,
                    recipient,
                    purpose,
                    at,
                    digest,
                    json.dumps(revisions, sort_keys=True),
                    json.dumps(included, ensure_ascii=False, sort_keys=True),
                    json.dumps(excluded, ensure_ascii=False, sort_keys=True),
                    json.dumps(source_versions, ensure_ascii=False, sort_keys=True),
                    json.dumps(field_versions, ensure_ascii=False, sort_keys=True),
                    json.dumps(scopes, sort_keys=True),
                    json.dumps(grants, ensure_ascii=False, sort_keys=True),
                ),
            )
            write(
                self.connection,
                at,
                recipient,
                "delivery.accept",
                delivery_id,
                {
                    "product": product_id,
                    "revision": revision,
                    "included": included,
                    "excluded": excluded,
                    "source_versions": source_versions,
                },
            )
        return self.receipt(delivery_id)

    def receipt(self, delivery_id):
        row = self.connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        receipt = dict(row)
        for name in (
            "policy_revisions",
            "included_fields",
            "excluded_fields",
            "source_versions",
            "field_versions",
            "policy_scopes",
            "grant_scopes",
        ):
            receipt[name] = json.loads(receipt[name])
        return receipt

    def disclose(self, delivery_id):
        """只返回接收方实际拿到的字段值及来源版本，不重新计算权限。"""
        receipt = self.receipt(delivery_id)
        records = {}
        for item in receipt["included_fields"]:
            asset = self.connection.execute(
                "SELECT payload FROM assets WHERE id=?", (item["asset"],)
            ).fetchone()
            payload = json.loads(asset["payload"])
            records.setdefault(item["asset"], {})[item["field"]] = payload.get(item["field"])
        return {
            "delivery": delivery_id,
            "recipient": receipt["recipient"],
            "purpose": receipt["purpose"],
            "included_fields": receipt["included_fields"],
            "excluded_fields": receipt["excluded_fields"],
            "source_versions": receipt["source_versions"],
            "field_versions": receipt["field_versions"],
            "records": records,
        }
