"""按原始接口身份裁剪非 NNI 业务，并检查其活动依赖。"""

from __future__ import annotations

import ipaddress
import re
from contextlib import suppress

from impl.common.domain.interface import InterfaceKind, interface_parent
from impl.common.domain.outcomes import (
    DeferredInterfaceReference,
    InterfacePruningOutcome,
)

from ..document import (
    CiscoDocument,
    CiscoNode,
)
from ..operations.interfaces.access import interface_nodes
from ..syntax.semantics import (
    _CISCO_INTERFACE_TOKEN,
    canonical_cisco_interface,
)
from .support import CiscoRuleVisitor, CiscoVisitorRule, document_scope

# 整个接口 token 一次规范化，避免 Gi/BE 缩写和主口/子口前缀误匹配。
_ADDRESS = re.compile(r"^(?:ipv4|ipv6)\s+address\s+(\S+)", re.I)


def interface_references(text: str, names: set[str]) -> set[str]:
    """返回文本中属于指定集合的完整接口引用。

    先排除桥域和描述等定义性语句，再使用规范化名称可避免把相似接口误判为引用。
    """
    # 桥组/桥域的标识符可以恰好等于 BVI 名称，它们是定义名称而非引用。
    if re.match(
        r"^(?:bridge\s+group|bridge-domain|description|remark)\b", text, re.I
    ):
        return set()
    indexed = {name.lower(): name for name in names}
    return {
        indexed[canonical_cisco_interface(match.group()).lower()]
        for match in _CISCO_INTERFACE_TOKEN.finditer(text)
        if canonical_cisco_interface(match.group()).lower() in indexed
    }


def _defer_interface_references(outcome, path, references, reason):
    """记录最终校验允许保留的精确接口引用。

    路径和接口名同时匹配才构成豁免，避免同名接口在其他位置形成未告警的悬空配置。
    """
    outcome.deferred_interface_references.update(
        DeferredInterfaceReference(
            path=tuple(path), interface=name, reason=reason
        )
        for name in references
    )


def _is_known_removed_reference(node, path):
    """判断节点是否是能够整节点删除的已知接口依赖。

    路由协议、MPLS、RSVP、L2VPN、EVPN、DHCP 和带内管理接口的绑定可与其父级结构
    一并确认；静态路由则要求节点本身是以明确前缀开头的下一跳语句。
    """
    top = path[0].lower()
    interface_binding = bool(
        re.match(
            r"^(?:interface|routed\s+interface)\s",
            node.header,
            re.I,
        )
    )
    known_binding = interface_binding and (
        re.match(
            r"^(?:router\s+(?:ospf\S*|isis|rip|vrrp)|mpls|"
            r"rsvp|l2vpn|vrf|evpn|dhcp\s+ipv[46])\b",
            top,
        )
        is not None
        or (
            top == "control-plane"
            and "management-plane" in path
            and "inband" in path
        )
    )
    static_route = top.startswith("router static") and bool(
        re.match(r"^[0-9a-fA-F:.]+/\d+\s", node.header)
    )
    return known_binding or static_route


def _addresses(block: CiscoNode) -> set[str]:
    """收集接口块子树中的已配置 IP 地址。

    无效或未编号前缀会被忽略，使地址清理只跟踪能够明确归类的本地地址。
    """
    result: set[str] = set()
    for child in block.walk():
        match = _ADDRESS.match(child.header)
        if match:
            with suppress(ValueError):
                result.add(str(ipaddress.ip_interface(match.group(1)).ip))
    return result


def _walk(nodes: list[CiscoNode], path: tuple[str, ...] = ()):
    """递归遍历活动 Cisco 节点及完整命令路径。

    跳过格式节点可让依赖分析只处理实际配置，同时保留报告定位所需的祖先路径。
    """
    for node in nodes:
        if not node.active or node.is_formatting:
            continue
        current = (*path, node.header)
        yield node, current
        yield from _walk(node.nodes, current)


class _BgpSourcePruner:
    """分析 BGP 邻居和模板的本地源继承。

    只有完整解析继承关系后才能判断哪些邻居或模板失去源地址，避免局部文本误删。
    """

    def __init__(
        self,
        document,
        removed_interfaces,
        removed_addresses,
        retained_addresses,
        outcome,
        record,
    ):
        """保存待分析文档、删除集合和结果回调。

        地址集合在构造时转为独立集合，避免后续接口裁剪修改输入数据。
        """
        self.document = document
        self.removed_interfaces = removed_interfaces
        self.all_removed_addresses = (
            set().union(*removed_addresses.values())
            if removed_addresses
            else set()
        )
        self.retained_addresses = retained_addresses
        self.outcome = outcome
        self.record = record

    def _preserve_uncertain(self, identity, reason):
        """保留无法确定继承关系的 BGP 对象并记录人工确认项。

        输入配置由设备生成，重复、缺失或循环可能来自本解析器尚未覆盖的继承规则；
        prune 不能据此判定输入非法，更不能删除可能仍有效的业务。
        """
        self.uncertain.add(identity)
        _, path, _ = self.sessions[identity]
        message = (
            f"BGP 配置无法确定，已保留原配置等待人工确认: "
            f"{' / '.join(path)}; {reason}"
        )
        if message not in self.outcome.warnings:
            self.outcome.warnings.append(message)
        for statement_ids in self.direct[identity].values():
            for statement_id in statement_ids:
                _, statement_path, source = self.statements[statement_id]
                if source[0] != "interface":
                    continue
                references = interface_references(
                    source[1], self.removed_interfaces
                )
                _defer_interface_references(
                    self.outcome,
                    statement_path,
                    references,
                    f"BGP 本地源继承无法确定: {reason}",
                )

    def vrf_for(self, path: tuple[str, ...]) -> str:
        """返回命令路径中最近的 VRF 名称。

        同名地址可能在不同 VRF 中并存，逐路径解析可避免跨实例误判源地址存活。
        """
        return next(
            (
                header.split(maxsplit=1)[1]
                for header in reversed(path)
                if re.match(r"^vrf\s+", header, re.I)
            ),
            "",
        )

    def source_key(self, value: str) -> tuple[str, str]:
        """把 local-address 值区分为 IP 地址或接口名称。

        两类源使用不同存活判断，先分类可避免把接口字符串当 IP 解析。
        """
        try:
            return "address", str(ipaddress.ip_address(value))
        except ValueError:
            return (
                "interface",
                self.document.interfaces.analysis.resolve_interface(value),
            )

    def source_lost(self, source: tuple[str, str], vrf: str) -> bool:
        """判断一个本地源是否已被裁剪。

        接口源依赖删除集合，IP 源还必须在同一 VRF 中不存在保留地址。
        """
        kind, value = source
        if kind == "interface":
            return bool(interface_references(value, self.removed_interfaces))
        # 全局模板的 IP 源也必须在实际邻居的 VRF 内存在，不能借用别的 VRF。
        return (
            value in self.all_removed_addresses
            and value not in self.retained_addresses.get(vrf, set())
        )

    def source_interfaces(self, source: tuple[str, str]) -> set[str]:
        """返回本地源关联的已裁剪接口名称。

        纯 IP 源没有直接接口身份，因此返回空集合供审计记录使用。
        """
        if source[0] == "interface":
            return interface_references(source[1], self.removed_interfaces)
        return set()

    def index(self, root):
        """建立 BGP 会话、模板、源值和 use 引用索引。

        先完成只读索引再分析，使后续删除决策基于同一份原始配置。
        """
        self.sessions: dict[int, tuple[CiscoNode, tuple[str, ...], bool]] = {}
        self.templates: dict[tuple[tuple[str, ...], str, str], list[int]] = {}
        self.statements: dict[
            int, tuple[CiscoNode, tuple[str, ...], tuple[str, str]]
        ] = {}
        self.direct: dict[int, dict[tuple[str, str], set[int]]] = {}
        self.uses: dict[int, list[tuple[str, str]]] = {}
        for node, path in _walk([root]):
            self._index_session(node, path)

    def _index_session(self, node, path) -> None:
        """索引 BGP 邻居、模板及其直接源和 use 引用。

        路径和模板种类一起保存，使后续可按词法作用域解析正确的继承目标。
        """
        match = re.fullmatch(
            r"(neighbor|neighbor-group|session-group)\s+(.+)",
            node.header,
            re.I,
        )
        if not match:
            return
        identity = id(node)
        kind, name = match.group(1).lower(), match.group(2)
        self.sessions[identity] = node, path, kind != "neighbor"
        if kind != "neighbor":
            self.templates.setdefault((path[:-1], kind, name), []).append(
                identity
            )
        self.direct[identity] = {}
        self.uses[identity] = []
        for child in node.nodes:
            if not child.active or child.is_formatting:
                continue
            source = re.fullmatch(
                r"(?:update-source|local-address)\s+(.+)",
                child.header,
                re.I,
            )
            if source:
                key = self.source_key(source.group(1).strip())
                self.direct[identity].setdefault(key, set()).add(id(child))
                self.statements[id(child)] = (
                    child,
                    (*path, child.header),
                    key,
                )
            use = re.fullmatch(
                r"use\s+(neighbor-group|session-group)\s+(\S+)",
                child.header,
                re.I,
            )
            if use:
                self.uses[identity].append((use.group(1).lower(), use.group(2)))

    def link_templates(self):
        """把会话的 use 引用解析为模板依赖边。

        缺失或重复定义会使当前会话及候选模板进入保留集合，避免猜测清理。
        """
        self.edges: dict[int, list[int]] = {
            identity: [] for identity in self.sessions
        }
        self.uncertain: set[int] = set()
        for identity, references in self.uses.items():
            _, path, _ = self.sessions[identity]
            for kind, name in references:
                candidates = [
                    (scope, ids)
                    for (
                        scope,
                        template_kind,
                        template_name,
                    ), ids in self.templates.items()
                    if template_kind == kind
                    and template_name == name
                    and path[: len(scope)] == scope
                ]
                if not candidates:
                    self._preserve_uncertain(
                        identity, f"引用缺失的 {kind} {name}"
                    )
                    continue
                _, ids = max(candidates, key=lambda item: len(item[0]))
                if len(ids) != 1:
                    self._preserve_uncertain(
                        identity, f"引用重复的 {kind} {name}"
                    )
                    self.uncertain.update(ids)
                    continue
                self.edges[identity].append(ids[0])

    def effective(
        self, identity: int, visiting: set[int]
    ) -> dict[tuple[str, str], set[int]]:
        """计算一个会话最终生效的本地源集合。

        显式源优先于模板，递归访问集合同时用于检测循环和缓存结果。
        """
        if identity in self.resolved:
            return self.resolved[identity]
        _, _, _ = self.sessions[identity]
        if identity in visiting:
            self.uncertain.update(visiting)
            self._preserve_uncertain(identity, "本地源模板继承存在循环")
            return {}
        # 本层显式值优先于模板；不能因模板失效而删除显式使用健康源的邻居。
        values = {key: set(ids) for key, ids in self.direct[identity].items()}
        if not values:
            for target in self.edges[identity]:
                if target in self.uncertain:
                    self.uncertain.add(identity)
                for key, ids in self.effective(
                    target, visiting | {identity}
                ).items():
                    values.setdefault(key, set()).update(ids)
        if len(values) > 1:
            self._preserve_uncertain(identity, "继承得到多个本地源")
        self.resolved[identity] = values
        return values

    def prune_sessions(self):
        """协调会话分类、模板依赖和失效源语句清理。

        先确定保留闭包再判断删除，避免删除仍被健康会话依赖的共享模板。
        """
        required, used_statements, affected_templates = (
            self._classify_sessions()
        )
        self._include_template_dependencies(required)
        self._remove_lost_templates(required, affected_templates)
        self._remove_unused_template_sources(required, used_statements)

    def _classify_sessions(
        self,
    ) -> tuple[set[int], set[int], set[int]]:
        """区分保留、失效和失去本地源的会话。

        模板与会话使用不同删除策略，分类结果同时携带仍需保留的源语句 ID。
        """
        required: set[int] = set()
        used_statements: set[int] = set()
        affected_templates: set[int] = set()
        for identity, (node, path, template) in self.sessions.items():
            values = self.resolved[identity]
            if identity in self.uncertain:
                required.add(identity)
                if not template:
                    for ids in values.values():
                        used_statements.update(ids)
                continue
            lost = any(
                self.source_lost(source, self.vrf_for(path))
                for source in values
            )
            if not template and lost:
                refs = set().union(
                    *(self.source_interfaces(source) for source in values)
                )
                self.record(
                    node,
                    path,
                    "BGP 有效本地源依赖已裁剪接口或地址，删除整个邻居",
                    refs,
                )
            elif template and lost:
                affected_templates.add(identity)
            else:
                required.add(identity)
                if not template:
                    for ids in values.values():
                        used_statements.update(ids)
        return required, used_statements, affected_templates

    def _include_template_dependencies(self, required: set[int]) -> None:
        """把保留会话依赖的全部模板加入闭包。

        递归依赖边可保留多级 use 链，避免只检查直接引用而误删上游模板。
        """
        pending = list(required)
        while pending:
            identity = pending.pop()
            for target in self.edges[identity]:
                if target not in required:
                    required.add(target)
                    pending.append(target)

    def _remove_lost_templates(
        self,
        required: set[int],
        affected_templates: set[int],
    ) -> None:
        """删除失去源且未被保留会话依赖的模板。

        先计算依赖闭包差集，可保留仍服务健康会话的共享模板。
        """
        for identity in affected_templates - required:
            node, path, _ = self.sessions[identity]
            refs = set().union(
                *(
                    self.source_interfaces(source)
                    for source in self.resolved[identity]
                )
            )
            self.record(
                node, path, "BGP 失效本地源模板不再被保留会话依赖", refs
            )

    def _remove_unused_template_sources(
        self,
        required: set[int],
        used_statements: set[int],
    ) -> None:
        """删除保留模板中未被有效会话使用的失效源。

        模板本身仍需存在时只清理无用默认源，避免扩大删除范围。
        """
        for identity in required:
            _, _, template = self.sessions[identity]
            if not template or identity in self.uncertain:
                continue
            for source, ids in self.direct[identity].items():
                for statement_id in ids:
                    child, path, _ = self.statements[statement_id]
                    if (
                        statement_id not in used_statements
                        and self.source_lost(source, self.vrf_for(path))
                    ):
                        self.record(
                            child,
                            path,
                            "保留 BGP 模板公共参数，移除未被有效会话使用的失效默认源",
                            self.source_interfaces(source),
                        )

    def run(self):
        """分析全部活动 BGP 实例并执行会话剪枝。

        每个 BGP 根独立建立索引和解析结果，错误实例整体跳过以避免部分删除。
        """
        for root in self.document.root.nodes:
            if not root.active or not re.match(
                r"^router\s+bgp\b", root.header, re.I
            ):
                continue
            self.index(root)
            self.link_templates()
            self.resolved = {}
            for identity in self.sessions:
                self.effective(identity, set())
            self.prune_sessions()


class _CiscoPruner:
    """分析跨接口、广播域和协议依赖的裁剪范围。

    分析器只通过回调记录删除，不直接持有事务，使多个规则共享判断并统一提交。
    """

    def __init__(self, document, protected_parents, outcome, record):
        """建立受保护接口闭包和待清理接口集合。

        同时扩展聚合父子关系，确保保护父口或成员时整组接口都不会被误删。
        """
        self.document = document
        self.outcome = outcome
        self.record = record
        self.protected = {
            interface_parent(
                self.document.interfaces.analysis.resolve_interface(name)
            )
            for name in protected_parents
        }
        members = self.document.interfaces.analysis.bundle_members()
        self.protected.update(
            bundle
            for member, bundle in members.items()
            if member in self.protected
        )
        self.protected.update(
            member
            for member, bundle in members.items()
            if bundle in self.protected
        )
        self.blocks = interface_nodes(self.document)
        self.names = {
            block.interface_name
            for block in self.blocks
            if block.interface_name
        }
        self.removed = {
            name
            for name in self.names
            if self.document.interfaces.analysis.interface_kind(name)
            in {InterfaceKind.PHYSICAL, InterfaceKind.BUNDLE}
            and interface_parent(name) not in self.protected
        }
        # 即使父口没有单独的 interface 块，外部也可能引用父口。
        self.removed.update(
            interface_parent(name) for name in list(self.removed)
        )

    def prune_domains(self):
        """删除仅依赖已裁剪接口的 L2VPN 广播域。

        仍含保留接入接口的广播域会继续保护其 routed gateway，保留有效业务链。
        """
        self.retained_gateways: set[str] = set()
        domains = [
            (node, path)
            for node, path in _walk(self.document.root.nodes)
            if path[0].lower() == "l2vpn"
            and re.match(r"^bridge-domain\s", node.header, re.I)
        ]
        for domain, path in domains:
            attachments = {
                self.document.interfaces.analysis.resolve_interface(
                    child.header.split(maxsplit=1)[1]
                )
                for child in domain.nodes
                if child.active
                and re.match(r"^interface\s", child.header, re.I)
            }
            gateways = {
                self.document.interfaces.analysis.resolve_interface(
                    re.sub(
                        r"^routed\s+interface\s+", "", child.header, flags=re.I
                    )
                )
                for child in domain.nodes
                if child.active
                and re.match(r"^routed\s+interface\s", child.header, re.I)
            }
            if attachments and not (attachments - self.removed):
                self.record(
                    domain,
                    path,
                    "广播域只关联已裁剪的接入接口",
                    attachments & self.removed,
                )
            elif attachments - self.removed:
                self.retained_gateways.update(gateways)

    def prune_p2p(self):
        """删除本地接入全部失效的点到点 L2VPN 服务。

        只处理 xconnect group 内的服务，避免影响其他 L2VPN 业务。
        """
        for service, path in list(_walk(self.document.root.nodes)):
            if (
                path[0].lower() != "l2vpn"
                or not re.match(r"^p2p\s+", service.header, re.I)
                or not any(
                    re.match(r"^xconnect\s+group\s+", ancestor, re.I)
                    for ancestor in path[:-1]
                )
            ):
                continue
            attachments = {
                self.document.interfaces.analysis.resolve_interface(
                    child.header.split(maxsplit=1)[1]
                )
                for child in service.nodes
                if child.active
                and re.match(r"^interface\s+", child.header, re.I)
            }
            if attachments and attachments <= self.removed:
                self.record(
                    service,
                    path,
                    "点到点业务的全部本地接口已裁剪，删除远端 PW 绑定",
                    attachments,
                )

    def prune_interfaces(self):
        """确定需要删除的接口、网关和地址集合。

        先汇总保留地址再减去，避免同一 IP 同时出现在保留接口时被错误标记为删除。
        """
        self.removed.update(
            name
            for name in self.names
            if self.document.interfaces.analysis.interface_kind(name)
            == InterfaceKind.GATEWAY
            and name not in self.retained_gateways
        )
        self.removed_addresses: set[str] = set()
        self.retained_addresses: set[str] = set()
        self.removed_sources: dict[str, set[str]] = {}
        self.retained_sources: dict[str, set[str]] = {}
        for block in self.blocks:
            name = block.interface_name
            vrf = next(
                (
                    child.header.split(maxsplit=1)[1]
                    for child in block.nodes
                    if child.active and re.match(r"^vrf\s+", child.header, re.I)
                ),
                "",
            )
            addresses = _addresses(block)
            if name in self.removed:
                self.removed_addresses.update(addresses)
                self.removed_sources.setdefault(vrf, set()).update(addresses)
                self.record(
                    block,
                    (block.header,),
                    "非 NNI 接口不参与核心路由实验",
                    {name},
                )
            else:
                self.retained_addresses.update(addresses)
                self.retained_sources.setdefault(vrf, set()).update(addresses)
        self.removed_addresses.difference_update(self.retained_addresses)
        self.outcome.removed_interfaces = self.removed
        self.outcome.removed_addresses = self.removed_addresses

    def _handle_removed_reference(
        self,
        node: CiscoNode,
        current: tuple[str, ...],
        references: set[str],
    ) -> bool:
        """处理单个节点对已删除接口的引用。

        已知绑定和静态路由整节点删除；其他语法保留并登记精确豁免，返回当前节点是否
        已按整节点处理。把命中分类集中在一处，使递归遍历只负责结构推进。
        """
        if _is_known_removed_reference(node, current):
            self.record(
                node,
                current,
                "删除依赖非 NNI 接口的绑定或静态路由",
                references,
            )
            return True
        self.outcome.warnings.append(
            f"接口引用无法确定，已保留原配置等待人工确认: "
            f"{' / '.join(current)}; "
            f"接口 {', '.join(sorted(references))}"
        )
        _defer_interface_references(
            self.outcome,
            current,
            references,
            "接口引用语法无法确定，保留原配置",
        )
        return False

    def clean(self, nodes: list[CiscoNode], path: tuple[str, ...] = ()) -> None:
        """递归删除或保留指向已裁剪接口的活动依赖。

        已知绑定自动删除，未知引用保留并记录 warning；递归依赖当前节点是否已被整节点
        处理，避免删除后的子树继续产生重复诊断。
        """
        for node in nodes:
            if not node.active or node.is_formatting:
                continue
            current = (*path, node.header)
            if re.match(r"^(?:description|remark)\b", node.header, re.I):
                continue
            references = interface_references(node.header, self.removed)
            if references and self._handle_removed_reference(
                node,
                current,
                references,
            ):
                continue
            self.clean(node.nodes, current)

    def prune_monitor_sessions(self):
        """删除目的接口已消失的镜像会话。

        只有会话自身明确引用已删除目的接口时才删除，避免影响其他镜像会话。
        """
        for node in self.document.root.nodes:
            if not node.active or not re.match(
                r"^monitor-session\b", node.header, re.I
            ):
                continue
            references = set()
            for child in node.walk():
                if re.match(r"^destination\s+interface\s+", child.header, re.I):
                    references.update(
                        interface_references(child.header, self.removed)
                    )
            if references:
                self.record(
                    node, (node.header,), "镜像会话的目的接口已删除", references
                )

    def analyze(self):
        """按依赖顺序执行全部非 NNI 裁剪分析。

        先建立广播域和接口删除集合，再清理协议依赖，避免后续规则基于过时边界判断。
        """
        self.prune_domains()
        self.prune_p2p()
        self.prune_interfaces()
        InterfaceDependencyCascade(
            self.document,
            self.removed,
            self.removed_sources,
            self.retained_sources,
            self.outcome,
            self.record,
        ).run()


class InterfaceDependencyCascade:
    """清理接口删除后共享的 BGP 本地源与 monitor-session 依赖。

    完整非 NNI 和聚合成员缩减只负责确定删除集合；本类保证两者从该集合开始使用
    相同的分析顺序和结果写入逻辑。地址映射由入口按原有语义提供，不在内核中重算。
    """

    def __init__(
        self,
        document,
        removed_interfaces,
        removed_addresses,
        retained_addresses,
        outcome,
        record,
    ):
        """保存删除集合、地址作用域和审计回调。

        该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
        """
        self.document = document
        self.removed_interfaces = removed_interfaces
        self.removed_addresses = removed_addresses
        self.retained_addresses = retained_addresses
        self.outcome = outcome
        self.record = record

    def run(self):
        """依次清理 BGP 本地源和监控会话。

        该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
        """
        _BgpSourcePruner(
            self.document,
            self.removed_interfaces,
            self.removed_addresses,
            self.retained_addresses,
            self.outcome,
            self.record,
        ).run()
        monitor = _CiscoPruner(self.document, set(), self.outcome, self.record)
        monitor.removed = set(self.removed_interfaces)
        monitor.prune_monitor_sessions()


class PruningVisitor(CiscoRuleVisitor):
    """收集一次剪枝规则的删除对象和审计事件。

    延迟到 finish 统一声明最外层删除，避免祖先与后代同时提交造成重叠事务。
    """

    def __init__(self, session):
        """创建空的删除映射和报告事件列表。

        两类集合在分析期间累计，只有成功提交后才映射到最终结果。
        """
        super().__init__(session)
        self.deletions = {}
        self.events = []

    def record(self, node, path, reason, references):
        """记录一个待删除节点及其接口依赖。

        使用节点标识去重可合并不同分析产生的同一删除，同时保留稳定审计顺序。
        """
        sources = sorted(references)
        self.deletions[id(node)] = node, reason, sources
        self.events.append(
            {
                "action": "remove",
                "path": list(path),
                "reason": reason,
                "source_interfaces": sources,
            }
        )

    def finish(self):
        """向规则会话提交最外层且互不重叠的删除。

        祖先已删除的后代不再单独提交，避免 祖先删除后继续编辑已移除节点。
        """
        if self.session.result.errors:
            return
        for node, reason, sources in self.deletions.values():
            parent = node.parent
            while parent is not None and id(parent) not in self.deletions:
                parent = parent.parent
            if parent is None:
                self.session.edits.delete(
                    node, reason, source_interfaces=sources
                )


class NonNNIVisitor(PruningVisitor):
    """执行完整非 NNI 接口和业务清理。

    该访问器只创建分析器并委托 analyze，自身负责事件收集和统一提交。
    """

    def visit_configuration(self, root):
        """从配置根启动完整非 NNI 剪枝分析。

        保护集合来自规则上下文，分析结果通过当前 Visitor 记录删除。
        """
        _CiscoPruner(
            self.session.document,
            self.session.context.protected_parents,
            self.session.result,
            self.record,
        ).analyze()


class MemberSourcesVisitor(PruningVisitor):
    """清理 NNI 聚合缩减后失效的成员源。

    该模式只传播已显式删除的成员接口，不重新计算完整非 NNI 范围。
    """

    def visit_configuration(self, root):
        """通过共享依赖级联内核清理已删除成员。

        删除集合直接取规则上下文，确保只处理调用方指定的成员。
        """
        InterfaceDependencyCascade(
            self.session.document,
            self.session.context.removed_interfaces,
            {},
            {},
            self.session.result,
            self.record,
        ).run()


class InterfaceBindingsVisitor(PruningVisitor):
    """清理接口删除后遗留的协议和路由绑定。

    该规则不修改接口集合，只沿已删除成员检查全局配置中的引用。
    """

    def visit_configuration(self, root):
        """从配置根递归检查已删除接口绑定。

        使用规则上下文的删除集合，未知依赖由分析器报告错误而不自动删除。
        """
        worker = _CiscoPruner(
            self.session.document, set(), self.session.result, self.record
        )
        worker.removed = set(self.session.context.removed_interfaces)
        worker.clean(root.nodes)


def pruning_result(session, visitor, edits):
    """把成功提交的剪枝事件写入最终结果。

    只有实际提交事务时才公开事件，祖先删除覆盖的后代记录仍保留报告语义。
    """
    session.result.changes[:] = visitor.events if edits.total else []


def prune_non_nni(
    document: CiscoDocument, protected_parents: set[str]
) -> InterfacePruningOutcome:
    """通过规则注册表执行完整非 NNI 清理。

    包装函数保持文档门面的稳定入口，具体执行由当前 PRUNE_RULES 决定。
    """
    from . import prune_non_nni as execute

    return execute(document, protected_parents)


def prune_interface_dependencies(document, removed_interfaces):
    """通过规则注册表清理已删除成员的依赖。

    复用成员源规则可保证注册表扩展后无需修改文档门面。
    """
    from . import prune_member_dependencies

    return prune_member_dependencies(document, removed_interfaces)


def _tracked_interface(document, node):
    """解析只含单一 line-protocol 接口的 track 定义。

    列表、无接口和混合类型无法确定唯一依赖，返回空值以阻止自动删除。
    """
    types = [
        child
        for child in node.nodes
        if child.active and re.match(r"^type\b", child.header, re.I)
    ]
    if len(types) != 1 or not re.fullmatch(
        r"type\s+line-protocol\s+state", types[0].header, re.I
    ):
        return None
    interfaces = [
        match.group(1)
        for child in types[0].nodes
        if child.active
        if (match := re.fullmatch(r"interface\s+(.+)", child.header, re.I))
    ]
    return (
        document.interfaces.analysis.resolve_interface(interfaces[0])
        if len(interfaces) == 1
        else None
    )


def _lost_track_targets(document, nodes, removed_interfaces):
    """返回 track 定义中已经被裁剪的监控接口集合。

    先只解析可唯一确认的 line-protocol 目标，再检查父接口是否失效；该纯查询结果
    供后续删除或保留决策使用，避免把依赖识别与事务登记混在一起。
    """
    targets = [_tracked_interface(document, node) for node in nodes]
    return {
        target
        for target in targets
        if target
        and (
            target in removed_interfaces
            or interface_parent(target) in removed_interfaces
        )
    }


def _is_vrrp_use(header, path, name):
    """判断命令是否是 VRRP 对指定 track 对象的合法引用。

    同时检查地址族和 VRRP 层级，可避免把其他模块的同名 track 当成安全依赖。
    """
    match = re.fullmatch(r"track\s+object\s+(\S+)(?:\s+\d+)?", header, re.I)
    return bool(
        match
        and match.group(1) == name
        and path[0].lower() == "router vrrp"
        and any(
            re.fullmatch(r"address-family\s+ipv[46]", part, re.I)
            for part in path[1:-1]
        )
        and re.fullmatch(r"vrrp\s+\d+", path[-2], re.I)
    )


def _track_uses(name, definitions, commands):
    """收集指定 track 的已知和未知消费者。

    只有 VRRP 引用可自动解除；其他消费者使整个 track 保留并告警。
    """
    pattern = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])")
    known = []
    warnings = []
    for node, path in commands:
        if any(node is definition for definition in definitions):
            continue
        if re.match(
            r"^(?:description|remark)\b", node.header, re.I
        ) or not pattern.search(node.header):
            continue
        if _is_vrrp_use(node.header, path, name):
            known.append((node, path))
        else:
            warnings.append(
                f"track {name} 存在无法确定的消费者，已保留原配置等待人工确认: "
                f"{' / '.join(path)}"
            )
    return known, warnings


def _defer_track_references(outcome, nodes, interfaces, reason):
    """记录保留 track 定义中仍指向已删除接口的精确引用。

    该操作在当前文档的业务处理边界内运行，调用方继续负责统一的结果记录和失败处理。
    """
    for node in nodes:
        for child, path in _walk([node]):
            references = interface_references(child.header, interfaces)
            _defer_interface_references(outcome, path, references, reason)


class TrackDependenciesVisitor(PruningVisitor):
    """删除失去监控接口的 track 及其合法 VRRP 引用。

    保留 VRRP 组本身，只解除已删除对象的跟踪关系，避免扩大业务删除范围。
    """

    def _handle_lost_track(
        self,
        name,
        nodes,
        removed_interfaces,
        commands,
    ):
        """删除单个失效 track，或在依赖不明确时完整保留。

        该对象是级联删除的安全边界：只有唯一 line-protocol track 且消费者仅为 VRRP
        时才登记删除，其余情况保留整条依赖链并豁免对应接口引用。
        """
        document, outcome = self.session.document, self.session.result
        lost = _lost_track_targets(document, nodes, removed_interfaces)
        if not lost:
            return
        if len(nodes) != 1:
            outcome.warnings.append(
                f"track {name} 存在重复定义，已保留原配置等待人工确认"
            )
            _defer_track_references(
                outcome,
                nodes,
                removed_interfaces,
                f"track {name} 存在重复定义",
            )
            return
        uses, warnings = _track_uses(name, nodes, commands)
        if warnings:
            outcome.warnings.extend(warnings)
            _defer_track_references(
                outcome,
                nodes,
                removed_interfaces,
                f"track {name} 存在无法确定的消费者",
            )
            return
        self.record(
            nodes[0],
            (nodes[0].header,),
            "track 监控的接口已删除，移除跟踪对象",
            lost,
        )
        for node, path in uses:
            self.record(
                node,
                path,
                f"track {name} 已删除，解除 VRRP 对象跟踪",
                lost,
            )

    def visit_configuration(self, root):
        """分析全部 track 定义和消费者并登记删除。

        重复定义或未知消费者会阻止该 track 自动裁剪，保证依赖链可解释。
        """
        removed_interfaces = self.session.context.removed_interfaces
        commands = list(_walk(root.nodes))
        definitions = {}
        for node in root.nodes:
            if node.active and (
                match := re.fullmatch(r"track\s+(\S+)", node.header, re.I)
            ):
                definitions.setdefault(match.group(1), []).append(node)

        for name, nodes in definitions.items():
            self._handle_lost_track(
                name,
                nodes,
                removed_interfaces,
                commands,
            )


def full_prune_condition(context):
    """判断上下文是否允许完整非 NNI 清理。

    完整清理和成员依赖清理互斥，通过条件避免同一规则集重复删除接口。
    """
    return context.remove_non_nni


def member_prune_condition(context):
    """判断上下文是否只执行聚合成员依赖清理。

    该条件关闭完整接口裁剪，使成员缩减阶段只传播已知删除集合。
    """
    return not context.remove_non_nni


NON_NNI_RULE = CiscoVisitorRule(
    "cisco.prune.non_nni",
    NonNNIVisitor,
    selector=document_scope,
    condition=full_prune_condition,
    outcome_factory=InterfacePruningOutcome,
    result_mapper=pruning_result,
)
MEMBER_SOURCES_RULE = CiscoVisitorRule(
    "cisco.prune.member_sources",
    MemberSourcesVisitor,
    selector=document_scope,
    condition=member_prune_condition,
    outcome_factory=InterfacePruningOutcome,
    result_mapper=pruning_result,
)
TRACK_DEPENDENCIES_RULE = CiscoVisitorRule(
    "cisco.prune.track_dependencies",
    TrackDependenciesVisitor,
    selector=document_scope,
    outcome_factory=InterfacePruningOutcome,
    result_mapper=pruning_result,
)
INTERFACE_BINDINGS_RULE = CiscoVisitorRule(
    "cisco.prune.interface_bindings",
    InterfaceBindingsVisitor,
    selector=document_scope,
    outcome_factory=InterfacePruningOutcome,
    result_mapper=pruning_result,
)
