"""跨厂商 Document 的窄能力协议。"""

from .bundles import BundleOperations
from .cleaning import CleaningOperations
from .compatibility import CompatibilityOperations
from .document import DocumentCore, VendorDocument
from .groups import GroupOperations
from .interfaces import (
    InterfaceAnalysis,
    InterfaceCapabilities,
    InterfaceTransform,
)
from .pruning import PruningOperations
from .references import ReferenceOperations
from .simulation import SimulationOperations
from .validation import ValidationOperations

__all__ = [
    "BundleOperations",
    "CleaningOperations",
    "CompatibilityOperations",
    "DocumentCore",
    "GroupOperations",
    "InterfaceAnalysis",
    "InterfaceCapabilities",
    "InterfaceTransform",
    "PruningOperations",
    "ReferenceOperations",
    "SimulationOperations",
    "ValidationOperations",
    "VendorDocument",
]
