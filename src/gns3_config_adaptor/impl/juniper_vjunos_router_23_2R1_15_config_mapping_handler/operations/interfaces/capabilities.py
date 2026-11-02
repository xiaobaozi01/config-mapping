"""Juniper Junos 接口能力组合。"""

from ...document import JunosDocument
from .analysis import JunosInterfaceAnalysis
from .transform import JunosInterfaceTransform


class JunosInterfaceCapabilities:
    """组合 Junos 接口查询和修改能力。

    每次访问子能力都创建轻量对象，不缓存长期服务。
    """

    __slots__ = ("_document",)

    def __init__(self, document: JunosDocument):
        """绑定接口能力所属文档。

        构造阶段不读取或修改配置。
        """
        self._document = document

    @property
    def analysis(self) -> JunosInterfaceAnalysis:
        """返回只读接口分析能力。

        返回对象只存活于当前调用链。
        """
        return JunosInterfaceAnalysis(self._document)

    @property
    def transform(self) -> JunosInterfaceTransform:
        """返回接口修改能力。

        修改事务由返回对象的方法管理。
        """
        return JunosInterfaceTransform(self._document)
