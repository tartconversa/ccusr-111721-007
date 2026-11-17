from .audit import write


class Policies:
    def __init__(self, connection):
        self.connection = connection

    def set(self, jurisdiction, classification, purpose, decision, at, actor, sensitivity="standard"):
        """登记法域规则。

        ``sensitivity`` 为 ``standard``（默认，只约束普通字段）或
        ``sensitive``（只约束敏感字段）；显式传 ``*`` 可登记同时覆盖两类
        字段的通配规则。旧库迁移来的规则即为 ``*``。
        """
        if decision not in {"allow", "deny"}:
            raise ValueError("规则决策无效")
        if sensitivity not in {"standard", "sensitive", "*"}:
            raise ValueError("规则敏感度无效")
        old = self.connection.execute(
            "SELECT revision FROM policies WHERE jurisdiction=? AND classification=? AND sensitivity=? AND purpose=?",
            (jurisdiction, classification, sensitivity, purpose),
        ).fetchone()
        revision = old["revision"] + 1 if old else 1
        with self.connection:
            self.connection.execute(
                "INSERT INTO policies(jurisdiction,classification,sensitivity,purpose,decision,revision) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(jurisdiction,classification,sensitivity,purpose) "
                "DO UPDATE SET decision=excluded.decision,revision=excluded.revision",
                (jurisdiction, classification, sensitivity, purpose, decision, revision),
            )
            write(
                self.connection,
                at,
                actor,
                "policy.set",
                jurisdiction,
                {"purpose": purpose, "sensitivity": sensitivity, "revision": revision},
            )
        return revision

    def decision(self, jurisdiction, classification, purpose, sensitivity=None):
        """返回 ``(decision, revision, scope)``。

        优先匹配具体敏感度；没有具体规则时回落到 ``*`` 通配规则，
        再没有则默认拒绝（修订号为 0）。
        """
        candidates = ["*"] if sensitivity is None else [sensitivity, "*"]
        for scope in candidates:
            row = self.connection.execute(
                "SELECT decision,revision FROM policies "
                "WHERE jurisdiction=? AND classification=? AND sensitivity=? AND purpose=?",
                (jurisdiction, classification, scope, purpose),
            ).fetchone()
            if row:
                return row["decision"], row["revision"], scope
        return "deny", 0, sensitivity or "*"
