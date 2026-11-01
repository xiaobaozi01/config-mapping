# Group 模块拆分设计

## 背景

`CiscoGroupExpander` 与 `JunosGroupExpander` 目前分别位于约 1189 行和 1775 行的
`operations/groups.py` 中。单个类同时承担 Group 控制语句解析、选择器匹配、
定义发现、依赖和 exclusion 求值、继承合并、冲突诊断、工作树遍历、原子发布和
能力门面。

现有 `GroupOperations` Protocol 是稳定的对外入口，但文件内部没有对应的职责边界。
本次重构保持 Group 算法和报告语义不变，把实现拆成可独立理解和测试的协作模块。

## 目标

- 保持 `impl.<vendor>.operations.groups` 公共导入路径可用。
- 保持 `CiscoGroupExpander`、`CiscoGroupOperations`、`JunosGroupExpander` 和
  `JunosGroupOperations` 名称与行为兼容。
- 拆分控制语句解析、选择器匹配、定义/依赖处理、继承合并、展开调度和发布事务。
- 把 Cisco 与 Juniper 的厂商差异留在各自模块中。
- 不引入共享 Group 算法框架；当前共享层只保留能力协议和结果模型。
- 保持事务式展开、优先级、冲突、歧义、coverage 统计和失败回滚。

## 非目标

- 不修改 Group 优先级、选择器语法、exclusion 语义或 identity 规则。
- 不改变 `GroupExpansionOutcome`、报告字段或 Pipeline 阶段顺序。
- 不把 Group 能力写回 Document。
- 不抽取一个配置代价过高的跨厂商 Group 基类。
- 不拆分 `rules/` 中的 identity 规则包。

## 方案比较

### 方案 A：只拆成 `selectors.py` 和 `expander.py`

文件数量最少，但 Juniper 的定义归一化、依赖闭包、exclusion 和遍历仍会挤在
`expander.py`；Cisco 的合并与发布也会继续与递归展开耦合，改善有限。

### 方案 B：拆成 models、selectors、definitions、merger、expander、capabilities

推荐该方案。每个模块有单一职责，依赖方向稳定；Cisco 和 Juniper 可以共享文件名，
但内部算法和状态模型保持厂商专用。

### 方案 C：每个 Group 处理函数一个文件

文件数量过多，调用链和测试定位成本高，不采用。

## 目标目录

```text
src/impl/<vendor>/operations/groups/
├── __init__.py
├── models.py
├── selectors.py
├── definitions.py
├── merger.py
├── expander.py
└── capabilities.py
```

`operations/groups.py` 删除，由同名包替代。Python 导入路径
`impl.<vendor>.operations.groups` 保持不变。

## 文件职责

### `models.py`

保存一次展开过程的数据结构，不执行业务算法。

Cisco：

- `GroupStatement`
- `GroupDefinition`
- `GroupApplication`
- `GroupControls`
- `ExpansionState`

Juniper：

- `GroupApplication`
- `ResolvedGroupInheritance`
- `ExpansionState`
- `PreparedExpansion`
- `ResolvedStatement`

原内部类去掉下划线后仅在厂商 `groups` 子包内部使用；不从包级 `__init__.py`
导出。数据结构继续使用当前字段和默认值，不改变优先级排序信息。

### `selectors.py`

提供厂商专用的 `GroupSelectors` 显式组件，负责纯文本层面的 Group
控制语句和选择器处理，不修改文档。

两个厂商同名接口：

- `parse_group_names(statement, control_keyword) -> list[str]`
- `rewrite_group_control(statement, control_keyword, remaining_names)`
- `selector_matches(selector_header, target_header) -> bool`
- `selector_literal_length(selector_header) -> int`
- `payload_nodes_at_path(definition, target_path) -> list[...]`

Cisco 额外包含：

- `group_definition_from_nodes(...)`

Juniper 额外包含：

- `group_definition_name(header) -> str`
- `format_group_name(name) -> str`

`selector_matches` 继续使用各厂商当前语法：

- Cisco 使用 IOS XR 接口缩写和引号内正则。
- Juniper 使用 `<...>` 与 `fnmatch` 通配。

### `definitions.py`

提供厂商专用的 `GroupDefinitions` 显式组件，负责 Group 定义、依赖、
exclusion 和工作树的准备。组件只在一次展开中持有 Document 和选择器引用。

Cisco：

- `has_applied_group(statements) -> bool`
- `load_group_definitions(statements, state)`
- `contains_nested_apply(nodes) -> bool`
- `build_working_tree(statements, root_names, state)`
- `collect_root_group_names(root) -> list[str]`

Juniper：

- `has_group_controls(document) -> bool`
- `prepare_expansion(document, known_interfaces, policy)`
- `group_containers(root)`
- `normalize_group_containers(root)`
- `merge_group_definitions(containers)`
- `remove_extra_group_containers(root)`
- `add_known_interfaces(root, known_interfaces)`
- `group_dependencies(group)`
- `group_exclusions(node)`
- `root_group_exclusions(root)`
- `validate_group_exclusions(...)`
- `collect_root_group_names(root)`
- `resolve_dependency_closure(...)`
- `prune_excluded_groups(...)`

Juniper definition 模块继续持有 exclusion 策略输入。不得把 exclusion 逻辑
移动到 `expander.py` 或 `merger.py`。

### `merger.py`

负责把已定位的 Group 配置合并到目标节点，并生成冲突/歧义诊断。

每个厂商定义 `GroupMerger`，由 `expander` 在一次展开中显式创建。`GroupMerger`
只保存当前展开所需的文档引用、策略和 Outcome，不长期挂到 Document。

共同职责：

- `merge_inherited_children(...)`
- `merge_inherited_block(...)`
- `merge_inherited_statement(...)`
- `prepare_merge(...)`
- `handle_unknown_identity_ambiguity(...)`
- `ambiguity_record(...)`
- `merge_statement_by_precedence(...)`
- `record_group_conflict(...)`

Cisco 额外保留：

- `statement_decisions(...)`
- `find_matching_statement(...)`

Juniper 额外保留：

- `is_mergeable_group_child(...)`
- `resolve_target_statements(...)`

合并器必须通过 identity resolver 获得语义决策；不得复制 identity 规则判断。

### `expander.py`

负责一次 Group 展开的编排和递归遍历。

Cisco：

- `CiscoGroupExpander`
- `apply_group_applications(...)`
- `expand_node(...)`
- `read_local_group_controls(...)`
- `validate_group_references(...)`
- `resolve_effective_applications(...)`
- `rewrite_group_controls(...)`
- `rebuild_top_level_nodes(...)`

Juniper：

- `JunosGroupExpander`
- `validate_dependency_chain(...)`
- `validate_root_exclusions(...)`
- `validate_selected_exclusions(...)`
- `select_effective_applications(...)`
- `group_controls_for_path(...)`
- `collect_nested_exclusions(...)`
- `discover_nested_applications(...)`
- `resolve_group_inheritance_for_path(...)`
- `advance_inheritance(...)`
- `expand_current_node(...)`
- `merge_effective_groups(...)`
- `walk_expansion_tree(...)`
- `read_local_group_controls(...)`
- `rewrite_group_controls(...)`

`expander.py` 负责组合 `definitions` 和 `GroupMerger`，但不实现具体选择器匹配、
定义归一化或语义合并。

### Cisco 发布辅助

Cisco 的 `GroupStatement` 工作树需要独立发布。创建：

```text
src/impl/cisco/operations/groups/publishing.py
```

包含：

- `from_context(context, origin="explicit")`
- `clone_statements(nodes, origin=None)`
- `render_statements(nodes, depth=1)`
- `publish_expansion(document, statements)`
- `rollback_expansion(outcome)`
- 内部 `signature` 辅助

该模块只负责工作语句树与正式 AST 的边界，不参与 Group 继承决策。

### `capabilities.py`

只定义：

```text
CiscoGroupOperations
JunosGroupOperations
```

每次调用创建对应的 Expander 并转发 `expand()`。能力对象不保存展开状态。

### `__init__.py`

稳定兼容门面，只重导出：

- Cisco：`CiscoGroupExpander`、`CiscoGroupOperations`
- Juniper：`JunosGroupExpander`、`JunosGroupOperations`

Group 内部 models、selectors、merger 和 publishing 不从包级导出，避免新的私有
API 面。

## 依赖方向

```text
capabilities.py
    -> expander.py
        -> GroupDefinitions helper
        -> GroupMerger helper
        -> GroupSelectors helper
        -> publishing.py (Cisco only)
        -> models.py

definitions.py
    -> selectors.py
    -> models.py
    -> syntax/support/identity

merger.py
    -> selectors.py
    -> models.py
    -> semantic identity

selectors.py
    -> models.py
    -> syntax semantics

models.py
    -> common outcomes/policies only
```

禁止：

- `models.py` 导入任何 Group 算法模块。
- `selectors.py` 导入 definitions、merger 或 expander。
- definitions 与 merger 互相导入。
- expander 反向从包级 `__init__.py` 导入内部实现。
- 子模块长期缓存 Document、AST 节点或 Outcome。

## 事务与状态

- Document 继续拥有正式 AST、修订号、EditScope 和接口分析缓存。
- Cisco 在工作 `GroupStatement` 树上完成展开，只有成功结果通过
  `publish_expansion()` 发布。
- Juniper 在 AST 副本上完成 normalization 和展开，通过
  `document.edit().replace_root()` 原子提交。
- 任一活动 Group 无法安全求值或 identity 策略要求失败时，整次展开保持原树。
- `GroupMerger` 只在一次 `expand()` 生命周期内创建，不保存在 Document 或
  GroupOperations 上。
- 冲突、歧义、覆盖率和普通事件仍写入同一个 `GroupExpansionOutcome`。

## 兼容策略

迁移期间：

1. `operations/groups.py` 改为 `operations/groups/` 包目录。
2. `Document.groups` 的调用方式保持不变。
3. `impl.cisco.CiscoGroupExpander` 和 `impl.juniper.JunosGroupExpander` 的
   公共导出保持不变。
4. 现有测试继续通过包级路径导入扩展器；测试 patch 需要指向实际定义模块。
5. 完成后删除旧单文件，不保留转发文件。

## 测试策略

新增架构测试：

- Cisco/Juniper 的 models、selectors、definitions、merger、expander 和
  capabilities 模块均可导入。
- `operations/groups.py` 文件不存在，同名包存在。
- `models` 不导入算法模块。
- `selectors` 不导入 definitions、merger 或 expander。
- definitions 与 merger 不互相导入。
- 包级导入仍返回同一扩展器和能力类。
- `CiscoGroupOperations` / `JunosGroupOperations` 满足 `GroupOperations`。
- Cisco `publishing.py` 不导入 definitions 或 merger。

回归测试：

- Cisco/Juniper Group 定义、apply/exclude、优先级、冲突和 identity 覆盖不变。
- Juniper 嵌套 group、transitive dependency、exclusion、inactive/protect 和
  group 容器归一化不变。
- 无效引用、循环依赖和严格 identity 策略仍整体回滚。
- mapping、report 和最终渲染不变化。
- 完整测试、Pyink、Ruff、docstring、函数长度和 diff 检查通过。

## 验收标准

- 两份 `operations/groups.py` 均拆成目标子包。
- 旧单文件删除，公共导入路径和能力类保持兼容。
- 每个算法文件有单一主导职责，新增 Group 行为时无需修改 `__init__.py`。
- Cisco/Juniper 的扩展器状态只存活于一次展开调用。
- Group 事务、错误、冲突和覆盖率语义与基线一致。
- 所有现有测试和新增架构测试通过。
- 相同 fixture 的配置、拓扑、映射和报告与基线一致。
