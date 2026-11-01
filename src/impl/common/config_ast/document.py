"""可变配置树的通用 Document 生命周期。"""

from copy import copy, deepcopy

from .editor import ConfigEditor
from .model import tree_signature


class AstDocument:
    """持有一棵可变配置树、修订号和派生分析缓存。

    原始文本不是第二份配置状态；``source`` 始终由当前树派生。
    """

    @property
    def source(self):
        """序列化当前树，但不应用最终部署策略。

        该视图保留显式树结构；``render`` 会另外应用目标镜像的空块策略。
        """
        return self.writer.write(self.root)

    @property
    def revision(self):
        """暴露当前修改代数，供诊断和缓存判断使用。

        有效树修改会推进修订号；规则恢复时可以连同其先前修订号一起还原。
        """
        return self._revision

    def edit(self):
        """为当前文档创建结构编辑收集器。

        Editor 先收集操作，使遍历完成后才改变子节点列表。
        """
        return ConfigEditor(self)

    def tree_changed(self):
        """把已校验的树修改同步到修订号和查询缓存。

        树签名可避免无实际变化的批量作用域使可复用分析结果失效。
        """
        signature = tree_signature(self.root)
        if signature != getattr(self, "_last_signature", None):
            self._revision += 1
            self._interface_analysis_cache = type(
                self._interface_analysis_cache
            )()
            self._last_signature = signature

    def restore_tree(self, root, revision=None):
        """恢复规则备份，并使旧节点引用和缓存失效。

        被替换树中的节点必须重新查询后，才能用于后续编辑。
        """
        self.root = root
        self._last_signature = None
        self.tree_changed()
        if revision is not None:
            self._revision = revision

    def __deepcopy__(self, memo):
        """直接复制 AST，不通过渲染或重新解析创建副本。

        父节点关系和派生缓存不得继续引用原文档。
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
        """规范化命令头首尾的空白。

        结构标点由 Parser 和 Writer 处理，不参与业务命令匹配。
        """
        return header.strip()
