"""适配既有 GNS3 业务客户端所需的最小集成契约。

导入器依赖 Protocol 而不是具体实现，业务侧可以复用已有的 GNS3 封装和 Console
实现，测试中也能直接替换成假对象。
"""

from __future__ import annotations

from typing import Protocol

from .models import Node


class GNS3Client(Protocol):
    """导入器需要的 gns3-server v3 controller 操作。

    只声明必需的七个方法：业务侧用既有客户端实现这些方法即可接入，导入器不关心
    HTTP 细节、鉴权和返回结构（接口对照见 docs/config-importer.md）。
    """

    def get_node(self, project_id: str, node_id: str) -> Node:
        """读取节点当前状态与 compute、Console 信息。

        前置校验和后续登录都要用这份信息，因此它必须反映节点此刻的真实状态，而不是
        创建时的缓存值。
        """
        ...

    def create_node_from_template(self, project_id: str, template_id: str, compute_id: str) -> Node:
        """按模板在指定 compute 上创建节点，用于起临时的 FTP 节点。

        必须落到目标设备所在的 compute，否则临时链路无法在两个节点间建立。
        """
        ...

    def start_node(self, project_id: str, node_id: str) -> None:
        """启动节点。

        启动是异步的，返回不代表节点已就绪，调用方需要再次查询状态或重试连接。
        """
        ...

    def write_node_file(self, project_id: str, node_id: str, path: str, content: bytes) -> None:
        """``POST /v3/projects/{p}/nodes/{n}/files/{path}``：写入 compute 上的节点目录，不是来宾系统。

        FTP 节点是 QEMU 虚拟机，GNS3 无法向运行中的来宾写文件；cfg 只能借节点目录
        下的 ``config.zip`` 在节点启动时经配置盘送进去，所以这一步必须在启动之前。
        """
        ...

    def create_link(self, project_id: str, a_node: str, a_adapter: int, a_port: int,
                    z_node: str, z_adapter: int, z_port: int) -> str:
        """连接两个节点的指定端口，返回 GNS3 分配的 ``link_id``。

        这个 id 是之后删除临时链路的唯一凭据，因此必须返回给调用方保存。
        """
        ...

    def delete_link(self, project_id: str, link_id: str) -> None:
        """删除链路，用于清理本次导入创建的临时连接。"""
        ...

    def delete_node(self, project_id: str, node_id: str) -> None:
        """删除节点，用于清理本次导入创建的临时 FTP 节点。"""
        ...


class Console(Protocol):
    """一个已建立的设备命令行连接。

    导入器只用到登录、执行单条命令和关闭这三种能力，因此 Telnet、串口或任何其它
    传输方式只要实现它们就能接入。
    """

    def login(self, username: str, password: str, ready_pattern: str, timeout: float) -> None:
        """完成登录对话，直到出现 ``ready_pattern`` 描述的就绪提示符。

        不同设备登录时要回答的提示数量不同，因此用"出现就绪提示符"作为结束条件，
        而不是固定的问答轮数。
        """
        ...

    def command(self, text: str, prompt_pattern: str, timeout: float) -> str:
        """发送一条命令，读到 ``prompt_pattern`` 为止并返回这段输出。

        返回值包含命令回显，调用方据此判断成功或错误文本（例如加载配置的报错）。
        """
        ...

    def close(self) -> None:
        """关闭连接，释放 GNS3 侧的 Console 会话。"""
        ...


class ConsoleFactory(Protocol):
    """按主机和端口建立 Console 连接。

    把"如何连接"抽出来，导入器就不必知道节点 Console 是 Telnet 还是别的传输。
    """

    def connect(self, host: str, port: int) -> Console:
        """建立一条尚未登录的连接。

        连接失败应抛 ``OSError`` 或 ``TimeoutError``：上层据此判断节点是否还在启动，
        从而决定重试还是报错。
        """
        ...
