"""AstrBot platform adapter for the Seele Spine desktop pet.

The desktop client connects to a small local WebSocket endpoint. Text, images,
screen captures and TTS audio are exchanged as JSON. AstrBot still owns the
persona, memory, LLM provider, plugins and TTS, so the pet behaves exactly like
the QQ bot.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

# Some TTS plugins replace the response chain with a Record-only chain.
# Capture the original response text before decorating-result hooks run.
_REPLY_TEXT_CACHE: dict[str, str] = {}


def remember_reply_text(session_id: str, text: str) -> None:
    value = str(text or "").strip()
    if session_id and value:
        _REPLY_TEXT_CACHE[str(session_id)] = value


def pop_reply_text(session_id: str) -> str:
    if not session_id:
        return ""
    return _REPLY_TEXT_CACHE.pop(str(session_id), "")


DEFAULT_TOKEN = "seele-desktop-local-token-2026"

# 由插件入口注入：读取 AstrBot 里已经配置好的天气（陪伴面板/私有陪伴插件）。
WEATHER_GETTER = None
# 由插件入口注入：返回 AstrBot 当前配置的 STT Provider。
STT_GETTER = None
# 由插件入口注入：sync_target_umo="auto" 时解析当前 QQ/OneBot 会话。
SYNC_TARGET_GETTER = None
# Injected by the plugin entry: available AstrBot TTS/SVC audio plugins.
AUDIO_CAPS_GETTER = None
# Injected by the plugin entry: optional async SVC file converter.
SVC_CONVERTER = None

DEFAULT_PLATFORM_CONFIG: dict[str, Any] = {
    "type": "seele_desktop",
    "enable": True,
    "id": "seele_desktop",
    "host": "127.0.0.1",
    "port": 9080,
    "path": "/astrbot/seele",
    "token": DEFAULT_TOKEN,
    "user_id": "master",
    "user_name": "主人",
    # auto = 根据当前 QQ 官方/OneBot 平台和身份别名自动解析；
    # 留空则桌宠使用独立会话。
    "sync_target_umo": "auto",
}

CONFIG_METADATA = {
    "host": {
        "description": "监听地址",
        "type": "string",
        "hint": "建议保持 127.0.0.1，只允许本机桌宠连接。",
    },
    "port": {
        "description": "WebSocket 端口",
        "type": "int",
        "hint": "桌宠连接端口，默认 9080。",
    },
    "path": {
        "description": "WebSocket 路径",
        "type": "string",
        "hint": "默认 /astrbot/seele。",
    },
    "token": {
        "description": "认证令牌",
        "type": "string",
        "hint": "必须和桌宠设置里的令牌一致。",
    },
    "user_id": {
        "description": "用户 ID",
        "type": "string",
        "hint": "桌宠会话使用的稳定用户 ID，用于记忆和人设。",
    },
    "user_name": {
        "description": "用户称呼",
        "type": "string",
        "hint": "显示给模型和插件看的用户名字。",
    },
    "sync_target_umo": {
        "description": "同步目标会话 UMO",
        "type": "string",
        "hint": "例如 SeeleVollerei:FriendMessage:xxxx。桌宠聊天会复用该会话的上下文；留空则独立。",
    },
}

IMAGE_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _clean_client_id(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.-]", "", str(value or ""))
    return text[:64] or "seele-pc"


def _decode_data_url(value: str) -> tuple[bytes, str]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("图片数据为空")
    if text.startswith("data:"):
        match = re.match(r"^data:(image/[\w.+-]+);base64,(.+)$", text, re.I | re.S)
        if not match:
            raise ValueError("图片 data URL 格式不正确")
        mime = match.group(1).lower()
        encoded = match.group(2)
    else:
        mime = "image/jpeg"
        encoded = text
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("图片 base64 无法解码") from exc
    if not payload:
        raise ValueError("图片内容为空")
    if len(payload) > 15 * 1024 * 1024:
        raise ValueError("图片超过 15MB")
    return payload, mime


def _weather_condition(text: str) -> str:
    value = str(text or "")
    if "雪" in value:
        return "snow"
    if "雷" in value:
        return "thunder"
    if "雨" in value:
        return "rain"
    if "晴" in value:
        return "clear"
    if "阴" in value or "多云" in value:
        return "cloudy"
    if "热" in value:
        return "hot"
    if "冷" in value or "寒" in value:
        return "cold"
    return "any"


def _weather_temperature(text: str) -> float | None:
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°C|℃|度|C)", str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


class SeeleDesktopEvent(AstrMessageEvent):
    """One desktop-pet message event."""

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        adapter: "SeeleDesktopAdapter",
        client_id: str,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.adapter = adapter
        self.client_id = client_id

    async def send(self, message: MessageChain) -> None:
        payload = await self.adapter.chain_payload(
            message,
            self.client_id,
            fallback_text=pop_reply_text(self.session_id),
        )
        if payload.get("text") or payload.get("audio") or payload.get("images"):
            payload["type"] = "reply"
            payload["requestId"] = getattr(self.message_obj, "message_id", "")
            payload["proactive"] = False
            await self.adapter.send_json(self.client_id, payload)
        await super().send(message)

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        final = MessageChain()
        async for chain in generator:
            if chain and chain.chain:
                final.chain.extend(chain.chain)
        if final.chain:
            await self.send(final)
        await super().send_streaming(generator, use_fallback)


@register_platform_adapter(
    "seele_desktop",
    "希儿 Spine 桌宠本地 WebSocket 适配器",
    default_config_tmpl=DEFAULT_PLATFORM_CONFIG,
    adapter_display_name="希儿桌宠",
    support_streaming_message=False,
    config_metadata=CONFIG_METADATA,
)
class SeeleDesktopAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.host = str(platform_config.get("host", "127.0.0.1") or "127.0.0.1")
        self.port = int(platform_config.get("port", 9080) or 9080)
        path = str(platform_config.get("path", "/astrbot/seele") or "/astrbot/seele")
        self.ws_path = path if path.startswith("/") else f"/{path}"
        self.token = str(platform_config.get("token", DEFAULT_TOKEN) or DEFAULT_TOKEN)
        self.platform_id = str(platform_config.get("id") or "seele_desktop")
        self.user_id = str(platform_config.get("user_id", "master") or "master")
        self.user_name = str(platform_config.get("user_name", "主人") or "主人")
        self.sync_target_umo = str(
            platform_config.get("sync_target_umo", "") or ""
        ).strip()
        self._clients: dict[str, web.WebSocketResponse] = {}
        self._session_to_client: dict[str, str] = {}
        self._client_audio_settings: dict[str, dict[str, bool]] = {}
        self._send_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        logger.info(
            f"[SeeleDesktop] adapter initialized: ws://{self.host}:{self.port}{self.ws_path}"
        )

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="seele_desktop",
            description="Seele Spine desktop pet bridge",
            id=str(self.config.get("id") or "seele_desktop"),
            adapter_display_name="希儿桌宠",
            support_streaming_message=False,
            support_proactive_message=True,
        )

    async def run(self) -> None:
        app = web.Application()
        app.router.add_get(self.ws_path, self._handle_ws)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info(
            f"[SeeleDesktop] listening on ws://{self.host}:{self.port}{self.ws_path}"
        )
        await self._stop_event.wait()

    async def terminate(self) -> None:
        self._stop_event.set()
        for ws in list(self._clients.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
        logger.info("[SeeleDesktop] adapter stopped")

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=24 * 1024 * 1024)
        await ws.prepare(request)

        client_id = ""
        try:
            hello = await asyncio.wait_for(ws.receive(), timeout=15)
            if hello.type != WSMsgType.TEXT:
                await ws.close(code=4001, message=b"hello required")
                return ws
            data = json.loads(hello.data)
            if not isinstance(data, dict) or data.get("type") != "hello":
                await ws.send_json({"type": "error", "message": "需要先发送 hello"})
                await ws.close(code=4001, message=b"hello required")
                return ws
            if self.token and str(data.get("token", "")) != self.token:
                await ws.send_json({"type": "error", "message": "认证令牌不匹配"})
                await ws.close(code=4001, message=b"auth failed")
                return ws

            client_id = _clean_client_id(data.get("clientId"))
            self._clients[client_id] = ws
            self._session_to_client[f"{self.platform_id}!{self.user_id}"] = client_id
            self._client_audio_settings[client_id] = {
                "useTts": bool(data.get("useTts", True)),
                "useSvc": bool(data.get("useSvc", False)),
            }
            logger.info(f"[SeeleDesktop] client connected: {client_id}")

            caps: dict[str, Any] = {}
            if AUDIO_CAPS_GETTER is not None:
                try:
                    value = await AUDIO_CAPS_GETTER()
                    if isinstance(value, dict):
                        caps = value
                except Exception as exc:
                    logger.debug(f"[SeeleDesktop] audio caps failed: {exc}")
            await ws.send_json(
                {
                    "type": "hello_ack",
                    "clientId": client_id,
                    "sessionId": f"{self.platform_id}!{self.user_id}",
                    "name": "希儿",
                    "audioCaps": caps,
                }
            )

            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._on_message(client_id, message.data)
                elif message.type == WSMsgType.ERROR:
                    logger.warning(f"[SeeleDesktop] ws error: {ws.exception()}")
                    break
        except asyncio.TimeoutError:
            await ws.close(code=4002, message=b"hello timeout")
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] client handler failed: {exc}")
        finally:
            if client_id:
                self._clients.pop(client_id, None)
                self._client_audio_settings.pop(client_id, None)
                for session_id, mapped in list(self._session_to_client.items()):
                    if mapped == client_id:
                        self._session_to_client.pop(session_id, None)
                logger.info(f"[SeeleDesktop] client disconnected: {client_id}")
        return ws

    async def _on_message(self, client_id: str, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return

        msg_type = str(data.get("type", "") or "")
        if msg_type == "ping":
            await self.send_json(client_id, {"type": "pong"})
            return
        if msg_type == "chat":
            await self._dispatch_chat(client_id, data)
            return
        if msg_type == "weather_request":
            await self._send_weather(client_id, data)
            return
        if msg_type == "audio_chat":
            await self._dispatch_audio_chat(client_id, data)
            return
        if msg_type == "touch":
            logger.debug(f"[SeeleDesktop] touch: {data.get('part')}")
            return
        if msg_type == "audio_settings":
            self._client_audio_settings[client_id] = {
                "useTts": bool(data.get("useTts", True)),
                "useSvc": bool(data.get("useSvc", False)),
            }
            logger.info(
                f"[SeeleDesktop] audio settings: {self._client_audio_settings[client_id]}"
            )
            return

    async def _send_weather(self, client_id: str, data: dict) -> None:
        request_id = str(data.get("requestId", "") or "")
        result: dict[str, Any] = {}
        getter = WEATHER_GETTER
        if getter is not None:
            try:
                value = await getter()
                if isinstance(value, dict):
                    result = value
            except Exception as exc:
                logger.warning(f"[SeeleDesktop] weather fetch failed: {exc}")

        prompt = str(result.get("prompt") or result.get("text") or "").strip()
        available = bool(prompt) and "暂无" not in prompt and "disabled" not in prompt
        condition = _weather_condition(prompt) if available else "any"
        temperature = _weather_temperature(prompt) if available else None
        await self.send_json(
            client_id,
            {
                "type": "weather",
                "requestId": request_id,
                "available": available,
                "condition": condition,
                "temperature": temperature,
                "text": prompt[:160],
                "source": str(result.get("source") or ""),
            },
        )

    async def _dispatch_chat(self, client_id: str, data: dict) -> None:
        text = str(data.get("text", "") or "").strip()
        image_data = str(data.get("image", "") or "").strip()
        request_id = str(data.get("requestId", "") or uuid.uuid4().hex)
        if not text and not image_data:
            await self.send_json(
                client_id,
                {"type": "error", "message": "消息为空", "requestId": request_id},
            )
            return

        components: list[Any] = []
        image_path = ""
        if text:
            components.append(Plain(text))
        if image_data:
            try:
                raw, mime = _decode_data_url(image_data)
                suffix = IMAGE_MIME_EXT.get(mime, ".jpg")
                folder = Path(tempfile.gettempdir()) / "seele_desktop"
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f"{uuid.uuid4().hex}{suffix}"
                target.write_bytes(raw)
                image_path = str(target)
                components.append(Image.fromFileSystem(image_path))
            except Exception as exc:
                await self.send_json(
                    client_id,
                    {
                        "type": "error",
                        "message": f"图片处理失败: {exc}",
                        "requestId": request_id,
                    },
                )
                return

        abm = AstrBotMessage()
        abm.type = MessageType.FRIEND_MESSAGE
        abm.self_id = self.client_self_id
        abm.sender = MessageMember(user_id=self.user_id, nickname=self.user_name)
        abm.session_id = f"{self.platform_id}!{self.user_id}"
        abm.message_id = request_id
        abm.timestamp = int(time.time())
        abm.message = components
        abm.message_str = text or "[图片]"
        abm.raw_message = {
            "client_id": client_id,
            "image_path": image_path,
            "request_id": request_id,
        }

        self._session_to_client[abm.session_id] = client_id
        event = SeeleDesktopEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            adapter=self,
            client_id=client_id,
        )
        # 保留真实桌宠入站 UMO；后面统一历史 UMO 时用它判断用户当前在哪一侧。
        try:
            event.set_extra("source_umo", abm.session_id)
        except Exception:
            pass
        if self.sync_target_umo:
            target = self.sync_target_umo.strip()
            if target.lower() == "auto" and SYNC_TARGET_GETTER is not None:
                try:
                    target = await SYNC_TARGET_GETTER()
                except Exception as exc:
                    logger.warning(f"[SeeleDesktop] auto sync target failed: {exc}")
                    target = ""
            if target and target.lower() != "auto":
                try:
                    # 关键：让 AstrBot 的对话管理、记忆和人设上下文使用
                    # 当前 QQ/OneBot 的 UMO，但 platform_meta 仍是 seele_desktop，
                    # 所以回复仍然发回桌宠客户端。
                    event.unified_msg_origin = target
                    logger.info(
                        f"[SeeleDesktop] synced context -> {target}"
                    )
                except Exception as exc:
                    logger.warning(f"[SeeleDesktop] invalid sync target: {exc}")
        if image_path:
            event.track_temporary_local_file(image_path)
        self.commit_event(event)
        await self.send_json(
            client_id, {"type": "ack", "requestId": request_id}
        )
        logger.info(
            f"[SeeleDesktop] chat from {client_id}: {abm.message_str[:60]}"
            f" image={bool(image_path)}"
        )

    async def _dispatch_audio_chat(self, client_id: str, data: dict) -> None:
        request_id = str(data.get("requestId", "") or uuid.uuid4().hex)
        encoded = str(data.get("data", "") or "").strip()
        if not encoded:
            await self.send_json(
                client_id,
                {"type": "error", "message": "语音内容为空", "requestId": request_id},
            )
            return
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[-1]
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            await self.send_json(
                client_id,
                {"type": "error", "message": f"语音解码失败: {exc}", "requestId": request_id},
            )
            return

        provider = None
        try:
            if STT_GETTER is not None:
                provider = await STT_GETTER()
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] get STT provider failed: {exc}")
        if provider is None:
            await self.send_json(
                client_id,
                {
                    "type": "error",
                    "message": "AstrBot 没有配置可用的语音识别（STT）Provider",
                    "requestId": request_id,
                },
            )
            return

        folder = Path(tempfile.gettempdir()) / "seele_desktop"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = ".webm"
        mime = str(data.get("mime", "") or "")
        if "ogg" in mime:
            suffix = ".ogg"
        elif "wav" in mime:
            suffix = ".wav"
        target = folder / f"{uuid.uuid4().hex}{suffix}"
        target.write_bytes(payload)
        try:
            text = await provider.get_text(str(target))
            text = str(text or "").strip()
            if not text:
                await self.send_json(
                    client_id,
                    {"type": "error", "message": "没有识别出语音内容", "requestId": request_id},
                )
                return
            logger.info(f"[SeeleDesktop] audio transcribed: {text[:60]}")
            await self._dispatch_chat(
                client_id,
                {
                    "text": text,
                    "requestId": request_id,
                    "clientId": data.get("clientId", ""),
                    "userName": data.get("userName", ""),
                },
            )
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] audio transcription failed: {exc}")
            await self.send_json(
                client_id,
                {"type": "error", "message": f"语音识别失败: {exc}", "requestId": request_id},
            )
        finally:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass

    async def chain_payload(
        self, message: MessageChain, client_id: str = "", fallback_text: str = ""
    ) -> dict[str, Any]:
        texts: list[str] = []
        audio: list[dict[str, str]] = []
        images: list[str] = []
        audio_settings = self._client_audio_settings.get(client_id, {})
        use_tts = bool(audio_settings.get("useTts", True))
        use_svc = bool(audio_settings.get("useSvc", False))

        for component in getattr(message, "chain", []) or []:
            if isinstance(component, Plain):
                if component.text:
                    texts.append(component.text)
            elif isinstance(component, Record):
                if component.text:
                    texts.append(component.text)
                if not use_tts:
                    continue
                try:
                    encoded = await component.convert_to_base64()
                    mime = "audio/wav"
                    file_name = str(getattr(component, "file", "") or "")
                    guessed, _ = mimetypes.guess_type(file_name)
                    if guessed and guessed.startswith("audio/"):
                        mime = guessed
                    if encoded and use_svc and SVC_CONVERTER is not None:
                        suffix = mimetypes.guess_extension(mime) or ".wav"
                        temp = Path(tempfile.gettempdir()) / "seele_desktop" / (
                            f"svc_{uuid.uuid4().hex}{suffix}"
                        )
                        temp.parent.mkdir(parents=True, exist_ok=True)
                        temp.write_bytes(base64.b64decode(encoded))
                        try:
                            converted = await SVC_CONVERTER(str(temp))
                            if converted and Path(converted).is_file():
                                encoded = base64.b64encode(
                                    Path(converted).read_bytes()
                                ).decode("ascii")
                                mime = "audio/wav"
                        except Exception as exc:
                            logger.warning(
                                f"[SeeleDesktop] SVC convert failed, using TTS audio: {exc}"
                            )
                        finally:
                            try:
                                temp.unlink(missing_ok=True)
                            except Exception:
                                pass
                    if encoded:
                        audio.append({"data": encoded, "mime": mime})
                except Exception as exc:
                    logger.warning(f"[SeeleDesktop] audio convert failed: {exc}")
            elif isinstance(component, Image):
                try:
                    encoded = await component.convert_to_base64()
                    if encoded:
                        file_name = str(getattr(component, "file", "") or "")
                        mime = mimetypes.guess_type(file_name)[0] or "image/jpeg"
                        if not mime.startswith("image/"):
                            mime = "image/jpeg"
                        images.append(f"data:{mime};base64,{encoded}")
                except Exception as exc:
                    logger.warning(f"[SeeleDesktop] image convert failed: {exc}")

        if not texts:
            # Some TTS plugins replace the reply with a text-less Record.
            # Prefer text captured before decorating-result hooks, then message_str.
            fallback = str(fallback_text or "").strip() or str(
                getattr(message, "message_str", "") or ""
            ).strip()
            if fallback:
                texts.append(fallback)
        text = "\n".join(dict.fromkeys(part for part in texts if part)).strip()
        payload: dict[str, Any] = {"text": text}
        if audio:
            payload["audio"] = audio[0]
        if images:
            payload["images"] = images
        return payload

    def has_active_client(self) -> bool:
        """Whether at least one desktop pet client is currently connected."""
        try:
            return any(not ws.closed for ws in self._clients.values())
        except Exception:
            return False

    async def send_json(self, client_id: str, payload: dict[str, Any]) -> bool:
        ws = self._clients.get(client_id)
        if ws is None or ws.closed:
            return False
        try:
            async with self._send_lock:
                await ws.send_json(payload)
            return True
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] send failed: {exc}")
            return False

    async def send_by_session(self, session, message_chain: MessageChain) -> None:
        target = self._session_to_client.get(str(session.session_id))
        if not target and len(self._clients) == 1:
            target = next(iter(self._clients.keys()))
        payload = await self.chain_payload(
            message_chain,
            target or "",
            fallback_text=pop_reply_text(session.session_id),
        )
        if payload.get("text") or payload.get("audio") or payload.get("images"):
            if target:
                payload["type"] = "reply"
                payload["proactive"] = True
                payload["requestId"] = ""
                await self.send_json(target, payload)
        await super().send_by_session(session, message_chain)
