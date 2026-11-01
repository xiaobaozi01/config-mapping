"""Junos Visitor 规则的生命周期、快照校验和受控编辑。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from impl.common.outcomes import (
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)

from ..antlr.context import JunosContext
from ..antlr.generated.JunosParserVisitor import JunosParserVisitor
from ..antlr.rewriting import RewriteResult

ContextT = TypeVar("ContextT")
OutcomeT = TypeVar(
    "OutcomeT",
    bound=InterfaceAdaptationOutcome | SimulationAdaptationOutcome,
)


class RuleContractError(RuntimeError):
    """表示规则违反托管执行器的只读、快照或提交约定。

    立即终止违规规则可防止绕过事务、复用过期 Context 或报告未发布变更。
    """


class RuleEdits:
    """只向 Visitor 暴露受控的文本编辑动作。

    白名单转发可阻止规则自行提交事务或访问执行器内部状态。
    """

    _ACTIONS = frozenset(
        {
            "delete",
            "replace_header",
            "replace_subtree",
            "insert_before",
            "insert_after",
            "append_child",
        }
    )

    def __init__(self, transaction):
        """绑定规则会话创建的重写事务。

        规则只能通过属性转发登记动作，事务生命周期仍由执行器独占。
        """
        self._transaction = transaction

    def __getattr__(self, name):
        """按白名单转发一个受支持的编辑方法。

        未列出的属性立即报错，避免规则直接调用提交或读取私有操作集合。
        """
        if name not in self._ACTIONS:
            raise RuleContractError("Visitor 只能收集编辑，提交由执行器管理")
        return getattr(self._transaction, name)


class RuleSession(Generic[ContextT, OutcomeT]):
    """保存单条规则执行期间的文档、结果和事务。

    会话集中管理快照一致性、只读限制和提交，使各 Visitor 无需重复实现生命周期。
    """

    def __init__(
        self,
        document,
        context: ContextT,
        result: OutcomeT,
        readonly: bool,
    ):
        """创建只读或可编辑的规则会话。

        只读会话不创建事务，从结构上保证校验规则无法登记配置修改。
        """
        self.document = document
        self.context = context
        self.result = result
        self.snapshot = document.snapshot
        self.readonly = readonly
        self._transaction = None if readonly else document.rewrite()
        self._edits = None if readonly else RuleEdits(self._transaction)

    @property
    def edits(self):
        """返回编辑收集入口；只读规则请求时立即失败。

        将只读约束放在访问入口而不是依赖规则自觉遵守，可形成强制安全边界。
        """
        if self.readonly:
            raise RuleContractError("只读校验规则不能申请编辑")
        return self._edits

    def assert_snapshot(self, snapshot):
        """确认文档没有被 Visitor 绕过事务修改。

        规则执行期间快照变化意味着编辑坐标失效，必须立即回滚并终止规则。
        """
        if self.document.snapshot is not snapshot:
            raise RuleContractError("规则绕过执行器修改了文档快照")

    def _commit(self) -> RewriteResult:
        """提交会话事务；只读规则返回空结果。

        统一完成入口让可编辑和只读规则共享执行器，同时避免虚假发布动作。
        """
        return RewriteResult() if self.readonly else self._transaction.commit()


class JunosRuleVisitor(JunosParserVisitor, Generic[ContextT, OutcomeT]):
    """为 Junos 规则绑定会话和遍历完成钩子。

    Visitor 通过会话读取 Context 和收集编辑，不能自行管理快照发布。
    """

    def __init__(self, session: RuleSession[ContextT, OutcomeT]):
        """绑定访问器所属的规则会话。

        固定会话引用使执行器能够校验访问器没有绕过当前事务。
        """
        self.session = session

    def finish(self) -> None:
        """在所有访问根结束后执行可选汇总。

        默认无动作，跨节点规则可在此统一分析后再登记编辑。
        """
        return None


class _DocumentActionVisitor(JunosParserVisitor):
    """在 ANTLR 根访问期间执行一次临时工作树动作。

    以生成 Visitor 作为统一入口，使复杂业务动作仍遵循规则执行协议。
    """

    def __init__(self, document, context, action):
        """保存文档、规则上下文和待执行动作。

        这些依赖由托管执行器注入，避免工作树动作自行查找全局状态。
        """
        self.document = document
        self.context = context
        self.action = action

    def visitConfiguration(self, context):
        """执行动作并返回其结果。

        根 Context 只用于触发统一访问生命周期，动作内部使用隔离工作树。
        """
        del context
        return self.action(self.document, self.context)


def _has_errors(result):
    """判断接口结果是否包含阻断提交的错误。

    错误状态优先于已收集编辑，确保失败规则不会发布部分配置。
    """
    return isinstance(result, InterfaceAdaptationOutcome) and bool(
        result.errors
    )


def _discard_changes(result):
    """丢弃未提交规则产生的变更统计。

    回滚不仅恢复文档，还必须清空结果，避免报告把计划动作误写成成功修改。
    """
    if isinstance(result, InterfaceAdaptationOutcome):
        result.changes.clear()
        if isinstance(result, InterfacePruningOutcome):
            result.removed_interfaces.clear()
            result.removed_addresses.clear()
    else:
        for name in ("added", "replaced", "removed", "rule_changes"):
            getattr(result, name).clear()


def _validate_roots(roots, snapshot):
    """校验访问根非空、属于当前快照且不存在祖先/后代重叠。

    重复覆盖会让同一子树被多次编辑并产生不确定冲突，因此启动前统一拒绝。
    """
    identities = set()
    for root in roots:
        if not isinstance(root, JunosContext) or root.snapshot is not snapshot:
            raise RuleContractError("访问根不属于当前 Junos 文档快照")
        if id(root) in identities:
            raise RuleContractError("访问根不能重复")
        identities.add(id(root))
    for root in roots:
        parent = root.parentCtx
        while parent is not None:
            if id(parent) in identities:
                raise RuleContractError("访问根不能同时包含祖先和后代")
            parent = parent.parentCtx


def _check_readonly_result(result):
    """禁止只读规则报告任何配置变更。

    即使没有实际写盘，虚假变更也会破坏调用方对校验规则无副作用的假设。
    """
    changes = (
        result.changes
        if isinstance(result, InterfaceAdaptationOutcome)
        else result.total
    )
    removed = isinstance(result, InterfacePruningOutcome) and (
        result.removed_interfaces or result.removed_addresses
    )
    if changes or removed:
        raise RuleContractError("只读校验规则不能报告配置变更或删除对象")


@dataclass(frozen=True, slots=True)
class JunosVisitorRule(Generic[ContextT, OutcomeT]):
    """声明 Junos Visitor 工厂、范围、条件和结果映射。

    执行器统一负责快照检查、提交和失败恢复，规则只描述业务动作。
    """

    rule_id: str
    visitor_factory: (
        Callable[
            [RuleSession[ContextT, OutcomeT]],
            JunosRuleVisitor[ContextT, OutcomeT],
        ]
        | None
    ) = None
    selector: Callable[[object, ContextT], Iterable[JunosContext]] = field(
        default=lambda document, context: (document.root,)
    )
    condition: Callable[[ContextT], bool] = field(default=lambda context: True)
    readonly: bool = False
    outcome_factory: Callable[[], OutcomeT] = field(
        default=InterfaceAdaptationOutcome
    )
    document_action: Callable[[object, ContextT], OutcomeT] | None = None

    def applies(self, context: ContextT) -> bool:
        """判断规则条件是否允许执行。

        条件在事务创建前求值，不适用规则不会分配会话或触碰文档。
        """
        return self.condition(context)

    def apply(self, document, context: ContextT) -> OutcomeT:
        """执行 Visitor，成功时只提交一次文本事务。

        标准 Visitor 和复杂工作树动作共用同一对外入口，但各自遵循原子提交边界。
        """
        if self.document_action is not None:
            return self._apply_document_action(document, context)
        return self._apply_visitor(document, context)

    def _apply_visitor(self, document, context: ContextT) -> OutcomeT:
        """执行标准 Visitor 会话并维护快照失败恢复。

        任何访问、提交或结果映射异常都会恢复原快照并清除未发布变更。
        """
        original = document.snapshot
        result = None
        try:
            result = self.outcome_factory()
            if not isinstance(
                result,
                (InterfaceAdaptationOutcome, SimulationAdaptationOutcome),
            ):
                raise RuleContractError("规则工厂必须返回受支持的 Outcome")
            session = RuleSession(document, context, result, self.readonly)
            self._visit_roots(document, context, session, original)
            session.assert_snapshot(original)
            if self.readonly:
                _check_readonly_result(result)
            if not _has_errors(result):
                edits = session._commit()
                if isinstance(result, InterfaceAdaptationOutcome):
                    result.changes.extend(
                        dict(change, rule_id=self.rule_id)
                        for change in edits.changes
                    )
            session.assert_snapshot(document.snapshot)
            return result
        except Exception:
            if isinstance(
                result,
                (
                    InterfaceAdaptationOutcome,
                    SimulationAdaptationOutcome,
                ),
            ):
                _discard_changes(result)
            if document.snapshot is not original:
                document._publish(original)
            raise

    def _visit_roots(self, document, context, session, original):
        """按选择器访问所有根并检查会话没有被绕过。

        每个访问根结束后校验快照，使直接修改文档的违规 Visitor 立即暴露。
        """
        roots = tuple(self.selector(document, context))
        _validate_roots(roots, original)
        if not roots:
            return
        if self.visitor_factory is None:
            raise RuleContractError("可编辑 Visitor 规则必须提供访问器工厂")
        visitor = self.visitor_factory(session)
        if (
            not isinstance(visitor, JunosRuleVisitor)
            or visitor.session is not session
        ):
            raise RuleContractError(
                "Visitor 工厂必须返回绑定当前会话的 JunosRuleVisitor"
            )
        for root in roots:
            visitor.visit(root)
            session.assert_snapshot(original)
        visitor.finish()

    def _apply_document_action(self, document, context: ContextT) -> OutcomeT:
        """在临时工作树上执行动作，并通过一个事务发布实际变化。

        嵌套调用复用调用方工作树，避免创建第二份临时状态或重复提交。
        """
        original = document.snapshot
        nested = hasattr(document, "_working_root")
        manager = _NullWorkingTree() if nested else document._working_tree()
        with manager as working_root:
            visitor = _DocumentActionVisitor(
                document, context, self.document_action
            )
            result = visitor.visit(original.root)
        if not isinstance(
            result,
            (InterfaceAdaptationOutcome, SimulationAdaptationOutcome),
        ):
            raise RuleContractError("规则动作必须返回受支持的 Outcome")
        if isinstance(result, InterfaceAdaptationOutcome):
            for change in result.changes:
                change["rule_id"] = self.rule_id
            if self.readonly:
                _check_readonly_result(result)
        if not nested:
            document._publish_work_root(working_root)
        if self.readonly:
            if document.snapshot is not original:
                document._publish(original)
                raise RuleContractError("只读校验规则修改了配置")
            _check_readonly_result(result)
        return result


class _NullWorkingTree:
    """在调用方已建立工作树时不创建第二份工作状态。

    空上下文管理器保留统一调用形式，同时让嵌套规则直接修改外层工作树。
    """

    def __init__(self):
        """初始化不含工作根的占位上下文。

        嵌套执行不需要新的根节点，显式空值可避免误用未管理的第二棵树。
        """
        self.root = None

    def __enter__(self):
        """进入占位作用域并返回空工作根。

        返回空值提醒调用方复用外层工作树，而不是依赖此处创建新状态。
        """
        return self.root

    def __exit__(self, *_args):
        """退出占位作用域且不屏蔽异常。

        返回 False 让真实异常继续传播，确保外层执行器完成恢复处理。
        """
        return False


def simulation_result(session, visitor, edits):
    """把实际提交的模拟动作转换为分类统计。

    只在事务成功后记录类别，使报告与真实写入的新快照保持一致。
    """
    del visitor
    for change in edits.changes:
        session.result.record(
            change["action"], change.get("category", "configuration")
        )


__all__ = [
    "JunosRuleVisitor",
    "JunosVisitorRule",
    "RuleContractError",
    "RuleEdits",
    "RuleSession",
    "simulation_result",
]
