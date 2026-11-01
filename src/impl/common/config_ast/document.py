"""Shared document lifecycle for mutable configuration trees."""

from copy import copy, deepcopy

from .editor import ConfigEditor
from .model import tree_signature


class AstDocument:
    """Own one mutable tree, its revision, and derived analysis caches.

    The original text is not a second configuration state; source is derived from the current tree.
    """

    @property
    def source(self):
        """Serialize the current tree without final deployment policies.

        This view preserves explicit tree structure; render separately applies deployment-specific empty-block policies.
        """
        return self.writer.write(self.root)

    @property
    def revision(self):
        """Expose the mutation generation for diagnostics.

        A completed semantic tree change advances the revision; restoring a rule can restore its prior revision.
        """
        return self._revision

    def edit(self):
        """Create a structural edit collector for this document.

        The editor collects operations so traversal can finish before child lists are changed.
        """
        return ConfigEditor(self)

    def tree_changed(self):
        """Publish a validated tree change to revision and query caches.

        Tree signatures prevent no-op bulk scopes from invalidating otherwise reusable analysis results.
        """
        signature = tree_signature(self.root)
        if signature != getattr(self, "_last_signature", None):
            self._revision += 1
            self._interface_analysis_cache = type(
                self._interface_analysis_cache
            )()
            self._last_signature = signature

    def restore_tree(self, root, revision=None):
        """Restore a rule backup and invalidate references and caches.

        Nodes from the replaced tree must be queried again before subsequent edits can use them.
        """
        self.root = root
        self._last_signature = None
        self.tree_changed()
        if revision is not None:
            self._revision = revision

    def __deepcopy__(self, memo):
        """Copy the AST directly without rendering or reparsing it.

        Parent relationships and derived caches must not retain references to the original document.
        """
        cloned = copy(self)
        cloned.root = self.root.clone()
        cloned._edit_scope = None
        cloned._parser = deepcopy(self._parser)
        cloned._interface_analysis_cache = type(
            self._interface_analysis_cache
        )()
        memo[id(self)] = cloned
        return cloned

    def normalize_header(self, header):
        """Normalize outer command whitespace.

        Structural punctuation belongs to the parser and writer, not to business command matching.
        """
        return header.strip()
