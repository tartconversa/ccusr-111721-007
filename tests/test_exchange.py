import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from exchange import Exchange


FIELDS = [
    ("count", "通行计数", "integer", False),
    ("traveller", "通关人员标识", "string", True),
]


class ExchangeFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "exchange.db"
        self.service = Exchange(self.database)
        self.service.catalog.register("flow", "机构甲", "甲地", "restricted", FIELDS, "2026-11-01")
        self.service.catalog.ingest(
            "item-1", "flow", {"count": 8, "traveller": "P-001"}, "2026-11-02"
        )
        self.service.grants.issue(
            "g-1", "flow", "机构乙", "运行协调", "2026-12-31", "2026-11-03", fields=["count"]
        )
        self.service.policies.set("甲地", "restricted", "运行协调", "allow", "2026-11-03", "机构甲", sensitivity="standard")
        self.service.policies.set("甲地", "restricted", "运行协调", "deny", "2026-11-03", "机构甲", sensitivity="sensitive")
        self.revision = self.service.products.assemble(
            "report", "通行观察", ["item-1"], "2026-11-04", "机构甲", fields={"item-1": ["count"]}
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_delivery_keeps_revision_and_request_identity(self):
        desk = self.service.delivery
        receipt = desk.send("d-1", "report", self.revision, "机构乙", "运行协调", "2026-11-05")
        self.assertEqual(receipt["product_revision"], self.revision)
        self.assertEqual(receipt, desk.send("d-1", "report", self.revision, "机构乙", "运行协调", "2026-11-05"))
        with self.assertRaises(ValueError):
            desk.send("d-1", "report", self.revision, "其他机构", "运行协调", "2026-11-05")

    def test_grant_and_policy_gate_delivery(self):
        with self.assertRaises(PermissionError):
            self.service.delivery.send("d-2", "report", self.revision, "陌生机构", "运行协调", "2026-11-05")
        self.service.policies.set("甲地", "restricted", "运行协调", "deny", "2026-11-06", "机构甲", sensitivity="standard")
        with self.assertRaises(PermissionError):
            self.service.delivery.send("d-3", "report", self.revision, "机构乙", "运行协调", "2026-11-07")
        self.assertTrue(self.service.history("report"))

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ValueError):
            self.service.catalog.ingest("item-2", "flow", {"unknown": 9}, "2026-11-03")

    def test_product_must_name_fields(self):
        with self.assertRaises(ValueError):
            self.service.products.assemble("empty", "空产品", ["item-1"], "2026-11-04", "机构甲")
        with self.assertRaises(ValueError):
            self.service.products.assemble(
                "empty", "空产品", ["item-1"], "2026-11-04", "机构甲", fields={"item-1": []}
            )

    def test_grant_must_name_fields(self):
        with self.assertRaises(ValueError):
            self.service.grants.issue("g-x", "flow", "机构乙", "运行协调", "2026-12-31", "2026-11-03")
        with self.assertRaises(ValueError):
            self.service.grants.issue("g-x", "flow", "机构乙", "运行协调", "2026-12-31", "2026-11-03", fields=["unknown"])

    def test_receipt_and_disclosure_show_scope_and_versions(self):
        receipt = self.service.delivery.send("d-1", "report", self.revision, "机构乙", "运行协调", "2026-11-05")
        self.assertEqual(
            [(item["field"], item["definition_version"], item["sensitive"]) for item in receipt["included_fields"]],
            [("count", 1, False)],
        )
        self.assertEqual(
            [(item["field"], item["reason"]) for item in receipt["excluded_fields"]],
            [("traveller", "not_in_product")],
        )
        self.assertEqual(receipt["field_versions"], {"flow": {"count": 1}})
        self.assertEqual(receipt["source_versions"]["flow"]["asset_schema_version"], 1)
        self.assertEqual(receipt["policy_revisions"], {"flow:standard": 1})

        disclosure = self.service.disclose("d-1")
        self.assertEqual(disclosure["records"], {"item-1": {"count": 8}})
        self.assertNotIn("traveller", json.dumps(disclosure["records"]))

    def test_ungranted_field_blocks_product_delivery(self):
        revision = self.service.products.assemble(
            "wide", "含身份字段", ["item-1"], "2026-11-04", "机构甲",
            fields={"item-1": ["count", "traveller"]},
        )
        with self.assertRaises(PermissionError) as caught:
            self.service.delivery.send("d-wide", "wide", revision, "机构乙", "运行协调", "2026-11-05")
        reasons = caught.exception.args[0]
        self.assertIn({"source": "flow", "field": "traveller", "reason": "grant"}, reasons)
        self.assertNotIn({"source": "flow", "field": "count", "reason": "grant"}, reasons)
        self.assertEqual(
            [
                (item["field"], item["sensitivity"], item["scope"])
                for item in reasons
                if item["reason"] == "policy"
            ],
            [("traveller", "sensitive", "sensitive")],
        )

    def test_new_field_does_not_inherit_old_grant(self):
        self.service.catalog.define(
            "flow",
            FIELDS + [("region", "通关区域", "string", False)],
            "2026-11-06",
            "机构甲",
        )
        self.service.catalog.ingest("item-2", "flow", {"count": 3, "region": "北"}, "2026-11-06")
        revision = self.service.products.assemble(
            "regional", "区域汇总", ["item-2"], "2026-11-07", "机构甲",
            fields={"item-2": ["count", "region"]},
        )
        with self.assertRaises(PermissionError) as caught:
            self.service.delivery.send("d-region", "regional", revision, "机构乙", "运行协调", "2026-11-08")
        reasons = caught.exception.args[0]
        self.assertIn({"source": "flow", "field": "region", "reason": "grant"}, reasons)
        self.assertNotIn({"source": "flow", "field": "count", "reason": "grant"}, reasons)

    def test_definition_change_requires_new_product_and_grant(self):
        before = self.service.products.fields("report", self.revision)
        self.service.catalog.define(
            "flow",
            [("count", "通行车辆计数", "integer", False)] + [FIELDS[1]],
            "2026-11-06",
            "机构甲",
        )
        stale = self.service.products.stale("report", self.revision)
        self.assertEqual(
            [(item["field"], item["product_version"], item["current_version"]) for item in stale],
            [("count", 1, 2)],
        )
        with self.assertRaises(PermissionError) as caught:
            self.service.delivery.send("d-stale", "report", self.revision, "机构乙", "运行协调", "2026-11-07")
        self.assertTrue(any(reason["reason"] == "stale" for reason in caught.exception.args[0]))
        with self.assertRaises(ValueError):
            # 旧记录仍带着 v1 字段值，不能直接装进新产品修订。
            self.service.products.assemble(
                "report", "通行观察", ["item-1"], "2026-11-07", "机构甲", fields={"item-1": ["count"]}
            )
        self.service.catalog.ingest("item-3", "flow", {"count": 5, "traveller": "P-002"}, "2026-11-07")
        revision = self.service.products.assemble(
            "report", "通行观察", ["item-3"], "2026-11-07", "机构甲", fields={"item-3": ["count"]}
        )
        with self.assertRaises(PermissionError) as caught:
            self.service.delivery.send("d-regrant", "report", revision, "机构乙", "运行协调", "2026-11-08")
        self.assertIn({"source": "flow", "field": "count", "reason": "grant"}, caught.exception.args[0])
        self.service.grants.issue(
            "g-2", "flow", "机构乙", "运行协调", "2026-12-31", "2026-11-08",
            fields=[("count", 2)],
        )
        receipt = self.service.delivery.send("d-regrant", "report", revision, "机构乙", "运行协调", "2026-11-08")
        self.assertEqual(receipt["field_versions"], {"flow": {"count": 2}})
        self.assertEqual(receipt["grant_scopes"], {"flow": {"grants": ["g-2"], "fields": ["count"]}})
        self.assertEqual(before[0]["definition_version"], 1)

    def test_retired_field_cannot_be_assembled_or_delivered(self):
        self.service.catalog.define(
            "flow",
            [FIELDS[0], FIELDS[1], ("note", "备注", "string", False)],
            "2026-11-06",
            "机构甲",
        )
        self.service.catalog.ingest("item-2", "flow", {"count": 4, "note": "x"}, "2026-11-06")
        revision = self.service.products.assemble(
            "notes", "备注产品", ["item-2"], "2026-11-07", "机构甲", fields={"item-2": ["note"]}
        )
        self.service.catalog.retire("flow", ["note"], "2026-11-08", "机构甲")
        self.assertIn("note", {item["field"] for item in self.service.products.stale("notes", revision)})
        with self.assertRaises(ValueError):
            self.service.catalog.ingest("item-3", "flow", {"note": "y"}, "2026-11-08")

    def test_audit_records_actual_disclosure_scope(self):
        self.service.delivery.send("d-1", "report", self.revision, "机构乙", "运行协调", "2026-11-05")
        entries = self.service.history("d-1")
        accepted = [entry for entry in entries if entry["action"] == "delivery.accept"]
        self.assertEqual(len(accepted), 1)
        details = json.loads(accepted[0]["details"])
        self.assertEqual([item["field"] for item in details["included"]], ["count"])
        self.assertEqual([item["field"] for item in details["excluded"]], ["traveller"])
        self.assertEqual(details["source_versions"]["flow"]["asset_schema_version"], 1)


class LegacyDatabaseTest(unittest.TestCase):
    def test_legacy_ledger_is_migrated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE sources (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, jurisdiction TEXT NOT NULL,
                    classification TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    schema_version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE fields (
                    source_id TEXT NOT NULL, name TEXT NOT NULL, meaning TEXT NOT NULL,
                    kind TEXT NOT NULL, sensitive INTEGER NOT NULL, PRIMARY KEY(source_id,name)
                );
                CREATE TABLE assets (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, schema_version INTEGER NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL, received_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'active'
                );
                CREATE TABLE grants (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, recipient TEXT NOT NULL,
                    purpose TEXT NOT NULL, valid_until TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE TABLE policies (
                    jurisdiction TEXT NOT NULL, classification TEXT NOT NULL, purpose TEXT NOT NULL,
                    decision TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(jurisdiction,classification,purpose)
                );
                CREATE TABLE products (
                    id TEXT NOT NULL, revision INTEGER NOT NULL, name TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
                    PRIMARY KEY(id,revision)
                );
                CREATE TABLE product_inputs (
                    product_id TEXT NOT NULL, product_revision INTEGER NOT NULL, asset_id TEXT NOT NULL,
                    PRIMARY KEY(product_id,product_revision,asset_id)
                );
                CREATE TABLE deliveries (
                    id TEXT PRIMARY KEY, product_id TEXT NOT NULL, product_revision INTEGER NOT NULL,
                    recipient TEXT NOT NULL, purpose TEXT NOT NULL, created_at TEXT NOT NULL,
                    input_digest TEXT NOT NULL, policy_revisions TEXT NOT NULL
                );
                CREATE TABLE audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
                    action TEXT NOT NULL, subject TEXT NOT NULL, details TEXT NOT NULL
                );
                INSERT INTO sources VALUES('flow','机构甲','甲地','business',1,1);
                INSERT INTO fields VALUES('flow','count','通行计数','integer',0);
                INSERT INTO assets VALUES('item-1','flow',1,'{"count":8}','d','2026-11-02','active');
                INSERT INTO policies VALUES('甲地','business','协作统计','allow',1);
                """
            )
            connection.commit()
            connection.close()
            service = Exchange(path)
            try:
                self.assertEqual(service.policies.decision("甲地", "business", "协作统计"), ("allow", 1, "*"))
                self.assertEqual(service.catalog.fields("flow")[0]["definition_version"], 1)
                revision = service.products.assemble(
                    "report", "通行观察", ["item-1"], "2026-11-04", "机构甲", fields={"item-1": ["count"]}
                )
                service.grants.issue("g-1", "flow", "机构乙", "协作统计", "2026-12-31", "2026-11-04", fields=["count"])
                receipt = service.delivery.send("d-1", "report", revision, "机构乙", "协作统计", "2026-11-05")
                self.assertEqual(receipt["field_versions"], {"flow": {"count": 1}})
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
