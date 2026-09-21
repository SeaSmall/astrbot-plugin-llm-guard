# astrbot_plugin_llm_guard

LLM 内容安全拦截兜底插件 —— 挡住被泄漏到用户面前的 API 错误文案，并在被拦截时换 provider 重新生成正常回复。

## 它解决什么问题

部分 LLM 服务商对高风险 prompt 会直接返回 HTTP 400，例如：

```json
{
  "error": {
    "type": "content_filter",
    "message": "The request was rejected because it was considered high risk"
  }
}
```

而 AstrBot 核心在 LLM 请求失败时，会把**原始错误文案当成机器人的回复发给用户**，并且这一轮完全没有正常回复。涉及的核心路径：

| 位置 | 行为 |
| --- | --- |
| `agent/runners/tool_loop_agent_runner.py` | 所有候选模型失败 → `LLMResponse(role="err", completion_text="All chat models failed: ...")` |
| `astr_agent_run_util.py` | 异常 → `Error occurred during AI execution. / Error Type / Error Message` |
| `pipeline/.../agent_sub_stages/internal.py` | `_send_llm_error_message()` 与 `process()` 外层 `except`，用 `event.send()` 直发 |

结果就是用户收到一句英文报错，而不是机器人该说的话。

## 工作原理：两层防护

### 1. 发送前兜底拦截（`on_decorating_result`）

在 pipeline 的 result_decorate 阶段检查即将发出的文本，命中拦截特征时按 `mode` 处理：

- `replace`（默认）：替换成重发结果或 `fallback_text`
- `drop`：整条丢弃
- `log`：只记日志，不改动

拦截特征按 **snake_case 机器标识符 = 强特征、散文措辞 = 软特征** 的原则选取，覆盖：
Kimi/Moonshot 的 `...considered high risk`、`content_filter`；AstrBot 核心自己拼的
`All chat models failed` / `Error occurred during AI execution` /
`Error occurred while processing agent request` / `LLM 响应错误` / `LLM 请求失败`；
第三方 runner 的 `阿里云百炼请求失败` / `Coze 请求失败` / `Dify 请求失败`。

二级特征默认关闭，且**刻意不含** `高风险` / `被拒绝` / `违规` 这类裸话题词 ——
否则模型正常说「高风险投资要谨慎」也会被拦掉。你的服务商若用了别的措辞，
用 `extra_markers` 补。

### 2. 源头加固（`patch_custom_error_reply`，默认开）

有几条路径把错误文案直接 `event.send()` 发出、不经过发送前钩子，第 1 层拦不到 ——
但它们都优先采用 persona 的自定义错误消息。本插件在每次请求前通过 AstrBot 公开 API
`set_persona_custom_error_message_on_event()` 把 `fallback_text` 挂到事件上，
从源头堵住。**不 monkey-patch 任何核心方法，也不覆盖你已配置的人设错误消息。**

### 3. 换 provider 重发（`retry_provider_id`）

检测到拦截后，用另一个 provider 重新生成一次再发出 —— 即使核心没配
`fallback_provider_ids`，机器人也能正常回话，而不只是显示一句兜底文案。

> ⚠️ 备用 provider 请填**与你当前不同的服务商**，否则同一家风控会再次拦截。

## 安装

把本仓库整个文件夹放进 AstrBot 的 `data/plugins/` 目录，然后在 WebUI 里重载插件。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `mode` | `replace` | `replace` / `drop` / `log` |
| `retry_provider_id` | 空 | 被拦截时用于重发的备用 provider ID，**建议填一个不同服务商** |
| `retry_timeout` | `60` | 重发超时（秒） |
| `fallback_text` | 一句友好提示 | 重发失败时发送的内容，留空则静默丢弃 |
| `patch_custom_error_reply` | `true` | 第 2 层防护开关 |
| `soft_scan` | `false` | 是否扫描泛化安全措辞（默认关，避免误伤） |
| `soft_max_len` | `300` | 二级特征扫描的最大文本长度 |
| `extra_markers` | 空 | 自定义拦截特征，逗号或换行分隔，按强特征处理 |
| `dump_on_reject` | `false` | 被拦截时转存当次请求，用于定位根因 |
| `dump_dir` | 空 | 转存目录，留空用工作目录下 `llm_guard_dumps/` |
| `log_prompt_preview` | `0` | 拦截时在日志打印 prompt 前 N 个字符 |

配置项在**每次使用时现读**，所以在 WebUI 改完即时生效，不依赖插件重载。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/llmguard`（别名 `/拦截状态`） | 查看启用状态、累计拦截次数、最近拦截记录、最近请求指纹 |
| `/llmguarddump`（别名 `/转存请求`） | 手动把最近一次 LLM 请求转存到本地 |

## 排查根因

打开 `dump_on_reject` 复现一次，会在 `llm_guard_dumps/` 落下 JSON，包含当次完整 prompt：

| 字段 | 含义 |
| --- | --- |
| `prompt` | 当次发给模型的完整 prompt |
| `system_prompt` | 人设 / 系统提示词 |
| `n_contexts` | 历史消息条数（太大容易触发风控） |
| `extra_parts` | 其它插件注入的附加内容（`extra_user_content_parts`） |
| `user_message` | 用户这一条发的原文 |
| `reason` | 命中的拦截特征 |

拿它比一比就能看出是**人设**、**历史**还是**某个插件注入的内容**触发了服务商风控。
排查完建议把 `dump_on_reject` 关掉。

## 兼容性

- 无第三方依赖，纯文本处理，不改动别人的消息链。
- 只注册 `on_decorating_result`、`on_llm_request` 两个钩子和两个指令，
  与 `astrbot_plugin_toolkit` / `meme_responder` / `image_bridge` / `daily-digest` /
  `proactive_guard` / `Elaina_meme_Bridge` 并存无冲突。
- 只在命中拦截特征时才动 `result.chain`，正常回复原样放行。
- 要求 AstrBot `>= 4.16.0`。

## License

MIT
