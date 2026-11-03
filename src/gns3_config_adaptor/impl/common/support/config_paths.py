"""按资源类别管理 YAML 配置路径及规则元数据。"""

from pathlib import Path

__all__ = [
    "cleaning_rules_path",
    "default_target_image",
    "empty_blocks_path",
    "identity_rules_path",
    "image_profiles_path",
    "input_normalization_path",
    "rule_prefix",
]

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_CONFIG_ROOT = _PROJECT_ROOT / "config"
_RULE_ROOT = _CONFIG_ROOT / "rules"


_DEFAULT_RULE_IMAGES = {
    "cisco_iosxr": "xrv9000",
    "juniper_junos": "vmx",
}
_RULE_PREFIXES = {
    "cisco_iosxr": "cisco",
    "juniper_junos": "juniper",
}


def rule_prefix(vendor: str) -> str:
    """返回 YAML 规则标识使用的厂商前缀。

    规则加载器共用此对应表，避免把规则命名约定放在适配层的厂商枚举中。
    """
    try:
        return _RULE_PREFIXES[vendor]
    except KeyError as exc:
        raise ValueError(f"{vendor} 没有可用规则前缀") from exc


def default_target_image(vendor: str) -> str:
    """返回厂商的默认规则镜像。

    默认镜像供资源定位和目标校验共用，没有内置规则包的厂商在此明确报错。
    """
    try:
        return _DEFAULT_RULE_IMAGES[vendor]
    except KeyError as exc:
        raise ValueError(f"{vendor} 没有可用目标镜像") from exc


def _rule_pack_dir(vendor: str) -> Path:
    """定位厂商默认镜像对应的规则目录。

    目录层级作为资源入口的内部实现，调用方无需选择镜像或自行拼接配置路径。
    """
    return _RULE_ROOT / vendor / default_target_image(vendor)


def image_profiles_path(path: Path | None = None) -> Path:
    """返回内置 profile 地址或调用方指定的文件路径。

    自定义路径原样透传；返回内置地址不代表自动加载，加载器继续控制默认值行为。
    """
    return path if path is not None else _CONFIG_ROOT / "image_profiles.yaml"


def cleaning_rules_path(vendor: str) -> Path:
    """返回厂商的清洗规则 YAML 路径。

    清洗加载器通过资源专用入口定位配置，文件名和默认镜像均在此管理。
    """
    return _rule_pack_dir(vendor) / "cleaning.yaml"


def input_normalization_path(vendor: str) -> Path:
    """返回厂商的输入整理 YAML 路径。

    输入整理加载器只提供厂商身份，具体文件名和所在规则包由此入口决定。
    """
    return _rule_pack_dir(vendor) / "input_normalization.yaml"


def empty_blocks_path(vendor: str) -> Path:
    """返回厂商的空块输出策略 YAML 路径。

    厂商策略加载器通过此入口获取默认文件，无需了解配置目录结构。
    """
    return _rule_pack_dir(vendor) / "empty_blocks.yaml"


def identity_rules_path(vendor: str) -> Path:
    """返回厂商的语义 identity 规则包目录。

    规则包作为一类资源提供目录入口，内部 manifest 和清单文件由规则加载器管理。
    """
    return _rule_pack_dir(vendor)
