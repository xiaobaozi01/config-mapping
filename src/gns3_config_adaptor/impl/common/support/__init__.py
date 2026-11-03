"""共享路径、错误、缓存和实验环境常量。"""

from .config_paths import (
    cleaning_rules_path,
    default_target_image,
    empty_blocks_path,
    identity_rules_path,
    image_profiles_path,
    input_normalization_path,
    rule_prefix,
)
from .errors import InvariantViolation, require_invariant
from .lab_account import LAB_PASSWORD, LAB_USERNAME

__all__ = [
    "LAB_PASSWORD",
    "LAB_USERNAME",
    "InvariantViolation",
    "cleaning_rules_path",
    "default_target_image",
    "empty_blocks_path",
    "identity_rules_path",
    "image_profiles_path",
    "input_normalization_path",
    "require_invariant",
    "rule_prefix",
]
