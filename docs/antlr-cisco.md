# Cisco ANTLR4 解析与重写

Cisco 的唯一配置状态是当前源码和对应的 ANTLR 解析快照。`CiscoDocument` 保留转换器使用的业务方法，内部不再维护可变的 CiscoNode 配置树。Juniper 使用独立 grammar 和相同的快照/事务原则，见 [Junos ANTLR4 说明](antlr-juniper.md)。

## 数据流

```text
源码 → 生成的 Lexer → 缩进 TokenSource → 生成的 Parser → Context
                                                        ↓
                                                 Python 业务规则
                                                        ↓
                                              RewriteTransaction
                                                        ↓
                                             TokenStreamRewriter
                                                        ↓
                                           候选源码 → 重新解析
                                                        ↓
                                               原子发布新快照
```

结构语法只识别配置行、缩进块、分隔符、注释、group/end-group 和 banner。普通命令内容是不透明文本；它不验证完整 IOS XR CLI 能力，增加业务规则通常不需要改语法。缩进 TokenSource 产生空文本的 INDENT/DEDENT 标记，不构造另一份树；group 支持无缩进主体，banner 正文作为一个不透明 token，不会被接口或 group 分析误读。

Context 由 ANTLR 生成，提供只读查询属性：`header`、`nodes`、`parentCtx`、`path`、`indent`、`text`、`snapshot`。`nodes` 是语义子 Context 的元组；ANTLR 自身的 `children` 含终结符，不能作为业务节点列表使用。`header` 不包含缩进和换行，`text` 是当前节点及子树的原始源码范围。注释、空白也保存在 token 中，不能把 `header` 拼接当作原文输出。

框架保留 `active=True` 作为只读查询便利；删除会从新快照中真正移除节点，不再通过 active=False 隐藏。旧 Context 仍描述旧快照，禁止用它编辑新修订。

## 重写事务

Cisco 五类代码规则统一使用 `CiscoVisitorRule`，通过 `session.edits` 收集修改，由执行器创建并提交事务；完整示例见[规则指南](configuration-rule-authoring.md)。跨对象裁剪的分析器也只声明动作，下面的低层接口供 Document 与解析、清洗等基础设施使用。

`transaction = document.rewrite()` 绑定当前快照。执行器为每条修改规则创建并提交一次事务；基础设施可按业务操作使用下面的低层方法：

事务负责快照、冲突、审计、重新解析和原子发布；候选文本由事务内部的
`TokenStreamRewriter` 按 token index 生成。每次 `preview()` 和 `commit()` 都使用
独立的 rewriter program。

| 方法 | 行为 |
| --- | --- |
| `replace_header(context, header, reason, **details)` | 替换命令头，保留原缩进、换行和子树 |
| `replace_subtree(context, text, reason, **details)` | 替换完整子树；按原位置缩进格式化片段 |
| `delete(context, reason, **details)` | 删除完整子树，必要时整理删除造成的相邻分隔符 |
| `insert_before/insert_after(context, text, reason, **details)` | 插入同级片段，同位置按声明顺序处理 |
| `append_child(context, text, reason, **details)` | 追加子片段；根上追加表示新增顶层配置 |
| `preview()` | 获取候选文本，不发布，也不保证候选语法已通过验证 |
| `commit()` | 重写、重新解析、发布并返回实际变更；失败保持原快照 |

移动表达为提取 `context.text`、删除源、向目标追加。新增片段重新缩进；未修改区域的空白、注释、Unicode、CRLF、无末尾换行都保留。子命令追加与同级插入在叶节点末尾相遇时，子命令排在同级命令之前，避免意外改变父子关系。

使用 `append_top_level(transaction, document, text, reason)` 创建顶层块，确保配置位于 end/commit 之前。空片段及相同内容替换不发布新快照；成功的空操作事务也不能复用。

外来或过期 Context、重叠删除/替换、落在被替换区间内部的插入会触发 `RewriteConflictError`。位于删除区间起点、属于其他目标的插入可以组合，例如把紧邻源命令移入前面的空块。原子性限于单次事务；前序规则不会自动回滚。默认每次提交重新解析，后续规则必须重新查询，不保存跨修订索引或 Context。

`commit()` 返回 `RewriteResult`，其中 `changes` 提供实际操作、源码目标、原因和业务详情，`total` 为实际操作数。一次移动通常有删除、插入两个底层操作，业务规则可汇总为既有 moved 事件。rule_id 继续由规则聚合器补充。语法无效或提交失败时不返回成功结果，不能报告计划中未发布的动作。

## 托管规则生命周期

`rules/runtime.py` 的 `CiscoVisitorRule` 实现现有 ConfigurationRule 协议，Cisco 五类内置注册集合全部使用它；公共执行协议仍兼容 FunctionRule。每次执行创建独立 RuleSession、Outcome 和 CiscoRuleVisitor；选择器从当前快照返回互不重叠的访问根，生成的 ANTLR Visitor 负责分派。局部规则使用 visitNode，跨对象分析可使用 visitConfiguration；全部根结束后执行一次 finish，然后统一提交。新节点不在同轮重复访问，后续规则重新选择新快照。

Visitor 只通过会话收集动作和诊断。业务错误时不提交，实际提交后才运行结果映射；提交或映射异常恢复该规则开始时的快照并继续抛出。映射读取实际 RewriteResult 整理 Outcome，不能再次修改 Document。前序规则已成功的修改保留，避免扩大回滚范围。

只读规则不创建事务，不能获取编辑入口或报告变更；执行器检查快照替换，违规恢复并抛出 RuleContractError。Context、token 本身仍须按只读契约使用，执行器不是隔离沙箱。范围限制 Visitor 从哪些根进入；业务可显式编辑其他当前 Context，例如将子接口命令移到父接口。作用范围、条件和跨对象编辑必须在规则业务中明确。

## 保持的业务约定

- Document 的接口迁移、清洗、模拟、校验等业务方法及 Outcome 保持；内部规则通过生成的 Visitor/Listener 或只读 Context 查询实现。
- YAML 清洗仍按先删除、后替换的逻辑执行，每轮首条命中；删除父节点跳过其后代。Cisco 在单次 AST 遍历中模拟这两轮，父命令改名后子规则直接使用新路径，最终只重解析并发布一次候选配置。
- group 优先级、来源与冲突判断使用临时 group 业务工作记录；这些记录不是 Document 的第二份 AST，不用于普通规则编辑。发布时只重写有语义变化的块、删除已展开的定义与控制语句，其他显式块保留原文。
- `render()` 不修改文档或修订号；它在临时重写中应用现有空块策略。内部重新解析使用 `document.source`，不会提前应用最终输出策略。
- 接口分析缓存按已提交的修订清空，返回的集合及 InterfaceSpec 仍为独立副本。解析语法只检验结构，未知业务命令仍需规则与目标镜像验收。

## 生成与离线发布

生成工具与 Python runtime 固定为 **4.13.2**。将官方 `antlr-4.13.2-complete.jar` 放在开发环境可访问的位置，然后执行：

```sh
python3 scripts/generate_antlr.py --jar /path/to/antlr-4.13.2-complete.jar
python3 scripts/generate_antlr.py --jar /path/to/antlr-4.13.2-complete.jar --check
```

语法在 `src/impl/cisco/antlr/*.g4`；生成的 Lexer、Parser、Visitor 和 Listener 在相邻 generated 包。生成文件随发布包交付，不手工修改；Java 和 JAR 仅用于开发时重新生成，程序安装、运行、处理配置不调用 Java，也不联网生成代码。内网安装需准备包含 Python runtime 在内的依赖包。

生成代码不受手写函数 50 行限制，一致性由生成检查验证；手写 TokenSource、事务及业务模块仍执行长度检查。

```sh
python3 scripts/check_method_lengths.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
```

参考：[ANTLR Python target](https://github.com/antlr/antlr4/blob/master/doc/python-target.md)、[TokenStreamRewriter](https://www.antlr.org/api/Java/org/antlr/v4/runtime/TokenStreamRewriter.html)。
