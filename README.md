# 跨机构数据协作台账

本服务记录数据来源、字段含义、合作许可、数据产品及交付凭据。合作方可在本地演练不同机构和法域之间的资料流转；示例记录均为虚构，不连接真实口岸或支付系统。

需要 Python 3.10 或更新版本，不依赖第三方包。

运行项目测试：`python -m unittest discover -s tests -v`。
检查源码：`python -m compileall -q exchange`。
演练入口：`python -m exchange --database demo.db`。

领域逻辑分布在来源目录、合作许可、产品编排和交付核验中。`exchange/service.py` 组合这些操作，`exchange/db.py` 保存本地 SQLite 表结构；调用接口和现有边界可从源码及测试读取。

## 字段级披露

披露范围由字段决定，而不是由整个来源决定：

- **来源目录**：`catalog.register` 登记字段定义；`catalog.define` 更新定义时递增来源 `schema_version` 和变更字段的 `definition_version`；`catalog.retire` 停用字段。每条记录入库时固定当时各字段的定义版本。
- **合作许可**：`grants.issue(..., fields=[...])` 必须逐字段登记，可写字段名或 `(字段名, 定义版本)`。空许可不授权任何披露；字段定义升级后旧许可自动失效，新增字段也不会继承旧许可。
- **法域规则**：`policies.set(..., sensitivity="standard"|"sensitive")` 分别约束普通字段和敏感字段；另有显式 `*` 通配规则用于兼容旧台账。
- **数据产品**：`products.assemble(..., fields={asset_id: [字段名]})` 必须明确引用字段，修订会固定每个字段的定义版本。来源字段定义更新或字段停用后，`products.stale` / `service.stale` 会列出需要重新判断的依赖；旧修订无法交付，也不能把旧记录里的过期字段值装进新修订。
- **交付凭据**：`send` 的返回值和 `receipt()` 包含 `included_fields`、`excluded_fields`、`source_versions`、`field_versions`、`policy_revisions`、`policy_scopes`、`grant_scopes`；`disclose(delivery_id)` 只给出接收方实际拿到的字段值。审计日志同样记录披露与排除清单及来源版本。

旧版按来源授权的数据库在打开时会自动迁移：字段定义从版本 1 起算，原有法域规则作为 `*` 通配规则保留。

