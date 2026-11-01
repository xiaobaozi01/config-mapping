# 手写配置 AST

Cisco 和 Juniper 使用纯 Python Parser，不依赖解析器生成器、Java 或外部运行时。
配置树是唯一当前状态：Parser → ConfigNode → Visitor/Listener → ConfigEditor → Writer。

## 节点与解析

共享代码位于 `src/impl/common/config_ast/`，厂商的 `ast.py`、`parser.py` 和
`writer.py` 位于各自实现包。节点保存 `header`、`children`、`parent`、`kind`、
注释及原始 `location`。`header` 不含缩进和结构分号、大括号；`kind` 区分根、
普通语句、块、group、注释、分隔符及不透明 banner。空块仍是块。

`location` 是输入位置，不随编辑重算；插入和克隆节点不具有原始位置。
`nodes` 返回活动直接子节点的元组，`walk()` 使用显式栈遍历后代，`path` 返回祖先头。

Cisco 按缩进和 `!` 建树，支持 Tab、group/end-group 和带自定义结束符的 banner。
Junos 使用词法状态识别引号、转义、跨行注释及结构标点，支持行内多语句。
未知命令保留为普通文本；结构缺失或不平衡报错并带原始位置。

## 遍历与修改

`ConfigVisitor` 提供 `visit`、`should_visit`、`visit_configuration`、`visit_node`
和 `visit_children`。默认继续访问子节点；覆盖回调时可选择不继续访问。
`ConfigListener` 提供 `enter_node`、`exit_node`；`ConfigTreeWalker.walk` 用显式栈
按深度优先顺序发送配对事件。Listener 用于索引和信息收集，不在回调中改变树。

```python
from impl.cisco.document import CiscoDocument
from impl.common.config_ast import ConfigVisitor

class Interfaces(ConfigVisitor):
    def __init__(self):
        self.matches = []

    def visit_node(self, node):
        if node.interface_name:
            self.matches.append(node)
        return self.visit_children(node)

document = CiscoDocument("interface Gi0/0/0/0\n description old\n")
visitor = Interfaces()
document.root.accept(visitor)
editor = document.edit()
editor.replace_header(visitor.matches[0], "interface Gi0/0/0/1", "接口映射")
editor.apply()
print(document.render())
```

Editor 收集 `delete`、`replace_header`、`replace_subtree`、`insert_before`、
`insert_after`、`append_child`、`move`、`clone`，在 `apply()` 时统一改变树。
文本片段先解析，再挂到目标节点；`preview()` 在副本上预览。禁止修改虚拟根、
跨文档引用、已删除引用或循环挂载；每项修改提供非空原因并产生审计记录。
不要在遍历期间增删 `children`。复杂厂商算法可在受控规则内批量处理树，结束后
重建父子关系并刷新分析缓存。

## 规则生命周期

`CiscoVisitorRule`、`JunosVisitorRule` 每次创建独立会话、结果和 Visitor。
Visitor 通过 `session.edits` 登记修改，`finish()` 在所有访问根完成后运行一次。
执行器统一应用编辑并映射结果；Visitor 不自行调用 `apply()` 或文档修改方法。

执行前复制 AST。异常、业务错误或结果映射失败时恢复规则开始前的树及修订号，
清除该规则变更统计，保留诊断和前序规则成功结果。回滚后必须重新查询节点。
`readonly=True` 禁止申请编辑器，同时检查节点值和结构是否被改动。
这是规则契约，不是 Python 安全沙箱。

## 输出与兼容性

`source` 序列化当前树，`render()` 另外应用已有镜像空块及 protect 策略。
Cisco 每层一个空格，Junos 每层四个空格，使用 LF；非空输出以换行结束。
保留注释、引号及 banner 正文，不保证原始空行和缩进逐字一致。
`render()` 不改变树，规范化后的输出再次解析和输出应保持稳定。

旧 Context、snapshot、rewrite、生成 Visitor 以及驼峰回调已移除。
业务阶段上下文、CLI、YAML 和报告结构继续使用原接口。

## 验证

```sh
pytest -q
ruff check src tests scripts
pyink --check src
python scripts/check_method_lengths.py $(rg --files src -g '*.py')
python scripts/check_docstrings.py
```

`tests/test_config_ast.py` 覆盖解析、输出幂等、遍历、编辑和回滚；业务测试继续覆盖
接口映射、group、引用清理和跨厂商流水线。性能测试见 `benchmarks/config_ast.py`。

### 本地规模测量（2026-11-02）

执行 `PYTHONPATH=src python benchmarks/config_ast.py --blocks 25000`，启用 tracemalloc。
下表为本机一次运行，时间用于定位成本，不作为跨机器性能阈值。

| 厂商 | 输入行数 | 解析 | 遍历 | source 输出 | 单次树备份 | 备份峰值分配 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Cisco | 100000 | 1.709 s | 0.241 s | 0.582 s | 1.500 s | 33.23 MiB |
| Juniper | 100002 | 5.274 s | 0.177 s | 0.339 s | 1.119 s | 25.11 MiB |

规则与其内部的 Editor 共用一个回滚作用域，每条规则只创建一份备份；独立 Editor 操作自行建立作用域。
表中是单次树复制的成本，不是整个适配流水线的峰值内存或总耗时。

## Document 与 AST 的职责边界

Document 持有 `root`、修订和分析缓存，提供厂商能力入口；AST 节点保存结构及纯语义属性。
节点不导入 Document、Writer 或 Editor，不提供 `text` 输出属性。子树文本使用
`document.writer.subtree(node)`；Cisco 共享命令语义位于 `impl/cisco/semantics.py`。

清洗遍历位于厂商 `configuration_cleaning.py`，引用转换位于 `references.py`；
Document 对应方法只转发调用。语法校验委托 Parser，规范化文本及最终输出策略由
Writer 负责。`render()` 不创建 Editor、不复制树，也不改变修订或缓存状态。

结构修改统一由 Editor 管理：普通操作收集后 `apply()`；现有多步厂商算法使用
`with document.edit().bulk() as root:`，由 Editor 在退出时校验父子关系和更新缓存，
失败恢复原树。独立构建的 group 结果通过 `Editor.replace_root()` 发布。
只读查询不进入修改作用域。

规则执行器建立独立 `EditScope`，所有内部 Editor 批次和 bulk 操作复用该备份。
业务错误主动回滚，异常自动回滚；嵌套编辑错误即使被业务代码捕获，外层也不能
发布部分结果。相邻规则各自拥有独立作用域，因此后序失败不会撤销前序成功结果。
`tests/test_ast_boundaries.py` 检查依赖方向、纯读取输出、单次备份和批量失败恢复。
