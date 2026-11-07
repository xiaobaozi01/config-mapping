"""只读提取华为成员绑定及 M-LAG 配对证据。

不创建华为配置 AST，不参与配置改写、目标端口分配或配置输出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from ipaddress import ip_address


def canonical_interface(value: str) -> str:
    """统一只读关系表中的华为接口名。

    忽略大小写和类型后的空格，并归一化明确的 GE/XGE 接口别名。
    """
    value = re.sub(r"\s+", "", value).lower()
    if re.fullmatch(r"eth-trunk\d+", value):
        return f"eth-trunk{int(value.removeprefix('eth-trunk'))}"
    for prefix, replacement in (
        ("xgigabitethernet", "xge"),
        ("gigabitethernet", "ge"),
        ("ethernet", "eth"),
    ):
        if value.startswith(prefix):
            return replacement + value[len(prefix) :]
    return value


@dataclass
class ExternalAggregation:
    """保存外部设备的端口归属和配对信息。

    members 中的 None 明确表示已读取的普通口；键缺失表示无法确认该端口。
    """

    members: dict[str, str | None] = field(default_factory=dict)
    mlags: dict[str, tuple[str, str]] = field(default_factory=dict)
    dfs_peers: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    peer_links: dict[str, str] = field(default_factory=dict)


def _blocks(text):
    """逐块读取 interface 和 dfs-group 配置。

    仅暂存当前块，其他顶层配置和 # 分隔符结束当前块，避免跨块误识别。
    """
    header, body = None, []
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        match = re.fullmatch(r"(interface|dfs-group)\s+(.+)", line, re.I)
        if match and not raw[:1].isspace():
            if header:
                yield *header, body
            header, body = (match[1].lower(), match[2]), []
        elif line and (line == "#" or not raw[:1].isspace()):
            if header:
                yield *header, body
            header, body = None, []
        elif header and line:
            body.append(line)
    if header:
        yield *header, body


def _unique(mapping, key, value):
    """登记唯一的端口或配对归属。

    重复的同值声明可合并，不同值声明必须报错，不能猜测应使用哪条绑定。
    """
    previous = mapping.get(key)
    if previous is not None and previous != value:
        raise ValueError(f"华为聚合配置的 {key} 存在冲突绑定")
    mapping[key] = value


def _trunk_members(command):
    """展开 Eth-Trunk 视图下的 trunkport 成员列表。

    支持单口、同类型多口和同一端口前缀下的 to 范围；无法解释时明确报错。
    """
    tokens = re.sub(
        r"\b((?!to\b)[a-z0-9-]*[a-z])\s+(?=\d)",
        r"\1",
        command,
        flags=re.I,
    ).split()
    index, prefix = 0, ""
    while index < len(tokens):
        token = tokens[index]
        if (
            token[:1].isdigit()
            and "/" in token
            and not re.search("[a-z]", token, re.I)
        ):
            token = prefix + token
        name = canonical_interface(token)
        match = re.fullmatch(r"(.+?)(\d+(?:/\d+)+)", name)
        if not match:
            raise ValueError("无法识别华为 trunkport 成员，请检查接口名或范围")
        prefix, index = match[1], index + 1
        if index < len(tokens) and tokens[index].lower() == "to":
            if index + 1 >= len(tokens):
                raise ValueError("华为 trunkport 范围缺少结束端口")
            end = tokens[index + 1]
            end = end if re.search("[a-z]", end, re.I) else prefix + end
            stem, first = name.rsplit("/", 1)
            end_stem, last = canonical_interface(end).rsplit("/", 1)
            if stem != end_stem or int(first) > int(last):
                raise ValueError(
                    "华为 trunkport 范围必须属于同一端口前缀且递增"
                )
            yield from (
                f"{stem}/{number}"
                for number in range(int(first), int(last) + 1)
            )
            index += 2
        else:
            yield name


def _read_interface(info, raw_name, body):
    """提取物理成员绑定和 Eth-Trunk 的 M-LAG 标记。

    子接口及其他类型接口不进入成员关系表，避免把业务配置误当物理聚合。
    """
    name = canonical_interface(raw_name)
    trunk = re.fullmatch(r"eth-trunk(\d+)", name)
    physical = re.fullmatch(r"[a-z0-9-]+\d+(?:/\d+)+(?:\:\d+)?", name)
    if not trunk and not physical:
        return
    if physical:
        info.members.setdefault(name, None)
    for line in body:
        member = re.fullmatch(r"eth-trunk\s+(\d+)", line, re.I)
        mlag = re.fullmatch(r"dfs-group\s+(\d+)\s+m-lag\s+(\d+)", line, re.I)
        peer_link = re.fullmatch(r"peer-link\s+(\d+)", line, re.I)
        ports = re.fullmatch(r"trunkport\s+(.+)", line, re.I)
        if physical and member:
            _unique(info.members, name, f"eth-trunk{int(member[1])}")
        if trunk and mlag:
            _unique(info.mlags, name, (str(int(mlag[1])), str(int(mlag[2]))))
        if trunk and peer_link:
            _unique(info.peer_links, name, str(int(peer_link[1])))
        if trunk and ports:
            for port in _trunk_members(ports[1]):
                _unique(info.members, port, name)


def parse_huawei_aggregation(text: str) -> ExternalAggregation:
    """从已有配置提取外部聚合元数据。

    只读取成员、Eth-Trunk 和 DFS 段，未涉及的配置原文不保存、不输出。
    """
    info = ExternalAggregation()
    for kind, name, body in _blocks(text):
        if kind == "interface":
            _read_interface(info, name, body)
        elif name.isdigit():
            for line in body:
                match = re.fullmatch(
                    r"(?:dual-active\s+detection\s+)?source(?:\s+|-)ip(?:v6)?\s+(\S+)"
                    r"(?:\s+vpn-instance\s+\S+)?(?:\s+peer\s+(\S+))?",
                    line,
                    re.I,
                )
                if match:
                    source = str(ip_address(match[1]))
                    peer = str(ip_address(match[2])) if match[2] else None
                    _unique(info.dfs_peers, str(int(name)), (source, peer))
    return info


def same_mlag(left, left_bundle, right, right_bundle, *, peer_link=False):
    """核验两个外部聚合是否属于同一 M-LAG。

    同编号不足以配对：还需双向 DFS 地址，或明确连接双方 peer-link 的拓扑。
    """
    identity = left.mlags.get(left_bundle)
    if identity is None or identity != right.mlags.get(right_bundle):
        return False
    group, _ = identity
    left_source, left_peer = left.dfs_peers.get(group, (None, None))
    right_source, right_peer = right.dfs_peers.get(group, (None, None))
    if left_peer or right_peer:
        return (
            left_source != right_source
            and left_peer == right_source
            and right_peer == left_source
            and left_source is not None
            and right_source is not None
        )
    return peer_link and (left_source is None or left_source != right_source)


def _has_peer_link(peers, infos, links):
    """检查原始拓扑是否明确连接两个 DFS 组的 peer-link 成员。

    外部设备间链路只用于核验，不缩减、不改名，也不为其分配目标接口。
    """
    for link in links:
        if {name for name, _ in link.endpoints()} != set(peers):
            continue
        matches = []
        for name, port in link.endpoints():
            info = infos[name]
            member_bundle = info.members.get(canonical_interface(port))
            group, _ = info.mlags[peers[name]]
            matches.append(info.peer_links.get(member_bundle) == group)
        if all(matches):
            return True
    return False


def verified_external_mlag(local_name, local_bundle, bundles, infos, links):
    """确认本端聚合的两个外部对端属于同一 M-LAG。

    每台外部设备只能对应一个明确的 Eth-Trunk；同编号但没有配对证据仍拒绝。
    """
    peers = {}
    for link in links:
        if bundles.get((link.row, local_name)) != local_bundle:
            continue
        for name, _ in link.endpoints():
            if name == local_name:
                continue
            remote_bundle = bundles.get((link.row, name))
            if name not in infos or remote_bundle is None:
                return False
            if name in peers and peers[name] != remote_bundle:
                return False
            peers[name] = remote_bundle
    if len(peers) != 2:
        return False
    left, right = peers
    if (
        peers[left] not in infos[left].mlags
        or peers[right] not in infos[right].mlags
    ):
        return False
    return same_mlag(
        infos[left],
        peers[left],
        infos[right],
        peers[right],
        peer_link=_has_peer_link(peers, infos, links),
    )
