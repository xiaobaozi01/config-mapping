"""单次配置导入的输入参数、校验规则与结果模型。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


# node_id、用户名、接口名等值会拼进设备 CLI、FTP 节点的 shell 命令和 FTP URL，
# 所以先用白名单限制字符集，避免空格、分号、引号等改变命令行的语义。
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class Port:
    """目标设备上为 FTP 临时链路预留的一个接口位置。

    三个字段一起才能确定链路的两个端点：adapter/port 供 GNS3 建链路，接口名供
    设备侧配置地址。它们无法互相推导，因此必须整体提供。
    """

    adapter_number: int
    port_number: int
    interface_name: str


@dataclass(frozen=True, slots=True)
class Node:
    """导入期间使用的 GNS3 节点快照。

    只保留导入真正需要的字段（状态、所在 compute、Console 接入信息和镜像标识），
    这样导入器不依赖业务侧完整的节点模型；``image`` 为空表示客户端未提供该信息。
    """

    node_id: str
    status: str
    compute_id: str
    console_host: str
    console_port: int
    image: str = ""


@dataclass(frozen=True, slots=True)
class FtpTransfer:
    """设备视角下的临时点到点 FTP 通路。

    驱动要按各自设备的语法拼地址、掩码和下载 URL，所以这里保存两端地址和凭据，
    而不是一个提前拼好的字符串，便于不同平台复用同一份数据。
    """

    server_ip: str
    device_ip: str
    prefix_length: int
    username: str
    password: str
    filename: str

    @property
    def netmask(self) -> str:
        """把前缀长度换算成点分十进制掩码。

        IOS XR 的地址命令只接受掩码形式（``ipv4 address <ip> <mask>``），而设置
        里用的是前缀长度，因此在这里按需转换。
        """
        return str(ipaddress.ip_network(f"{self.server_ip}/{self.prefix_length}", strict=False).netmask)

    @property
    def url(self) -> str:
        """拼出设备可直接下载的 FTP URL。

        用户名和密码做完整百分号编码：密码常含 ``@``、``:``、``/`` 等字符，不编码
        会破坏 URL 结构，甚至把凭据片段误解析成主机名。
        """
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return f"ftp://{user}:{password}@{self.server_ip}/{self.filename}"


@dataclass(frozen=True, slots=True)
class ImportSettings:
    """一次导入的全部外部输入，并在构造时就完成校验。

    这些字段大多会进入设备命令、FTP 节点的 shell 命令或 FTP URL，业务侧的值又
    常来自界面和配置文件，所以在最外层构造时就把非法值挡掉，避免会话或临时节点
    已经创建之后才失败、还得回头清理。
    """

    project_id: str
    node_id: str
    image: str  # xrv9000 或 vmx
    ftp_template_id: str
    reserved_port: Port
    console_username: str
    console_password: str
    ftp_username: str
    ftp_password: str
    ftp_directory: str
    ftp_server_ip: str = "10.0.0.1"
    device_ip: str = "10.0.0.2"
    prefix_length: int = 24
    ftp_console_username: str = ""
    ftp_console_password: str = ""
    # GNS3 配置盘在 FTP 来宾内的分区名，取决于模板的 hdd 磁盘接口
    # （SCSI/SATA 一般为 /dev/sdb1，virtio 一般为 /dev/vdb1）。
    ftp_config_disk: str = "/dev/sdb1"
    command_timeout: float = 120.0
    reachability_timeout: float = 120.0

    def __post_init__(self) -> None:
        """在冻结的 dataclass 构造完成后立即校验字段。

        类本身不可变，没有别的地方能承载校验逻辑；放在这里可以让任何构造点都
        得到同样的检查结果。
        """
        self._check_required()
        self._check_names()
        self._check_network()
        if self.command_timeout <= 0 or self.reachability_timeout < 0:
            raise ValueError("command_timeout 必须大于零，reachability_timeout 不能为负")

    @property
    def transfer(self) -> FtpTransfer:
        """由设置推导出本次使用的 FTP 通路。

        文件名固定为 ``<node_id>.cfg``：与节点绑定后，同一 FTP 模板被多次复用也
        不会互相覆盖，或让设备下载到上一次导入的文件。
        """
        return FtpTransfer(self.ftp_server_ip, self.device_ip, self.prefix_length,
                           self.ftp_username, self.ftp_password, f"{self.node_id}.cfg")

    def _check_required(self) -> None:
        """拒绝缺失的必填字段和无效的预留端口。

        这些问题不需要任何外部交互就能发现，先失败可以省掉后面创建 FTP 节点、
        建链路和登录设备的开销。
        """
        for label in ("project_id", "node_id", "image", "ftp_template_id", "console_username",
                      "ftp_username", "ftp_password"):
            if not getattr(self, label):
                raise ValueError(f"{label} 不能为空")
        port = self.reserved_port
        if port.adapter_number < 0 or port.port_number < 0 or not port.interface_name:
            raise ValueError("预留端口必须包含有效的 adapter、port 和接口名")

    def _check_names(self) -> None:
        """校验会进入命令行的名称、FTP 目录和配置盘路径。

        这些值最终出现在设备 CLI、FTP 节点 shell 命令和 FTP URL 路径里，所以排除
        空格、分号等特殊字符以及 ``..``，防止命令被改写或文件被写到目录之外。
        """
        # 这些值会进入 CLI 命令、shell 命令和 FTP URL 路径。
        if not SAFE_NAME.fullmatch(self.reserved_port.interface_name.replace("/", "")):
            raise ValueError("预留接口名包含不支持的字符")
        if not self.ftp_directory.startswith("/") or ".." in Path(self.ftp_directory).parts:
            raise ValueError("FTP 目录必须是绝对路径，且不能包含 ..")
        if not re.fullmatch(r"/dev/[A-Za-z0-9_/-]+", self.ftp_config_disk):
            raise ValueError("ftp_config_disk 必须是 /dev 下的设备路径")
        if not SAFE_NAME.fullmatch(self.node_id) or not SAFE_NAME.fullmatch(self.ftp_username):
            raise ValueError("node_id 和 ftp_username 只能包含字母、数字、下划线、点和连字符")

    def _check_network(self) -> None:
        """确认临时链路两端是同一网段内两个不同的 IPv4 地址。

        设备必须把 FTP 地址当作直连邻居才能下载配置，地址不同网段或重合都会让
        后续的连通性探测永远失败，所以在配置之前就查出来。
        """
        server = ipaddress.ip_address(self.ftp_server_ip)
        device = ipaddress.ip_address(self.device_ip)
        if not isinstance(server, ipaddress.IPv4Address) or not isinstance(device, ipaddress.IPv4Address):
            raise ValueError("FTP 临时链路只支持 IPv4")
        network = ipaddress.ip_network(f"{self.ftp_server_ip}/{self.prefix_length}", strict=False)
        if device == server or device not in network:
            raise ValueError("设备地址与 FTP 地址必须在同一子网")


@dataclass(frozen=True, slots=True)
class ImportResult:
    """一次 ``import_cfg`` 的结果，区分“配置是否生效”和“临时资源是否清理干净”。

    配置提交成功但清理失败时，资源仍留在 GNS3 中；把两者拆开报告，并把残留对象的
    标识一并返回，业务侧才能决定是重试清理还是人工处理。字段只描述状态，不含
    Console 回显和凭据。
    """

    config_path: Path
    applied: bool
    cleanup_ok: bool
    error: str | None = None
    cleanup_errors: tuple[str, ...] = ()
    remaining_link_id: str | None = None
    remaining_ftp_node_id: str | None = None
    temporary_device_address_may_remain: bool = False

    @property
    def success(self) -> bool:
        """只有配置已生效且没有残留资源，才算整体成功。"""
        return self.applied and self.cleanup_ok


class ImportStageError(RuntimeError):
    """某个已知导入阶段上的、已脱敏的可读失败。

    带上阶段名是为了让业务侧能直接定位失败步骤；消息只包含阶段说明和异常类型，
    绝不携带 Console 原始回显、命令内容或 URL 里的凭据。
    """

    def __init__(self, stage: str, message: str):
        """记录阶段名，并把阶段名并入面向运维的异常文本。"""
        self.stage = stage
        super().__init__(f"{stage}: {message}")


def describe_error(exc: Exception, fallback: str) -> str:
    """把异常转成一句可以安全外传的说明。

    ``ImportStageError`` 的文本本身就是脱敏的，可以直接使用；其它异常（网络库、
    telnet 等）的原文可能带上命令回显和凭据，所以只保留异常类型名，并配上调用方
    给出的场景说明。
    """
    return str(exc) if isinstance(exc, ImportStageError) else f"{fallback}: {type(exc).__name__}"
