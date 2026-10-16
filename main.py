"""图片功能守门插件（独立补丁）

插件由 **冬瓜** 与 **丹宝** 合作完成。

作用：把 Gitee AI Image 的全部功能，按「功能」拆成独立开关 + 独立放行名单，
      管理员始终放行，其他人按功能逐项放行。

两层硬闸（都在代码里，不靠模型自觉）：
  1) LLM 工具层：on_llm_request 里按功能把生图工具从请求里摘掉；
  2) 命令层：/画图 /改图 /自拍 /视频 等，按功能逐项拦截。
"""

import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.custom_filter import CustomFilter

try:  # 兼容不同 AstrBot 版本
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # pragma: no cover
    from astrbot.api.event import MessageChain  # type: ignore

# ── 功能清单 ─────────────────────────────────────────────
FUNCTIONS = ("draw", "edit", "selfie", "video", "batch", "task", "characters")

# 1:1 对应工具的：直接按功能摘/留
TOOL_FUNC = {
    "gitee_draw_image": "draw",
    "gitee_edit_image": "edit",
    "aiimg_batch_generate": "batch",
    "aiimg_task_status": "task",
    "aiimg_characters": "characters",
    "grok_generate_video": "video",
}

# 画图 / 改图 / 自拍 共用一个工具（靠 mode 参数区分），单独放行时无法在工具层细分，
# 故采取「三个都放行才保留该工具」的安全策略；命令层仍是逐项精确控制。
SHARED_GENERATE_TOOL = "aiimg_generate"
SHARED_FUNCS = ("draw", "edit", "selfie")

# 命令词 → 功能
CMD_FUNC = {
    # 画图
    "文生图": "draw",
    "文生图预设列表": "draw",
    "aiimg": "draw",
    "生图": "draw",
    "画图": "draw",
    "绘图": "draw",
    "出图": "draw",
    "预设列表": "draw",
    "重发图片": "draw",
    # 改图
    "aiedit": "edit",
    "图生图": "edit",
    "改图帮助": "edit",
    "改图": "edit",
    "修图": "edit",
    # 自拍
    "自拍参考": "selfie",
    "自拍": "selfie",
    # 视频
    "视频预设列表": "video",
    "视频": "video",
    # 批量
    "批量": "batch",
    # 人物 / 形象
    "换形象": "characters",
    "形象": "characters",
    "人物": "characters",
}

_WORDS = sorted(CMD_FUNC, key=len, reverse=True)
_CMD_RE = re.compile(
    r"^\s*[/!！.。．]?\s*(" + "|".join(re.escape(w) for w in _WORDS) + r")(?:\s|$)"
)


class ImageCommandFilter(CustomFilter):
    """只在「被唤醒 + 消息像图片命令」时命中，避免群里无关消息误唤醒。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        try:
            if not getattr(event, "is_at_or_wake_command", False):
                return False
        except Exception:
            return False
        return bool(_CMD_RE.match((event.get_message_str() or "").strip()))


@register(
    "astrbot_plugin_image_gate",
    "冬瓜 & 丹宝",
    "图片功能守门：Gitee AI Image 各功能独立开关 + 独立放行名单",
    "1.1.0",
    "",
)
class ImageGate(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    # ---------- 配置读取 ----------
    def _cfg(self, key: str, default=None):
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _fn_enabled(self, fn: str) -> bool:
        return bool(self._cfg(f"{fn}_enabled", True))

    def _fn_allow(self, fn: str) -> set[str]:
        raw = self._cfg(f"{fn}_allow") or []
        if not isinstance(raw, (list, tuple)):
            return set()
        return {str(x).strip() for x in raw if str(x).strip()}

    def _fn_allowed(self, event: AstrMessageEvent, fn: str) -> bool:
        """管理员始终放行（无视总开关）；非管理员看总开关 + 该功能放行名单。

        这样多个管理员时，不用往每个分项的放行名单里逐个添加。
        """
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        if not self._fn_enabled(fn):
            return False
        try:
            sid = str(event.get_sender_id() or "").strip()
        except Exception:
            sid = ""
        return bool(sid) and sid in self._fn_allow(fn)

    def _deny_text(self) -> str:
        txt = str(self._cfg("deny_message") or "").strip()
        return txt or "这个功能没你份"

    # ---------- 第 1 层：LLM 工具 ----------
    @filter.on_llm_request(priority=-1000)
    async def guard_image_tools(self, event: AstrMessageEvent, req) -> None:
        ts = getattr(req, "func_tool", None)
        if ts is None:
            return
        removed: list[str] = []

        for tool_name, fn in TOOL_FUNC.items():
            try:
                if ts.get_tool(tool_name) is None:
                    continue
                if not self._fn_allowed(event, fn):
                    ts.remove_tool(tool_name)
                    removed.append(f"{tool_name}({fn})")
            except Exception as e:
                logger.warning(f"[ImageGate] 处理工具 {tool_name} 失败: {e}")

        try:
            if ts.get_tool(SHARED_GENERATE_TOOL) is not None and not all(
                self._fn_allowed(event, f) for f in SHARED_FUNCS
            ):
                ts.remove_tool(SHARED_GENERATE_TOOL)
                removed.append(f"{SHARED_GENERATE_TOOL}(draw/edit/selfie)")
        except Exception as e:
            logger.warning(f"[ImageGate] 处理共用工具失败: {e}")

        if removed:
            logger.info(
                f"[ImageGate] 用户 {event.get_sender_id()} 被摘除工具: {removed}"
            )

    # ---------- 第 2 层：命令 ----------
    @filter.custom_filter(ImageCommandFilter, priority=1000)
    async def block_image_commands(self, event: AstrMessageEvent):
        msg = (event.get_message_str() or "").strip()
        m = _CMD_RE.match(msg)
        if not m:
            return
        fn = CMD_FUNC.get(m.group(1))
        if not fn:
            return
        if self._fn_allowed(event, fn):
            return
        logger.info(
            f"[ImageGate] 拦截 {event.get_sender_id()} 的「{fn}」命令: {msg[:30]}"
        )
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain([Comp.Plain(self._deny_text())]),
            )
        except Exception as e:
            logger.warning(f"[ImageGate] 无权提示发送失败: {e}")
        event.stop_event()
