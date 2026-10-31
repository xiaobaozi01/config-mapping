"""ANTLR Visitor 规则的生命周期：选择、收集、提交、映射及失败恢复。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from impl.common.outcomes import (
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)

from ..antlr.context import CiscoContext
from ..antlr.generated.CiscoParserVisitor import CiscoParserVisitor
from ..antlr.rewriting import RewriteResult
from .scopes import document_scope

if TYPE_CHECKING:
    from ..document import CiscoDocument

ContextT = TypeVar("ContextT")
OutcomeT = TypeVar(
    "OutcomeT", bound=InterfaceAdaptationOutcome | SimulationAdaptationOutcome
)


class RuleContractError(RuntimeError):
    """表示规则违反托管执行器约定。

    包括绕过会话修改文档、使用无效访问根或让只读规则报告变更，必须终止当前规则。
    """


class RuleEdits:
    """向 Visitor 暴露受控的编辑收集接口。

    commit 和 preview 等生命周期方法由执行器独占，规则只能登记允许的编辑。
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

        底层事务不直接公开，所有访问必须通过动作白名单转发。
        """
        self._transaction = transaction

    def __getattr__(self, name):
        """按白名单转发规则可执行的事务动作。

        未知属性立即报错，防止规则自行提交事务或访问内部状态。
        """
        if name not in self._ACTIONS:
            raise RuleContractError(
                "Visitor 只能收集编辑，提交由 CiscoVisitorRule 管理"
            )
        return getattr(self._transaction, name)


class RuleSession(Generic[ContextT, OutcomeT]):
    """保存单条规则执行期间的文档、上下文、结果和事务。

    会话集中管理快照一致性、只读限制和提交，使各个 Visitor 无需重复实现生命周期。
    """

    def __init__(
        self,
        document: CiscoDocument,
        context: ContextT,
        result: OutcomeT,
        readonly: bool,
    ):
        """创建只读或可编辑的规则会话。

        只读会话不创建重写事务，从结构上保证校验规则不能修改配置。
        """
        self.document = document
        self.context = context
        self.result = result
        self.snapshot = document.snapshot
        self.readonly = readonly
        self._transaction = None if readonly else document.rewrite()
        self._edits = None if readonly else RuleEdits(self._transaction)

    @property
    def edits(self) -> RuleEdits:
        """返回当前会话允许使用的编辑入口。

        只读会话请求编辑会立即报错，避免规则运行时分支绕过只读约束。
        """
        if self.readonly:
            raise RuleContractError("只读校验规则不能申请编辑")
        return self._edits

    def assert_snapshot(self, snapshot):
        """断言文档仍使用规则开始时的快照。

        Context 只在原快照内有效，快照变化说明规则绕过了托管事务。
        """
        if self.document.snapshot is not snapshot:
            raise RuleContractError("规则绕过执行器修改了文档快照")

    def _commit(self) -> RewriteResult:
        """提交可编辑会话的重写事务。

        只读会话返回空结果，使执行器可以复用同一完成流程而不产生写操作。
        """
        return RewriteResult() if self.readonly else self._transaction.commit()


class CiscoRuleVisitor(CiscoParserVisitor, Generic[ContextT, OutcomeT]):
    """为 Cisco 规则提供会话绑定和完成钩子。

    finish 在所有访问根处理完后调用一次，使跨节点动作可以延迟到完整分析后再提交。
    """

    def __init__(self, session: RuleSession[ContextT, OutcomeT]):
        """绑定访问器所属的规则会话。

        Visitor 通过会话读取上下文并收集编辑，不能自行管理文档生命周期。
        """
        self.session = session

    def finish(self) -> None:
        """完成遍历后的可选汇总动作。

        默认不执行任何操作，需要延迟提交的规则可以覆盖此方法。
        """
        pass


def always(context) -> bool:
    """无条件允许规则执行。

    作为默认条件函数，简单规则无需重复定义恒真条件。
    """
    return True


def _has_errors(result):
    """判断接口结果是否包含错误。

    模拟结果没有 errors 字段，通过结果类型区分可避免访问不存在的属性。
    """
    return isinstance(result, InterfaceAdaptationOutcome) and bool(
        result.errors
    )


def _discard_changes(result):
    """清除失败规则已经收集的变更统计。

    除恢复文档快照外还要清理结果对象，避免报告把未提交动作描述为成功。
    """
    if isinstance(result, InterfaceAdaptationOutcome):
        result.changes.clear()
        if isinstance(result, InterfacePruningOutcome):
            result.removed_interfaces.clear()
            result.removed_addresses.clear()
    else:
        for name in ("added", "replaced", "removed", "rule_changes"):
            getattr(result, name).clear()


def _default_result(session, visitor, edits):
    """把事务实际提交的编辑写入接口结果。

    默认映射只使用提交后的 edits，未提交或已回滚动作不会出现在报告中。
    """
    if not isinstance(session.result, InterfaceAdaptationOutcome):
        raise RuleContractError("模拟规则必须提供分类结果映射")
    session.result.changes[:] = edits.changes


def _validate_roots(roots, snapshot):
    """校验访问根属于同一快照且不存在祖先后代重叠。

    重复覆盖会导致同一子树被多次编辑并触发冲突，因此在 Visitor 启动前统一拒绝。
    """
    identities = set()
    for root in roots:
        if not isinstance(root, CiscoContext) or root.snapshot is not snapshot:
            raise RuleContractError("访问根不属于当前文档快照")
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
    """拒绝只读规则报告变更或删除对象。

    即使规则没有实际修改文档，也不能返回虚假变更，否则会破坏只读调用方假设。
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
class CiscoVisitorRule(Generic[ContextT, OutcomeT]):
    """声明 Visitor 工厂、选择范围、条件和结果映射。

    dataclass 让规则注册保持声明式，同时由 apply 统一执行快照校验和失败恢复。
    """

    rule_id: str
    visitor_factory: Callable[
        [RuleSession[ContextT, OutcomeT]], CiscoRuleVisitor[ContextT, OutcomeT]
    ]
    selector: Callable[[CiscoDocument, ContextT], Iterable[CiscoContext]] = (
        field(default=document_scope)
    )
    condition: Callable[[ContextT], bool] = field(default=always)
    readonly: bool = False
    outcome_factory: Callable[[], OutcomeT] = field(
        default=InterfaceAdaptationOutcome
    )
    result_mapper: (
        Callable[
            [
                RuleSession[ContextT, OutcomeT],
                CiscoRuleVisitor[ContextT, OutcomeT],
                RewriteResult,
            ],
            None,
        ]
        | None
    ) = None

    def applies(self, context: ContextT) -> bool:
        """调用规则条件判断上下文是否适用。

        条件在事务创建前求值，不适用规则不会分配会话或触碰文档。
        """
        return self.condition(context)

    def apply(self, document: CiscoDocument, context: ContextT) -> OutcomeT:
        """执行 Visitor 规则并返回提交后或清理后的结果。

        每次执行都校验原始快照、访问根和结果类型，任何异常都会恢复文档并清除变更。
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
            if document.snapshot is not original:
                raise RuleContractError("Outcome 工厂不能修改文档")
            session = RuleSession(document, context, result, self.readonly)
            roots = tuple(self.selector(document, context))
            _validate_roots(roots, original)
            session.assert_snapshot(original)
            if roots:
                visitor = self._visit(session, roots)
                self._finish(session, visitor)
            return result
        except Exception:
            if isinstance(
                result,
                (InterfaceAdaptationOutcome, SimulationAdaptationOutcome),
            ):
                _discard_changes(result)
            if document.snapshot is not original:
                document._publish(original)
            raise

    def _visit(self, session, roots):
        """创建访问器并逐个执行访问根。

        每个根处理后检查快照，使直接修改文档的违规 Visitor 立即暴露。
        """
        visitor = self.visitor_factory(session)
        if (
            not isinstance(visitor, CiscoRuleVisitor)
            or getattr(visitor, "session", None) is not session
        ):
            raise RuleContractError("Visitor 工厂必须返回 CiscoRuleVisitor")
        session.assert_snapshot(session.snapshot)
        for root in roots:
            visitor.visit(root)
            session.assert_snapshot(session.snapshot)
        visitor.finish()
        session.assert_snapshot(session.snapshot)
        return visitor

    def _finish(self, session, visitor):
        """校验结果、提交事务并执行结果映射。

        错误状态优先于提交，确保失败规则不会发布配置；提交后再次确认快照和只读约束。
        """
        if self.readonly:
            _check_readonly_result(session.result)
        if _has_errors(session.result):
            _discard_changes(session.result)
            return
        edits = session._commit()
        committed = session.document.snapshot
        mapper = self.result_mapper or _default_result
        mapper(session, visitor, edits)
        session.assert_snapshot(committed)
        if _has_errors(session.result):
            if session.document.snapshot is not session.snapshot:
                session.document._publish(session.snapshot)
            _discard_changes(session.result)
        if self.readonly:
            _check_readonly_result(session.result)


def simulation_result(session, visitor, edits):
    """把实际提交的模拟动作转换为分类统计。

    每条动作可以携带 count，使删除整棵语义子树时仍按原语句数量报告。
    """
    for change in edits.changes:
        session.result.record(
            change["action"], change["category"], change.get("count", 1)
        )
