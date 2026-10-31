"""配置语义 identity 规则的内部数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MergeKind(StrEnum):
    """同一配置路径下语句的合并类型。

    不同的合并语义决定 group 继承时是覆盖、按键合并还是追加；显式枚举可避免在
    解析器各处散落魔字符串，也让规则文件里的 ``merge`` 字段可被校验。
    """

    REPLACE = "replace"
    KEYED_SET = "keyed-set"
    SET = "set"
    DIRECTIONAL = "directional"
    PRESENCE = "presence"
    ORDERED_LIST = "ordered-list"
    OPAQUE = "opaque"


@dataclass(frozen=True, slots=True)
class StatementContext:
    """一条待识别配置及其所在完整路径。

    identity 规则需要同时看语句文本和它所在的层级路径才能唯一判定语义；把二者
    打包成不可变上下文，既便于作为 ``resolve`` 的缓存键，也避免调用方散传参数。
    """

    vendor: str
    path: tuple[str, ...]
    statement: str
    node_kind: str = "leaf"


@dataclass(frozen=True, slots=True)
class SemanticRule:
    """从 YAML 加载并校验后的语义规则。

    ``loader`` 在启动时把 YAML 模板编译成不可变元组，之后匹配阶段只读使用；提前
    校验可让规则错误在加载阶段暴露，而不是在转换中途才失败。
    """

    rule_id: str
    node_kind: str
    path: tuple[str, ...]
    statement: tuple[str, ...]
    identity: tuple[str, ...]
    merge_kind: MergeKind


@dataclass(frozen=True, slots=True)
class SemanticDecision:
    """规则引擎对一条配置的识别结果。

    ``matched`` 区分“命中已知规则”与“回退为 opaque”，``identity`` 与 ``merge_kind``
    供 group 冲突判定比较；``normalized`` 保留供报告和歧义检测使用。
    """

    identity: tuple[str, ...]
    merge_kind: MergeKind
    rule_id: str | None
    matched: bool
    normalized: str

    @property
    def key(self) -> str:
        """转换为可写入 report.json 的稳定键。

        把 merge 类型和 identity 拼成一个字符串，便于报告与日志用同一稳定形式展示，
        也能直接作为字典键参与去重。
        """
        return f"{self.merge_kind.value}:{' '.join(self.identity)}"
