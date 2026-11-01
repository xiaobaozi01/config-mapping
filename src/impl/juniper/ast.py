"""Mutable Junos nodes and state-prefix semantics."""

from impl.common.config_ast.model import ConfigNode


def base_header(header):
    """Remove state prefixes for command matching.

    Inactive and protect prefixes affect state, but must not change the identity used by business matching.
    """
    value = header.strip()
    while value.startswith(("inactive:", "protect:")):
        value = value.split(":", 1)[1].strip()
    return value


class JuniperNode(ConfigNode):
    """Represent a Junos statement independently of its punctuation.

    State prefixes remain part of the command head so inactive configuration can be preserved in output.
    """

    @property
    def header(self):
        """Return the command head without a terminating semicolon.

        Command heads are stored without outer whitespace or the terminating structural semicolon.
        """
        return self._header

    @header.setter
    def header(self, value):
        """Normalize a command head supplied by parsers or business edits.

        Command heads are stored without outer whitespace or the terminating structural semicolon.
        """
        self._header = value.strip().removesuffix(";").rstrip()

    @property
    def base_header(self):
        """Return the semantic command without status prefixes.

        Inactive and protect prefixes affect state, but must not change the identity used by business matching.
        """
        return base_header(self.header)

    @property
    def configured_inactive(self):
        """Recognize inactive regardless of its order among prefixes.

        Repeated protect prefixes may precede inactive, so checking only the first prefix would miss disabled nodes.
        """
        value = self.header
        while value.startswith(("inactive:", "protect:")):
            if value.startswith("inactive:"):
                return True
            value = value.split(":", 1)[1].strip()
        return False

    @property
    def effective(self):
        """Exclude removed, inactive, and comment-only nodes.

        A disabled statement must not participate in interface discovery or inherited configuration resolution.
        """
        return (
            self.active
            and not self.configured_inactive
            and not self.is_comment_only
        )
