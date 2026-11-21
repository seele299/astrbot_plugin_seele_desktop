"""Inbox-based proactive video ingestion for Seele desktop.

The desktop (or an external screen plugin) owns the source file.  This module
only reads it, optionally transcribes/transcodes it with ffmpeg, calls the
configured video model, writes a memory through the provider-neutral bridge,
and leaves an ack.  Source files are never deleted or moved.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

VIDEO_EXTS = {".webm", ".mp4", ".mkv", ".mov"}
DEFAULT_PROMPT = (
    "这是桌宠主动观察到的电脑屏幕录像。请用简洁中文说明用户正在做什么，"
    "提取值得记住的偏好、习惯、情绪或事件；不要朗读密码、聊天内容、"
    "身份证、银行卡等敏感信息。"
)


class ProactiveVideoInbox:
    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.context = getattr(plugin, "context", None)
        self.config = getattr(plugin, "config", {}) or {}
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._seen: dict[str, tuple[int, int]] = {}
        self._stable: dict[str, int] = {}
        self._processed: set[str] = set()

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("proactive_video_enabled", False))

    @property
    def inbox(self) -> Path:
        raw = str(self._cfg("proactive_video_inbox", "") or "").strip()
        if raw:
            return Path(raw).expanduser()
        data_dir = Path(__file__).resolve().parent.parent.parent
        return data_dir / "seele_record_inbox"

    @property
    def ack_dir(self) -> Path:
        return self.inbox / "ack"

    def start(self) -> None:
        if not self.enabled or self._task and not self._task.done():
            return
        self._cleanup_tmp_dir()
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="seele.proactive_video")

    async def stop(self) -> None:
        self._stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        interval = max(2, int(self._cfg("proactive_video_poll_seconds", 15) or 15))
        logger.info(f"[SeeleDesktop] proactive video inbox: {self.inbox}")
        while not self._stopping:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[SeeleDesktop] proactive video scan failed: {exc}")
            await asyncio.sleep(interval)

    async def _scan_once(self) -> None:
        if not self.enabled:
            return
        root = self.inbox
        try:
            root.mkdir(parents=True, exist_ok=True)
            self.ack_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] cannot create video inbox: {exc}")
            return

        videos = []
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
                continue
            if path.name.startswith("."):
                continue
            videos.append(path)

        for path in videos:
            record_id = self._record_id(path)
            if record_id in self._processed:
                continue
            ack = self.ack_dir / f"{record_id}.json"
            if ack.is_file():
                self._processed.add(record_id)
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"
            last = self._seen.get(str(path))
            self._seen[str(path)] = (stat.st_size, stat.st_mtime_ns)
            if last != (stat.st_size, stat.st_mtime_ns):
                self._stable[str(path)] = 0
                continue
            self._stable[str(path)] = self._stable.get(str(path), 0) + 1
            # Require the file to be unchanged across two polls and at least 2s old.
            if self._stable[str(path)] < 2 or time.time() - stat.st_mtime < 2:
                continue

            await self._process(path, record_id)
            self._processed.add(record_id)
            self._stable.pop(str(path), None)

    def _record_id(self, path: Path) -> str:
        # Manifest may supply an ID; otherwise derive a stable ID from file identity.
        manifest = path.with_suffix(".json")
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
                value = str(data.get("recordId") or data.get("record_id") or "").strip()
                if value:
                    return self._safe_id(value)
            except Exception:
                pass
        digest = hashlib.sha1(
            f"{path.name}:{path.stat().st_size}:{path.stat().st_mtime_ns}".encode(
                "utf-8", "ignore"
            )
        ).hexdigest()
        return f"scr_{digest[:24]}"

    @staticmethod
    def _safe_id(value: str) -> str:
        cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_.")[:80]
        return cleaned or f"scr_{int(time.time())}"

    def _manifest(self, path: Path) -> dict[str, Any]:
        manifest = path.with_suffix(".json")
        try:
            if manifest.is_file():
                value = json.loads(manifest.read_text(encoding="utf-8-sig"))
                if isinstance(value, dict):
                    return value
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] invalid manifest {manifest}: {exc}")
        return {}

    def _tmp_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / "seele_video_tmp"

    def _cleanup_tmp_dir(self) -> None:
        """Remove generated intermediate files; inbox sources are never touched."""
        folder = self._tmp_dir()
        try:
            if not folder.is_dir():
                return
            removed = 0
            for item in folder.iterdir():
                if not item.is_file():
                    continue
                if item.suffix.lower() not in {".mp4", ".jpg", ".jpeg", ".part"}:
                    continue
                try:
                    item.unlink()
                    removed += 1
                except Exception as exc:
                    logger.warning(f"[SeeleDesktop] temp cleanup failed: {item}: {exc}")
            if removed:
                logger.info(f"[SeeleDesktop] cleaned {removed} temporary video files")
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] temp cleanup skipped: {exc}")

    def _which(self, name: str) -> str:
        configured = str(self._cfg(f"proactive_video_{name}_path", "") or "").strip()
        if configured and Path(configured).is_file():
            return configured
        return shutil.which(name) or ""

    def _probe(self, path: Path, ffprobe: str) -> dict[str, Any]:
        if not ffprobe:
            return {}
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration,format_name",
                    "-show_entries",
                    "stream=codec_type,codec_name,width,height",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return {}
            data = json.loads(result.stdout or "{}")
            fmt = data.get("format") or {}
            streams = data.get("streams") or []
            video = next((s for s in streams if s.get("codec_type") == "video"), {})
            audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
            return {
                "duration": float(fmt.get("duration") or 0),
                "format": str(fmt.get("format_name") or ""),
                "codec": str(video.get("codec_name") or ""),
                "width": int(video.get("width") or 0),
                "height": int(video.get("height") or 0),
                "has_audio": bool(audio),
            }
        except Exception:
            return {}

    def _transcode(
        self,
        source: Path,
        ffmpeg: str,
        probe: dict[str, Any],
        budget_mb: float | None = None,
    ) -> Path:
        tmp_dir = self._tmp_dir()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_dir / f"{source.stem}_seele.mp4"
        duration = max(0.5, float(probe.get("duration") or 0))
        max_mb = max(1, min(100, int(self._cfg("proactive_video_max_mb", 30) or 30)))
        if budget_mb is not None:
            # Do not let a small source balloon past the direct-frame threshold.
            max_mb = min(max_mb, max(1.0, float(budget_mb)))
        # Keep audio separate from the video budget where possible.
        audio_kbps = 128 if probe.get("has_audio") else 0
        total_kbps = int((max_mb * 8 * 1024 * 1024) / duration / 1000)
        video_kbps = max(300, total_kbps - audio_kbps)
        for attempt in range(3):
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-b:v",
                f"{video_kbps}k",
                "-pix_fmt",
                "yuv420p",
            ]
            if probe.get("has_audio"):
                cmd += ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
            else:
                cmd += ["-an"]
            cmd += ["-movflags", "+faststart", str(target)]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180
            )
            if result.returncode == 0 and target.is_file():
                if target.stat().st_size <= max_mb * 1024 * 1024 or attempt == 2:
                    return target
            video_kbps = max(250, int(video_kbps * 0.75))
        raise RuntimeError("ffmpeg 转码失败")

    def _analysis_content(self, video_b64: str, prompt: str) -> list:
        fmt = str(self._cfg("proactive_video_format", "qwen") or "qwen").lower()
        fps = max(1, int(self._cfg("proactive_video_fps", 1) or 1))
        if fmt == "gemini":
            return [
                {"type": "image_url", "image_url": {"url": f"data:video/mp4;base64,{video_b64}"}},
                {"type": "text", "text": prompt},
            ]
        return [
            {
                "type": "video_url",
                "video_url": {"url": f"data:video/mp4;base64,{video_b64}"},
                "fps": fps,
            },
            {"type": "text", "text": prompt},
        ]

    def _extract_fallback_frame(self, source: Path, ffmpeg: str) -> Path | None:
        """Extract one representative JPEG for image-model fallback."""
        if not ffmpeg:
            return None
        tmp_dir = self._tmp_dir()
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            target = tmp_dir / f"{source.stem}_fallback.jpg"
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    "1",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0 and target.is_file() and target.stat().st_size > 1024:
                return target
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] fallback frame failed: {exc}")
        return None

    async def _analyze_frame(self, frame: Path, prompt: str, client: Any, model: str) -> str:
        data = base64.b64encode(frame.read_bytes()).decode()
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{data}"
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt + "\n（这是从录像中抽取的单帧，请只依据该帧回答。）",
                        },
                    ],
                }
            ],
            max_tokens=700,
        )
        text = ""
        if resp.choices:
            text = str(resp.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("降级单帧模型返回为空")
        return text

    async def _analyze(
        self,
        path: Path,
        *,
        force_frame: bool = False,
        source_size_mb: float | None = None,
    ) -> tuple[str, str]:
        """Analyze video; large source clips go directly to one-frame fallback."""
        api_key = str(self._cfg("proactive_video_api_key", "") or "").strip()
        base_url = str(self._cfg("proactive_video_api_base", "") or "").strip()
        model = str(self._cfg("proactive_video_model", "") or "").strip()
        if not api_key or not model:
            raise RuntimeError("视频模型 API Key 或模型未配置")
        try:
            from openai import AsyncOpenAI
        except Exception as exc:
            raise RuntimeError(f"openai 依赖不可用: {exc}") from exc

        prompt = str(self._cfg("proactive_video_prompt", DEFAULT_PROMPT) or DEFAULT_PROMPT)
        timeout = max(10, int(self._cfg("proactive_video_timeout", 90) or 90))
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
            max_retries=0,
        )
        ffmpeg = self._which("ffmpeg")
        direct_frame_mb = max(
            0.5,
            float(self._cfg("proactive_video_direct_frame_mb", 3) or 3),
        )
        size_mb = (
            float(source_size_mb)
            if source_size_mb is not None
            else (path.stat().st_size / (1024 * 1024))
        )
        if force_frame or size_mb > direct_frame_mb:
            frame = self._extract_fallback_frame(path, ffmpeg)
            if frame is not None:
                try:
                    text = await self._analyze_frame(frame, prompt, client, model)
                    logger.info(
                        f"[SeeleDesktop] direct frame analysis size={size_mb:.1f}MB "
                        f"threshold={direct_frame_mb}MB"
                    )
                    return text, "frame_fallback_direct"
                except Exception as exc:
                    logger.warning(
                        f"[SeeleDesktop] direct frame analysis failed, trying video: {exc}"
                    )

        video_error = ""
        try:
            data = base64.b64encode(path.read_bytes()).decode()
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": self._analysis_content(data, prompt),
                    }
                ],
                max_tokens=700,
            )
            text = ""
            if resp.choices:
                text = str(resp.choices[0].message.content or "").strip()
            if not text:
                raise RuntimeError("视频模型返回为空")
            return text, "video"
        except Exception as exc:
            video_error = str(exc)
            logger.warning(
                f"[SeeleDesktop] video analysis failed, trying frame fallback: {exc}"
            )

        frame = self._extract_fallback_frame(path, ffmpeg)
        if frame is None:
            raise RuntimeError(f"视频分析失败且无法生成降级单帧: {video_error}")
        try:
            text = await self._analyze_frame(frame, prompt, client, model)
            return text, "frame_fallback"
        except Exception as exc:
            raise RuntimeError(
                f"视频分析失败且单帧降级失败: video={video_error}; frame={exc}"
            ) from exc

    async def _write_ack(self, record_id: str, payload: dict[str, Any]) -> None:
        try:
            self.ack_dir.mkdir(parents=True, exist_ok=True)
            target = self.ack_dir / f"{record_id}.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(target)
        except Exception as exc:
            logger.warning(f"[SeeleDesktop] write ack failed: {exc}")

    async def _process(self, path: Path, record_id: str) -> None:
        logger.info(f"[SeeleDesktop] processing proactive video: {path.name}")
        manifest = self._manifest(path)
        ffprobe = self._which("ffprobe")
        ffmpeg = self._which("ffmpeg")
        result: dict[str, Any] = {
            "recordId": record_id,
            "ok": False,
            "remembered": False,
            "memoryId": "",
            "analysis": "",
            "error": "",
        }
        temporary: list[Path] = []
        try:
            probe = self._probe(path, ffprobe)
            if not probe and ffprobe:
                raise RuntimeError("ffprobe 无法读取视频")
            original_size_mb = path.stat().st_size / (1024 * 1024)
            direct_frame_mb = max(
                0.5,
                float(self._cfg("proactive_video_direct_frame_mb", 3) or 3),
            )
            source = path
            force_frame = original_size_mb > direct_frame_mb
            if not force_frame and (
                path.suffix.lower() != ".mp4"
                or path.stat().st_size
                > int(self._cfg("proactive_video_max_mb", 30) or 30)
                * 1024
                * 1024
            ):
                if not ffmpeg:
                    raise RuntimeError("需要 ffmpeg 转码，但未配置 ffmpeg 路径")
                source = self._transcode(
                    path,
                    ffmpeg,
                    probe,
                    budget_mb=direct_frame_mb,
                )
                temporary.append(source)
            analysis, analysis_basis = await self._analyze(
                source,
                force_frame=force_frame,
                source_size_mb=original_size_mb,
            )
            result["analysis"] = analysis
            result["analysisBasis"] = analysis_basis
            memory = await self.plugin.record_screen_observation(
                record_id=record_id,
                summary=analysis,
                user_id=str(self._cfg("memory_owner_user_id", "") or ""),
                metadata={
                    "file_name": path.name,
                    "duration": probe.get("duration"),
                    "width": probe.get("width"),
                    "height": probe.get("height"),
                    "has_audio": probe.get("has_audio"),
                    "analysis_basis": analysis_basis,
                },
            )
            result.update(
                {
                    "ok": True,
                    "remembered": bool(memory.get("remembered")),
                    "memoryId": str(memory.get("memory_id") or ""),
                    "memoryError": memory.get("error_code"),
                }
            )
        except Exception as exc:
            result["error"] = str(exc)
            logger.warning(f"[SeeleDesktop] proactive video failed: {exc}")
        finally:
            for item in temporary:
                try:
                    item.unlink(missing_ok=True)
                except Exception:
                    pass
            self._cleanup_tmp_dir()
            await self._write_ack(record_id, result)
