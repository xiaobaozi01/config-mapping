# GNS3 配置自适应

将 IOS XR 和 Junos 大括号配置转换为适用于 GNS3 XRv9000/vMX 的配置。需求基线见 [docs/requirements.md](docs/requirements.md)，完整转换过程见 [docs/processing-flow.md](docs/processing-flow.md)。

转换前会展开常见的 IOS XR `group/apply-group` 和 Junos `groups/apply-groups` 配置，使继承的接口及认证配置进入后续 NNI、UNI、Auth 责任链。已应用 group 无法静态求值时转换失败，不输出可能不完整的设备配置。

group 展开是事务式的，并遵循显式配置、嵌套层级和 group 列表顺序的厂商优先级。冲突详情写入 `report.json` 的 `group_conflicts` 字段。

## 厂商实现边界

- `parsers/cisco_iosxr.py`：只负责 IOS XR 的语法、group/apply-group、接口与认证改写。
- `parsers/juniper_junos.py`：只负责 Junos 大括号语法、groups/apply-groups、接口与认证改写。
- `parsers/common.py`：只放接口数据结构以及父接口、子接口编号等无厂商语义的工具。
- `handlers.py`：保留 Group、NNI、UNI、Auth 责任链，只通过统一文档接口调度，不再包含厂商分支。

`documents.py` 仅作为旧导入路径的兼容门面；新增厂商逻辑应放入各自模块。

## 测试数据

测试配置和拓扑均为独立文件，位于 `tests/fixtures/`，不会嵌入 Python 代码：

- `iosxr_bundle/`：IOS XR 聚合 NNI、UNI、group 和认证清洗样例。
- `junos_bundle/`：Junos 聚合 NNI、UNI、groups 和认证清洗样例。
- `iosxr_duplicate_vlan/`：UNI VLAN 冲突重新分配样例。
- `iosxr_mlag/`：IOS XR 同一 Bundle 跨对端拆分、一对多引用与 QinQ UNI 样例。
- `junos_mlag/`：Junos ae 跨对端拆分、裸口清理与 QinQ UNI 样例。
- `cross_vendor/`：Cisco XRv9000 与 Juniper vMX 互联时，普通 NNI 和 `Bundle-Ether`/`ae` 聚合两端独立适配样例。
- `junos_unresolved/`：无法解析 group 时事务回滚样例。
- `group_configs/`：独立的厂商 group 优先级和通配匹配配置。

每个端到端样例目录包含一个双 sheet 的 `topology.xlsx` 和对应的 `configs/*.cfg`。

## 安装与运行

```bash
python3 -m pip install -e .
config-adaptor convert \
  --topology topology.xlsx \
  --config-dir configs \
  --output-dir output \
  --profiles config/image_profiles.yaml
```

`--profiles` 可调整两类镜像的数据接口列表；列表最后一个接口用于 UNI，其余接口可分配给 NNI。需要增加保守清洗规则时传入 `--rules config/cleaning_rules.example.yaml`。

Cisco 与 Juniper 的跨厂商 NNI 会按链路两端各自的厂商语法和镜像 Profile 独立分配接口，不要求两端目标接口同名。转换器不审计链路两端的 IP、VLAN、MTU 或封装是否一致，这些业务一致性由输入配置保证。

UNI 只迁移有 IP、L2VC/L2Circuit、L2 绑定或全局业务引用的接口；裸口删除。Cisco 会沿 `bridge-domain` 关联活跃 attachment circuit 与 BVI，Juniper 会沿 `bridge-domains`/`vlans` 关联活跃接口、VLAN 与 IRB；未关联活跃业务的 BVI/IRB 不迁移。目标 UNI 统一重写为 QinQ，旧标签匹配和 VLAN rewrite 清除，外层 VLAN 是设备内唯一的目标子接口编号，内层优先保留可识别的原 VLAN。

也可以不安装直接运行：

```bash
PYTHONPATH=src python3 -m config_adaptor convert --topology topology.xlsx --config-dir configs --output-dir output
```

固定实验账号为 `labadmin / Gns3Lab@2026`，仅用于隔离的 GNS3 实验环境。

IOS XR 认证清洗同时删除自定义 `usergroup`、`taskgroup` 以及 `line` 下对旧认证、授权、计费和用户组的引用；`exec-timeout` 等非认证终端参数保留。删除结果按类别写入 `report.json` 认证事件的 `removed_by_type`。

## 默认 Excel 列

设备列表：`设备名称`、`厂商`、`配置文件`。链接表：`A端设备`、`A端接口`、`Z端设备`、`Z端接口`。程序同时识别常见中英文别名。

## 输出

- `topology-adapted.xlsx`
- `configs/*.cfg`
- `interface-mapping.json`
- `report.json`
- `README.txt`
