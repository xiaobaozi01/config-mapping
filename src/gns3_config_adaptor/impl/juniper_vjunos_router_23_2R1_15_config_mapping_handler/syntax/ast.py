"""可变的 Junos 节点及状态前缀语义。"""

from ...common.config_ast.model import ConfigNode


def base_header(header):
    """移除状态前缀，得到用于命令匹配的内容。

    ``inactive`` 和 ``protect`` 只影响状态，不能改变业务匹配使用的命令身份。
    """
    value = header.strip()
    while value.startswith(("inactive:", "protect:")):
        value = value.split(":", 1)[1].strip()
    return value


def is_prose_statement(header: str) -> bool:
    """识别包含说明文字的 Junos 语句。

    说明内容中的接口名称不构成业务依赖，引用改写也应保留这些文字。
    """
    words = base_header(header).split(maxsplit=1)
    return bool(
        words and words[0] in {"description", "message", "announcement"}
    )


class JuniperNode(ConfigNode):
    """表示 Junos 语句，并与分号等结构标点解耦。

    状态前缀保留在命令头中，供生效判断和部署输出过滤使用。
    """

    @property
    def header(self):
        """返回不带结束分号的命令头。

        存储时去除首尾空白和结构上用于结束语句的分号。
        """
        return self._header

    @header.setter
    def header(self, value):
        """规范化 Parser 或业务编辑传入的命令头。

        存储时去除首尾空白和结构上用于结束语句的分号。
        """
        self._header = value.strip().removesuffix(";").rstrip()

    @property
    def base_header(self):
        """返回移除状态前缀后的语义命令。

        ``inactive`` 和 ``protect`` 只影响状态，不能改变业务匹配使用的命令身份。
        """
        return base_header(self.header)

    @property
    def configured_inactive(self):
        """无论 ``inactive`` 位于哪个前缀位置都能识别。

        多个 ``protect`` 前缀可能出现在 ``inactive`` 之前，只检查第一个前缀会漏掉停用节点。
        """
        value = self.header
        while value.startswith(("inactive:", "protect:")):
            if value.startswith("inactive:"):
                return True
            value = value.split(":", 1)[1].strip()
        return False

    @property
    def effective(self):
        """排除已删除、inactive 和纯注释节点。

        停用语句不得参与接口发现或继承配置解析。
        """
        return (
            self.active
            and not self.configured_inactive
            and not self.is_comment_only
        )


def top_block(
    root: JuniperNode,
    name: str,
    *,
    create: bool = False,
) -> JuniperNode | None:
    """查找活动顶层配置块，并可按需创建。

    新块使用显式子节点列表创建；调用方负责在需要时通过 Document Editor
    发布工作树修改。
    """
    for node in root.nodes:
        if node.effective and node.is_block and node.base_header == name:
            return node
    if not create:
        return None
    node = JuniperNode(name, [])
    root.children.append(node)
    node.parent = root
    return node
