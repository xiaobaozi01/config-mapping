"""Cisco IOS XR Group 公共门面。"""

from .capabilities import CiscoGroupOperations
from .expander import CiscoGroupExpander

__all__ = ["CiscoGroupExpander", "CiscoGroupOperations"]
