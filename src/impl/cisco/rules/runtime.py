"""ANTLR Visitor 规则的生命周期：选择、收集、提交、映射及失败恢复。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Generic, TYPE_CHECKING, TypeVar

from impl.common.outcomes import InterfaceAdaptationOutcome, InterfacePruningOutcome, SimulationAdaptationOutcome
from ..antlr.context import CiscoContext
from ..antlr.generated.CiscoParserVisitor import CiscoParserVisitor
from ..antlr.rewriting import RewriteResult
from .scopes import document_scope

if TYPE_CHECKING:
    from ..document import CiscoDocument

ContextT = TypeVar("ContextT")
OutcomeT = TypeVar("OutcomeT", bound=InterfaceAdaptationOutcome | SimulationAdaptationOutcome)


class RuleContractError(RuntimeError):
    """规则绕过执行器修改文档、访问范围无效或违反只读约定。"""


class RuleEdits:
    """只开放收集动作；commit/preview 等生命周期操作由执行器持有。"""

    _ACTIONS = frozenset({"delete", "replace_header", "replace_subtree", "insert_before", "insert_after", "append_child"})

    def __init__(self, transaction):
        self._transaction = transaction

    def __getattr__(self, name):
        if name not in self._ACTIONS:
            raise RuleContractError("Visitor 只能收集编辑，提交由 CiscoVisitorRule 管理")
        return getattr(self._transaction, name)


class RuleSession(Generic[ContextT, OutcomeT]):
    def __init__(self, document: CiscoDocument, context: ContextT, result: OutcomeT, readonly: bool):
        self.document = document
        self.context = context
        self.result = result
        self.snapshot = document.snapshot
        self.readonly = readonly
        self._transaction = None if readonly else document.rewrite()
        self._edits = None if readonly else RuleEdits(self._transaction)

    @property
    def edits(self) -> RuleEdits:
        if self.readonly:
            raise RuleContractError("只读校验规则不能申请编辑")
        return self._edits

    def assert_snapshot(self, snapshot):
        if self.document.snapshot is not snapshot:
            raise RuleContractError("规则绕过执行器修改了文档快照")

    def _commit(self) -> RewriteResult:
        """执行器内部入口；Visitor 应只使用 edits。"""
        return RewriteResult() if self.readonly else self._transaction.commit()


class CiscoRuleVisitor(CiscoParserVisitor, Generic[ContextT, OutcomeT]):
    """继承生成的 Visitor；finish 在全部访问根处理完后调用一次，用于汇总动作。"""

    def __init__(self, session: RuleSession[ContextT, OutcomeT]):
        self.session = session

    def finish(self) -> None:
        pass


def always(context) -> bool:
    return True


def _has_errors(result):
    return isinstance(result, InterfaceAdaptationOutcome) and bool(result.errors)


def _discard_changes(result):
    if isinstance(result, InterfaceAdaptationOutcome):
        result.changes.clear()
        if isinstance(result, InterfacePruningOutcome):
            result.removed_interfaces.clear()
            result.removed_addresses.clear()
    else:
        for name in ("added", "replaced", "removed", "rule_changes"):
            getattr(result, name).clear()


def _default_result(session, visitor, edits):
    if not isinstance(session.result, InterfaceAdaptationOutcome):
        raise RuleContractError("模拟规则必须提供分类结果映射")
    session.result.changes[:] = edits.changes


def _validate_roots(roots, snapshot):
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
    changes = result.changes if isinstance(result, InterfaceAdaptationOutcome) else result.total
    removed = isinstance(result, InterfacePruningOutcome) and (result.removed_interfaces or result.removed_addresses)
    if changes or removed:
        raise RuleContractError("只读校验规则不能报告配置变更或删除对象")


@dataclass(frozen=True, slots=True)
class CiscoVisitorRule(Generic[ContextT, OutcomeT]):
    rule_id: str
    visitor_factory: Callable[[RuleSession[ContextT, OutcomeT]], CiscoRuleVisitor[ContextT, OutcomeT]]
    selector: Callable[[CiscoDocument, ContextT], Iterable[CiscoContext]] = field(default=document_scope)
    condition: Callable[[ContextT], bool] = field(default=always)
    readonly: bool = False
    outcome_factory: Callable[[], OutcomeT] = field(default=InterfaceAdaptationOutcome)
    result_mapper: Callable[[RuleSession[ContextT, OutcomeT], CiscoRuleVisitor[ContextT, OutcomeT], RewriteResult], None] | None = None

    def applies(self, context: ContextT) -> bool:
        return self.condition(context)

    def apply(self, document: CiscoDocument, context: ContextT) -> OutcomeT:
        original = document.snapshot
        result = None
        try:
            result = self.outcome_factory()
            if not isinstance(result, (InterfaceAdaptationOutcome, SimulationAdaptationOutcome)):
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
            if isinstance(result, (InterfaceAdaptationOutcome, SimulationAdaptationOutcome)):
                _discard_changes(result)
            if document.snapshot is not original:
                document._publish(original)
            raise

    def _visit(self, session, roots):
        visitor = self.visitor_factory(session)
        if not isinstance(visitor, CiscoRuleVisitor) or getattr(visitor, "session", None) is not session:
            raise RuleContractError("Visitor 工厂必须返回 CiscoRuleVisitor")
        session.assert_snapshot(session.snapshot)
        for root in roots:
            visitor.visit(root)
            session.assert_snapshot(session.snapshot)
        visitor.finish()
        session.assert_snapshot(session.snapshot)
        return visitor

    def _finish(self, session, visitor):
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
    """统计实际提交动作；count 支持删除一个语义子树时按原有语句数量报告。"""
    for change in edits.changes:
        session.result.record(change["action"], change["category"], change.get("count", 1))
