"""
astrbot_plugin_llm_guard —— LLM 内容安全拦截（content_filter）兜底插件

背景
----
部分 LLM 服务商（如 Moonshot / Kimi）会对高风险 prompt 直接返回 HTTP 400：

    {"error": {"type": "content_filter",
               "message": "The request was rejected because it was considered high risk"}}

AstrBot 核心在 LLM 请求失败时，会把**原始错误文案当成机器人回复发给用户**
（见 astrbot/core/agent/runners/tool_loop_agent_runner.py 与
astrbot/core/astr_agent_run_util.py 的 err 分支）。于是用户会收到一句英文报错，
而且这一轮**完全没有正常回复**。上下文压缩（compaction）请求被拦截时同理。

本插件做两件事
--------------
1. 兜底拦截（on_decorating_result）
   在「发送消息前」阶段检查即将发出的文本。命中拦截特征时按配置：
     - replace：换成 retry 结果或 fallback_text
     - drop   ：整条丢弃，什么都不发
     - log    ：只记录，不改动（用于纯诊断）
   该钩子在 pipeline 的 result_decorate 阶段执行，能覆盖核心那几条把错误
   写进 EventResult 的路径。

2. 换 provider 重发（可选，retry_provider_id）
   检测到拦截后，用另一个 provider 重新生成一次正常回复再发出。
   这样即使核心没配 fallback_provider_ids，机器人也能正常回话。

3. 自诊断（dump_on_reject）
   被拦截时把**当次请求的完整 prompt**转存到本地文件，便于定位到底是
   哪段内容触发了服务商风控（人设？历史？注入内容？），补上「无法复现」这一环。

要求：AstrBot 4.x（>= 4.16.0）。纯文本处理，无第三方依赖。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star

# ---------------------------------------------------------------------------
# 拦截特征
# ---------------------------------------------------------------------------

# 一级特征：LLM 服务商错误被原样透传到用户面前时的强特征。
# 这些串几乎不可能出现在模型的正常回复里，命中即判定为「错误文案泄漏」。
# 区分原则：snake_case 机器标识符（content_filter）正常散文里不可能出现 → 强特征；
# 散文措辞（content policy / high risk）可能是模型在正常讨论 → 软特征。
HARD_MARKERS: tuple[str, ...] = (
    "the request was rejected because it was considered high risk",
    "was rejected because it was considered high risk",
    # AstrBot 核心自己拼的错误文案（tool_loop_agent_runner / astr_agent_run_util /
    # agent_sub_stages/internal 三处 err 分支）
    "all chat models failed",
    "all available chat models are unavailable",
    "no messages remain for the llm request",
    "error occurred during ai execution",
    "error occurred while processing agent request",
    "llm 响应错误",
    "llm 请求失败",
    # 第三方 Agent runner 的 err 文案
    "阿里云百炼请求失败",
    "coze 请求失败",
    "dify 请求失败",
    # 服务商机器标识符
    "content_filter",
    "请求被拒绝",
)

# 二级特征：通用的「内容安全拦截」措辞。单独出现时可能是模型正常在讨论
# 「高风险」这类话题，所以只在整条消息很短、且不像一段成文回复时才判定。
SOFT_MARKERS_EN: tuple[str, ...] = (
    "high risk",
    "rejected because",
    "content policy",
    "risk control",
    "safety check",
    "safe check",
)
# 注意：这里**刻意不包含**「高风险」「被拒绝」「违规」「敏感内容」这类裸话题词。
# 它们会出现在模型的正常回复里（比如「高风险投资要谨慎」），加进来必然误伤。
# 只保留「拒绝式」措辞——正常聊天几乎不会说「无法处理该请求」。
# 若你的服务商返回的是别的措辞，用 extra_markers 自己补，别改内置表。
SOFT_MARKERS_CN: tuple[str, ...] = (
    "内容安全",
    "不予处理",
    "拒绝回答",
    "无法满足该请求",
    "无法处理该请求",
    "请求被拒绝",
    "请求已被拒绝",
    "涉及敏感",
    "包含敏感",
    "不符合内容规范",
)

DEFAULT_FALLBACK_TEXT = "（刚才那条没能生成出来，再发一次试试？）"


def _as_bool(value: object, default: bool) -> bool:
    """容错解析布尔配置（WebUI 可能存成字符串）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    if value is None:
        return default
    return bool(value)


def _as_int(value: object, default: int) -> int:
    """容错解析整数配置。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_markers(value: object) -> tuple[str, ...]:
    """把用户填写的自定义特征解析成小写元组（支持逗号 / 换行 / 分号分隔）。"""
    if not value:
        return ()
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"[,;，；\n\r]+", str(value))
    return tuple(item.strip().lower() for item in raw if item.strip())


class LLMGuardPlugin(Star):
    """LLM 内容安全拦截兜底：挡住错误文案，并用备用 provider 重发。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}

        # 配置项一律在「用的时候」现读（见下方 property），不在这里缓存 ——
        # 这样在 WebUI 改完设置后即便插件没重载，新值也能立即生效。
        # 最近一次 LLM 请求的快照（用于诊断与「换 provider 重发」）
        self._last_request: dict = {}
        # 拦截记录（最近 N 条，供 /llmguard 查看）
        self._recent: deque[dict] = deque(maxlen=20)
        self._blocked_total = 0
        self._retried_ok = 0

    def _cfg(self, key: str, default: object) -> object:
        return self.config.get(key, default)

    # ------------------------------------------------------------------ #
    # 配置项（每次现读，保证 WebUI 改动即时生效）
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        """总开关。"""
        return _as_bool(self._cfg("enabled", True), True)

    @property
    def mode(self) -> str:
        """命中拦截特征后的处理方式：replace / drop / log。"""
        value = str(self._cfg("mode", "replace") or "replace").strip().lower()
        return value if value in ("replace", "drop", "log") else "replace"

    @property
    def fallback_text(self) -> str:
        """重发失败时发送的兜底文案。"""
        return str(self._cfg("fallback_text", DEFAULT_FALLBACK_TEXT) or "")

    @property
    def retry_provider_id(self) -> str:
        """被拦截时用于重新生成回复的备用 provider ID。"""
        return str(self._cfg("retry_provider_id", "") or "").strip()

    @property
    def retry_timeout(self) -> int:
        """备用 provider 重发的超时时间（秒）。"""
        return _as_int(self._cfg("retry_timeout", 60), 60)

    @property
    def soft_scan(self) -> bool:
        """是否启用二级（泛化措辞）特征扫描。"""
        return _as_bool(self._cfg("soft_scan", False), False)

    @property
    def soft_max_len(self) -> int:
        """二级特征扫描的最大文本长度。"""
        return _as_int(self._cfg("soft_max_len", 300), 300)

    @property
    def extra_markers(self) -> tuple[str, ...]:
        """用户自定义的拦截特征。"""
        return _as_markers(self._cfg("extra_markers", ""))

    @property
    def patch_custom_error_reply(self) -> bool:
        """是否把兜底文案挂到 persona 自定义错误消息上。"""
        return _as_bool(self._cfg("patch_custom_error_reply", True), True)

    @property
    def dump_on_reject(self) -> bool:
        """是否在拦截时转存当次请求。"""
        return _as_bool(self._cfg("dump_on_reject", False), False)

    @property
    def dump_dir(self) -> str:
        """转存目录。"""
        return str(self._cfg("dump_dir", "") or "").strip()

    @property
    def log_prompt_preview(self) -> int:
        """拦截时在日志里打印 prompt 的前 N 个字符。"""
        return _as_int(self._cfg("log_prompt_preview", 0), 0)

    # ------------------------------------------------------------------ #
    # 拦截判定
    # ------------------------------------------------------------------ #
    def _match_reason(self, text: str) -> str | None:
        """判定一段文本是否为「被泄漏的 LLM 错误文案」。

        Args:
            text: 待检查的文本。

        Returns:
            命中时返回可读的原因字符串，未命中返回 None。
        """
        if not text:
            return None
        stripped = text.strip()
        if not stripped:
            return None
        low = stripped.lower()

        for marker in HARD_MARKERS:
            if marker in low:
                return f"hard:{marker}"

        # 用户自定义特征：显式配置即视为强特征，不受长度限制
        for marker in self.extra_markers:
            if marker and marker in low:
                return f"extra:{marker}"

        if not self.soft_scan:
            return None

        # 二级特征：只对「短文本、且不像成段回复」生效，避免误伤正常聊天。
        if len(stripped) > self.soft_max_len:
            return None
        if stripped.count("\n") > 3:
            return None
        for marker in SOFT_MARKERS_EN:
            if marker in low:
                return f"soft:{marker}"
        for marker in SOFT_MARKERS_CN:
            if marker in stripped:
                return f"soft:{marker}"
        return None

    @staticmethod
    def _plain_text(chain) -> str:
        """拼接消息链里所有 Plain 分片的文本。

        刻意用 `isinstance(comp, Plain)` 而不是鸭子类型的 `getattr(comp, "text")`：
        真实 AstrBot 里除 Plain 外还有别的组件带 `text` 字段（如 Record 的字幕），
        若检测时把它们算作文本、替换时又按 `isinstance(c, Plain)` 保留它们，
        就会出现「检测命中但错误文案没被清掉」的不一致。
        """
        parts: list[str] = []
        for comp in chain or []:
            if isinstance(comp, Plain):
                parts.append(comp.text)
        return "".join(parts)

    # ------------------------------------------------------------------ #
    # 请求快照（诊断 + 重发用）
    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """请求前：把兜底错误文案挂到事件上，并记录当次请求指纹。"""
        self._ensure_custom_error_reply(event)
        try:
            prompt = getattr(req, "prompt", "") or ""
            system_prompt = getattr(req, "system_prompt", "") or ""
            contexts = getattr(req, "contexts", None) or getattr(req, "messages", None) or []
            extras = getattr(req, "extra_user_content_parts", None) or []

            extra_texts: list[str] = []
            for part in extras:
                text = getattr(part, "text", None)
                if not isinstance(text, str) and isinstance(part, dict):
                    text = part.get("text")
                if isinstance(text, str) and text.strip():
                    extra_texts.append(text.strip())

            self._last_request = {
                "ts": time.time(),
                "umo": str(getattr(event, "unified_msg_origin", "") or ""),
                "user_message": (event.message_str or "").strip(),
                "prompt": prompt,
                "prompt_len": len(prompt),
                "prompt_sha1": hashlib.sha1(prompt.encode("utf-8", "ignore")).hexdigest(),
                "system_prompt": system_prompt,
                "system_prompt_len": len(system_prompt),
                "n_contexts": len(contexts),
                "extra_parts": extra_texts,
                "n_extra_parts": len(extra_texts),
            }
        except Exception as e:  # 快照失败绝不能影响主流程
            logger.debug(f"[llm_guard] 记录请求快照失败: {e}")

    # ------------------------------------------------------------------ #
    # 兜底拦截 + 换 provider 重发
    # ------------------------------------------------------------------ #
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送前检查：命中拦截特征时改写或丢弃，并可选换 provider 重发。"""
        if not self.enabled:
            return
        try:
            result = event.get_result()
            if result is None or not result.chain:
                return

            text = self._plain_text(result.chain)
            reason = self._match_reason(text)
            if reason is None:
                return
        except Exception as e:
            logger.error(f"[llm_guard] 发送前检查异常，已放行: {e}")
            return

        self._blocked_total += 1
        # 去掉命中片段，只保留一条便于排查的预览
        preview = text.strip().replace("\n", " ")[:160]
        logger.warning(f"[llm_guard] 拦截到被泄漏的 LLM 错误文案（{reason}）：{preview!r}")

        record = {
            "ts": time.time(),
            "reason": reason,
            "text": preview,
            "umo": str(getattr(event, "unified_msg_origin", "") or ""),
        }

        # 诊断转存：把当次请求的完整 prompt 落盘，便于定位触发源
        if self.dump_on_reject:
            try:
                path = self._dump_request(reason)
                if path:
                    record["dump"] = str(path)
                    logger.warning(f"[llm_guard] 已转存当次请求到：{path}")
            except Exception as e:
                logger.error(f"[llm_guard] 转存请求失败: {e}")

        if self.log_prompt_preview > 0:
            snapshot = self._last_request or {}
            raw = str(snapshot.get("prompt") or "")
            if raw:
                logger.warning(
                    f"[llm_guard] 当次 prompt 预览（前 {self.log_prompt_preview} 字）："
                    f"{raw[: self.log_prompt_preview]!r}"
                )

        self._recent.append(record)

        if self.mode == "log":
            return

        if self.mode == "drop":
            event.clear_result()
            logger.info("[llm_guard] 已丢弃该条消息（mode=drop）")
            return

        # mode == "replace"：优先换 provider 重新生成，失败再用静态兜底文案
        replacement = ""
        if self.retry_provider_id:
            replacement = await self._retry_with_provider(event)
            if replacement:
                self._retried_ok += 1
                record["retried"] = True
                logger.info(
                    f"[llm_guard] 已用备用 provider `{self.retry_provider_id}` 重新生成回复"
                )

        if not replacement:
            replacement = self.fallback_text

        kept = [c for c in result.chain if not isinstance(c, Plain)]
        if replacement.strip():
            kept.append(Plain(replacement))
        if kept:
            result.chain = kept
        else:
            event.clear_result()
        logger.info(
            f"[llm_guard] 已替换错误文案（mode=replace，"
            f"{'重发成功' if replacement and self.retry_provider_id else '使用兜底文案'}）"
        )

    async def _retry_with_provider(self, event: AstrMessageEvent) -> str:
        """用备用 provider 重新生成一次回复。

        Args:
            event: 当前消息事件。

        Returns:
            生成的文本；失败时返回空字符串。
        """
        snapshot = self._last_request or {}
        user_message = str(snapshot.get("user_message") or "").strip()
        if not user_message:
            user_message = (event.message_str or "").strip()
        if not user_message:
            logger.warning("[llm_guard] 无可用的问题文本，跳过重发")
            return ""

        kwargs: dict = {
            "chat_provider_id": self.retry_provider_id,
            "prompt": user_message,
        }
        system_prompt = str(snapshot.get("system_prompt") or "").strip()
        if system_prompt:
            kwargs["system_prompt"] = system_prompt

        try:
            import asyncio

            resp = await asyncio.wait_for(
                self.context.llm_generate(**kwargs),
                timeout=self.retry_timeout,
            )
        except Exception as e:
            logger.warning(f"[llm_guard] 备用 provider 重发失败: {e}")
            return ""

        text = self._llm_text(resp)
        # 备用 provider 也可能被拦，二次确认，避免把错误文案又发出去
        if not text or self._match_reason(text):
            logger.warning("[llm_guard] 备用 provider 同样被拦截或返回为空")
            return ""
        return text

    @staticmethod
    def _llm_text(resp: object) -> str:
        """从 LLM 响应对象里取纯文本。"""
        if resp is None:
            return ""
        for attr in ("completion_text", "result", "text"):
            value = getattr(resp, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        try:
            chain = getattr(resp, "result_chain", None)
            if chain is not None and hasattr(chain, "get_plain_text"):
                text = chain.get_plain_text()
                if text:
                    return str(text).strip()
        except Exception:
            pass
        return ""

    def _ensure_custom_error_reply(self, event: AstrMessageEvent) -> None:
        """把兜底文案写进事件的 persona 自定义错误消息（仅在未配置时）。

        为什么需要这一层：AstrBot 有若干条把错误文案直接 `event.send()` 出去的路径，
        它们**不经过 result_decorate 阶段**，`on_decorating_result` 拦不到：

        - `agent_sub_stages/internal.py` 的 `_send_llm_error_message()`（provider 选择失败）
        - `agent_sub_stages/internal.py:443` 的 `process()` 外层 `except`
          （文案为 `Error occurred while processing agent request: ...`）
        - `astr_agent_run_util.py:355` 的异常分支在非流式下 `set_result` 后直接 `return`，
          没有 `yield`，洋葱模型下可能到不了 result_decorate

        而这几条路径**都会优先采用 persona 的自定义错误消息**
        （`custom_error_message or <原始报错>`）。所以在这里把它挂到事件上，
        从源头保证用户看到的是人话而不是英文报错。

        用户已在人设里配置了 `custom_error_message` 时不覆盖，尊重其选择。
        """
        if not self.patch_custom_error_reply:
            return
        text = (self.fallback_text or "").strip()
        if not text:
            # 用户选择静默丢弃：不设置自定义文案（空串会被规范化成 None）
            return
        try:
            from astrbot.core.persona_error_reply import (
                extract_persona_custom_error_message_from_event,
                set_persona_custom_error_message_on_event,
            )
        except ImportError:
            logger.debug("[llm_guard] 当前 AstrBot 版本无 persona_error_reply，跳过该层防护")
            return
        try:
            if extract_persona_custom_error_message_from_event(event):
                return  # 用户在 persona 里已配置，尊重之
            set_persona_custom_error_message_on_event(event, text)
        except Exception as e:
            logger.debug(f"[llm_guard] 设置自定义错误文案失败: {e}")

    def _dump_request(self, reason: str) -> Path | None:
        """把当次请求快照写入本地文件，用于定位风控触发源。"""
        snapshot = self._last_request or {}
        if not snapshot:
            return None
        base = Path(self.dump_dir) if self.dump_dir else Path.cwd() / "llm_guard_dumps"
        base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = base / f"reject-{stamp}-{snapshot.get('prompt_sha1', 'nohash')[:8]}.json"
        payload = dict(snapshot)
        payload["reason"] = reason
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    # ------------------------------------------------------------------ #
    # 状态查询
    # ------------------------------------------------------------------ #
    @filter.command("llmguard", alias={"拦截状态"})
    async def llmguard_status(self, event: AstrMessageEvent):
        """查看兜底插件的运行状态与最近拦截记录。"""
        lines = [
            "🛡 LLM 安全拦截兜底",
            f"启用：{'是' if self.enabled else '否'}｜模式：{self.mode}",
            f"备用 provider：{self.retry_provider_id or '（未配置）'}",
            f"二级特征扫描：{'开' if self.soft_scan else '关'}",
            f"累计拦截：{self._blocked_total}｜重发成功：{self._retried_ok}",
        ]
        if self._recent:
            lines.append("")
            lines.append("最近拦截：")
            for item in list(self._recent)[-5:]:
                when = time.strftime("%m-%d %H:%M:%S", time.localtime(item["ts"]))
                lines.append(f"· {when} [{item['reason']}] {item['text'][:60]}")
        snapshot = self._last_request or {}
        if snapshot:
            lines.append("")
            lines.append(
                f"最近请求指纹：prompt {snapshot.get('prompt_len', 0)} 字｜"
                f"system {snapshot.get('system_prompt_len', 0)} 字｜"
                f"历史 {snapshot.get('n_contexts', 0)} 条｜"
                f"注入 {snapshot.get('n_extra_parts', 0)} 段"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("llmguarddump", alias={"转存请求"})
    async def llmguard_dump(self, event: AstrMessageEvent):
        """手动把最近一次 LLM 请求转存到本地，便于定位风控触发源。"""
        try:
            path = self._dump_request("manual")
        except Exception as e:
            yield event.plain_result(f"❌ 转存失败：{e}")
            return
        if not path:
            yield event.plain_result("⚠️ 还没有记录到任何 LLM 请求")
            return
        yield event.plain_result(f"✅ 已转存最近一次请求到：{path}")
