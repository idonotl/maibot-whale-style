"""小鲸鱼表达学习插件。

学习闭环（与设计讨论一致）：

    素材（B站 转写 + 画面描述 + 弹幕）
        → 提炼「什么场合怎么接」的结构
        → 存进 rules 表（带场景、置信度、权重）
        → 回复前按场景取 1-2 条注入参考
        → 只学结构，不学事实、称呼与关系

素材获取交给 B站 插件（它的缓存里有转写和画面描述），本插件只负责提炼与注入。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from maibot_sdk import API, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from . import learner, style_prompt
from .config import StyleConfig
from .store import StyleStore

AFFECT_PLUGIN = "local.whale-affect"

PLAYFUL_SCENES = frozenset({"接梗", "自嘲", "吐槽"})
LEVEL_PLAYFUL = {
    "stranger": False,
    "acquaintance": False,
    "familiar": "partial",
    "close": True,
    "intimate": True,
}
LEVEL_FALLBACK_RATE = {
    "stranger": 0.1,
    "acquaintance": 0.2,
    "familiar": 0.35,
    "close": 0.5,
    "intimate": 0.6,
}

WHALE_KEYWORDS = ("鲸鱼", "鲸娘", "大肥鱼", "小鲸鱼", "蓝毛", "鱼娘", "鲸鲸", "肥鱼")

# 自动学习频率：对方发来的可以多学，自己刷到的少学
AUTO_LEARN_LIMITS = {
    "user": {"daily": 12, "cooldown_minutes": 3},
    "browse": {"daily": 3, "cooldown_minutes": 45},
}

BILI_PLUGIN = "local.bili-topics"
MATERIAL_API = "learning_material"


class StylePlugin(MaiBotPlugin):
    """表达学习插件主体。"""

    config_model = StyleConfig

    def __init__(self) -> None:
        """初始化插件，不做任何 IO。"""
        super().__init__()
        self.store: StyleStore | None = None
        self._last_learn = 0.0
        self._last_text: Dict[str, str] = {}
        self._daily: Dict[str, int] = {}
        self._rules_cache: List[dict] = []
        self._rules_loaded_at = 0.0
        self._level_cache: Dict[str, tuple] = {}
        self._auto_daily: Dict[str, int] = {}
        self._auto_last: Dict[str, float] = {}

    # ──── 生命周期 ────────────────────────────────────────

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            return
        data_dir = Path(str(self.ctx.paths.data_dir))
        self.store = StyleStore(data_dir / "style.sqlite3")
        await asyncio.to_thread(self.store.start)
        self._get_logger().info("whale-style ready: %s", self.store.path)

    async def on_unload(self) -> None:
        self.store = None
        self._rules_cache=[]
        self._last_text.clear()
        self._level_cache.clear()

    async def on_config_update(self, scope, config_data, version) -> None:
        if scope in ('','self'):
            await self.on_unload()
            await self.on_load()


    @HookHandler(
        "maisaka.planner.before_request",
        name="remember_last_message",
        description="缓存本会话最近一条用户消息文本，供回复前做场景识别；只读不改。",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=200,
        error_policy=ErrorPolicy.SKIP,
    )
    async def remember_last_message(self, **kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "").strip()
        items = kwargs.get("items") or []
        if not session_id or not items:
            return
        text = self._user_text_from_items(items)
        if text:
            self._last_text[session_id] = text[-300:]

    @staticmethod
    def _user_text_from_items(items: Any) -> str:
        """从 planner items 里取最近一条非本机消息的文本。"""
        for item in reversed(list(items)):
            if not isinstance(item, dict):
                continue
            chunks: List[str] = []
            for part in item.get("parts") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    chunks.append(str(part.get("text") or ""))
            text = "\n".join(chunk for chunk in chunks if chunk).strip()
            if not text or 'is_self_message="true"' in text:
                continue
            match = re.search(r"<message[^>]*>(.*?)</message>", text, re.S)
            if not match:
                continue
            return match.group(1).strip()
        return ""


    async def _affinity_level(self, session_id: str, reply_message_id: str) -> str:
        """向心情插件要好感档位；失败时按“熟人”保守处理。"""
        key = "%s|%s" % (session_id, reply_message_id)
        cached = self._level_cache.get(key)
        now = time.time()
        if cached and now - cached[1] < 300:
            return cached[0]
        level = "acquaintance"
        try:
            result = await asyncio.wait_for(self.ctx.api.call(
                f"{AFFECT_PLUGIN}.affinity_hint",
                version="1",
                session_id=session_id,
                reply_message_id=reply_message_id,
            ),timeout=0.5)
            if isinstance(result, dict) and result.get("ok"):
                level = str(result.get("level") or "acquaintance")
        except Exception as exc:
            self._get_logger().warning("whale-style: 取好感档位失败 %s: %s", type(exc).__name__, str(exc)[:120])
        self._level_cache[key] = (level, now)
        if len(self._level_cache) > 200:
            self._level_cache = dict(list(self._level_cache.items())[-100:])
        return level

    # ──── 提示注入 ────────────────────────────────────────

    @HookHandler(
        "maisaka.replyer.before_request",
        name="inject_style_reference",
        description="按当前消息场景取 1-2 条已学表达，作为参考追加到回复提示。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_style_reference(self, **kwargs: Any) -> Dict[str, Any]:
        cfg = self.config.plugin
        cleaned = style_prompt.strip_block(str(kwargs.get('extra_prompt') or ''))
        clean_result = {'action':'continue','modified_kwargs':{**kwargs,'extra_prompt':cleaned}}
        if not cfg.enabled or not cfg.inject_enabled or not self.store or cfg.max_rules_per_reply <= 0:
            return clean_result
        text = await self._current_text(kwargs)
        scenes = style_prompt.detect(text) if text else []
        session_id = str(kwargs.get("session_id") or "")
        reply_id = str(kwargs.get("reply_message_id") or "")
        level = await self._affinity_level(session_id, reply_id)
        playful = LEVEL_PLAYFUL.get(level, False)
        max_rules = cfg.max_rules_per_reply
        cached_rules = self._rules_cache or await asyncio.to_thread(self.store.top_rules, 40)
        allowed = [item for item in cached_rules if learner.safe_rule(item)
                   and (playful is True or item["scene"] not in PLAYFUL_SCENES
                   or (playful == "partial" and item["scene"] != "接梗"))]
        rules: List[dict] = []
        for scene in scenes:
            rules.extend(item for item in allowed
                         if item["scene"] == scene and item["confidence"] >= cfg.min_confidence)
            if len(rules) >= max_rules:
                break
        # A sad message must never inherit a random playful rule.
        if '安慰' in scenes:
            rules = [item for item in rules if item['scene']=='安慰']
        rules = [item for item in rules if item['confidence'] >= cfg.min_confidence]
        rules = rules[:max_rules]
        self._get_logger().debug("whale-style: 好感档=%s 场景=%s 注入=%d 条",
                                 level, scenes, len(rules))
        block = style_prompt.build_block(rules)
        if not block:
            return clean_result
        updated = dict(kwargs)
        updated["extra_prompt"] = style_prompt.append_block(str(kwargs.get("extra_prompt") or ""), block)
        await asyncio.to_thread(self.store.mark_used, [int(item["id"]) for item in rules])
        return {"action": "continue", "modified_kwargs": updated}

    async def _current_text(self, kwargs: Dict[str, Any]) -> str:
        """取本轮要回复的消息文本：优先用宿主能力按 reply_message_id 查。"""
        session = str(kwargs.get('session_id') or '')
        target = str(kwargs.get('reply_message_id') or '')
        if session and target:
            try:
                raw = await asyncio.wait_for(self.ctx.message.get_by_id(target, chat_id=session),timeout=0.4)
                return self._text_of(raw)
            except Exception:
                return ''
        for key in ("message_text", "text", "user_text"):
            value = kwargs.get(key)
            if isinstance(value, str) and value.strip():
                return value
        session_id = str(kwargs.get("session_id") or "").strip()
        # No trustworthy target: do not infer a scene from tool/prompt/other-user text.
        return ""

    @staticmethod
    def _text_of(raw: Any) -> str:
        """从宿主消息对象里尽力取出纯文本。"""
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        for name in ("processed_plain_text", "plain_text", "text", "content"):
            value = getattr(raw, name, None)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(raw, dict):
            for name in ("processed_plain_text", "plain_text", "text", "content"):
                value = raw.get(name)
                if isinstance(value, str) and value.strip():
                    return value
        return ""


    @staticmethod
    def _whale_related(material: Dict[str, Any]) -> bool:
        """看素材里有没有鲸鱼娘相关线索。"""
        blob = " ".join(str(material.get(key) or "") for key in ("title", "description", "transcript", "scenes"))
        return any(word in blob for word in WHALE_KEYWORDS)

    def _auto_allowed(self, source: str) -> bool:
        """检查自动学习的频率额度。"""
        limits = AUTO_LEARN_LIMITS.get(source, AUTO_LEARN_LIMITS["browse"])
        day = time.strftime("%Y-%m-%d")
        used = self._auto_daily.get(day, 0)
        if used >= limits["daily"]:
            return False
        last = self._auto_last.get(source, 0.0)
        return time.time() - last >= limits["cooldown_minutes"] * 60

    def _auto_mark(self, source: str) -> None:
        day = time.strftime("%Y-%m-%d")
        self._auto_daily[day] = self._auto_daily.get(day, 0) + 1
        self._auto_last[source] = time.time()

    async def _learn_from_material(self, bvid: str, source: str, material: Dict[str, Any],
                                   weight: float) -> Dict[str, Any]:
        """提炼并入库，返回结果摘要。"""
        text = await self._extract(material)
        result = learner.parse(text)
        if not result["rules"] and not result["samples"]:
            return {"ok": False, "reason": "nothing"}
        await asyncio.to_thread(self._persist, bvid, source, weight, result)
        self._rules_cache = await asyncio.to_thread(self.store.top_rules, 40)
        self._rules_loaded_at = time.time()
        return {"ok": True, "rules": len(result["rules"]), "samples": len(result["samples"]),
                "items": [{"scene": r["scene"], "behavior": r["behavior"]} for r in result["rules"][:4]]}

    @API(
        "auto_learn",
        description="B站插件在深看完成后调用：判断素材是否与大肥鱼/鲸鱼娘相关，相关则按频率自动学习",
        version="1",
        public=True,
    )
    async def api_auto_learn(self, bvid: str = "", source: str = "browse") -> Dict[str, Any]:
        """自动学习入口。"""
        cfg = self.config.plugin
        if not cfg.enabled or not self.store:
            return {"ok": False, "reason": "disabled"}
        origin = "user" if source == "user" else "browse"
        bvid = (bvid or "").strip()
        if not bvid:
            return {"ok": False, "reason": "no_bvid"}
        material = await self._material(bvid)
        if not material:
            return {"ok": False, "reason": "no_material"}
        if not self._whale_related(material):
            return {"ok": False, "reason": "not_related"}
        limits=AUTO_LEARN_LIMITS[origin]
        if not await asyncio.to_thread(self.store.reserve_auto,origin,limits['daily'],limits['cooldown_minutes']):
            return {"ok": False, "reason": "rate_limited"}
        weight = cfg.user_weight if origin == "user" else cfg.browse_weight
        outcome = await self._learn_from_material(bvid, origin, material, weight)
        if outcome.get("ok"):
            self._get_logger().info("whale-style: 自动学习 %s（%s）%d 条规则",
                                    bvid, origin, outcome["rules"])
        return outcome

    # ──── 工具 ────────────────────────────────────────────

    @Tool(
        "style_learn",
        description=(
            "从一条 B站 视频里学表达方式（不是学内容）：提炼“什么场合可以怎么接”，"
            "存进表达库，之后聊天时按场景小比例参考。需要视频已经有转写或画面描述。"
            "对方发来视频并说“学一下/看看人家怎么聊/这说话方式不错”时用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url_or_bvid",
                param_type=ToolParamType.STRING,
                description="视频链接或 BV 号。",
                required=True,
            ),
            ToolParameterInfo(
                name="source",
                param_type=ToolParamType.STRING,
                description="user=对方发来的（权重大）；browse=自己刷到的（权重小）。默认 user。",
                required=False,
            ),
        ],
    )
    async def style_learn(self, url_or_bvid: str, source: str = "user", **kwargs: Any) -> str:
        cfg = self.config.plugin
        if not cfg.enabled or not self.store:
            return "表达学习现在是关的。"
        bvid = self._pick_bvid(url_or_bvid)
        if not bvid:
            return "没能识别出 BV 号。"
        wait = self._cooldown_left(cfg.learn_cooldown_minutes)
        if wait > 0:
            return "刚学过一条啦，过 %d 分钟再学吧。" % max(1, int(wait // 60))
        day = time.strftime("%Y-%m-%d")
        if self._daily.get(day, 0) >= cfg.learn_daily_max:
            return "今天学的够多了，明天再学新的吧。"
        material = await self._material(bvid)
        if not material:
            return ("这条还没有素材。先让我听一遍（bili_transcript）或者看一眼画面（bili_frames），"
                    "拿到台词或画面描述之后我再学。")
        weight = cfg.user_weight if source != "browse" else cfg.browse_weight
        text = await self._extract(material)
        result = learner.parse(text)
        if not result["rules"] and not result["samples"]:
            return "这条里没提炼出可复用的表达，先不记了。"
        await asyncio.to_thread(self._persist, bvid, source, weight, result)
        self._last_learn = time.time()
        self._rules_cache = await asyncio.to_thread(self.store.top_rules,40)
        self._daily[day] = self._daily.get(day, 0) + 1
        return ("学完了：《%s》\n提炼出 %d 条表达、%d 条素材，权重 %.1f。\n%s"
                % (material.get("title", "")[:30], len(result["rules"]), len(result["samples"]), weight,
                   "\n".join("- %s时：%s" % (r["scene"], r["behavior"]) for r in result["rules"][:4])))

    @Tool(
        "style_rules",
        description="看看现在学过哪些表达方式（按置信度排序）。对方问“你学到了什么”时用。",
        parameters=[
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="看几条，默认 10。",
                required=False,
            ),
        ],
    )
    async def style_rules(self, limit: int = 10, **kwargs: Any) -> str:
        if not self.store:
            return "表达库还没准备好。"
        rows = await asyncio.to_thread(self.store.top_rules, max(1, min(int(limit or 10), 30)))
        if not rows:
            return "还没学过东西呢。"
        lines = ["从别人聊天里学到的表达（参考用，不是台词）："]
        for row in rows:
            lines.append("- %s时：%s（置信度 %.2f，用过 %d 次）" % (
                row["scene"], row["behavior"], row["confidence"], row["uses"]))
        return "\n".join(lines)

    @API("style_stats", version="1", public=True)
    async def style_stats(self) -> Dict[str, Any]:
        """给 WebUI / 其它插件看的统计。"""
        if not self.store:
            return {"ok": False}
        stats = await asyncio.to_thread(self.store.stats)
        stats["ok"] = True
        return stats

    # ──── 内部 ────────────────────────────────────────────

    @staticmethod
    def _pick_bvid(text: str) -> str:
        import re
        match = re.search(r"BV[0-9A-Za-z]{10}", text or "")
        return match.group(0) if match else ""

    def _cooldown_left(self, minutes: int) -> float:
        if minutes <= 0:
            return 0.0
        return max(0.0, self._last_learn + minutes * 60 - time.time())

    async def _material(self, bvid: str) -> Dict[str, Any] | None:
        """向 B站 插件要素材（转写 + 画面描述 + 弹幕）。"""
        try:
            result = await self.ctx.api.call(
                f"{BILI_PLUGIN}.{MATERIAL_API}", version="1", bvid=bvid
            )
        except Exception:
            self._get_logger().warning("whale-style: 取素材失败 %s", bvid)
            return None
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        return result.get("material") or None

    async def _extract(self, material: Dict[str, Any]) -> str:
        prompt = learner.build_prompt(material)
        response = await self.ctx.llm.generate(
            prompt,
            task_name="learner",
            temperature=0.3,
            max_tokens=1500,
            rpc_timeout_ms=380_000,
        )
        if isinstance(response, dict):
            return str(response.get("response") or "")
        return str(response or "")

    def _persist(self, bvid: str, source: str, weight: float, result: Dict[str, Any]) -> None:
        if not self.store:
            return
        for sample in result["samples"]:
            self.store.add_sample(bvid, source, sample["scene"], sample["excerpt"], weight)
        for rule in result["rules"]:
            confidence = max(0.1, min(rule["confidence"] * weight, 0.9))
            self.store.upsert_rule(
                rule["scene"], rule["behavior"], rule["example"], confidence, weight, bvid, source
            )


def create_plugin() -> StylePlugin:
    """插件工厂，供 Runner 加载。"""
    return StylePlugin()
