# 配置处理规则扩展

配置处理分为四个执行时机：裁剪在接口迁移前执行，接口适配在清洗后执行，模拟适配随后执行，最终校验最后执行。流程顺序保持固定。

Cisco 和 Juniper 的裁剪、接口适配、兼容校验、最终校验和模拟适配代码规则分别使用
`CiscoVisitorRule` 与 `JunosVisitorRule`：注册项声明范围和执行策略，Visitor 编写
匹配及业务动作，会话与执行器管理文本事务和结果。底层机制分别见
[手写 AST 说明](config-ast.md)。Cisco 完整可运行模板见
[`rules/examples.py`](../src/gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/rules/examples.py)，
内网大模型可直接使用[编写提示词](rule-templates/internal-model-prompt.md)。

## 先决定放在哪里

| 需求 | 入口 |
| --- | --- |
| 按路径和正则固定删除或替换 | 对应镜像的 `cleaning.yaml` |
| 结合拓扑和业务依赖裁剪非 NNI 配置 | 厂商规则注册表的 `PRUNE_RULES` |
| 根据接口类型和关系调整配置 | `ADAPT_RULES` |
| 调整模拟运行参数 | `SIMULATION_RULES` |
| 检查最终配置，不修改 AST | `VALIDATION_RULES` |

Cisco 注册表位于 `src/gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/rules/__init__.py`，Junos 位于 `src/gns3_config_adaptor/impl/juniper_vjunos_router_23_2R1_15_config_mapping_handler/rules/__init__.py`。没有新增规则配置文件；代码规则的增删通过注册表完成。非 NNI 接口、广播域、网关和 BGP 源的基础裁剪保留为一个完整规则；跨对象的 track 裁剪和普通接口绑定清理分别注册，按依赖顺序执行。

Cisco 的规则代码和镜像 YAML 集中在同一个包：

```text
gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/rules/
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

`from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler import rules` 访问同一套注册表与执行入口。新增业务模块直接放在此包内，再在 `__init__.py` 登记。共享协议仍位于 `gns3_config_adaptor/impl/common/rules/engine.py`。

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

硬件属性只由现有 `post_rewrite` YAML 清洗阶段执行删除，配置读取及校验继续位于 `CleaningRulesHandler`。UNI 迁移及最终校验不再读取 YAML 提取硬件表达式，也不增加迁移前的清洗阶段。

普通清洗仍按 YAML 的 `enable`、`path` 和动作执行。UNI 源父口属性保留在暂存树中等待清洗，业务按正确层级复制到目标接口；清洗完成后，兼容阶段检查父口剩余业务属性并移除暂存树。这样声明了删除规则的属性能先由正常清洗处理，不会在迁移阶段因为缺少硬编码识别而提前报错；清洗后仍不能安全继承的业务配置继续阻断输出。

Cisco 硬件规则覆盖接口及子接口；Junos 分别处理父口、options 和明确不适用于 unit 的物理过滤器属性，不扩大其他 unit 命令的删除范围，不删除有效 NNI 的 `802.3ad` 或 LACP。硬件清理仍不受模拟模式控制，实际是否删除由 YAML 开关决定。最终兼容校验继续检查已建模的不兼容残留，不执行删除。

聚合属性的共享定义位于各厂商 `support/interface_commands.py`。UNI 扁平化、普通聚合改名和目标兼容保留各自的匹配边界，不因为共用定义就删除有效 NNI 绑定。ethertype 的父口合并和冲突仍在兼容阶段处理，旧接口引用仍在最终校验检查。

Cisco 的未知接口命令统一由最终校验报告一次，结构化诊断带目标接口，并通过映射补充源接口列表；UNI 迁移不再重复告警。Junos 继续在 UNI 迁移阶段保留未知命令告警。

## 新增接口适配或校验

从 `gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules.support` 导入编写接口，继承 `CiscoRuleVisitor`（继承共享 ConfigVisitor）。例如新增一项实验 MTU 上限校验：

```python
from gns3_config_adaptor.impl.common.domain.interface import InterfaceKind
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules.support import CiscoRuleVisitor, CiscoVisitorRule, InterfaceScope


class TargetMtuVisitor(CiscoRuleVisitor):
    def visit_node(self, block):
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

这是新增实验约束的例子，不是默认的设备能力限制。接口适配使用同样的注册方式加入
`ADAPT_RULES`；省略 `readonly=True`，通过 `self.session.edits` 声明修改。接口
Outcome 默认从成功提交的实际编辑生成，不手工追加计划动作。Junos 使用相同的
Visitor 会话生命周期，入口位于 `gns3_config_adaptor.impl.juniper_vjunos_router_23_2R1_15_config_mapping_handler.rules.support`。

每个阶段使用自己的上下文：`PruneContext` 提供保护父口，以及前序规则已删除的接口和地址集合；`remove_non_nni` 区分完整裁剪与仅处理已删除成员依赖，`AdaptContext` 提供目标接口，`ValidationContext` 还提供已删除接口。裁剪执行器按注册顺序合并每条规则的删除结果，为下一条规则创建新上下文快照；其他阶段不需要传递删除状态。上下文使用 frozen dataclass 和 frozenset；规则不依赖应用层的 `ConversionContext`。

## 新增模拟适配

模拟规则指定 `SimulationAdaptationOutcome` 工厂及分类结果映射，使用已有镜像 Profile 中的 `SimulationAdaptationPolicy` 读取参数。需要新参数时在现有策略及 Profile 加载器中明确增加，不另建规则开关文件。以下注册假设业务 Visitor 已实现，编辑详情携带 `category`：

```python
from gns3_config_adaptor.impl.common.domain.outcomes import SimulationAdaptationOutcome
from gns3_config_adaptor.impl.cisco_iosxrv9k_7_11_1_config_mapping_handler.rules.support import simulation_result


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

## 会话、范围和 AST Visitor

通过 `document.root.nodes` 查询顶层节点，`node.nodes` 查询子命令，`node.path`
获取祖先命令路径，`document.writer.subtree(node)` 获取当前子树的规范化文本。`location` 仅记录原始位置。

规则继承 `CiscoRuleVisitor` 或 `JunosRuleVisitor`。覆盖 `visit_node(node)` 做局部分析，
通过 `visit_children(node)` 继续访问后代；覆盖 `visit_configuration(root)` 做全局分析。
`should_visit(node)` 可跳过整棵子树。每次执行创建独立会话、Outcome 和 Visitor；
`finish()` 在所有访问根结束后执行一次，用于跨节点检查和修改汇总。

通过 `session.document` 查询，`session.context` 读取阶段输入，`session.result`
收集诊断，`session.edits` 收集编辑。编辑动作包括删除、改头、替换子树、前后插入、
追加子节点、移动和克隆；每项操作提供原因，不能覆盖框架审计字段。
顶层创建使用 `append_top_level`，确保新配置位于 `end` 之前。

Visitor 不自行调用 Editor.apply 或文档修改方法。执行器遍历完成后应用编辑，
按节点关系维护结构，无需重新解析整份配置。文本片段在挂入树前完成语法校验。
业务冲突、运行异常或结果映射失败会恢复本规则开始前的树副本，清除变更统计，
保留诊断和前序规则已完成的结果。回滚后重新查询节点，不复用旧树引用。

只读规则声明 `readonly=True`，不能申请编辑器；执行器检查完整树签名，发现直接
写入时恢复树并抛出 `RuleContractError`。Listener 的进入/退出事件适合收集索引，
不应在遍历事件内修改子节点列表。上述约定不是 Python 安全沙箱。

非 NNI、BGP 源、track 和接口绑定裁剪仍使用同一套注册规则，保留原业务事件及
删除集合传递语义。底层操作数量不必等于业务事件数量；只有成功动作进入报告。

`source` 按当前 AST 输出，`render()` 另外应用镜像策略。Cisco 每层一个空格，Junos
每层四个空格；保留注释、banner 和字符串内容，统一 LF，不保证原始空白逐字一致。
完整接口和示例见[手写 AST 指南](config-ast.md)。

## 从模板新增一条规则

1. 简单删除或替换复制 [`rule-templates/cleaning.yaml`](rule-templates/cleaning.yaml) 的列表项到实际镜像 cleaning.yaml，填写匹配路径、动作及原因。模板文件本身不会加载。
2. 代码规则从 `rules/examples.py` 选择 Visitor、选择器及 `CiscoVisitorRule` 常量，复制到 `gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/rules/<业务名>.py`；替换 RULE_DEMO 合成条件与业务动作。
3. 在 `gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/rules/__init__.py` 显式导入新模块，在对应元组加入 `module.NEW_RULE`。放置位置决定顺序，尤其依赖裁剪应位于产生删除集合的规则之后；条件用具名函数声明，不使用 lambda。
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
```

所有手写源码执行相同的长度、复杂度和文档检查。扩展测试覆盖作用范围、空集合、冲突、幂等性和真实 Document 入口；状态比较使用树签名，不能依赖旧快照身份。规则回滚测试见 `tests/test_cisco_rule_runtime.py` 和 `tests/test_cisco_managed_pruning.py`。
