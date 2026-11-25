"""插件配置：根配置持有 plugin 分区。"""

from maibot_sdk import Field, PluginConfigBase


class StyleSettings(PluginConfigBase):
    """表达学习的开关与节奏。"""

    __ui_label__ = "表达学习"
    __ui_icon__ = "book"
    __ui_order__ = 0

    config_version: str = Field(default="0.1.0", description="配置版本")
    enabled: bool = Field(default=False, description="是否启用表达学习")
    inject_enabled: bool = Field(default=False, description="是否把学到的表达注入回复提示")
    max_rules_per_reply: int = Field(default=2, ge=0, le=4, description="每次回复最多参考几条")
    min_confidence: float = Field(default=0.35, ge=0.0, le=1.0, description="低于这个置信度不注入")
    user_weight: float = Field(default=1.0, ge=0.1, le=1.0, description="对方发来的视频权重")
    browse_weight: float = Field(default=0.4, ge=0.1, le=1.0, description="自己刷到的视频权重")
    learn_cooldown_minutes: int = Field(default=10, ge=0, le=1440, description="两次学习最小间隔")
    learn_daily_max: int = Field(default=6, ge=0, le=50, description="每天最多学习几次")


class StyleConfig(PluginConfigBase):
    """配置根节点。"""

    plugin: StyleSettings = Field(default_factory=StyleSettings)
