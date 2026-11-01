"""带 Cisco 专用语义查询的配置节点。"""

from impl.common.config_ast.model import ConfigNode

from .semantics import (
    _interface_name_from_header,
    _is_l2transport_header,
    _is_preconfigured_interface_header,
)


class CiscoNode(ConfigNode):
    """表示 IOS XR 命令、块、注释或 banner。

    厂商查询检查命令头，层级和遍历能力继续由公共模型提供。
    """

    @property
    def interface_name(self):
        """使用公共厂商解析逻辑解析接口声明。

        纯命令语义会与接口分析使用相同的规则规范化缩写。
        """
        return _interface_name_from_header(self.header)

    @property
    def is_preconfigured_interface(self):
        """识别硬件实例化之前声明的接口。

        分配真实设备接口前，必须先区分尚未实例化的硬件声明。
        """
        return _is_preconfigured_interface_header(self.header)

    @property
    def l2transport(self):
        """识别二层接口的 ``l2transport`` 后缀。

        该后缀属于接口声明本身，不依赖接口内部的后代命令。
        """
        return _is_l2transport_header(self.header)
