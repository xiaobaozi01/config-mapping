"""AST Visitor 规则的生命周期：选择、收集、提交、映射及失败恢复。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from impl.common.config_ast import ConfigVisitor
from impl.common.config_ast.editor import EditResult, EditScope
from impl.common.config_ast.model import tree_signature
from impl.common.domain.outcomes import (
    InterfaceAdaptationOutcome,
    InterfacePruningOutcome,
    SimulationAdaptationOutcome,
)

from ..syntax.ast import JuniperNode


def document_scope(document, context):
    """为文档级规则选择完整配置树。

    只返回一次根节点，使全文档规则可以建立索引且不会选择重叠子树。
    """
    return (document.root,)


if TYPE_CHECKING:
    from ..document import JunosDocument

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
            "move",
            "clone",
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
                "Visitor 只能收集编辑，提交由 JunosVisitorRule 管理"
            )
        return getattr(self._transaction, name)


class RuleSession(Generic[ContextT, OutcomeT]):
    """保存单条规则执行期间的文档、上下文、结果和事务。

    会话集中管理树状态一致性、只读限制和提交，使各个 Visitor 无需重复实现生命周期。
    """

    def __init__(
        self,
        document: JunosDocument,
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
        self.baseline = tree_signature(document.root)
        self.readonly = readonly
        self._transaction = None if readonly else document.edit()
        self._edits = None if readonly else RuleEdits(self._transaction)

    @property
    def edits(self) -> RuleEdits:
        """返回当前会话允许使用的编辑入口。

        只读会话请求编辑会立即报错，避免规则运行时分支绕过只读约束。
        """
        if self.readonly:
            raise RuleContractError("只读校验规则不能申请编辑")
        return self._edits

    def assert_unchanged(self):
        """拒绝绕过受控编辑收集器直接修改配置。

        Visitor 必须通过会话收集编辑；直接修改会绕过执行器的审计和回滚边界。
        """
        if tree_signature(self.document.root) != self.baseline:
            raise RuleContractError("规则绕过 Editor 修改了配置")

    def apply_edits(self):
        """在遍历完成后应用已收集的操作。

        只读会话返回空结果，不创建 Editor，也不推进文档修订号。
        """
        return EditResult() if self.readonly else self._transaction.apply()


class JunosRuleVisitor(ConfigVisitor, Generic[ContextT, OutcomeT]):
    """为 Junos 规则提供会话绑定和完成钩子。

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

    除恢复文档树状态外还要清理结果对象，避免报告把未提交动作描述为成功。
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


def _validate_roots(roots, document_root):
    """校验访问根属于同一树状态且不存在祖先后代重叠。

    重复覆盖会导致同一子树被多次编辑并触发冲突，因此在 Visitor 启动前统一拒绝。
    """
    members = {
        id(node)
        for node in document_root.walk(
            include_self=True, include_formatting=True
        )
    }
    identities = set()
    for root in roots:
        if not isinstance(root, JuniperNode) or id(root) not in members:
            raise RuleContractError("访问根不属于当前文档树状态")
        if id(root) in identities:
            raise RuleContractError("访问根不能重复")
        identities.add(id(root))
    for root in roots:
        parent = root.parent
        while parent is not None:
            if id(parent) in identities:
                raise RuleContractError("访问根不能同时包含祖先和后代")
            parent = parent.parent


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
class JunosVisitorRule(Generic[ContextT, OutcomeT]):
    """声明 Visitor 工厂、选择范围、条件和结果映射。

    dataclass 让规则注册保持声明式，同时由 apply 统一执行树签名校验和失败恢复。
    """

    rule_id: str
    document_action: Callable | None = None
    visitor_factory: Callable | None = None
    selector: Callable[[JunosDocument, ContextT], Iterable[JuniperNode]] = (
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
                JunosRuleVisitor[ContextT, OutcomeT],
                EditResult,
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

    def apply(self, document: JunosDocument, context: ContextT) -> OutcomeT:
        """在 AST 上执行规则，并在失败时恢复备份。

        编辑或规则失败时恢复树备份；成功修改只通过返回结果对外暴露。
        """
        if self.document_action is not None:
            return self._apply_document_action(document, context)
        with EditScope(document, independent=True) as scope:
            original = scope.backup
            result = None
            try:
                result = self.outcome_factory()
                if not isinstance(
                    result,
                    (InterfaceAdaptationOutcome, SimulationAdaptationOutcome),
                ):
                    raise RuleContractError("规则工厂必须返回受支持的 Outcome")
                if tree_signature(document.root) != tree_signature(original):
                    raise RuleContractError("Outcome 工厂不能修改配置")
                session = RuleSession(document, context, result, self.readonly)
                roots = tuple(self.selector(document, context))
                _validate_roots(roots, document.root)
                session.assert_unchanged()
                if roots:
                    visitor = self._visit(session, roots)
                    self._finish(session, visitor)
                if _has_errors(result):
                    if tree_signature(original) != tree_signature(
                        document.root
                    ):
                        scope.rollback()
                    _discard_changes(result)
                return result
            except Exception:
                if isinstance(
                    result,
                    (InterfaceAdaptationOutcome, SimulationAdaptationOutcome),
                ):
                    _discard_changes(result)
                scope.rollback()
                raise

    def _visit(self, session, roots):
        """在应用收集到的编辑前遍历选定节点。

        所有选定根共享一个 Visitor 实例，并在回调全部完成后只调用一次 ``finish``。
        """
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
        visitor.finish()
        session.assert_unchanged()
        return visitor

    def _finish(self, session, visitor):
        """应用编辑并映射审计结果，同时执行只读规则约束。

        结果映射也会检查意外修改，防止报告阶段引入未经审计的配置变化。
        """
        if self.readonly:
            _check_readonly_result(session.result)
        if _has_errors(session.result):
            return
        edits = session.apply_edits()
        baseline = tree_signature(session.document.root)
        mapper = self.result_mapper or _default_result
        mapper(session, visitor, edits)
        if tree_signature(session.document.root) != baseline:
            raise RuleContractError("结果映射不能修改配置")
        if self.readonly:
            _check_readonly_result(session.result)

    def _tag_changes(self, result):
        """为已完成的批量编辑审计记录附加规则身份。

        批量动作与标准 Visitor 会话产生的编辑使用同一报告来源字段。
        """
        if isinstance(result, InterfaceAdaptationOutcome):
            for change in result.changes:
                change["rule_id"] = self.rule_id

    def _run_action(self, document, context):
        """为可变动作提供编辑边界，并保持校验动作只读。

        只读动作不会获取 Editor；规则作用域仍会检测绕过修改。
        """
        if self.readonly:
            return self.document_action(document, context)
        with document.edit().bulk():
            return self.document_action(document, context)

    def _apply_document_action(self, document, context):
        """使用规则级回滚保护批量 AST 动作。

        备份同时覆盖厂商直接操作和返回的业务错误，防止留下部分规则结果。
        """
        with EditScope(document, independent=True) as scope:
            original = scope.backup
            result = None
            try:
                result = self._run_action(document, context)
                if not isinstance(
                    result,
                    (InterfaceAdaptationOutcome, SimulationAdaptationOutcome),
                ):
                    raise RuleContractError("规则动作必须返回受支持的 Outcome")
                if self.readonly:
                    _check_readonly_result(result)
                    if tree_signature(original) != tree_signature(
                        document.root
                    ):
                        raise RuleContractError("只读规则修改了配置")
                if _has_errors(result):
                    if tree_signature(original) != tree_signature(
                        document.root
                    ):
                        scope.rollback()
                    _discard_changes(result)
                self._tag_changes(result)
                return result
            except Exception:
                scope.rollback()
                if isinstance(
                    result,
                    (InterfaceAdaptationOutcome, SimulationAdaptationOutcome),
                ):
                    _discard_changes(result)
                raise


def simulation_result(session, visitor, edits):
    """把实际提交的模拟动作转换为分类统计。

    每条动作可以携带 count，使删除整棵语义子树时仍按原语句数量报告。
    """
    for change in edits.changes:
        session.result.record(
            change["action"], change["category"], change.get("count", 1)
        )
