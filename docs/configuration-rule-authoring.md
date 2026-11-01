# 配置处理规则扩展

配置处理分为四个执行时机：裁剪在接口迁移前执行，接口适配在清洗后执行，模拟适配随后执行，最终校验最后执行。流程顺序保持固定。

Cisco 的裁剪、接口适配、兼容校验、最终校验和模拟适配代码规则统一使用 `CiscoVisitorRule`：注册项声明范围和执行策略，Visitor 编写匹配及业务动作，会话与执行器管理文本事务和结果。底层机制见 [ANTLR4 说明](antlr-cisco.md)。完整可运行模板见
[`rules/examples.py`](../src/impl/cisco/rules/examples.py)，
内网大模型可直接使用[编写提示词](rule-templates/internal-model-prompt.md)。

## 先决定放在哪里

| 需求 | 入口 |
| --- | --- |
| 按路径和正则固定删除或替换 | 对应镜像的 `cleaning.yaml` |
| 结合拓扑和业务依赖裁剪非 NNI 配置 | 厂商规则注册表的 `PRUNE_RULES` |
| 根据接口类型和关系调整配置 | `ADAPT_RULES` |
| 调整模拟运行参数 | `SIMULATION_RULES` |
| 检查最终配置，不修改 AST | `VALIDATION_RULES` |

Cisco 注册表位于 `src/impl/cisco/rules/__init__.py`，Junos 位于 `src/impl/juniper/rules/__init__.py`。没有新增规则配置文件；代码规则的增删通过注册表完成。非 NNI 接口、广播域、网关和 BGP 源的基础裁剪保留为一个完整规则；跨对象的 track 裁剪和普通接口绑定清理分别注册，按依赖顺序执行。

Cisco 的规则代码和镜像 YAML 集中在同一个包：

```text
impl/cisco/rules/
├── __init__.py             # 显式阶段注册表与执行入口
├── runtime.py              # 会话、Visitor 生命周期、提交与结果映射
├── scopes.py               # 文档、目标接口、模拟数据口范围
├── support.py              # 规则编写统一导入入口
├── compatibility.py        # 聚合属性、ethertype 适配与只读校验
├── pruning.py              # 跨对象依赖裁剪
├── simulation.py           # 接口启用与 BFD 调整
├── examples.py             # 四类模板，默认不启用
└── xrv9000/                # 镜像清洗与输入整理 YAML
```

`from impl.cisco import rules` 访问同一套注册表与执行入口。新增业务模块直接放在此包内，再在 `__init__.py` 登记。共享协议仍位于 `impl/common/rules.py`。

接口删除的多级依赖按具体对象建模。Cisco `rules/pruning.py` 的 `TRACK_DEPENDENCIES_RULE` 以 `cisco.prune.track_dependencies` 注册到 `PRUNE_RULES`，通过 `TrackDependenciesVisitor` 处理“被删除接口 → line-protocol track → VRRP track object”链：只解除对象跟踪，保留 VRRP 组的其他配置。发现未知消费者或不明确的 track 定义时停止自动删除、保留原配置并记录 warning；它不属于固定命令清洗。普通非 NNI 裁剪和冗余成员清理都执行同一个 `PRUNE_RULES` 注册表；移除该条目会同时停止这两个入口的 track 裁剪。

## 新增固定清洗

在已有的 `cleaning.yaml` 中添加规则：

```yaml
- id: remove.interface.example
  vendor: cisco_iosxr
  enable: true
  category: example
  path:
    - '^interface\b'
    - '**'
  match: '^example-command\b'
  action: delete
  reason: '目标镜像不使用此功能'
```

声明式清洗规则只使用 `path + match + action`，不感知 NNI 映射、目标接口闭包或模拟 Profile。`path` 限定配置位置；如果需要只处理目标接口，应使用下面的代码型 `CiscoVisitorRule` 并用 selector 限定范围。

匹配按先删除后替换、同轮首条命中执行。关闭一条删除规则不会自动关闭最终校验；残留不兼容配置仍可能导致转换失败。

硬件属性删除现在只由 YAML 执行。Cisco 使用一条规则清理所有保留接口上的正反硬件命令；Junos 用两条路径分别匹配父口直接属性和 options 内部属性，不删除 `802.3ad` 或 LACP。硬件清理不受模拟模式控制；模拟规则只整理清理后变空的 options。

## 新增接口适配或校验

从 `impl.cisco.rules.support` 导入编写接口，继承 `CiscoRuleVisitor`（继承生成的 ANTLR Visitor）。例如新增一项实验 MTU 上限校验：

```python
from impl.common.interface import InterfaceKind
from impl.cisco.rules.support import CiscoRuleVisitor, CiscoVisitorRule, InterfaceScope


class TargetMtuVisitor(CiscoRuleVisitor):
    def visitNode(self, block):
        for node in block.nodes:
            words = node.header.split()
            if len(words) == 2 and words[0] == "mtu":
                value = words[1]
                if value.isdecimal() and int(value) > 9000:
                    self.session.result.errors.append(f"接口 {block.interface_name} MTU 超过实验上限")
        return None


# 在业务模块定义，再将常量添加至 VALIDATION_RULES 元组。
TARGET_MTU_RULE = CiscoVisitorRule(
    "cisco.validate.target_mtu", TargetMtuVisitor,
    selector=InterfaceScope("target_interfaces", False, frozenset({InterfaceKind.PHYSICAL})),
    readonly=True,
)
```

这是新增实验约束的例子，不是默认的设备能力限制。接口适配使用同样的注册方式加入 `ADAPT_RULES`；省略 `readonly=True`，通过 `self.session.edits` 声明修改。接口 Outcome 默认从成功提交的实际编辑生成，不手工追加计划动作。Junos 保持原有 `FunctionRule` 协议。

每个阶段使用自己的上下文：`PruneContext` 提供保护父口，以及前序规则已删除的接口和地址集合；`remove_non_nni` 区分完整裁剪与仅处理已删除成员依赖，`AdaptContext` 提供目标接口，`ValidationContext` 还提供已删除接口。裁剪执行器按注册顺序合并每条规则的删除结果，为下一条规则创建新上下文快照；其他阶段不需要传递删除状态。上下文使用 frozen dataclass 和 frozenset；规则不依赖应用层的 `ConversionContext`。

## 新增模拟适配

模拟规则指定 `SimulationAdaptationOutcome` 工厂及分类结果映射，使用已有镜像 Profile 中的 `SimulationAdaptationPolicy` 读取参数。需要新参数时在现有策略及 Profile 加载器中明确增加，不另建规则开关文件。以下注册假设业务 Visitor 已实现，编辑详情携带 `category`：

```python
from impl.common.outcomes import SimulationAdaptationOutcome
from impl.cisco.rules.support import simulation_result


def stable_mode(context):
    return context.policy.mode == "stable"


EXAMPLE_RULE = CiscoVisitorRule(
    "cisco.simulate.example", ExampleVisitor,
    selector=InterfaceScope("data_interfaces", True, frozenset({InterfaceKind.PHYSICAL})),
    condition=stable_mode,
    outcome_factory=SimulationAdaptationOutcome, result_mapper=simulation_result,
)
```

将规则常量加入 `SIMULATION_RULES` 即可；公共执行器调用 `applies(context)` 后调用 `apply(document, context)`。直接调用 `apply` 不额外判断条件，测试模式开关时应通过 Document 入口执行。

模拟规则各自声明模式条件。接口启用只处理目标数据口；已有 BFD 调整处理协议配置中的已有定时器，只提高低于阈值的值。新增规则应保持幂等，并测试 `off`、`compatible`、`stable` 和非目标接口。

## 会话、范围和 ANTLR4 Visitor

通过 `document.root.nodes` 查询顶层 Context，`context.nodes` 查询子命令，`context.path` 获取祖先命令路径，`context.text` 获取完整源码片段。Context 是当前修订的只读视图，不直接修改 ANTLR 的 children 或其他字段。

规则继承 `CiscoRuleVisitor`。局部匹配可覆盖 `visitNode(context)`，用 `self.visitChildren(context)` 访问子树；不调用即跳过该子树。需要先建立跨对象索引的规则可选择 `document_scope` 并覆盖 `visitConfiguration(root)`，在该入口调用只读依赖分析器。`finish()` 在全部访问根结束后执行一次，可完成聚合、冲突检查和待插入片段组装。每次执行创建独立会话、Outcome 和 Visitor，不能在注册对象或模块变量缓存 Context、事务或设备状态。

`session.document` 用于查询，`session.context` 提供阶段输入，`session.result` 收集诊断和删除对象集合，`session.edits` 只开放编辑收集。以下代码位于 Visitor 中：

```python
def visitNode(self, block):
    for command in block.nodes:
        if command.header == "description OLD":
            self.session.edits.replace_header(
                command, "description NEW", "更新描述", interface=block.interface_name,
            )
    return None
```

注册项的 `selector(document, context)` 从当前快照返回互不重叠的访问根；外来、过期、重复或祖先与后代同时入选会报协议错误。`document_scope` 选择整份文档；`InterfaceScope` 声明目标字段、子接口范围和允许类型，复用接口缩写规范化。它按目标父口选择范围，即目标是子接口时也先归一为父口。空接口集合不匹配任何接口，不创建 Visitor。

兼容校验使用专门的 `compatibility_interfaces_scope`，沿用目标父口闭包及文档中全部 BVI 的范围。BFD 按全局协议子命令范围访问，接口启用按目标数据口访问。新增代码规则应选择业务所需范围，不能一律遍历整份文档。访问范围不是编辑沙箱；ethertype 迁移会向源接口范围之外的父接口插入片段。

编辑入口提供 `replace_header`、`replace_subtree`、`delete`、`insert_before`、`insert_after`、`append_child`；参数使用 Context 和文本，不创建可变 CiscoNode。每项操作必须有原因，详情不能覆盖 rule_id 等框架审计字段。顶层创建用 `append_top_level(session.edits, session.document, text, reason)`，保持 end/commit 在配置末尾。移动通过删除源、把 `source.text` 插入目标表达，框架调整片段缩进。

Visitor 不调用 `document.rewrite()`、`commit()` 或其他 Document 修改方法。执行器在遍历完成且无业务错误时提交，候选源码重新解析成功才发布。业务冲突追加到 `session.result.errors`，本规则编辑和待报告变化全部丢弃。提交或结果映射异常会恢复本规则开始时的快照并重新抛出；前序规则的成功提交保留。不要吞掉 `RewriteConflictError` 等编程错误。

成功提交后旧 Context 不可用于新修订，下一规则重新选择范围。新插入节点不在当前轮访问。只读规则声明 `readonly=True`，不创建事务，访问 `session.edits` 会报协议错误；执行器也检查快照是否被替换，违规恢复并抛出 `RuleContractError`。这是一项编写契约，不是 Python 隔离沙箱，不允许直接篡改 Context 或 token。只读校验保留收集全部适用规则诊断的行为。

## 结果映射与复杂依赖

`outcome_factory` 默认创建 `InterfaceAdaptationOutcome`；裁剪模板指定 `InterfacePruningOutcome`，删除集合可先暂存到 `session.result`，执行成功后才交给现有聚合器累计，失败会清空。`readonly` 规则不能报告变化或删除对象。

默认映射将实际 `RewriteResult.changes` 写入接口 Outcome；`RewriteResult.total` 为实际操作数，删除子树计一次。可选 `result_mapper(session, visitor, edits)` 在成功提交后整理既有报告语义，不返回新 Outcome，也不能再次修改 Document。模拟规则用 `simulation_result` 按实际编辑的 `category` 统计，语义子树删除可用 `count` 保持原有语句计数。ethertype 用自定义映射把删除与插入汇总为既有 `moved` 事件。不要把计划中未提交的动作计入报告；rule_id 由原聚合器补充。

非 NNI、BGP 源、track 和接口绑定裁剪也使用同一套 `CiscoVisitorRule`。`NonNNIVisitor`、`MemberSourcesVisitor`、`TrackDependenciesVisitor` 和 `InterfaceBindingsVisitor` 在 `visitConfiguration` 中执行跨对象分析；分析器接收会话 Outcome 和记录回调，只产生诊断、删除集合与待删除节点，不创建或提交事务。

这些规则复用业务基类 `PruningVisitor`：`record(node, path, reason, references)` 暂存删除事件，`finish()` 过滤已被祖先删除覆盖的后代动作，并通过 `session.edits` 声明实际删除。提交成功后 `pruning_result` 映射为已有的 `remove` 事件，保留路径、原因及来源接口；祖先覆盖的后代业务事件也保持原报告语义，因此业务事件数量可与底层操作数量不同。失败时不报告这些事件或删除集合。现有 `prune_*` 函数仅作为委托规则对象执行的兼容入口。

Cisco 五类内置注册集合全部登记 `CiscoVisitorRule` 常量；新增 Cisco 规则遵循同一方式。共享协议保留 `FunctionRule` 以兼容 Juniper 和既有扩展。低层 `document.rewrite()` 仅供 Document 与解析、清洗等基础设施使用，规则及其分析器不自行管理事务。

未修改源码保留原空白、注释与换行；只格式化新增或移动片段。render 另行应用现有空块策略，不发布新修订。完整操作、冲突边界和生成代码维护方式见 [ANTLR4 说明](antlr-cisco.md)。

## 从模板新增一条规则

1. 简单删除或替换复制 [`rule-templates/cleaning.yaml`](rule-templates/cleaning.yaml) 的列表项到实际镜像 cleaning.yaml，填写匹配路径、动作及原因。模板文件本身不会加载。
2. 代码规则从 `rules/examples.py` 选择 Visitor、选择器及 `CiscoVisitorRule` 常量，复制到 `impl/cisco/rules/<业务名>.py`；替换 RULE_DEMO 合成条件与业务动作。
3. 在 `impl/cisco/rules/__init__.py` 显式导入新模块，在对应元组加入 `module.NEW_RULE`。放置位置决定顺序，尤其依赖裁剪应位于产生删除集合的规则之后；条件用具名函数声明，不使用 lambda。
4. 若希望校验同时参与独立兼容校验和最终校验，加入 COMPATIBILITY_VALIDATION_RULES；仅针对最终状态的校验加入 VALIDATION_RULES 的附加部分，避免在两个集合中重复登记。
5. 在 tests/fixtures 下提供合成 .cfg，复制对应模板测试，执行范围、冲突、幂等性和真实 Document 入口检查，再运行全量回归。

| 模板注册常量 | 注册集合 | 对应演示 |
| --- | --- | --- |
| `PRUNE_DEMO_RULE` | PRUNE_RULES | 消费前序删除集合，删除合成的 description 依赖 |
| `ADAPT_DEMO_RULE` | ADAPT_RULES | 目标接口改写，冲突时整条规则不提交 |
| `SIMULATE_DEMO_RULE` | SIMULATION_RULES | 按策略处理数据父口并汇总实际变化 |
| `VALIDATE_DEMO_RULE` | VALIDATION_RULES | Visitor 检查路径，只读返回错误 |

演示配置在 `tests/fixtures/rule_extensions/`，可运行测试在 `tests/test_rule_extensions.py`。四类模板不在默认注册表内，不会改变业务配置。规则执行器在执行任何规则前检查实际集合的空 ID 和重复 ID；同一条校验规则在不同入口复用是允许的。

## 结果与质量约束

接口变更包含 `rule_id`，错误和告警在 `diagnostics` 中保留规则来源；报告沿用现有事件类型。模拟报告的 `rule_changes` 按规则统计变更数。校验会收集全部适用规则的结果，阶段完成后仍由流水线按错误短路。

厂商规则只操作本厂商 AST，共享包不导入厂商或应用层模块。保留文档门面入口和结果类型。直接调用 `adapt_interfaces` 或 `adapt_to_simulation` 不再执行迁入 YAML 的硬件删除；需要完整转换时使用流水线。

手写新增、修改函数最多 50 行，不计空行和纯注释行；文档字符串计入，嵌套函数代码计入外层。按完整业务职责拆分，避免为减少行数压缩语句或制造大量转发方法。

```sh
python3 scripts/check_method_lengths.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
python3 scripts/generate_antlr.py --jar /path/to/antlr-4.13.2-complete.jar --check
```

ANTLR 生成文件通过专用生成一致性检查，不计入手写函数长度限制。长度检查默认检查工作区相对 HEAD 新增、修改的函数；传入文件路径时检查文件内所有函数。扩展测试至少覆盖作用范围、空集合、配置冲突、幂等性及真实 Document 注册入口；校验比较执行前后解析快照。运行机制测试见 `tests/test_cisco_rule_runtime.py`；真实裁剪规则的状态隔离、业务事件、删除集合传递和异常回滚见 `tests/test_cisco_managed_pruning.py`。
