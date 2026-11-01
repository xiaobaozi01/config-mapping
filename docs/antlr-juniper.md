# Juniper Junos ANTLR4 解析与重写

Junos 与 Cisco 使用同一套源码快照和文本事务原则，但 grammar 独立维护在
`src/impl/juniper/antlr/`。`JunosDocument.source` 和当前 `JunosSnapshot`
是唯一配置状态，业务代码通过只读的 `JunosContext` 查询大括号树。

## 数据流

```text
源码 → JunosLexer → JunosParser → JunosContext
                                      ↓
                               JunosVisitorRule
                                      ↓
                           JunosRewriteTransaction
                                      ↓
                         候选源码 → 重新解析 → 原子发布
```

Grammar 只识别结构，不维护完整 Junos CLI 字典。语句头是不透明文本；Lexer 会
整体消费双引号、单引号、行注释和块注释，因此其中的 `{`、`}`、`;` 和 `#` 不会
改变层级。支持多行块、空块、行内嵌套块和可选的 `};`。

## Context

`JunosDocument.root` 是生成的 `JunosParser.ConfigurationContext`。Context 提供：

- `nodes`：直接语句或独立注释。
- `header`：不含块的 `{}`；叶子保留分号。
- `base_header`：去除 `inactive:` 和 `protect:` 后的语句。
- `parentCtx`、`path`、`text`、`indent`。
- `comment`：仅当前语句同一行末尾的注释。
- `is_block`、`is_comment_only`、`configured_inactive`、`effective`。
- `snapshot`：所属不可变解析快照。

Context 不提供 setter。节点删除会从下一个快照中真正移除；不能再用
`active=False` 隐藏配置。旧 Context 和旧事务在提交后失效。

`inactive:` 子树保留在源码和输出中，但不进入继承及接口业务语义；
`protect:` 参与语义匹配，并在 `render()` 的临时输出视图中移除。

## 重写事务

`document.rewrite()` 支持 `replace_header`、`replace_subtree`、`delete`、
`insert_before`、`insert_after`、`append_child`、`preview()` 和 `commit()`。
未修改区域按原字节保留，包括空白、注释、Unicode、CRLF 和无末尾换行；新增和移动
片段使用原文档换行符及目标缩进。

`commit()` 必须一次重新解析候选文本。外来 Context、过期 Context、祖先/后代重叠
编辑和区间内部插入都会抛出 `RewriteConflictError`。解析失败不发布快照。

Transaction 会传播“源码块/生成块”元数据。转换过程中新建的空容器不会因为
`empty_blocks.yaml` 而被意外输出；源码原有空块仍按路径策略转为分号、保留大括号
或省略。

## Visitor 规则

规则编写入口位于 `impl/juniper/rules/support.py`：

```python
from impl.juniper.rules.support import JunosRuleVisitor, JunosVisitorRule


class RenameVisitor(JunosRuleVisitor):
    def visitStatement(self, context):
        if context.header == "old;":
            self.session.edits.replace_header(
                context,
                "new;",
                "rename statement",
            )
        return self.visitChildren(context)


RULE = JunosVisitorRule("juniper.example.rename", RenameVisitor)
```

Visitor 只通过 `session.edits` 收集动作，执行器负责一次提交和失败恢复。只读规则
不能获取编辑入口。Junos 裁剪、接口适配、兼容/最终校验和模拟适配注册表都使用
`JunosVisitorRule`；复杂 Group、裁剪和模拟算法可在本次遍历内建立临时业务工作树，
但工作树不会成为 Document 的配置状态，发布仍只通过候选文本事务完成。

## 生成与检查

生成工具和 Python runtime 固定为 ANTLR **4.13.2**。使用官方 complete JAR：

```sh
python3 scripts/generate_antlr.py --jar /path/to/antlr-4.13.2-complete.jar
python3 scripts/generate_antlr.py --jar /path/to/antlr-4.13.2-complete.jar --check
```

默认同时检查 Cisco 和 Juniper；可用 `--vendor cisco` 或 `--vendor juniper`
只处理一个厂商。生成文件随安装包交付，运行和配置转换不启动 Java、不联网生成代码。

```sh
python3 scripts/check_method_lengths.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
```
