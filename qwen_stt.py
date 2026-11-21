"""Small DashScope Qwen3-ASR provider for the Seele desktop plugin.

The desktop records local WebM/WAV audio. Qwen-Audio-Filetrans only accepts
public HTTP(S) URLs and is asynchronous, so this provider uses the synchronous
Qwen3-ASR-Flash DashScope endpoint with Base64 audio instead.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
import tempfile
from pathlib import Path

import aiohttp

DEFAULT_BASE = "https://maas.qianwenaiapi.com/api/v1"
DEFAULT_MODEL = "qwen3-asr-flash"


class QwenSTTProvider:
    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = "",
        model: str = "",
        ffmpeg_path: str = "",
        timeout: int = 120,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.api_base = str(api_base or DEFAULT_BASE).strip().rstrip("/")
        self.model = str(model or DEFAULT_MODEL).strip()
        self.ffmpeg_path = str(ffmpeg_path or "").strip()
        self.timeout = max(10, int(timeout or 120))

    def _endpoint(self) -> str:
        base = self.api_base
        if not base.endswith("/api/v1"):
            base += "/api/v1"
        return base + "/services/aigc/multimodal-generation/generation"

    def _to_wav(self, audio_path: str) -> tuple[Path, Path | None]:
        source = Path(audio_path)
        if source.suffix.lower() == ".wav":
            return source, None
        ffmpeg = self.ffmpeg_path
        if not ffmpeg:
            import shutil

            ffmpeg = shutil.which("ffmpeg") or ""
        if not ffmpeg:
            raise RuntimeError("语音不是 WAV 且未配置 ffmpeg，无法转换")
        temp = Path(tempfile.gettempdir()) / "seele_desktop" / "qwen_stt"
        temp.mkdir(parents=True, exist_ok=True)
        target = temp / f"{source.stem}_16k.wav"
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-ar",
                "16000",
                "-ac",
                "1",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0 or not target.exists():
            raise RuntimeError(f"ffmpeg 转 WAV 失败: {result.stderr[-300:]}")
        return target, target

    async def get_text(self, audio_path: str) -> str:
        wav, temp = await asyncio.to_thread(self._to_wav, audio_path)
        try:
            data = "data:audio/wav;base64," + base64.b64encode(
                wav.read_bytes()
            ).decode()
            body = {
                "model": self.model,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "audio", "audio": data}],
                        }
                    ]
                },
            }
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._endpoint(),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ) as response:
                    payload = await response.json(content_type=None)
                    if response.status != 200:
                        message = str(
                            payload.get("message")
                            or payload.get("error")
                            or "Qwen ASR 请求失败"
                        )
                        raise RuntimeError(
                            f"Qwen ASR HTTP {response.status}: {message[:400]}"
                        )
            choices = ((payload.get("output") or {}).get("choices") or [])
            if not choices:
                raise RuntimeError("Qwen ASR 返回为空")
            content = ((choices[0].get("message") or {}).get("content") or [])
            texts = [
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("text")
            ]
            text = "".join(texts).strip()
            if not text:
                raise RuntimeError("Qwen ASR 没有识别出文本")
            return text
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except Exception:
                    pass

class OpenAISTTProvider:
    """OpenAI-compatible /audio/transcriptions provider for other vendors."""

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        model: str,
        ffmpeg_path: str = "",
        timeout: int = 120,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.api_base = str(api_base or "").strip().rstrip("/")
        self.model = str(model or "").strip()
        self.ffmpeg_path = str(ffmpeg_path or "").strip()
        self.timeout = max(10, int(timeout or 120))

    def _endpoint(self) -> str:
        base = self.api_base
        if base.endswith("/audio/transcriptions"):
            return base
        if base.endswith("/v1"):
            return base + "/audio/transcriptions"
        return base + "/v1/audio/transcriptions"

    def _to_wav(self, audio_path: str) -> tuple[Path, Path | None]:
        source = Path(audio_path)
        if source.suffix.lower() == ".wav":
            return source, None
        ffmpeg = self.ffmpeg_path
        if not ffmpeg:
            import shutil

            ffmpeg = shutil.which("ffmpeg") or ""
        if not ffmpeg:
            raise RuntimeError("语音不是 WAV 且未配置 ffmpeg，无法转换")
        temp = Path(tempfile.gettempdir()) / "seele_desktop" / "generic_stt"
        temp.mkdir(parents=True, exist_ok=True)
        target = temp / f"{source.stem}_16k.wav"
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-ar",
                "16000",
                "-ac",
                "1",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0 or not target.exists():
            raise RuntimeError(f"ffmpeg 转 WAV 失败: {result.stderr[-300:]}")
        return target, target

    async def get_text(self, audio_path: str) -> str:
        wav, temp = await asyncio.to_thread(self._to_wav, audio_path)
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            form = aiohttp.FormData()
            form.add_field(
                "file",
                wav.read_bytes(),
                filename="audio.wav",
                content_type="audio/wav",
            )
            form.add_field("model", self.model)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._endpoint(),
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=form,
                ) as response:
                    payload = await response.json(content_type=None)
                    if response.status != 200:
                        message = str(
                            payload.get("error")
                            or payload.get("message")
                            or "STT API 请求失败"
                        )
                        raise RuntimeError(
                            f"STT API HTTP {response.status}: {message[:400]}"
                        )
            text = str(payload.get("text") or "").strip()
            if not text:
                raise RuntimeError("STT API 返回为空")
            return text
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except Exception:
                    pass

