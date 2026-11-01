"""Cisco nodes with vendor-specific semantic queries."""

from impl.common.config_ast.model import ConfigNode

from .semantics import (
    _interface_name_from_header,
    _is_l2transport_header,
    _is_preconfigured_interface_header,
)


class CiscoNode(ConfigNode):
    """Represent an IOS XR command, block, comment, or banner.

    Vendor queries inspect command heads while hierarchy and traversal remain shared with the generic model.
    """

    @property
    def interface_name(self):
        """Resolve an interface declaration using the common vendor parser.

        Pure command semantics canonicalize abbreviations consistently with interface analysis.
        """
        return _interface_name_from_header(self.header)

    @property
    def is_preconfigured_interface(self):
        """Identify interfaces declared before hardware instantiation.

        Uninstantiated hardware declarations must be distinguished before allocating real device interfaces.
        """
        return _is_preconfigured_interface_header(self.header)

    @property
    def l2transport(self):
        """Identify the layer-two interface suffix.

        The suffix belongs to the interface declaration and does not depend on descendant commands.
        """
        return _is_l2transport_header(self.header)
