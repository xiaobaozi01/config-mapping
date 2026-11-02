"""Cisco IOS XR 接口能力组合。"""

from ...document import CiscoDocument
from .analysis import CiscoInterfaceAnalysis
from .transform import CiscoInterfaceTransform


class CiscoInterfaceCapabilities:
    """组合 IOS XR 接口查询和修改能力。

    每次访问子能力都创建轻量对象，不缓存长期服务。
    """

    __slots__ = ("_document",)

    def __init__(self, document: CiscoDocument):
        """绑定接口能力所属文档。

        构造阶段不读取或修改配置。
        """
        self._document = document

    @property
    def analysis(self) -> CiscoInterfaceAnalysis:
        """返回只读接口分析能力。

        返回对象只存活于当前调用链。
        """
        return CiscoInterfaceAnalysis(self._document)

    @property
    def transform(self) -> CiscoInterfaceTransform:
        """返回接口修改能力。

        修改事务由返回对象的方法管理。
        """
        return CiscoInterfaceTransform(self._document)
