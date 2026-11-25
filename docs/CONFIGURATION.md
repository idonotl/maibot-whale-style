# 配置字段参考

由本次发布的 config.py 提取；最终校验以代码与宿主配置 UI 为准。不要把实际配置提交到仓库。

| 配置类.字段 | 类型 | 默认值/工厂 | 说明 |
|---|---|---|---|
| StyleSettings.config_version | `str` | `'0.1.0'` | 配置版本 |
| StyleSettings.enabled | `bool` | `False` | 是否启用表达学习 |
| StyleSettings.inject_enabled | `bool` | `False` | 是否把学到的表达注入回复提示 |
| StyleSettings.max_rules_per_reply | `int` | `2` | 每次回复最多参考几条 |
| StyleSettings.min_confidence | `float` | `0.35` | 低于这个置信度不注入 |
| StyleSettings.user_weight | `float` | `1.0` | 对方发来的视频权重 |
| StyleSettings.browse_weight | `float` | `0.4` | 自己刷到的视频权重 |
| StyleSettings.learn_cooldown_minutes | `int` | `10` | 两次学习最小间隔 |
| StyleSettings.learn_daily_max | `int` | `6` | 每天最多学习几次 |
| StyleConfig.plugin | `StyleSettings` | `StyleSettings` |  |
