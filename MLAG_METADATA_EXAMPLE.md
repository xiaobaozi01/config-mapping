# M-LAG 元数据改进示例

## 问题场景

假设有一个Cisco IOS XR设备R1，配置如下：

```cisco
interface Bundle-Ether10
 description "连接到对端B和C的聚合"
 ipv4 address 10.0.0.1 255.255.255.0
!
interface GigabitEthernet0/0/0/0
 bundle id 10 mode active
!
interface GigabitEthernet0/0/0/1
 bundle id 10 mode active
!
```

拓扑中的链接表：

| A端设备 | A端接口 | Z端设备 | Z端接口 |
|--------|---------|--------|---------|
| R1 | Gi0/0/0/0 | B | ge-0/0/0 |
| R1 | Gi0/0/0/1 | C | ge-0/0/0 |

这两条物理链路都属于同一个 `Bundle-Ether10`，但连接到**不同的对端**（B 和 C）。这就是 **M-LAG**（Multi-peer Link Aggregation）。

---

## 改进前

`interface-mapping.json` 的输出：

```json
[
  {
    "device": "R1",
    "source_interface": "Bundle-Ether10",
    "role": "NNI",
    "action": "clone-flatten",
    "target_interface": "GigabitEthernet0/0/0/0",
    "link_rows": [2],
    "reason": "M-LAG 按对端拆分"
  },
  {
    "device": "R1",
    "source_interface": "Bundle-Ether10",
    "role": "NNI",
    "action": "clone-flatten",
    "target_interface": "GigabitEthernet0/0/0/1",
    "link_rows": [3],
    "reason": "M-LAG 按对端拆分"
  }
]
```

**用户的困惑**：
- ✗ 看到 `Bundle-Ether10` 被映射了两次
- ✗ 虽然 `reason` 说是"M-LAG按对端拆分"，但不知道具体的关系
- ✗ 不知道这两个映射是关联的还是独立的
- ✗ 如果手动修改GigabitEthernet0/0/0/0的配置，不知道是否也需要修改GigabitEthernet0/0/0/1

---

## 改进后

新的 `interface-mapping.json` 输出：

```json
[
  {
    "device": "R1",
    "source_interface": "Bundle-Ether10",
    "role": "NNI",
    "action": "clone-flatten",
    "target_interface": "GigabitEthernet0/0/0/0",
    "link_rows": [2],
    "reason": "M-LAG 按对端拆分",
    "is_mlag_clone": true,
    "mlag_group_id": "Bundle-Ether10:mlag-GigabitEthernet0/0/0/0-GigabitEthernet0/0/0/1",
    "mlag_peers": [
      "GigabitEthernet0/0/0/0",
      "GigabitEthernet0/0/0/1"
    ]
  },
  {
    "device": "R1",
    "source_interface": "Bundle-Ether10",
    "role": "NNI",
    "action": "clone-flatten",
    "target_interface": "GigabitEthernet0/0/0/1",
    "link_rows": [3],
    "reason": "M-LAG 按对端拆分",
    "is_mlag_clone": true,
    "mlag_group_id": "Bundle-Ether10:mlag-GigabitEthernet0/0/0/0-GigabitEthernet0/0/0/1",
    "mlag_peers": [
      "GigabitEthernet0/0/0/0",
      "GigabitEthernet0/0/0/1"
    ]
  }
]
```

**用户现在能看到**：

✓ **是M-LAG克隆**：`"is_mlag_clone": true`  
✓ **克隆组ID一致**：两条映射的 `mlag_group_id` 相同  
✓ **完整的对端列表**：`mlag_peers` 包含所有目标接口  
✓ **一目了然的关系**：这两个映射本来是同一个源，因为对端不同而拆分

---

## 使用场景

### 场景1：人工审查mapping

```
网络工程师在GNS3中导入配置后，看着mapping.json：
"Gi0/0/0/0 和 Gi0/0/0/1 的mlag_group_id都是 'Bundle-Ether10:mlag-...' "
→ 立即明白：这两个口的配置必须一致，都是来自同一个Bundle
```

### 场景2：自动化工具集成

```python
# 某个自动化脚本可以读取mapping，识别M-LAG关系
for mapping in mappings:
    if mapping.is_mlag_clone:
        print(f"M-LAG组 {mapping.mlag_group_id}:")
        print(f"  源接口: {mapping.source_interface}")
        print(f"  所有目标: {mapping.mlag_peers}")
        print(f"  当前目标: {mapping.target_interface}")
        # → 可以提示用户"这些接口配置必须同步"
```

### 场景3：审计和故障排查

```
如果用户手动修改了其中一个目标接口的配置，审计工具可以：
1. 读取mapping中的 mlag_group_id
2. 找到同组的其他接口
3. 检查它们的配置是否一致
4. 提醒用户"你改了Gi0/0/0/0，也要改Gi0/0/0/1"
```

---

## 技术细节

### 新增字段说明

| 字段 | 含义 | 示例 |
|------|------|------|
| `is_mlag_clone` | 是否为M-LAG克隆 | `true` / `false` |
| `mlag_group_id` | M-LAG组的唯一标识 | `"Bundle-Ether10:mlag-Gi0/0/0/0-Gi0/0/0/1"` |
| `mlag_peers` | 同组内的所有目标接口 | `["Gi0/0/0/0", "Gi0/0/0/1"]` |

### 什么时候有M-LAG

- ✓ 同一个聚合（Bundle-Ether 或 ae）的成员链接到**不同对端**时
- ✗ 普通NNI接口（不是聚合的一部分）
- ✗ 同一对端上的聚合（这只是普通的Bundle扁平化）

### 普通聚合（非M-LAG）的例子

```json
{
  "source_interface": "Bundle-Ether10",
  "target_interface": "GigabitEthernet0/0/0/0",
  "is_mlag_clone": false,
  "mlag_group_id": null,
  "mlag_peers": []
}
```

---

## 总结

这个改进**不改变转换逻辑**，只是让 `interface-mapping.json` 的输出**更清晰**：
- 用户能一眼看出M-LAG关系
- 自动化工具能准确识别克隆关系
- 审计和维护变得更容易
