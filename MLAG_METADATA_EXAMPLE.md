# 跨对端聚合的当前边界

同一 Bundle/ae 的成员连接不同设备，不能仅凭拓扑判定这些设备属于同一个可运行的多机聚合系统。当前缺少可靠 MC-LAG 还原支持，转换明确失败，不再生成 clone-flatten 或伪造克隆元数据。

`InterfaceMapping` 中保留旧字段仅用于兼容已有报告读取器，新转换不会将 `is_mlag_clone` 设为 true。普通同对端聚合只缩减物理成员，Bundle/ae 及子接口使用 preserve 自映射，成员使用 map-member 记录目标物理口。
