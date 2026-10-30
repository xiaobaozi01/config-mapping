# Cisco IOS XR 与 Juniper Junos 配置处理流程

## 1. 文档目的

本文档说明配置自适应系统如何把真实 Cisco IOS XR 和 Juniper Junos 配置转换为适用于 GNS3 XRv9000 与 vMX 的配置。

内容按照“整体流程 → 公共处理 → 厂商处理 → 具体配置改写”的顺序展开，重点说明：

- Excel 拓扑如何参与接口分类。
- Cisco 和 Juniper 配置如何解析。
- Group 如何展开以及冲突如何处理。
- 为什么必须先计算 NNI 保留闭包，再清理非 NNI。
- 聚合业务层如何保留。
- 非 NNI 如何连同业务依赖一起裁剪。
- 认证和权限配置如何清洗。
- 接口引用、报告和输出如何生成。

## 2. 输入与输出

### 2.1 输入

系统接收三类输入。

#### Excel 拓扑

Excel 至少包含两个工作表：

- `设备列表`：设备名称、厂商、配置文件。
- `链接表`：A 端设备、A 端接口、Z 端设备、Z 端接口。

链接表中出现的接口均被视为 NNI 物理接口。

#### 设备配置

- Cisco：IOS XR 文本配置。
- Juniper：Junos 大括号层级配置。
- Huawei：不参与配置转换，也不需要配置文件，只作为拓扑对端；与 Huawei 相连的 Cisco/Juniper 端点仍按本端 Profile 适配，Huawei 侧接口名保持不变。

#### 镜像接口配置

镜像 Profile 定义 XRv9000 和 vMX 可以使用的数据接口，并按照列表位置划分用途：

```text
interfaces[:-3]  → NNI 可分配接口
interfaces[-3:]  → 三个空置预留接口
```

### 2.2 输出

成功转换后生成：

```text
output/
├── topology-adapted.xlsx
├── configs/
│   ├── R1.cfg
│   ├── R2.cfg
│   ├── J1.cfg
│   └── J2.cfg
├── interface-mapping.json
├── report.json
└── README.txt
```

- `topology-adapted.xlsx`：更新 NNI 接口并删除冗余聚合成员行，保留 Huawei 等未适配设备行及其链路。
- `configs/*.cfg`：适配后的设备配置。
- `interface-mapping.json`：源接口到目标接口的结构化映射。
- `report.json`：错误、告警、Group 冲突、清洗和转换事件。
- `README.txt`：实验账号和输出文件说明。

## 3. 总体处理流程

```text
读取 Excel 拓扑
        ↓
识别设备厂商并加载配置文件
        ↓
加载 XRv9000/vMX 镜像接口 Profile
        ↓
按厂商规则整理文件两端的采集回显
        ↓
按厂商解析配置
        ↓
预检链路端点并跳过无法识别的链路
        ↓
展开并校验全部活动 Group 引用
        ↓
计算 NNI 保留闭包并清理非 NNI
        ↓
处理 NNI 和聚合成员压缩
        ↓
更新协议和策略中的接口引用
        ↓
执行 post_rewrite YAML 清洗和认证替换
        ↓
按目标接口上下文处理兼容性
        ↓
按镜像 Profile 适配模拟参数
        ↓
最终接口校验
        ↓
生成配置、拓扑、映射和报告
```

核心转换采用显式 Pipeline。每个 Stage 不持有下一阶段，由 Pipeline 统一保证顺序并在首次错误后停止：

```text
CleaningRulesHandler(pre_analysis)
        ↓
TopologyPreflightHandler
        ↓
GroupExpansionHandler
        ↓
PreconfiguredInterfaceCleanupHandler
        ↓
InterfaceClassificationHandler
        ↓
NonNNICleanupHandler
        ↓
NNIHandler
        ↓
ReferenceRewriteHandler
        ↓
CleaningRulesHandler(post_rewrite)
        ↓
InterfaceCompatibilityHandler
        ↓
SimulationAdaptationHandler
        ↓
FinalInterfaceValidationHandler
```

任一阶段产生错误后，责任链停止，不继续执行后续阶段。

NNI 中的聚合链路识别先由纯函数 `plan_nni_components()` 生成不可变分组计划，校验成功后再分配目标端口并修改拓扑和配置。非 NNI 清理在接口改名前计算完整保留闭包，避免源名与目标名重叠造成误删。

`TopologyPreflightHandler` 在任何配置改写前验证链路端点，只跳过端点缺失或厂商无法识别/未登记的链路，并为本端生成 `action=skip` 的 NNI 映射。Huawei 设备在输入加载阶段登记到 `external_devices`，按仅适配对端处理，不会使链路失效。后续非 NNI 清理和 NNI 映射消费有效链路及明确保留的 NNI 身份，避免误删停用链路端点。

`GroupExpansionHandler` 统一展开并校验所有活动的 group 引用及其依赖。未被活动配置引用的模板不参与求值；成功展开后删除 group 定义和对应控制语句，任一活动引用无法安全解析时整体回滚。

`InterfaceClassificationHandler` 使用厂商物理接口白名单分类接口。已知虚拟接口和未知接口均不参与 NNI 物理端口分配；未知接口原样保留并写入告警，若链接表把非物理接口作为端点则转换失败。

`interface preconfigure` 表示源设备硬件尚未插入时保存的物理接口候选配置。系统按“当前设备已经实例化并生效的配置”生成 GNS3 实验，因此 Group 展开后，`PreconfiguredInterfaceCleanupHandler` 在接口分类前通过 `active=False` 忽略整个候选块。预配置接口不会进入 NNI、接口引用改写或目标端口分配，输出中也不会出现 `interface preconfigure`。

`CleaningRulesHandler` 在两个固定点运行。`pre_analysis` 的 `cisco.remove.banner` 在任何业务分析前按分隔符停用完整 exec/login banner；`post_rewrite` 先执行两家目标镜像目录下 `cleaning.yaml` 中 `enable: true` 的规则，再原子执行管理认证替换。默认启用的 `cisco.remove.ptp.interface` 只将 `interface PTP...` 虚拟接口块标记为非活动；全局 `ptp`、普通数据口下的 `ptp` 及频率同步配置不命中该规则。同样默认启用的还有接口类型约束清理规则：删除 LLDP/CDP、以太网 CFM/OAM、非 Bundle-Ether 接口上的 `bfd`、接口 QoS 挂载以及 Junos 服务链（`service-set`、接口服务挂载、服务接口），避免这些与路由验证无关、且受接口类型限制的配置在迁移后导致导入报错。Junos 容器被删空时按 `empty_blocks.yaml` 省略输出。

两家厂商在最终渲染时分别读取目标镜像目录的 `empty_blocks.yaml`。Junos 支持 `semicolon`、`braces` 和 `omit`；IOS XR 支持 `keep`（默认保留空块原命令及格式）和 `omit`（省略整个空块及其注释）。`path` 与清洗规则使用相同的正则和 `*` / `**` 语法，但须匹配包含空块自身的完整路径，按顺序首条命中生效。IOS XR 从子到父判断空块，注释、空行和 `!` 分隔符不算有效命令；省略子块后变空的父块继续按策略处理，叶子命令不受空块策略影响。

## 4. 输入准备

### 4.1 厂商识别

设备列表中的厂商名称会被规范化：

```text
Cisco / IOS XR / 思科       → cisco_iosxr
Juniper / Junos / vMX / 瞻博 → juniper_junos
Huawei / 华为               → huawei
```

未知厂商属于输入错误。

### 4.2 配置文件定位

设备列表指定配置文件时，系统从配置目录下读取该文件。

未指定时，依次尝试：

```text
设备名.cfg
设备名.conf
设备名.txt
```

所有候选路径都会先规范化，并确认仍在用户指定的配置目录中，防止通过 `../` 读取目录外文件。

读取文件后、构造厂商语法树前，系统加载随程序发布的 `input_normalization.yaml`。规则统一使用 `match` 列表，每项为一行的正则；多个条目按顺序匹配连续多行。规则 `id` 可只写局部名称，加载时自动补上 `cisco.` 或 `juniper.` 前缀；已带本厂商前缀的 ID 原样使用。`region: leading` 只从开头向内删除，`region: trailing` 只从末尾向内删除。两端允许跳过空行寻找下一条规则，命中后继续匹配，直到没有规则命中。正文中的匹配内容原样保留；清理后只剩空白时直接返回空白文本。Juniper 默认删除命令回显、提交时间注释、尾部提示符、采集分隔行及完整的 `{ / master / }` 三行尾块；`version` 等有效配置保留。规则命中时，报告记录规则 ID 与原始行号，不包含被删除的配置文本。Cisco 默认规则为空，可按实际采集格式增加相同结构的规则。

### 4.3 NNI 初始定义

Excel 链接表提供 NNI 的权威来源。例如：

```text
R1 Gi0/0/0/0 ←→ R2 Gi0/0/0/1
```

表示两端接口都是 NNI。不属于这些端点聚合与子接口闭包的物理、聚合业务接口会被删除。

当链路连接 Cisco 与 Juniper 时，两端分别使用本端解析器和镜像 Profile 完成接口分配及引用改写，因此无需目标接口同名。该阶段只执行端口自适应，不比较两端的 IP、VLAN、MTU 或封装；业务一致性由输入配置负责。

## 5. 为什么先计算 NNI 闭包，再删除非 NNI

Excel 的物理成员可能只承载 `bundle id` 或 `802.3ad`，地址、封装和协议引用实际位于 Bundle/ae 及其子接口。必须在 Group 展开后，以原始端点为起点保留父口、成员、逻辑聚合及其子接口，随后清理集合之外的物理/聚合接口。

清理发生在 NNI 改名前，避免目标口恰好也是被删除源接口时覆盖错误。停用链路端点的 NNI 身份也保留，不能因没有目标映射被当作非 NNI。非 NNI 业务不再迁移为 QinQ，不使用最后三个预留口。

删除接口后，同步删除业务引用和完全失去接入口的容器；混合桥域保留剩余 NNI 成员和仍需要的 BVI/IRB 网关。

## 6. 公共 Group 处理原则

Group 必须在接口分类之前展开，因为接口、地址、VLAN、协议和认证配置都可能来自 Group。

两个厂商统一遵循以下原则：

1. 显式配置高于继承配置。
2. 更内层应用的 Group 高于更外层应用的 Group。
3. 同一 Group 列表中靠前的 Group 优先。
4. 使用完整配置路径和语义键判断冲突。
5. Group 展开采用事务模式。
6. 任一已应用 Group 无法安全求值时，整台设备回滚。

Group 展开失败后：

- 原配置保持不变。
- Group 定义和应用语句仍然保留。
- 不继续执行 非 NNI 清理、NNI、配置清洗和参数适配。
- 转换状态标记为失败。

## 7. Cisco IOS XR 处理流程

### 7.1 配置解析

IOS XR 配置首先按顶层非缩进命令分段，再根据块内缩进和 `!` 的深度建立完整节点树。

输入：

```text
interface GigabitEthernet0/0/0/0
 description TO-R2
 ipv4 address 10.0.0.1 255.255.255.252
!
```

内部结构：

```text
CiscoDocument.root
├── CiscoNode: interface GigabitEthernet0/0/0/0
│   ├── CiscoNode: description TO-R2
│   └── CiscoNode: ipv4 address 10.0.0.1 255.255.255.252
└── CiscoNode: !
```

顶层和嵌套配置都使用 `CiscoNode`。每个节点包含 `active` 状态，逻辑删除的节点及子树在最终渲染时被跳过。

### 7.2 接口名规范化

拓扑可能使用接口缩写，配置可能使用完整名称。系统统一处理常见前缀：

```text
Gi  → GigabitEthernet
Te  → TenGigE
Hu  → HundredGigE
Fo  → FortyGigE
BE  → Bundle-Ether
Lo  → Loopback
```

例如：

```text
Gi0/0/0/0 → GigabitEthernet0/0/0/0
```

### 7.3 Cisco Group 展开

#### 收集 Group

系统识别：

```text
group GROUP-NAME
 ...
end-group
```

此阶段只收集定义，不修改原配置。

#### 安全检查

以下情况无法静态展开：

- `apply-group` 引用未定义 Group。
- Group 包含 `$` 运行时变量。
- Group 内再次应用其他 Group。
- 配置结构无法可靠求值。

#### 临时语法树

普通配置块会临时按照缩进转换成树，用完整层级判断冲突。例如：

```text
router ospf 1
 area 0
  interface GigabitEthernet0/0/0/0
   cost 10
```

#### 优先级

从高到低：

```text
显式配置
更内层 apply-group
更外层 apply-group
同一 apply-group 列表中靠前的 Group
```

例如：

```text
apply-group FIRST SECOND
```

当 `FIRST` 和 `SECOND` 配置相同语义键时，`FIRST` 优先。

#### 正则选择器

支持引号包裹的接口正则：

```text
interface 'Gig.*'
interface 'GigabitEthernet0/0/0/.*'
```

多个选择器命中同一接口时，字面内容更长、匹配更具体的选择器优先；长度相同则按表达式词法顺序处理。

#### 语义冲突

冲突键由 `cisco/rules/xrv9000/` 下的路径感知规则生成。规则区分单值、带 key 的可重复集合、方向属性和 presence 命令；未命中语句使用完整文本保守保留。

系统不会只比较第一个单词。

以下两条不是冲突：

```text
ipv4 address 10.0.0.1 255.255.255.0
ipv4 access-group ACL-IN ingress
```

典型单值配置：

- `description`
- `mtu`
- 主 IPv4 地址
- `shutdown/no shutdown`
- `router-id`
- 指定方向的 ACL 或 service-policy

典型可重复配置：

- IPv6 地址
- BGP network
- 不同 logging host
- 不同方向的策略

#### 提交

只有全部 Group 成功展开后才会：

- 删除 Group 定义。
- 删除 `apply-group` 和 `exclude-group`。
- 写回展开后的配置。
- 记录冲突的胜出来源和值。

### 7.4 Cisco NNI 处理

#### 普通 NNI

普通 NNI 按 Excel 行号稳定排序并分配镜像物理接口。

与 Huawei 相连的普通 NNI 只解析本端：Huawei 端点不加载配置，Cisco/Juniper 端仍按本端 Profile 分配目标口并改写本端配置，Huawei 侧接口名原样保留。Huawei 侧聚合关系不可见，保留本端聚合，无法读取的对端配置明确标记未核验。

系统执行：

1. 规范化源接口名。
2. 分配下一个可用 NNI 目标接口。
3. 改名接口块及子接口。
4. 保留地址、描述、MTU、VRF 等配置。
5. 记录结构化接口映射。

#### 聚合成员识别

系统通过以下配置识别 Bundle 成员：

```text
interface GigabitEthernet0/0/0/0
 bundle id 10 mode active
```

物理成员对应逻辑接口：

```text
Bundle-Ether10
```

#### 保留聚合、映射物理成员

两端聚合关系确认后，同一对端的冗余成员压缩为最早一条链路。仅暂存、改名物理成员并重建 `bundle id`；原 Bundle 及全部子接口保持名称与业务配置。原最小活动链路数大于 1 时调整为 1，最小活动带宽门限删除并报告。原静态/LACP 模式保持，两端已知冲突时失败。

#### 跨对端聚合

当前没有可靠的 MC-LAG 还原输入与执行能力，跨不同设备的同一聚合在迁移前失败，不复制 Bundle 地址或展开逻辑引用。物理成员含地址/VRF/独立业务子接口也视为归属冲突。归属不明且有业务的孤立聚合在裁剪前报错。

### 7.5 Cisco 非 NNI 清理与接口兼容

非 NNI 物理口、Bundle 及子接口删除，同时清理路由协议、VRF、L2VPN 和桥域中的相关引用。只剩非 NNI 接入口的桥域删除；BVI 仅在保留的 NNI 桥域仍引用它时保持 BVI 类型。

NNI 改名与引用更新后，兼容阶段按最终物理父口、L3 子接口、L2 transport 子接口上下文处理已知命令限制，不能只替换 `interface` 名称。已知无法安全转换的冲突必须报错。最终校验检查迁移结果，不把规则未覆盖的命令宣称为已验证。

### 7.6 Cisco 认证与权限清洗

以下顶层配置整块删除：

```text
username
aaa
tacacs-server / tacacs
radius-server / radius
taskgroup / task-group
usergroup / user-group
snmp-server
ssh server / ssh client
telnet server
```

`line` 配置只删除认证相关子命令：

```text
password
secret
login authentication
authorization
accounting
users group
```

以下非认证终端参数继续保留：

```text
exec-timeout
session-timeout
transport
```

最后添加实验账号：

```text
username root
 secret 0 admin123
 group root-system
```

`root-system` 是 IOS XR 内置权限组，不需要重新创建 taskgroup 或 usergroup。

聚合绑定与 LACP 保留；成员缩减门限由 NNI 阶段定向适配，不进入认证处理器。

## 8. Juniper Junos 处理流程

### 8.1 配置解析

Junos 使用大括号表达层级：

```text
interfaces {
    ge-0/0/0 {
        unit 0 {
            family inet {
                address 10.0.0.1/30;
            }
        }
    }
}
```

进入原逐行解析器之前，预处理器会在引号和注释之外按 `{`、`}`、`;` 将单行块
展开成标准多行文本，之后仍由原有栈逻辑解析成树。因此多行写法和
`interfaces { ge-0/0/0 { unit 0; } }` 这类单行嵌套写法使用相同语义；字符串或
注释内部的大括号和分号不会被当成结构符号。

解析结果如下：

```text
interfaces
└── ge-0/0/0
    └── unit 0
        └── family inet
            └── address 10.0.0.1/30
```

节点包含：

- `header`：当前语句。
- `children`：大括号中的子节点。
- `active`：是否输出。
- `origin`：显式配置或来源 Group。
- `rank`：Group 继承优先级。

配置大括号不平衡时直接报错。

`inactive:` 和 `protect:` 前缀在名称、路径及 identity 比较时被去除。两者的
生效状态不同：`inactive:` 节点及其完整子树不参与求值，父节点保留时仍以停用状态
输出；`protect:` 节点仍作为有效配置参与转换，但最终输出会去掉保护
前缀，避免生产设备的编辑保护妨碍 GNS3 配置加载和后续调整。

### 8.2 Junos Groups 展开

#### 事务副本

系统首先深拷贝整棵配置树，所有 Group 操作都在副本上完成。
vMX 默认在建立 Group 依赖索引前裁剪 `re1` 定义，并从各层
`apply-groups`、`apply-groups-except` 中移除对 `re1` 的引用；列表中的其他
名称保持原有顺序。该策略仅按完整 Group 名称匹配，不影响 `re10` 等名称。
裁剪与展开一同提交，后续校验失败时会整体回滚。

#### Group 定义

系统识别：

```text
groups {
    GROUP-A { ... }
    "GROUP B" { ... }
}
```

Group 名称包含空格时使用双引号；内部索引使用去引号后的完整名称。
手写或拼接配置出现多个顶层 `groups {}` 时，系统保留第一个作为规范容器，
将后续定义按原始顺序合入；同名 Group 的子配置也会合并到同一个定义节点。

#### Group 应用

支持：

```text
apply-groups GROUP-A;
apply-groups "GROUP B";
apply-groups [ GROUP-A "GROUP B" ];
apply-groups-except GROUP-A;
```

Group 定义内可以继续应用其他 Group。系统递归求取依赖闭包，在真实配置
层级上动态展开，因此间接 Group 中的通配节点和局部
`apply-groups-except` 仍按最终接口路径生效。未应用 Group 不参与依赖求值；
一旦存在活动引用并成功完成静态展开，原 `groups` 容器整体从输出中移除。

优先级从高到低：

```text
显式配置
更内层 apply-groups
更外层 apply-groups
同一列表中靠前的 Group
```

`apply-groups-except` 会阻止指定 Group 在当前层级及其子层级继续继承。

#### 通配节点

支持常见 Junos 通配选择器：

```text
<ge-*>
<ge-0/0/*>
<*>
```

多个选择器同时命中时，更具体的选择器优先。

#### 语义冲突

冲突键由 `juniper/rules/vmx/` 下的路径感知规则生成，因此 `peer-as`、`interface-mode`、同一前缀的 `address` 等可以按实际槽位进行覆盖，不同地址则继续并存。

典型单值语句：

- `description`
- `mtu`
- `vlan-id`
- `encapsulation`
- `host-name`
- `router-id`
- `class`
- `metric`
- `preference`

`address` 等可重复语句按前缀作为 key：不同地址并存，同一地址的属性变化按继承优先级覆盖。

#### 回滚条件

以下情况回滚：

- `apply-groups` 引用未定义 Group。
- Group 直接或间接形成循环引用。
- 配置层级无法安全求值。

成功后才停用原 `groups` 块，并删除已经处理的活动 apply 语句。inactive 节点
不会触发展开或依赖校验；其父节点仍存在时会原样保留停用状态。

### 8.3 Junos NNI 处理

#### 普通 NNI

普通接口直接改名到目标 vMX 物理接口，原 unit 跟随父接口迁移。

例如：

```text
xe-0/0/3.0 → ge-0/0/0.0
```

#### 聚合成员识别

系统通过 `802.3ad` 识别 ae 成员：

```text
ge-0/0/0 {
    gigether-options {
        802.3ad ae0;
    }
}
```

#### 保留 ae 及 unit

物理成员映射为目标 ge 口，保留其 `802.3ad aeN` 绑定；ae 及所有 unit、地址、封装和引用保持。模拟适配不得整块删除 `gigether-options`、`ether-options`、`aggregated-ether-options`，只清理已确认的硬件叶子。门限适配和跨对端限制与 Cisco 相同。

### 8.4 Junos 非 NNI 清理

非 NNI 物理口、ae 及其 unit 删除，协议、routing-instance、bridge-domain 和 VLAN 内的对应引用同步清理。完全依赖被删接入口的实例或桥域删除，混合容器保留剩余 NNI 成员。IRB 仅在剩余 NNI 桥域或 VLAN 仍依赖它时按原类型保留。

不再建立共享 UNI 父口、分配运输 VLAN、写入双层 `vlan-tags`，也不再将 IRB 地址搬到物理 unit。

### 8.5 Junos 认证与权限清洗

系统在 `system` 下删除：

```text
login
root-authentication
authentication-order
radius-server
tacplus-server
radius-options
tacplus-options
accounting
```

同时删除顶层 `snmp`、`system services` 下的 SSH/Telnet、嵌套的 NETCONF-over-SSH，以及 `security ssh-known-hosts`。GNS3 的 Telnet console 连接由虚拟机串口提供，不依赖设备自身的 Telnet server。

删除整个 `system login` 会同时清除：

- 原本地用户。
- 自定义 login class。
- `user remote` 模板。
- 用户密码和 SSH key。
- allow/deny commands 权限限制。

随后重新生成：

```text
system {
    root-authentication encrypted-password "...";
}
```

- 密码 `admin123` 使用 SHA-512 crypt 哈希，固定 salt 为 `admin123`。
- root authentication 用于保证配置可以正常提交。

### 8.6 统一配置清洗规则

PTP、协议认证、PKI、硬件、NAT、流量统计和镜像扩展清洗按厂商分别定义在随程序发布的 `config_adaptor/cisco/rules/xrv9000/cleaning.yaml` 和 `config_adaptor/juniper/rules/vmx/cleaning.yaml` 中。转换时固定合并加载这两份文件，不接受外部规则路径。

```yaml
rules:
  - id: remove.ptp.interface
    vendor: cisco_iosxr
    enable: true
    category: ptp-interface
    path: []
    match: '^interface\s+PTP(?:\d|$)'
    action: delete
    reason: 'XRv9000 实验不使用 PTP 虚拟接口'
```

规则字段含义：

- `enable`：布尔值，决定该规则是否执行。
- `id`：不带厂商前缀的点号名称；加载时根据 `vendor` 自动生成 `cisco.<id>` 或 `juniper.<id>`。
- `category`：报告中的清洗分类，不参与匹配。
- `path`：目标节点的完整父级路径。`[]` 只匹配顶层，`*` 匹配恰好一级，`**` 匹配零级或多级。
- `match`：匹配目标节点本身的正则表达式。
- `action`：仅支持 `delete`、`replace`；`replace` 还必须提供 `value`。旧的 `mask`、`warn` 清洗动作不再接受。

`path` 的每个普通元素对应一级父节点。例如删除第四层的 `maximum-paths` 时，前三层全部写入路径：

```yaml
path:
  - '^router\s+bgp\s+65000$'
  - '^vrf\s+CUST-A$'
  - '^address-family\s+ipv4\s+unicast$'
match: '^maximum-paths\s+ebgp\b'
```

Cisco 使用去除缩进后的命令头匹配；Junos 的 `inactive:` 节点不参与清洗，其他节点会先去除 `protect:` 和行尾分号。`path` 必须匹配从配置根到目标父节点的完整路径，避免同名命令在其他层级被误删。

协议认证、PKI、硬件、NAT 和流量统计规则默认启用。需要保留某类配置时由项目维护者把对应规则的 `enable` 改为 `false`；同一 `category` 的定义和引用规则应同时开启或关闭。规则命中事件记录 ID、类别、动作、数量和原因，不记录被删除的秘密值。

### 8.7 模拟参数适配

镜像 Profile 中的 `simulation_adaptation` 控制 `SimulationAdaptationHandler`：

```yaml
simulation_adaptation:
  mode: stable
  ensure_data_interfaces_enabled: true
  remove_physical_interface_knobs: true
  bfd_minimum_interval_ms: 300
  bfd_minimum_multiplier: 3
```

- `off`：关闭本阶段的接口启用和 BFD 调整；独立接口兼容阶段仍清理已确认不兼容的命令。YAML 中建议写成 `"off"`，避免被解析为布尔值。
- `compatible`：保证已映射的数据父接口启用，并删除这些目标口上的 speed、duplex、FEC、协商、链路防抖等物理设备属性。
- `stable`：包含 compatible 行为，并只对配置中已经存在的 BFD interval/multiplier 执行下限钳制。

适配不会为原本未启用 BFD 的协议或接口新增 BFD，也不会默认修改 OSPF、IS-IS、BGP 的 hello/hold/SPF 等业务定时器。非映射接口不参与接口属性清理。`remove_physical_interface_knobs` 仅控制模拟阶段，不能关闭 Cisco 目标接口的强制兼容处理。处理结果按设备写入 `report.json` 的 `simulation-adaptation` 事件；重复执行不会继续产生变更。旧 `param_adjustment` YAML 节点仍兼容读取，但不能与新节点同时配置。

## 9. 全局接口引用更新

NNI 处理过程中会生成全局接口映射，例如：

```text
Bundle-Ether10     → Bundle-Ether10（preserve）
Bundle-Ether10.100 → Bundle-Ether10.100（preserve）

ae0                → ae0（preserve）
ae0.0              → ae0.0（preserve）
```

映射用于更新：

- OSPF
- IS-IS
- BGP
- MPLS
- L2VPN
- VRF
- ACL
- QoS
- Segment Routing
- Telemetry

普通物理接口映射是一对一；聚合及其子接口为自映射，不将业务引用改成成员物理口。跨对端聚合不生成克隆。

Cisco 会更新非接口配置块头和块内容中的引用。

Junos 会跳过 `interfaces` 定义本身，只递归更新其他层级中的引用，避免对已经迁移完成的接口节点再次替换。

## 10. 统一清洗规则

`CleaningRulesHandler` 同时执行 YAML 规则和实现 `ExecutableCleaningRule` 的代码规则。YAML 规则固定在 `post_rewrite`，一次批量提交给 Document，最多遍历 AST 两轮。代码规则只能选择 `pre_analysis` 或 `post_rewrite`；banner 在分析前处理，认证替换晚于 YAML 清洗：

```text
第一轮 delete  → 每个节点只执行首条匹配的删除规则，命中后跳过子树
第二轮 replace → 剩余节点只执行首条匹配的替换规则，不连续替换
```

两组规则分别保留 YAML 内的相对顺序，跨动作固定删除在前。A→B、B→C 两条替换规则作用于 A 时只得到 B。替换父节点后使用新 header 匹配后代路径；审计事件按删除组、替换组、代码规则的顺序输出。路径候选列表只在本轮调用中复用，不保存为全局或 Document 长期缓存。

可用 `PYTHONPATH=src python benchmarks/cleaning_rules.py --lines 40000 --rules 100` 对比旧逐规则遍历和新两轮遍历。基准包含编译时间，并独立统计节点访问、路径及命令正则搜索次数。

示例：

```yaml
rules:
  - id: remove.hardware.feature
    vendor: cisco_iosxr
    enable: false
    category: hardware
    path: []
    match: '^hw-module\b'
    action: delete
    reason: 'XRv9000 不支持真实硬件模块配置'
```

每次命中都会在 `report.json` 中记录规则 ID、类别、动作、命中数量和原因。两家厂商规则均随程序发布，并在创建默认流水线时固定加载。

## 11. 失败处理

以下情况必须将转换标记为失败：

- Excel 缺少必需工作表或列。
- 设备名重复。
- 链路端点信息不完整。
- 配置文件不存在或越出配置目录。
- 厂商无法识别。
- NNI 数量超过镜像可用物理接口数量。
- 同一对端聚合组内的成员关系不一致。
- 已知目标接口类型冲突或最终校验失败。
- Group 无法完整展开。
- Junos 大括号不平衡。
- 配置结构无法安全解析。

失败时仍写出：

```text
interface-mapping.json
report.json
```

但不生成设备配置和适配后的拓扑，防止用户误用不完整结果。

## 12. 报告内容

`report.json` 主要包含：

- 总体成功或失败状态。
- 全局和设备级错误。
- 告警。
- 拓扑预检和跳过链路事件。
- Group 展开事件。
- Group 冲突路径、语义键、胜出值和被覆盖值。
- 聚合成员压缩和逻辑保留事件。
- 跨对端聚合的拒绝原因和 Excel 行。
- 接口映射数量。
- 有效和跳过链路数量；Huawei 对端链路计入有效链路。
- 清洗规则的 ID、类别、动作和命中数量。
- 认证清洗分类统计。
- 模拟参数适配分类统计。
- 清洗规则命中记录。

认证报告只记录删除类别和数量，不记录原密码、密钥或其他认证秘密。

## 13. 当前边界

当前版本不处理：

- Huawei 配置转换。
- Huawei 侧接口名与配置适配；Huawei 设备只作为拓扑对端。
- IOS、IOS XE 或 NX-OS 到 IOS XR 的语法转换。
- 非 NNI 接入业务的模拟和外部连接。
- 包含运行时变量且无法静态求值的复杂 Group。
- 未通过内置逻辑或内置清洗规则明确指定的大范围硬件配置删除。

对于无法安全处理的配置，系统优先失败或保留并告警，不进行猜测式改写。

## 14. 自动测试与实机验收

自动测试验证转换、裁剪、兼容规则及最终校验行为；模拟 Console 的导入测试只验证命令交互和错误处理。当前未连接真实 XRv9000，不能据此声称输出已经通过实际镜像 load/commit。应使用实际目标镜像版本完成导入、接口状态与路由邻接检查，并将设备返回的失败上下文反馈为回归样例。模拟参数 `off` 只关闭参数调整，不关闭接口兼容和最终校验。
