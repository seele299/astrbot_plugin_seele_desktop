# 希儿桌宠桥接插件

为 Seele Spine 桌宠提供 AstrBot 平台适配器，并支持可选的主动录屏视频分析与记忆写入。

## 功能

- 注册 `seele_desktop` 平台，默认端口 `9080`，路径 `/astrbot/seele`
- 文字消息收发
- typing / error 状态回传
- 图片、屏幕截图识别
- TTS / SVC 能力检测
- STT 语音转文字
- QQ 官方 / OneBot / SnowLuma UMO 同步
- 天气上下文
- 可选主动录屏 inbox：
  - 读取裸视频或 manifest
  - ffprobe 检测格式
  - 用户配置的 ffmpeg 转码
  - 单文件大小上限与自动降码率
  - 视频模型分析
  - 写入外部记忆 Provider
  - ack 回执，源文件只读不删

## 依赖

```text
aiohttp
openai
```

`aiohttp` 为运行必需；`openai` 仅在开启主动录屏并配置视频模型时使用（懒加载，不装不影响插件启动与聊天）。它是 OpenAI 官方 SDK，也用于调用任何兼容 OpenAI 协议的服务，`proactive_video_api_base` 可填自己的接口地址。

主动录屏还需要本机可用的 ffmpeg/ffprobe，路径由用户在插件配置中填写。

## STT 语音转文字

STT 的选择顺序：

```text
1. custom_stt_enabled + custom_stt_api_key：自定义厂商
2. qwen_stt_enabled + qwen_stt_api_key：Qwen3-ASR
3. AstrBot 当前配置的 STT Provider
```

### 自定义厂商 STT

支持两种协议：

```text
openai：OpenAI 兼容 /audio/transcriptions
dashscope：DashScope 同步多模态接口
```

配置项：

```text
custom_stt_enabled
custom_stt_protocol
custom_stt_api_key
custom_stt_api_base
custom_stt_model
custom_stt_ffmpeg_path
custom_stt_timeout
```

OpenAI 兼容示例：

```text
custom_stt_enabled = true
custom_stt_protocol = openai
custom_stt_api_base = https://api.openai.com/v1
custom_stt_model = whisper-1
```

桌宠录音是 WebM 时需要 ffmpeg 转 WAV；可填 `custom_stt_ffmpeg_path`，留空使用 PATH 中的 ffmpeg。

API Key 只填写在 AstrBot 的 seele 插件配置中，不要写进桌宠目录或分享包。

## 安装

将 `astrbot_plugin_seele_desktop` 放入 AstrBot 插件目录，重启 AstrBot。插件会自动注册平台配置。

也可以在 AstrBot WebUI →「插件管理 → 安装插件」直接上传 `astrbot_plugin_seele_desktop.zip`，或用「从 URL 安装」填写本仓库地址。

## 用户必须自行配置的内容

本插件不包含任何 API Key、模型地址、用户身份或本机路径。以下内容由用户在自己的 AstrBot 环境中配置：

### 平台

- 端口、路径、token
- 桌宠客户端连接地址
- 用户 ID / 称呼

### 视频模型

- `proactive_video_api_base`
- `proactive_video_api_key`
- `proactive_video_model`
- 视频请求格式与采样帧率
- 超时和大小上限
- 体积阈值 `proactive_video_direct_frame_mb`（默认 3 MB，超过则直接抽 1 帧做图片理解）

### 转码

- `proactive_video_ffmpeg_path`
- `proactive_video_ffprobe_path`

不配置 ffmpeg 时，只处理不需要转码且未超过大小上限的 MP4。

### inbox

- `proactive_video_enabled`
- `proactive_video_inbox`

留空时使用 AstrBot 数据目录下的 `seele_record_inbox`。源视频文件只读，不删除、不移动。

### 记忆

支持：

```text
memory_provider = auto
memory_provider = memory_companion
memory_provider = custom
memory_provider = none
```

- `auto`：优先 memory_companion，其次使用第一个兼容的外部记忆插件
- `memory_companion`：明确使用 memory_companion
- `custom`：按 `memory_plugin` 名称选择其他记忆插件
- `none`：不写记忆

还需要：

- `memory_owner_user_id`：记忆归属用户 ID
- `memory_plugin`：自定义记忆插件名称片段

如果记忆 Provider 不可用，分析结果仍会写入 ack，但不会写长期记忆。

## 多平台会话同步

插件支持 QQ 官方、OneBot 与 SnowLuma：

- QQ 官方在线时优先使用 QQ 官方会话
- OneBot / SnowLuma 私聊通过 `sync_umo_map` 映射到同一份 QQ 官方会话
- 回复仍回到原平台，因此各平台之间可以互切后继续同一话题

SnowLuma 已停止维护，后续可迁移到其他 OneBot 实现。

## 主动录屏流程

```text
外部/本地把视频放入 inbox
→ 文件稳定检测
→ ffprobe
→ 必要时 ffmpeg 转码
→ 视频模型分析
→ 写入记忆 Provider
→ 写 ack
```

源视频始终保留在 inbox，由产生方负责清理。

## 常见问题

- 没有 ack：检查 `proactive_video_enabled`、inbox 路径和插件日志
- `ffmpeg_missing`：配置 ffmpeg/ffprobe 路径
- `memory_provider_unavailable`：确认记忆插件已启用
- `memory_user_id_missing`：配置 `memory_owner_user_id`
- 视频分析失败：检查 API Base、Key、模型和网络

### 平台没有自动出现

1. 插件目录名必须是 `<AstrBot数据目录>\plugins\astrbot_plugin_seele_desktop`
2. 插件列表里该插件已启用
3. 完全重启 AstrBot（不是刷新网页）
4. 日志中应出现 `[SeeleDesktop] added platform entry seele_desktop on port 9080`
5. 刷新 WebUI，或重新进入「机器人」页面

### 主动录屏没有 ack 或未写入记忆

依次检查：①桌宠是否开启主动录屏、inbox 路径是否正确 ②`proactive_video_enabled`
③ffmpeg / ffprobe ④视频模型四项配置 ⑤`memory_provider` 与 `memory_owner_user_id`
⑥记忆插件的 `accept_external_records` ⑦向量召回所需 embedding。

## 不包含

- AstrBot 本体
- 任何 API Key
- 任何模型
- GPT-SoVITS / RVC/SVC 模型
- 桌宠本体

## 许可

代码以 MIT 授权（见 `LICENSE`）；本仓库不含任何官方美术、模型或音频素材，
角色相关内容版权归米哈游及原权利人所有，MIT 授权不覆盖此类素材。
