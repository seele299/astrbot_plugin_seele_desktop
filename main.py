"""Entry point for the Seele desktop-pet bridge plugin."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time

import aiohttp
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

from . import adapter as adapter_module
from . import memory_bridge
from .adapter import DEFAULT_PLATFORM_CONFIG, SeeleDesktopAdapter
from .proactive_video import ProactiveVideoInbox


class SeeleDesktopPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._ensure_platform()
        # 天气查询每次都从当前 AstrBot 插件实例读取，避免插件加载顺序问题。
        adapter_module.WEATHER_GETTER = self._get_weather_context
        adapter_module.STT_GETTER = self._get_stt_provider
        adapter_module.SYNC_TARGET_GETTER = self._resolve_sync_target
        adapter_module.AUDIO_CAPS_GETTER = self._get_audio_caps
        adapter_module.SVC_CONVERTER = self._convert_audio_svc
        self._sync_target_cache = ("", 0.0)
        self._onebot_sync_cache: tuple[dict[str, str], float] = ({}, 0.0)
        self.proactive_video = ProactiveVideoInbox(self)

    @filter.on_using_llm_tool()
    async def capture_tts_tool_text(self, event: AstrMessageEvent, tool, tool_args):
        """Capture speech text from TTS tools before they send audio directly."""
        try:
            name = str(getattr(tool, "name", "") or "").lower()
            if not any(
                token in name for token in ("tts", "gsv", "speech", "voice", "say")
            ):
                return
            args = tool_args if isinstance(tool_args, dict) else {}
            for key in ("message", "text", "content", "speech"):
                value = str(args.get(key) or "").strip()
                if value:
                    adapter_module.remember_reply_text(event.session_id, value)
                    return
        except Exception as exc:
            logger.debug(f"[SeeleDesktop] capture TTS tool text failed: {exc}")

    @filter.on_llm_response()
    async def capture_llm_text(self, event: AstrMessageEvent, response):
        """Capture assistant/TTS tool text before TTS replaces the reply chain."""
        try:
            text = str(getattr(response, "completion_text", "") or "").strip()
            if not text:
                chain = getattr(response, "result_chain", None)
                chain_items = getattr(chain, "chain", None) or []
                text = "".join(
                    str(getattr(item, "text", "") or "")
                    for item in chain_items
                    if getattr(item, "text", "")
                ).strip()
            if not text:
                names = list(getattr(response, "tools_call_name", []) or [])
                args = list(getattr(response, "tools_call_args", []) or [])
                for name, arg in zip(names, args):
                    if not any(
                        token in str(name).lower()
                        for token in ("tts", "gsv", "speech", "voice", "say")
                    ):
                        continue
                    if isinstance(arg, dict):
                        for key in ("message", "text", "content", "speech"):
                            value = str(arg.get(key) or "").strip()
                            if value:
                                text = value
                                break
                    if text:
                        break
            if text:
                adapter_module.remember_reply_text(event.session_id, text)
        except Exception as exc:
            logger.debug(f"[SeeleDesktop] capture LLM text failed: {exc}")

    @filter.on_decorating_result(priority=1)
    async def capture_reply_text(self, event: AstrMessageEvent):
        """Remember LLM text before TTS plugins replace the chain with audio."""
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None) or []
            texts = [
                str(getattr(item, "text", "") or "")
                for item in chain
                if isinstance(item, Plain) and getattr(item, "text", "")
            ]
            if texts:
                adapter_module.remember_reply_text(event.session_id, "\n".join(texts))
        except Exception as exc:
            logger.debug(f"[SeeleDesktop] capture reply text failed: {exc}")

    def _read_json_config(self, name: str) -> dict:
        candidates = [
            Path(__file__).parent.parent.parent / "config" / name,
        ]
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_path

            candidates.append(
                Path(get_astrbot_path()) / "data" / "config" / name
            )
        except Exception:
            pass
        for path in candidates:
            try:
                if path.is_file():
                    return json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
        return {}

    def _parse_aliases(self, raw) -> list[tuple[str, str, str]]:
        result: list[tuple[str, str, str]] = []
        if isinstance(raw, (list, tuple)):
            tokens = [str(item) for item in raw]
        else:
            tokens = re.split(r"\s+", str(raw or ""))
        for item in tokens:
            for token in str(item).split():
                token = token.strip().strip("[],'\"")
                if "=" not in token:
                    continue
                left, canonical = token.split("=", 1)
                left = left.strip().strip("[],'\"")
                canonical = canonical.strip().strip("[],'\"")
                if not left or not canonical:
                    continue
                if ":" in left:
                    platform, user = left.split(":", 1)
                    user = user.replace("master:", "")
                else:
                    platform, user = "", left
                result.append((platform.strip(), user.strip(), canonical))
        return result

    def _latest_private_conversations(
        self, platform_ids: set[str]
    ) -> dict[str, tuple[str, float]]:
        """读取 conversations 表里各平台最近一条私聊 UMO。"""
        data_dir = Path(__file__).parent.parent.parent
        db = data_dir / "data_v4.db"
        if not platform_ids or not db.is_file():
            return {}
        placeholders = ",".join("?" for _ in platform_ids)
        query = (
            "SELECT platform_id, user_id, updated_at FROM conversations "
            f"WHERE platform_id IN ({placeholders}) "
            "AND user_id LIKE '%:FriendMessage:%' "
            "ORDER BY updated_at DESC LIMIT 80"
        )
        result: dict[str, tuple[str, float]] = {}
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                rows = con.execute(query, tuple(platform_ids)).fetchall()
            finally:
                con.close()
        except Exception as exc:
            logger.debug(f"[SeeleDesktop] read conversation DB failed: {exc}")
            return {}
        for platform_id, user_id, updated_at in rows:
            if platform_id in result:
                continue
            try:
                ts = datetime.fromisoformat(str(updated_at)).timestamp()
            except (TypeError, ValueError):
                ts = 0.0
            result[str(platform_id)] = (str(user_id), ts)
        return result

    def _onebot_sync_umo_map(self) -> dict[str, str]:
        now = time.monotonic()
        cached, cached_at = self._onebot_sync_cache
        if cached and now - cached_at < 30:
            return cached
        result: dict[str, str] = {}
        try:
            data_dir = Path(__file__).parent.parent.parent
            path = data_dir / "cmd_config.json"
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            for platform in data.get("platform", []) or []:
                if str(platform.get("type") or "") != "aiocqhttp":
                    continue
                mapping = platform.get("sync_umo_map") or {}
                if isinstance(mapping, dict):
                    for key, value in mapping.items():
                        key = str(key).strip()
                        value = str(value).strip()
                        if key and value:
                            result[key] = value
        except Exception as exc:
            logger.debug(f"[SeeleDesktop] read sync_umo_map failed: {exc}")
        self._onebot_sync_cache = (result, now)
        return result

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE,
        priority=1_000_000,
    )
    async def sync_onebot_history_umo(self, event: AstrMessageEvent):
        """让 SnowLuma/OneBot 私聊和 QQ 官方读写同一个历史 UMO。"""
        try:
            platform_name = str(event.get_platform_name() or "").lower()
            if platform_name not in {"aiocqhttp", "snowluma", "onebot"}:
                return
            qq_available = False
            try:
                qq_available = any(
                    str(platform.meta().name).lower() in {"qq_official", "qqofficial"}
                    for platform in self.context.platform_manager.platform_insts
                )
            except Exception:
                pass
            if not qq_available:
                return
            sender_id = str(event.get_sender_id() or "").strip()
            target = self._onebot_sync_umo_map().get(sender_id)
            if not target or target == event.unified_msg_origin:
                return
            # 只改会话历史/记忆使用的 UMO；aiocqhttp 的 event.send 用
            # platform + sender_id 发送，因此回复仍然回到 SnowLuma。
            # 同时保留真实入站 UMO，供主动消息/陪伴插件判断用户当前在哪一侧。
            source_umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if source_umo and source_umo != target:
                try:
                    event.set_extra("source_umo", source_umo)
                except Exception:
                    pass
            event.unified_msg_origin = target
            try:
                event.set_extra("history_umo", target)
            except Exception:
                pass
            logger.info(
                f"[SeeleDesktop] OneBot history UMO synced: "
                f"{platform_name}/{sender_id} -> {target}"
            )
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] sync OneBot history UMO failed: {exc}")

    async def _resolve_sync_target(self) -> str:
        """QQ 官方优先；它和 SnowLuma 通过 sync_umo_map 共用同一份历史。"""
        now = time.monotonic()
        cached_target, cached_at = self._sync_target_cache
        if cached_target and now - cached_at < 5:
            return cached_target

        platforms = {}
        try:
            for platform in self.context.platform_manager.platform_insts:
                meta = platform.meta()
                platforms[str(meta.name).lower()] = str(meta.id)
        except Exception:
            pass

        qq_id = platforms.get("qq_official") or platforms.get("qqofficial")
        onebot_id = (
            platforms.get("aiocqhttp")
            or platforms.get("snowluma")
            or platforms.get("onebot")
        )
        latest = self._latest_private_conversations(
            {item for item in (qq_id, onebot_id) if item}
        )
        qq_row = latest.get(qq_id) if qq_id else None
        onebot_row = latest.get(onebot_id) if onebot_id else None

        # 只要 QQ 官方平台在线，就以 QQ 官方的私聊 UMO 作为唯一历史目标。
        # OneBot/SnowLuma 的 sync_umo_map 可能把本地账号映射到统一 UMO，
        # 因此 QQ 官方、SnowLuma、桌宠可能读写同一份会话历史。
        if qq_id and qq_row:
            target = qq_row[0]
            self._sync_target_cache = (target, now)
            logger.info(
                f"[SeeleDesktop] auto sync target: {target} "
                f"(source=qq_official priority, onebot={onebot_row[0] if onebot_row else '-'})"
            )
            return target
        if not qq_id and onebot_id and onebot_row:
            target = onebot_row[0]
            self._sync_target_cache = (target, now)
            logger.info(
                f"[SeeleDesktop] auto sync target: {target} (source=aiocqhttp)"
            )
            return target

        # 数据库里没有 QQ 官方记录时，回退到 target_platform + identity_aliases。
        private_config = self._read_json_config(
            "astrbot_plugin_private_companion_config.json"
        )
        memory_config = self._read_json_config(
            "astrbot_plugin_memory_companion_config.json"
        )
        target_platform = (
            "qq_official" if qq_id else "aiocqhttp"
        )
        aliases = []
        aliases.extend(
            self._parse_aliases(memory_config.get("identity_aliases") or "")
        )
        aliases.extend(
            self._parse_aliases(private_config.get("private_user_aliases") or "")
        )

        platform_id = platforms.get(target_platform, "")
        if not platform_id:
            for name in ("qq_official", "aiocqhttp", "snowluma"):
                if platforms.get(name):
                    target_platform = name
                    platform_id = platforms[name]
                    break
        if not platform_id:
            return ""

        canonical = ""
        local_user = ""
        for name, user, value in aliases:
            if not canonical:
                canonical = value
            if target_platform in {"qq_official", "qqofficial"}:
                if name in {"qq_official", "qqofficial", "seelevollerei"} or (
                    not name and user
                ):
                    canonical = value
                    local_user = user
            elif name in {target_platform, platform_id.lower(), "default", "snowluma"}:
                if user and user != "master":
                    local_user = user
                    canonical = value

        user_id = (
            canonical
            if target_platform in {"qq_official", "qqofficial"}
            else local_user or canonical
        )
        if not user_id:
            return ""
        target = f"{platform_id}:FriendMessage:{user_id}"
        self._sync_target_cache = (target, now)
        logger.info(
            f"[SeeleDesktop] auto sync fallback: {target} "
            f"(platform={target_platform}, aliases={len(aliases)})"
        )
        return target

    def _has_screen_companion(self) -> bool:
        """检测 AstrBot 是否已加载屏幕伙伴插件，避免桌宠重复主动识屏。"""
        try:
            for star in self.context.get_all_stars():
                names = []
                for attr in ("name", "star_name", "module_name"):
                    value = getattr(star, attr, "")
                    if value:
                        names.append(str(value))
                star_cls = getattr(star, "star_cls", None) or type(star)
                names.extend(
                    [
                        getattr(star_cls, "__name__", ""),
                        getattr(star_cls, "__module__", ""),
                    ]
                )
                text = " ".join(names).lower()
                if "screen_companion" in text:
                    return True
                if "screen" in text and "companion" in text:
                    return True
        except Exception as exc:
            logger.debug(f"[SeeleDesktop] screen companion detection skipped: {exc}")
        for name in (
            "astrbot_plugin_screen_companion_config.json",
        ):
            try:
                if self._read_json_config(name):
                    return True
            except Exception:
                pass
        return False

    async def _probe_gsv_service(self, base_url: str) -> bool:
        """Return True when the GPT-SoVITS HTTP service is reachable."""
        url = str(base_url or "").strip().rstrip("/")
        if not url:
            url = "http://127.0.0.1:9880"
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        try:
            timeout = aiohttp.ClientTimeout(total=1.8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url + "/docs", allow_redirects=True
                ) as response:
                    return response.status < 500
        except Exception:
            return False

    async def _get_audio_caps(self) -> dict:
        """Detect AstrBot's currently available TTS/SVC audio plugins."""
        tts_provider = None
        try:
            tts_provider = await self.context.get_using_tts_provider_async()
        except Exception:
            pass
        gsv = self._read_json_config("astrbot_plugin_GPT_SoVITS_config.json")
        gsv_enabled = bool(gsv.get("enabled", False))
        gsv_base = str((gsv.get("client") or {}).get("base_url") or "").strip()
        gsv_healthy = await self._probe_gsv_service(gsv_base) if gsv_enabled else False
        tts_plugin = gsv_enabled and gsv_healthy
        rvc = self._read_json_config("astrbot_plugin_rvc_svc_config.json")
        svc = bool(str(rvc.get("svc_base_url") or "").strip()) and bool(
            str(rvc.get("auto_svc_model") or "").strip()
        )
        tts_hint = ""
        if gsv_enabled and not gsv_healthy:
            tts_hint = "GPT-SoVITS 已启用但服务未就绪"
        return {
            "tts": bool(tts_provider) or tts_plugin,
            "svc": svc,
            "ttsName": (
                type(tts_provider).__name__
                if tts_provider
                else ("GPT-SoVITS" if tts_plugin else "")
            ),
            "ttsHint": tts_hint,
            "svcName": "RVC/SVC" if svc else "",
            "screenCompanion": self._has_screen_companion(),
        }

    async def _convert_audio_svc(self, audio_path: str) -> str:
        """Convert a TTS wav through the configured RVC/SVC backend."""
        config = self._read_json_config("astrbot_plugin_rvc_svc_config.json")
        base_url = str(config.get("svc_base_url") or "").strip()
        model = str(config.get("auto_svc_model") or "").strip()
        if not base_url or not model:
            return ""
        try:
            from gradio_client import Client, handle_file
        except Exception:
            logger.debug("[SeeleDesktop] gradio_client unavailable for SVC")
            return ""

        def _run() -> str:
            client = Client(base_url)
            return str(
                client.predict(
                    handle_file(audio_path),
                    0,
                    0,
                    0,
                    model,
                    api_name="/convert_file",
                )
            )

        try:
            timeout = float(config.get("inference_timeout", 300) or 300)
            result = await asyncio.wait_for(
                asyncio.to_thread(_run), timeout=max(10.0, timeout)
            )
            return result if result and Path(result).is_file() else ""
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] SVC conversion failed: {exc}")
            return ""

    async def _get_stt_provider(self):
        """Use plugin-configured STT first, otherwise AstrBot's STT provider."""
        custom_key = str(self.config.get("custom_stt_api_key") or "").strip()
        custom_base = str(self.config.get("custom_stt_api_base") or "").strip()
        custom_model = str(self.config.get("custom_stt_model") or "").strip()
        if (
            bool(self.config.get("custom_stt_enabled", False))
            and custom_key
            and custom_base
            and custom_model
        ):
            protocol = str(
                self.config.get("custom_stt_protocol") or "openai"
            ).strip().lower()
            if protocol in ("dashscope", "qwen"):
                from .qwen_stt import QwenSTTProvider

                return QwenSTTProvider(
                    api_key=custom_key,
                    api_base=custom_base,
                    model=custom_model,
                    ffmpeg_path=str(
                        self.config.get("custom_stt_ffmpeg_path") or ""
                    ),
                    timeout=int(self.config.get("custom_stt_timeout", 120) or 120),
                )
            from .qwen_stt import OpenAISTTProvider

            return OpenAISTTProvider(
                api_key=custom_key,
                api_base=custom_base,
                model=custom_model,
                ffmpeg_path=str(self.config.get("custom_stt_ffmpeg_path") or ""),
                timeout=int(self.config.get("custom_stt_timeout", 120) or 120),
            )

        qwen_key = str(self.config.get("qwen_stt_api_key") or "").strip()
        if bool(self.config.get("qwen_stt_enabled", False)) and qwen_key:
            from .qwen_stt import QwenSTTProvider

            return QwenSTTProvider(
                api_key=qwen_key,
                api_base=str(self.config.get("qwen_stt_api_base") or ""),
                model=str(self.config.get("qwen_stt_model") or ""),
                ffmpeg_path=str(self.config.get("qwen_stt_ffmpeg_path") or ""),
                timeout=int(self.config.get("qwen_stt_timeout", 120) or 120),
            )
        try:
            return await self.context.get_using_stt_provider_async()
        except Exception as exc:
            logger.debug(f"[SeeleDesktop] STT provider unavailable: {exc}")
            return None

    async def _get_weather_context(self) -> dict:
        """复用 AstrBot 中已经配置好的天气（陪伴面板/私有陪伴插件）。"""
        try:
            stars = self.context.get_all_stars()
        except Exception:
            return {}
        for star in stars:
            instance = getattr(star, "star_cls", None)
            if instance is None:
                continue
            ensure = getattr(instance, "_ensure_weather_context", None)
            if callable(ensure):
                try:
                    value = await ensure()
                    if isinstance(value, dict) and value.get("prompt"):
                        return value
                except Exception as exc:
                    logger.debug(f"[SeeleDesktop] weather context skipped: {exc}")
            fetch = getattr(instance, "_fetch_own_weather_prompt", None)
            if callable(fetch):
                try:
                    value = await fetch()
                    if isinstance(value, dict) and value.get("prompt"):
                        return value
                except Exception as exc:
                    logger.debug(f"[SeeleDesktop] weather fetch skipped: {exc}")
        return {}

    async def record_screen_observation(
        self,
        *,
        record_id: str,
        summary: str,
        user_id: str = "",
        metadata: dict | None = None,
    ) -> dict:
        """Write one proactive-screen observation through the memory provider.

        The method is intentionally provider-neutral; local deployments can use
        memory_companion while packaged/test deployments can use auto discovery
        or another compatible memory plugin.
        """
        return await memory_bridge.record_screen_observation(
            self.context,
            self.config,
            record_id=record_id,
            summary=summary,
            user_id=user_id,
            metadata=metadata,
        )

    async def terminate(self) -> None:
        await self.proactive_video.stop()

    def _ensure_platform(self) -> None:
        """Add the platform entry once, before PlatformManager initializes."""
        try:
            conf = self.context.get_config()
            platforms = conf.setdefault("platform", [])
            if not isinstance(platforms, list):
                logger.warning("[SeeleDesktop] platform config is not a list")
                return
            if any(
                isinstance(item, dict) and item.get("type") == "seele_desktop"
                for item in platforms
            ):
                return

            platforms.append(dict(DEFAULT_PLATFORM_CONFIG))
            conf.save_config()
            logger.info(
                "[SeeleDesktop] added platform entry seele_desktop on port 9080"
            )
        except Exception as exc:
            logger.error(f"[SeeleDesktop] failed to register platform config: {exc}")

    async def initialize(self) -> None:
        """Point the screen companion at this pet when it has no target yet."""
        self.proactive_video.start()
        await asyncio.sleep(2.0)
        platform_id = str(DEFAULT_PLATFORM_CONFIG.get("id") or "seele_desktop")
        user_id = str(DEFAULT_PLATFORM_CONFIG.get("user_id") or "master")
        try:
            for item in self.context.get_config().get("platform", []):
                if isinstance(item, dict) and item.get("type") == "seele_desktop":
                    platform_id = str(item.get("id") or platform_id)
                    user_id = str(item.get("user_id") or user_id)
                    break
        except Exception:
            pass
        target = f"{platform_id}:FriendMessage:{platform_id}!{user_id}"
        try:
            for star in self.context.get_all_stars():
                instance = getattr(star, "star_cls", None)
                if instance is None:
                    continue
                if not hasattr(instance, "proactive_target"):
                    continue
                if getattr(instance, "proactive_target", ""):
                    continue
                instance.proactive_target = target
                plugin_config = getattr(instance, "plugin_config", None)
                if plugin_config is not None and hasattr(
                    plugin_config, "proactive_target"
                ):
                    plugin_config.proactive_target = target
                logger.info(
                    f"[SeeleDesktop] screen companion proactive target -> {target}"
                )
        except Exception as exc:
            logger.debug(f"[SeeleDesktop] screen target patch skipped: {exc}")
