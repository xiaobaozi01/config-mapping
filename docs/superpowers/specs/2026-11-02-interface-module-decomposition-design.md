# 接口模块拆分设计

## 背景

Cisco 与 Juniper 的 `operations/interfaces.py` 目前分别约 736 和 890 行，
同时包含接口树结构访问、接口规格分析、业务依赖识别、接口修改事务和三个能力类。
文件规模已经超过单个开发者一次容易理解的合理范围。

现有 `InterfaceAnalysis` 和 `InterfaceTransform` Protocol 已经表达出最重要的
职责边界，但实现文件没有同步拆分。本次重构让文件结构与能力协议保持一致。

## 目标

- 把接口代码拆成结构访问、规格分析、业务分析和修改操作四个实现模块。
- 保持公共导入路径 `impl.<vendor>.operations.interfaces` 可用。
- 保持 `CiscoInterfaceAnalysis`、`CiscoInterfaceTransform`、
  `CiscoInterfaceCapabilities` 及其 Juniper 对应类兼容。
- 不改变接口分类、聚合关系、business 判断、事务或缓存语义。
- 让规则和 support 模块依赖明确的接口模块，而不是大文件中的所有内容。

## 非目标

- 不修改接口算法或转换顺序。
- 不修改 `InterfaceAnalysis`、`InterfaceTransform` 协议。
- 不把接口能力移回 Document。
- 不拆分 Group、Pruning 或规则运行时。
- 不改变报告、映射或设备输出。

## 方案比较

### 方案 A：只按 analysis/transform 拆成两个文件

优点是文件少；缺点是 size 仍然偏大，尤其 Juniper 的 business 分析约数百行。
结构访问被 analysis 与 transform 共同依赖，容易形成模糊的反向导入。

### 方案 B：拆成 access、analysis、business、transform 和能力门面

推荐该方案。结构访问与业务判断分开，analysis 可以复用 business 推导，transform
只依赖结构访问和语法编辑。每个文件有清晰职责，也能直接对应能力协议。

### 方案 C：每个公开函数一个文件

文件过碎，导入和维护成本高于收益，不采用。

## 目标目录

```text
src/impl/<vendor>/operations/interfaces/
├── __init__.py
├── access.py
├── analysis.py
├── business.py
├── capabilities.py
└── transform.py
```

`operations/interfaces.py` 删除，由同名包替代。Python 导入路径
`impl.<vendor>.operations.interfaces` 保持不变。

## 文件职责

### `access.py`

只负责访问接口树和规范化名称，不进行业务判断，不修改节点。

Cisco：

- `interface_nodes`
- `find_interface_node`
- `resolve_interface`

Juniper：

- `interfaces_block`
- `interface_nodes`
- `interface_name`
- `unit_nodes`
- `unit_number`
- `find_vlan`
- `find_inner_vlan`
- `find_interface_node`
- `resolve_interface`
- `walk_effective_nodes`，由现有私有 `_walk` 改为公共名称

### `analysis.py`

负责把接口树转换为跨厂商规格，并实现只读分析能力。

两个厂商共同包含：

- `interface_specs`
- `interface_kind`
- `bundle_members`
- `interface_has_business`
- `bundle_binding_is_ambiguous`
- `logical_names_under`
- `CiscoInterfaceAnalysis` 或 `JunosInterfaceAnalysis`

接口分析继续通过 Document 的 `_read_interface_analysis()` 缓存结果。分析模块不
持有 Document，也不保存跨调用缓存。

### `business.py`

负责判断接口是否承载业务，以及广播域、VLAN 和网关之间的关联。

Cisco：

- `bridge_domain_bindings`
- `gateway_inner_vlan_hints`
- `external_configuration_text`
- `business_interface_names`

Juniper：

- `business_interface_names`
- `collect_interface_business`
- `collect_reference_business`
- `expand_vlan_tokens`，由现有私有 `_expand_vlan_tokens` 改为公共名称
- `activate_domain_gateways`
- `domain_gateways`
- `collect_active_vlans`，由现有私有 `_collect_active_vlans` 改为公共名称
- `interface_index`
- `update_active_vlans`

规则模块不再导入私有 `_collect_active_vlans`、`_expand_vlan_tokens` 或 `_walk`。
它们改用 `business.collect_active_vlans`、`business.expand_vlan_tokens` 和
`access.walk_effective_nodes`。

### `transform.py`

负责接口树修改，并实现修改能力。

两个厂商共同包含：

- `remove_interface`
- `rename_interface_tree`
- `clone_interface_tree`
- `ensure_parent_interface`
- `CiscoInterfaceTransform` 或 `JunosInterfaceTransform`

厂商私有辅助函数随 transform 保留：

- Cisco：节点身份、克隆片段、重复接口合并、顶层插入
- Juniper：聚合 options 清理、重复接口合并

每个公开修改方法自行管理 `document.edit().bulk()`。不能因为拆分把事务移到包门面
或调用方。

### `capabilities.py`

只定义：

```text
CiscoInterfaceCapabilities
JunosInterfaceCapabilities
```

每次访问 `analysis` 或 `transform` 属性时按需创建对应轻量对象。

### `__init__.py`

作为稳定公共门面，重导出以下名称：

- 三个能力类
- `interface_nodes`
- `find_interface_node`
- `resolve_interface`
- `logical_names_under`
- 现有测试和外部调用依赖的 transform 公共函数

Juniper 额外重导出 `interface_name`、`unit_nodes`、`unit_number`、`find_vlan`
和 `find_inner_vlan`。

`__init__.py` 不实现业务逻辑。

## 依赖方向

```text
__init__.py
    -> capabilities.py
        -> analysis.py
        -> transform.py

analysis.py
    -> access.py
    -> business.py

business.py
    -> access.py

transform.py
    -> access.py
    -> syntax/
```

不允许：

- `access.py` 导入 analysis、business 或 transform。
- `business.py` 导入 analysis 或 transform。
- analysis 与 transform 相互导入。
- 各子模块反向导入 operations.interfaces 包本身。

## 事务和缓存

- Document 继续拥有接口分析缓存、树修订和 EditScope。
- analysis 和 business 只读，不创建 Editor。
- transform 的公开方法拥有局部事务。
- 嵌套调用必须复用当前 EditScope，不能开启 independent scope。
- 子模块不得产生长期 Document 引用或缓存 AST 节点。

## 兼容与调用方迁移

1. `operations/interfaces.py` 改为包目录，公共导入路径不变。
2. 现有调试图标和测试应改为 patch 实际定义模块：
   - analysis 收集函数从 `operations.interfaces.analysis` patch。
   - business 函数从 `operations.interfaces.business` patch。
   - structure 查询从 `operations.interfaces.access` patch。
3. 生产规则和 support 模块直接导入具体子模块，避免依赖包级重导出。
4. 包级 `__init__.py` 继续提供兼容重导出。
5. 迁移完成后删除旧单文件。

## 测试策略

新增架构测试：

- 六个目标模块对两个厂商都可导入。
- `operations/interfaces.py` 文件不存在，同名包存在。
- `analysis.py` 不包含修改函数，`transform.py` 不包含业务分析函数。
- access 不导入 analysis、business 或 transform。
- 包级导入继续返回同一组能力类和公共 helper。
- 能力类仍满足 `InterfaceAnalysis` 和 `InterfaceTransform` Protocol。

回归测试：

- 接口规格、聚合关系、业务接口识别不变。
- 改名、克隆、删除和父口创建的事务回滚不变。
- 缓存跨树修改正确失效。
- 规则和 support 对接口树的读取结果不变。
- 完整测试、Pyink、Ruff、docstring、函数长度和 diff 检查通过。

## 验收标准

- 两份 `operations/interfaces.py` 均拆成目标子包。
- analysis、business、transform 和 access 没有反向依赖。
- 公共导入路径与能力类保持兼容。
- 规则和 support 不再导入接口模块私有 helper。
- Document 接口能力入口签名不变。
- 所有现有行为测试和新增架构测试通过。
- 相同 fixture 的配置、映射和报告输出与基线一致。
