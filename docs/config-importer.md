# GNS3 单设备配置导入

`config_importer` 与 `config_adaptor` 相互独立。业务端为一台已启动的 XRv9000 或 vMX 构造 `ConfigImporter`，然后逐个调用 `import_cfg(local_cfg_path)`。没有 CLI 入口，也不会自动触发配置自适应。

```python
from config_importer import ConfigImporter, ImportSettings, Port, TelnetConsoleFactory

settings = ImportSettings(
    project_id="<GNS3 project UUID>",
    node_id="<target node UUID>",
    image="vmx",  # 或 xrv9000
    ftp_template_id="<existing FTP template ID>",
    reserved_port=Port(adapter_number=7, port_number=0, interface_name="ge-0/0/7"),
    console_username="root",
    console_password="<initial console password>",
    ftp_username="ftpuser",
    ftp_password="<FTP password>",
    ftp_directory="/data/ftpuser",
)
result = ConfigImporter(existing_gns3_client, TelnetConsoleFactory(), settings).import_cfg("router.cfg")
if not result.success:
    # applied=True, cleanup_ok=False 表示配置已提交，但临时资源清理失败。
    raise RuntimeError((result.error, result.cleanup_errors,
                        result.remaining_link_id, result.remaining_ftp_node_id))
```

业务侧 GNS3 客户端需对接 gns3-server v3 controller（`/v3` 前缀，需 Bearer token），实现 `config_importer.ports.GNS3Client` 的七个方法：

| 方法 | v3 接口 |
|---|---|
| `get_node` | `GET /v3/projects/{p}/nodes/{n}`；`console` 映射为 `console_port`，`console_host` 为 `0.0.0.0` 时换成 compute 地址；`image` 填规范标识（`vmx` / `xrv9000`），不填磁盘文件名 |
| `create_node_from_template` | `POST /v3/projects/{p}/templates/{t}`，body 必须带 `x`、`y`，并传入 `compute_id` |
| `start_node` | `POST /v3/projects/{p}/nodes/{n}/start` |
| `write_node_file` | `POST /v3/projects/{p}/nodes/{n}/files/{path}`，写入 compute 上的节点目录，不是来宾系统 |
| `create_link` | `POST /v3/projects/{p}/links`，body `{"nodes": [{node_id, adapter_number, port_number}, ...]}`，返回 `link_id` |
| `delete_link` | `DELETE /v3/projects/{p}/links/{l}` |
| `delete_node` | `DELETE /v3/projects/{p}/nodes/{n}` |

FTP 节点是 QEMU 虚拟机，GNS3 无法向运行中的来宾写文件，因此 cfg 经 GNS3 配置盘送入：导入器在 FTP 节点首次启动前把 `<node_id>.cfg` 打包成节点目录下的 `config.zip`；启动时 GNS3 将其写入挂在来宾第二块盘上的 FAT 配置盘。导入器登录 FTP 节点后以只读方式 `mount` 该盘（`ftp_config_disk`，默认 `/dev/sdb1`，virtio 接口一般为 `/dev/vdb1`），复制到 `ftp_directory` 并设置属主和 `600` 权限。前提：FTP 模板开启 `create_config_disk`，compute 主机安装 `mtools`，来宾内核支持 vfat，FTP Console 账号有 `mount` 和 `chown` 权限。

配置文件名在 FTP 节点上统一为 `<node_id>.cfg`，下载 URL 为 `ftp://<user>:<password>@<ftp_server_ip>/<node_id>.cfg`，因此 `ftp_directory` 必须是 FTP 账号登录后的根目录。导入器先上传文件，再连接 FTP 节点的 `0/0` 与目标设备注入的预留口。设备侧先识别当前模式（vMX 可从 shell、operational 或 `[edit]` 进入；XR 遇到残留配置会话先 `abort`），配置临时地址后在 `reachability_timeout` 内反复 ping FTP 地址，连通后 vMX 执行 `load override` / `commit`，XRv9000 执行 `load` / `commit replace` 并确认替换。加载回显中出现语法或部分加载错误时不会提交。结束时 vMX 执行 `rollback 0` 并退出配置模式，XR 执行 `abort`，不给下一次导入留下候选配置。导入成功或失败都会尝试关闭 Console、删除本次创建的链路和 FTP 节点。结果不包含 Console 原始回显或凭据。若临时接口地址已提交、最终导入却失败，`temporary_device_address_may_remain=True` 提醒业务侧处理该地址。

`TelnetConsoleFactory` 基于 `telnetlib`；Python 3.13 起由依赖 `standard-telnetlib` 提供。也可注入任何实现 `config_importer.ports.Console` 的连接。

模块划分：`importer` 只编排流程并按创建顺序倒序清理；`ftp_server` 管理临时 FTP 节点；`drivers` 每个类封装一种设备的 CLI 会话，新增平台时继承 `Driver` 并登记到 `DRIVERS`；`consoles` 负责带重试的 Console 连接和登录；`ImportSettings` 在构造时校验参数。

当前端口映射、FTP 模板、初始登录凭据由业务侧提供；设备必须已经启动。真实镜像提示符、FTP 模板上传路径和导入回显需要在对应 GNS3 环境中验证。
