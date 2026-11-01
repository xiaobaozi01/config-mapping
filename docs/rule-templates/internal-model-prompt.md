# 内网大模型：配置规则编写提示词

将以下提示词和业务需求交给内网模型。配置样例只需在内网提供；使用合成配置即可开发框架。

---

你正在维护本仓库的 IOS XR → XRv9000 配置转换器。请实现下面的规则，并提供可运行测试。

## 业务输入（由需求方填写）

- 规则目标与原因：
- 执行阶段：清洗 / 依赖裁剪 / 接口适配 / 模拟适配 / 最终校验；无法确定时说明判断依据。
- 适用的配置路径、接口类型、父口/子接口范围：
- 前置条件、依赖的其他规则或删除对象集合：
- 命中前配置与期望配置（可用合成或内网样例）：
- 必须保留的配置及不应命中的反例：
- 同名对象、重复值、引用不明等冲突的处理要求：
- 模拟策略或镜像参数要求（如适用）：

信息不足以确定匹配范围、语义或冲突处理时，先提出具体问题；不要根据命令名称猜测设备能力。真实镜像约束需有需求方确认依据。

## 阅读顺序

1. 阅读仓库适用的 AGENTS.md，保留已有工作区修改。
2. 阅读 `docs/configuration-rule-authoring.md`、`src/impl/cisco/rules/__init__.py` 和 `src/impl/common/rules.py`，确认阶段、上下文与执行顺序。
3. 阅读 `src/impl/cisco/rules/` 中的 `support.py`、`runtime.py`、`scopes.py` 和 `docs/config-ast.md`，理解托管生命周期及文本重写。
4. 选择 `src/impl/cisco/rules/examples.py` 中对应类型的模板，并参考 `tests/test_rule_extensions.py`。真实删除与移动示例见 `compatibility.py`。

## 实现约束

- 按路径和正则固定删除、替换优先使用已有 cleaning.yaml；复制 `docs/rule-templates/cleaning.yaml` 的列表项，保留现有 YAML 执行语义。
- Cisco 裁剪、适配、兼容校验、最终校验和模拟适配代码规则统一放入 `impl/cisco/rules/<业务名>.py`，从 `impl.cisco.rules.support` 导入 `CiscoRuleVisitor`、`CiscoVisitorRule` 和范围工具。定义 Visitor 与注册常量，在 `impl/cisco/rules/__init__.py` 对应阶段显式登记一条常量，ID 为 `cisco.<阶段>.<业务名>`；不要为跨对象规则另建 FunctionRule 分支。
- 注册项声明 Visitor 工厂、selector、具名 condition、readonly、outcome_factory 和可选 result_mapper。每次执行使用新的 Visitor、会话和 Outcome；不在模块变量或注册项缓存节点、编辑器或设备状态。
- 不修改流水线来接入普通规则，不自动发现目录，不添加通用参数字典或新的规则配置体系。确需新业务参数时扩展既有策略和 Profile 加载契约。
- AST 是可变配置状态。Visitor 通过 session.document 查询、session.result 收集诊断、session.edits 收集编辑；执行器在遍历完成后统一应用修改，并在失败时恢复树副本。
- 命令头修改用 replace_header(context, new_header, reason)，完整子树修改用 replace_subtree；输入文本，不克隆或新建 CiscoNode。
- selector 从当前快照返回互不重叠的访问根；使用 `InterfaceScope("target_interfaces" 或 "data_interfaces", include_subinterfaces, kinds)` 显式声明接口范围，空集合不匹配。目标子接口也按父口归一。整份文档使用 document_scope，兼容校验范围使用 compatibility_interfaces_scope（包含全部 BVI），不得无依据扩大范围。
- 使用 node.path 判断祖先语义，覆盖 visit_node 或 visit_configuration，调用 visit_children 继续遍历。finish 在全部访问根完成后运行一次。回滚后必须重新查询节点。
- 移动和克隆使用 Editor.move 与 Editor.clone；文本片段先解析再挂入树。每项修改提供原因，禁止跨文档引用或循环挂载。
- 业务冲突追加到 `session.result.errors`，执行器丢弃本规则编辑及待报告变化。提交或结果映射异常恢复本规则开始时快照，再向外抛出；不回滚前序成功规则。不吞掉 EditConflictError 等编程错误。
- 只读校验声明 readonly=True，不访问 edits；执行器不创建事务，检查快照和违规变更报告。裁剪指定 InterfacePruningOutcome 工厂，删除集合可暂存到 session.result；只有成功执行才累计传给下一规则，失败清空。
- 跨对象依赖也使用 CiscoVisitorRule：选择 document_scope，在 visit_configuration(root) 中构建索引或调用只读分析器，finish 汇总编辑。参考 pruning.py 的 NonNNIVisitor、TrackDependenciesVisitor；分析器不得创建、提交事务或直接写 Document。需要保留原裁剪报告时复用 PruningVisitor 的 record 和 pruning_result，在成功提交后生成 remove 事件；祖先覆盖后代时只声明祖先删除，业务事件可保留后代语义。
- 默认接口 Outcome 使用实际 RewriteResult.changes，不手工追加计划动作。模拟指定 SimulationAdaptationOutcome 工厂、simulation_result 映射，并为编辑填写 category；必要时 count 保留语义子树计数。自定义 `result_mapper(session, visitor, edits)` 在提交成功后修改 session.result，不返回 Outcome、不再写 Document；复合移动可汇总为 moved。不要手写 rule_id，聚合器会补充。
- 模拟规则遵守 off / compatible / stable 及相关策略开关。规则必须幂等；默认保留无法确定语义的配置，不扩大删除范围。
- common 不导入 cisco、juniper 或 adaptation；cisco 不导入 adaptation 或 juniper。每个新增、修改函数遵守仓库 50 行检查。
- 保持 COMPATIBILITY_VALIDATION_RULES 被最终校验复用的关系；只针对最终状态的校验放在附加部分。校验收集全部适用规则诊断，阶段间保持现有错误短路。
- 普通命令内容是不透明文本，业务匹配通常不修改语法；只有结构语法扩展才修改 .g4，并用固定 4.13.2 JAR 生成及检查，禁止手改生成文件。框架示例 RULE_DEMO 标记不能进入生产注册项，模板默认不启用。

## 必须输出

1. 规则意图、选择该阶段的原因、作用范围和冲突策略。
2. 实现代码及 `impl/cisco/rules/__init__.py` 的精确注册修改；YAML 规则给出实际列表项。
3. tests/fixtures 下的 .cfg 样例：命中、不命中、非目标对象及冲突。
4. 测试覆盖实际 Document 入口、报告规则 ID、作用范围（空集合、缩写、父子接口、非目标对象）、多设备及重复执行不共享状态、幂等性、业务冲突不提交。校验比较执行前后解析快照；依赖裁剪验证删除集合传递，模拟验证模式、开关及分类计数。新增结果映射需验证映射失败恢复且失败动作不进报告。
5. 执行下列检查并如实报告结果；测试失败时修复，不能声称测试通过等价于实机验证。

```sh
python3 scripts/check_method_lengths.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
git diff --check
```
