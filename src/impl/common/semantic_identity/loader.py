"""从随代码发布的 YAML 规则包加载语义规则。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .models import MergeKind, SemanticRule


_CAPTURE_NAME_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_-]*)(?:\.\.\.)?(?::[^}]+)?\}")
_RULE_KEYS = {"id", "node", "path", "statement", "identity", "merge"}


def _string_list(value: object, field: str, rule_id: str) -> tuple[str, ...]:
    """校验字段值是非空字符串列表并返回元组。

    规则文件由人手工维护，字段类型错误要尽早报错；返回不可变元组让规则在加载后
    只读使用，也避免后续匹配阶段意外修改模板。
    """
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"identity 规则 {rule_id} 的 {field} 必须是非空字符串列表")
    return tuple(value)


def _parse_rule(raw: object, source: str) -> SemanticRule:
    """把一条 YAML 规则字典解析并校验成 ``SemanticRule``。

    除了字段存在性和类型，还要校验 identity 引用的 capture 都已在 path/statement
    中定义；在加载阶段拒绝无效规则，可让转换期只处理可靠输入，而不是运行到一半
    才因模板错误失败。
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: identity 规则必须是键值映射")
    unknown = sorted(set(raw) - _RULE_KEYS)
    missing = sorted(_RULE_KEYS - set(raw))
    if unknown:
        raise ValueError(f"{source}: identity 规则包含未知字段: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{source}: identity 规则缺少字段: {', '.join(missing)}")
    rule_id = raw["id"]
    if not isinstance(rule_id, str) or not rule_id:
        raise ValueError(f"{source}: identity 规则 id 必须是非空字符串")
    node_kind = raw["node"]
    if node_kind not in {"leaf", "block"}:
        raise ValueError(f"identity 规则 {rule_id} 的 node 必须是 leaf 或 block")
    path = _string_list(raw["path"], "path", rule_id)
    statement = _string_list(raw["statement"], "statement", rule_id)
    identity = _string_list(raw["identity"], "identity", rule_id)
    try:
        merge_kind = MergeKind(raw["merge"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"identity 规则 {rule_id} 的 merge 无效: {raw['merge']}") from exc

    captures = set()
    for value in [*path, *statement]:
        captures.update(_CAPTURE_NAME_RE.findall(value))
    used = {name for value in identity for name in _CAPTURE_NAME_RE.findall(value)}
    missing_captures = sorted(used - captures)
    if missing_captures:
        raise ValueError(
            f"identity 规则 {rule_id} 引用了未定义 capture: {', '.join(missing_captures)}"
        )
    return SemanticRule(
        rule_id=rule_id,
        node_kind=node_kind,
        path=path,
        statement=statement,
        identity=identity,
        merge_kind=merge_kind,
    )


def load_rule_pack(
    directory: Path,
    *,
    vendor: str | None = None,
    image: str | None = None,
) -> tuple[SemanticRule, ...]:
    """按 manifest 显式列出的文件加载并校验一个规则包。

    manifest 声明 schema、目标厂商/镜像和规则文件清单；显式清单避免无差别扫描目录
    引入无关文件，并让目标校验与重复 id 检查在启动时统一完成，保证后续匹配使用的
    规则集完整且唯一。
    """
    manifest_path = directory / "manifest.yaml"
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle) or {}
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError(f"{manifest_path}: schema_version 必须为 1")
    target = manifest.get("target")
    if not isinstance(target, dict):
        raise ValueError(f"{manifest_path}: target 必须是键值映射")
    if vendor is not None and target.get("vendor") != vendor:
        raise ValueError(f"{manifest_path}: target.vendor 必须为 {vendor}")
    if image is not None and target.get("image") != image:
        raise ValueError(f"{manifest_path}: target.image 必须为 {image}")
    files = manifest.get("rule_files")
    if not isinstance(files, list) or not files or not all(isinstance(item, str) for item in files):
        raise ValueError(f"{manifest_path}: rule_files 必须是非空字符串列表")

    rules: list[SemanticRule] = []
    seen: set[str] = set()
    for filename in files:
        rule_path = directory / filename
        if rule_path.parent != directory or rule_path.suffix not in {".yaml", ".yml"}:
            raise ValueError(f"{manifest_path}: 非法规则文件路径 {filename}")
        with rule_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        raw_rules = payload.get("rules") if isinstance(payload, dict) else None
        if not isinstance(raw_rules, list):
            raise ValueError(f"{rule_path}: rules 必须是列表")
        for raw in raw_rules:
            rule = _parse_rule(raw, str(rule_path))
            if rule.rule_id in seen:
                raise ValueError(f"重复的 identity 规则 id: {rule.rule_id}")
            seen.add(rule.rule_id)
            rules.append(rule)
    return tuple(rules)
