"""从源码安装位置定位随程序发布的规则包目录。"""

from enum import StrEnum
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_RULE_ROOT = _PROJECT_ROOT / "config" / "rules"


class RuleYamlFile(StrEnum):
    """定义由规则包目录和程序共同约定的 YAML 文件。

    固定文件集中声明可避免各加载器重复拼写文件名，同时允许 manifest 继续掌控
    identity 规则文件的完整清单。
    """

    MANIFEST = "manifest.yaml"
    CLEANING = "cleaning.yaml"
    INPUT_NORMALIZATION = "input_normalization.yaml"
    EMPTY_BLOCKS = "empty_blocks.yaml"


def rule_pack_dir(vendor_dir: str, image: str) -> Path:
    """返回指定厂商和目标镜像的规则包目录。

    调用方只提供领域身份，目录层级由公共路径模块统一维护，新增镜像无需复制拼接逻辑。
    """
    return _RULE_ROOT / vendor_dir / image


def rule_yaml_path(
    vendor_dir: str,
    image: str,
    rule_file: RuleYamlFile,
) -> Path:
    """返回规则包内指定类型 YAML 的完整路径。

    固定文件名与规则包目录组合集中在这里，使加载模块无需了解 `config/rules` 的布局。
    """
    return rule_pack_dir(vendor_dir, image) / rule_file.value
