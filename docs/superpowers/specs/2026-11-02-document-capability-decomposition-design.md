# Document 能力拆分设计

## 背景

`CiscoDocument` 和 `JunosDocument` 当前分别有 36 和 46 个方法，
`VendorConfiguration` Protocol 要求实现 30 个业务能力。多数方法只是转发到
`operations`、`support` 或 `rules`，但每个调用方仍依赖整个宽接口。

本次重构不删除 Document。配置树、修订号、编辑事务和分析缓存具有强一致性要求，
继续由 Document 作为聚合根管理。业务能力改为按需创建的轻量能力对象，并通过窄
Protocol 暴露给对应流水线阶段。

## 目标

- Document 只保留配置树生命周期、语法访问、hostname、渲染和能力组合入口。
- `VendorConfiguration` 不再包含全部业务方法。
- 每个能力 Protocol 只覆盖一个流水线职责。
- 能力对象无长期状态，不自建树缓存，不形成 Document 与能力对象之间的强引用环。
- 流水线阶段只依赖自己需要的窄能力，不再依赖 30 个方法的宽接口。
- Cisco 和 Juniper 使用同一套能力边界，但保持各自实现。
- 保持现有转换顺序、输出、错误、告警、缓存失效和回滚语义。

## 非目标

- 不把 Document 缩减到只有 AST 操作。
- 不为每个方法建立独立 Service 类。
- 不引入依赖注入容器或 Service Locator。
- 不改变转换阶段顺序。
- 不重写接口、Group、清洗或校验算法。
- 不改变输出文件、报告字段或实验账号。

## 目标结构

共享能力协议位于：

```text
src/impl/common/domain/capabilities/
├── __init__.py
├── document.py
├── interfaces.py
├── groups.py
├── bundles.py
├── cleaning.py
├── pruning.py
├── compatibility.py
├── validation.py
├── references.py
└── simulation.py
```

厂商能力实现继续位于各自的 `operations`、`support` 和 `rules`：

```text
src/impl/cisco/operations/interfaces.py
    CiscoInterfaceCapabilities
    CiscoInterfaceAnalysis
    CiscoInterfaceTransform

src/impl/cisco/operations/bundles.py
    CiscoBundleOperations

src/impl/cisco/operations/groups.py
    CiscoGroupOperations

src/impl/cisco/operations/cleaning.py
    CiscoCleaningOperations

src/impl/cisco/...
    CiscoPruningOperations
    CiscoCompatibilityOperations
    CiscoValidationOperations
    CiscoReferenceOperations
    CiscoSimulationOperations
```

Juniper 使用相同结构，类名前缀为 `Junos`。

Document 最终公开：

```python
document.interfaces.analysis
document.interfaces.transform
document.bundles
document.groups
document.cleaning
document.pruning
document.compatibility
document.validation
document.references
document.simulation
```

## Document 核心职责

Document 保留：

- `root`
- `source`
- `revision`
- `edit()`
- `tree_changed()`
- `restore_tree()`
- `__deepcopy__()`
- `parse_fragment()`
- `normalize_header()`
- `validate_header()`
- `hostname()`
- `render()`
- 厂商 parser、writer、header 缓存和接口分析缓存的所有权
- 能力属性入口

Document 不再实现：

- 接口查询和接口修改方法
- Bundle/ae 业务方法
- Group 展开方法
- 清洗、裁剪、兼容、校验、引用和模拟方法

配置语法辅助，例如 Juniper 的 `_top_block`、`_interface_nodes`、
`_unit_nodes`、`_find_vlan`、`_find_inner_vlan`，从 Document 移出：

- 通用顶层块查询进入 Juniper `syntax/` 辅助模块。
- 接口、unit、VLAN 和业务分析进入 `operations/interfaces.py`。
- Document 不再保留同名私有转发方法。

## 能力协议

### `DocumentCore`

```text
vendor: str
hostname() -> str | None
render() -> str
```

供拓扑输入校验、报告和最终输出使用。

### `VendorDocument`

组合 `DocumentCore` 和以下能力属性：

```text
interfaces: InterfaceCapabilities
bundles: BundleOperations
groups: GroupOperations
cleaning: CleaningOperations
pruning: PruningOperations
compatibility: CompatibilityOperations
validation: ValidationOperations
references: ReferenceOperations
simulation: SimulationOperations
```

`DeviceContext.document` 使用该组合类型。单条流水线函数和阶段内部应继续收敛为
实际使用的能力类型，而不是接收整个 `VendorDocument`。

### `InterfaceCapabilities`

```text
analysis: InterfaceAnalysis
transform: InterfaceTransform
```

### `InterfaceAnalysis`

```text
interface_specs() -> list[InterfaceSpec]
interface_kind(name) -> InterfaceKind
bundle_members() -> dict[str, str]
interface_has_business(name) -> bool
bundle_binding_is_ambiguous(name) -> bool
resolve_interface(value) -> str
logical_names_under(parent) -> list[str]
business_interface_names() -> set[str]
```

### `InterfaceTransform`

```text
remove(name, include_children=False) -> None
rename(source, target, strip_bundle=False) -> None
clone(source, target, strip_bundle=False) -> None
ensure_parent(name) -> None
```

### `BundleOperations`

```text
mode(member) -> BundleMode
adapt(name) -> InterfaceAdaptationOutcome
```

### `GroupOperations`

```text
expand(known_interfaces, policy) -> GroupExpansionOutcome
```

### `CleaningOperations`

```text
clean_management_access() -> CleanupOutcome
add_lab_account() -> None
apply_rule(path, match, action, value) -> int
apply_rules(operations) -> list[int]
```

`apply_rule` 和 `apply_rules` 属于同一能力，不拆成两个服务。

### `PruningOperations`

```text
prune_non_nni(protected_parents, strict_input_validation=True)
    -> InterfacePruningOutcome
prune_interface_dependencies(removed_interfaces, strict_input_validation=True)
    -> InterfacePruningOutcome
```

### `CompatibilityOperations`

```text
adapt_interfaces(target_interfaces) -> InterfaceAdaptationOutcome
```

### `ValidationOperations`

```text
validate_nni_clone(source, target_interfaces)
    -> InterfaceAdaptationOutcome
validate_source_semantics(target_interfaces)
    -> InterfaceAdaptationOutcome
validate_final_integrity(
    target_interfaces,
    removed_interfaces,
    deferred_interface_references=(),
) -> InterfaceAdaptationOutcome
validate_interfaces(
    target_interfaces,
    removed_interfaces,
    deferred_interface_references=(),
    strict_input_validation=True,
) -> InterfaceAdaptationOutcome
```

### `ReferenceOperations`

```text
replace(replacements: dict[str, list[str]]) -> None
```

### `SimulationOperations`

```text
adapt(policy, data_interfaces) -> SimulationAdaptationOutcome
```

## 能力对象生命周期

Document 能力属性按需创建轻量对象：

```python
@property
def interfaces(self) -> CiscoInterfaceCapabilities:
    from .operations.interfaces import CiscoInterfaceCapabilities

    return CiscoInterfaceCapabilities(self)
```

能力对象只在当前调用链内持有 Document。禁止：

- 在 Document 构造器中长期创建并保存所有能力对象。
- 在能力对象中缓存配置树或分析结果。
- 让能力对象反向注册到 Document 的单例容器。

原因：

- 现有测试要求文档和缓存在释放后可以被回收。
- 长期双向引用会形成对象环并延长内存生命周期。
- 能力对象需要读取的树状态和修订号必须始终来自当前 Document。

## 状态、缓存与事务

### 分析缓存

Document 继续拥有接口分析缓存。`InterfaceAnalysis` 只调用 Document 的缓存读取入口；
缓存失效仍由 `tree_changed()` 和 `restore_tree()` 统一管理。

能力对象不得复制或保存：

- `interface_specs` 结果
- `bundle_members` 结果
- `business_interface_names` 结果
- AST 节点列表

### 编辑事务

- `InterfaceTransform`、`BundleOperations`、`CleaningOperations` 和
  `PruningOperations` 在修改树时自行创建 `document.edit().bulk()` 或普通 Editor 事务。
- 同一次能力方法内的修改必须一次性提交；失败时由 `EditScope` 回滚。
- NNI 等跨多个能力调用的流程可以在外层创建回滚作用域，嵌套作用域继续复用外层边界。
- Document 不再重复包裹底层能力已经管理的普通事务。

### 只读校验

`ValidationOperations` 只读，不得创建 Editor。结果仍通过
`InterfaceAdaptationOutcome` 返回，由应用层统一写入报告。

## 应用层迁移

流水线阶段目标调用：

```text
GroupExpansionHandler
    -> document.groups.expand(...)

SourceSemanticValidationHandler
    -> document.interfaces.analysis.*
    -> document.validation.validate_source_semantics(...)

PreconfiguredInterfaceCleanupHandler
    -> 保持 Cisco 专用阶段，继续访问 CiscoDocument.root

InterfaceClassificationHandler
    -> document.interfaces.analysis.interface_specs()

NonNNICleanupHandler
    -> document.pruning.prune_non_nni(...)

NNIHandler
    -> document.interfaces.analysis.*
    -> document.interfaces.transform.*
    -> document.bundles.*
    -> document.validation.validate_nni_clone(...)

ReferenceRewriteHandler
    -> document.references.replace(...)

InterfaceCompatibilityHandler
    -> document.compatibility.adapt_interfaces(...)

FinalInterfaceValidationHandler
    -> document.validation.validate_final_integrity(...)

SimulationAdaptationHandler
    -> document.simulation.adapt(...)
```

`PreconfiguredInterfaceCleanupHandler` 是 Cisco 专用流水线阶段，不属于共享
`VendorDocument` 能力。本次保留其 `cast(CiscoDocument, ...).root` 用法，
不把 `root` 暴露到共享 Protocol；以后如需扩展语法维护能力，再单独建模。

## 兼容策略

迁移期间：

1. 新能力先与旧 Document 方法并存。
2. 旧方法只转发到新能力，保持测试和外部调用可用。
3. 应用层按 Stage 逐个迁移到新能力。
4. 所有生产调用和新测试完成后，删除 Document 上的旧业务方法。
5. `VendorConfiguration` 名称保留为 `VendorDocument` 的兼容别名，包级导入
   `from impl.common import VendorConfiguration` 继续有效。

最终结构不再让应用层调用：

```text
document.interface_specs()
document.prune_non_nni()
document.validate_interfaces()
document.apply_cleaning_rules()
```

## 分阶段实施

### 阶段 1：整理厂商接口实现

- 将 Juniper 的接口、unit、VLAN 和顶层块辅助逻辑下沉。
- 保持 Document 公共方法不变。
- 建立统一的接口修改事务边界。

### 阶段 2：引入能力协议与能力对象

- 新增 `common/domain/capabilities/`。
- 为 Cisco 和 Juniper 添加能力类和 Document 能力属性。
- 旧 Document 方法改为兼容转发。

### 阶段 3：迁移只读接口能力

- 迁移 `resolve_interface`、`interface_specs`、`interface_kind`、
  `bundle_members`、业务接口识别和逻辑名称查询。
- 每次迁移后运行接口、拓扑、NNI 和缓存测试。

### 阶段 4：迁移修改型能力

- 迁移接口改名、克隆、删除和父口创建。
- 迁移 Bundle、Cleaning 和 Pruning。
- 明确事务全部由能力层负责。

### 阶段 5：迁移流水线阶段

按以下顺序逐 Stage 迁移并保持测试绿：

1. Group
2. 源语义校验
3. 接口分类
4. 非 NNI 裁剪
5. NNI
6. 引用改写
7. 目标兼容
8. 模拟适配
9. 最终校验

### 阶段 6：收窄公共协议并删除旧接口

- `DeviceContext.document` 改用 `VendorDocument`。
- `VendorConfiguration` 改为兼容别名。
- 删除 Document 上的业务转发方法。
- 更新测试直接使用对应能力。

## 错误与回滚

- 能力方法继续返回现有 Outcome 类型，不引入新的异常协议。
- 修改型能力内部失败必须回滚当前范围。
- 校验能力不得修改树。
- 能力层不得吞掉异常；未预期异常继续交给 Pipeline 的首错终止和内部报告机制。
- Group 展开仍保持事务式全量提交或回滚。

## 测试策略

新增架构测试：

- Document 类只包含核心方法和能力属性。
- 公共能力 Protocol 不包含其他能力的方法。
- Cisco 和 Juniper 的所有能力对象都满足对应 Protocol。
- 应用层阶段不调用已删除的旧 Document 业务方法。
- 能力属性按需返回对象，Document 不长期保存能力对象集合。

新增行为测试：

- 每种能力通过新入口执行后，渲染结果与旧入口一致。
- 接口修改失败后树、修订号和分析缓存恢复一致。
- 查询能力结果在树修改后失效。
- 能力对象释放后不阻止 Document 被回收。
- 嵌套 EditScope 外层回滚覆盖多个能力调用。

回归测试：

- 完整运行现有测试。
- 运行 Pyink、Ruff、函数长度和 docstring 门禁。
- 对比目录重组基线 `3ebb24c` 的 fixture 输出，确保配置、映射和报告不变。

## 验收标准

- `DocumentCore` 和能力 Protocol 按本规格拆分。
- `DeviceContext.document` 不再依赖 30 个业务方法的宽协议。
- Document 不再直接定义接口、Bundle、Group、清洗、裁剪、兼容、校验、引用和
  模拟业务方法，只保留能力入口。
- Cisco 和 Juniper 共享能力边界，但厂商算法仍位于各自实现包。
- 分析缓存、修订号和 EditScope 仍由 Document 统一协调。
- 能力对象不会形成长期循环引用。
- 所有现有测试和新增架构测试通过。
- 相同 fixture 的配置、拓扑、映射和报告与基线一致。
