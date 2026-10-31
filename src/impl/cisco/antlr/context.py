"""生成 Context 的只读查询能力；不保存第二份配置树或 Document 引用。"""

import re
from antlr4 import ParserRuleContext


class CiscoContext(ParserRuleContext):
    @property
    def snapshot(self):
        root = self
        while root.parentCtx is not None:
            root = root.parentCtx
        return root._snapshot

    @property
    def nodes(self):
        return tuple(child for child in self.getChildren() if isinstance(child, CiscoContext))

    @property
    def raw_header(self):
        return self.start.text.splitlines()[0] if self.parentCtx is not None else "<root>"

    @property
    def header(self):
        raw = self.raw_header.strip()
        return "" if raw.startswith("!") and raw != "!" else raw

    @property
    def comment(self):
        raw = self.raw_header.strip()
        return raw if raw.startswith("!") and raw != "!" else None

    @property
    def active(self):
        return True

    @property
    def is_formatting(self):
        return self.header in {"", "!"}

    @property
    def is_block(self):
        if getattr(self, "_was_block", False) or any(not child.is_formatting or child.header == "!" for child in self.nodes):
            return True
        top_level = self.parentCtx is not None and self.parentCtx.parentCtx is None
        return top_level and bool(re.match(
            r"^(?:interface|router|vrf|l2vpn|mpls|username|line|segment-routing|telemetry)\b", self.header, re.I,
        ))

    @property
    def interface_name(self):
        from ..document import _interface_name_from_header
        return _interface_name_from_header(self.header)

    @property
    def is_preconfigured_interface(self):
        return bool(re.match(r"^interface\s+preconfigure(?:\s|$)", self.header, re.I))

    @property
    def l2transport(self):
        return bool(re.match(r"interface\s+.+\s+l2transport\s*$", self.header, re.I))

    @property
    def indent(self):
        raw = self.raw_header.expandtabs(8)
        return len(raw) - len(raw.lstrip())

    @property
    def text(self):
        if self.parentCtx is None:
            return self.snapshot.text
        return self.snapshot.text[self.start.start:self.stop.stop + 1]

    @property
    def path(self):
        ancestors = []
        parent = self.parentCtx
        while parent is not None and parent.parentCtx is not None:
            ancestors.append(parent.header)
            parent = parent.parentCtx
        return tuple(reversed(ancestors))

    def walk(self, *, include_self=False, include_formatting=False):
        pending = [self] if include_self else list(reversed(self.nodes))
        while pending:
            context = pending.pop()
            if include_formatting or not context.is_formatting:
                yield context
            pending.extend(reversed(context.nodes))
