"""Optional memory-provider bridge for Seele desktop observations.

The desktop itself never depends on a specific memory plugin.  The local
AstrBot deployment can point this at ``astrbot_plugin_memory_companion`` while
packaged/test deployments can use ``auto`` discovery, a custom provider name,
or disable memory writing entirely.
"""

from __future__ import annotations

import inspect
from typing import Any, Iterable

from astrbot.api import logger

MEMORY_TYPE = "screen_observation"
SOURCE_PLUGIN = "seele_desktop"


def _candidate_name(metadata: Any, instance: Any = None) -> str:
    parts: list[str] = []
    for obj in (metadata, instance):
        if obj is None:
            continue
        for attr in ("name", "star_name", "module_name", "root_dir_name"):
            value = getattr(obj, attr, "")
            if value:
                parts.append(str(value))
        cls = getattr(obj, "star_cls_type", None) or type(obj)
        parts.extend(
            [
                str(getattr(cls, "__name__", "") or ""),
                str(getattr(cls, "__module__", "") or ""),
            ]
        )
    return " ".join(parts).lower()


def _bridge_from_star(instance: Any) -> Any | None:
    """Find a memory bridge exposed by a registered plugin instance."""
    for attr in ("memory_companion", "memory_bridge", "bridge"):
        candidate = getattr(instance, attr, None)
        if callable(getattr(candidate, "record_external_memory", None)):
            return candidate
    if callable(getattr(instance, "record_external_memory", None)):
        return instance
    return None


def _iter_candidates(context: Any) -> Iterable[tuple[str, Any]]:
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return
    try:
        stars = getter()
    except Exception:
        return
    if not isinstance(stars, (list, tuple)):
        return
    for metadata in stars:
        instance = getattr(metadata, "star_cls", None)
        if instance is None:
            continue
        if getattr(metadata, "activated", True) is not True:
            continue
        bridge = _bridge_from_star(instance)
        if bridge is None:
            continue
        yield _candidate_name(metadata, instance), bridge


def resolve_memory_writer(context: Any, config: dict[str, Any] | None = None) -> Any | None:
    """Resolve the configured external-memory writer.

    Config keys (all optional):
      memory_provider: auto | memory_companion | custom | none
      memory_plugin: plugin-name fragment used by ``custom``
    """
    cfg = config or {}
    provider = str(cfg.get("memory_provider") or "auto").strip().lower()
    if provider in {"none", "off", "disabled"}:
        return None

    candidates = list(_iter_candidates(context))
    if not candidates:
        return None

    if provider == "memory_companion":
        for name, bridge in candidates:
            if "memory_companion" in name:
                return bridge
        return None

    if provider == "custom":
        wanted = str(cfg.get("memory_plugin") or "").strip().lower()
        if not wanted:
            return None
        for name, bridge in candidates:
            if wanted in name:
                return bridge
        return None

    # auto: prefer memory_companion, otherwise use the first compatible bridge
    for name, bridge in candidates:
        if "memory_companion" in name:
            return bridge
    return candidates[0][1]


def _call_kwargs(method: Any, values: dict[str, Any]) -> dict[str, Any]:
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return values
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return values
    return {key: value for key, value in values.items() if key in params}


async def record_screen_observation(
    context: Any,
    config: dict[str, Any] | None,
    *,
    record_id: str,
    summary: str,
    user_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one screen-observation memory through the configured provider.

    Returns a normalized result.  A missing provider is an expected runtime
    state, not an exception, so callers can keep the source video and write an
    ack without crashing.
    """
    cfg = config or {}
    provider = str(cfg.get("memory_provider") or "auto").strip().lower()
    if provider in {"none", "off", "disabled"}:
        return {
            "ok": False,
            "remembered": False,
            "memory_id": "",
            "deduplicated": False,
            "error_code": "memory_provider_disabled",
        }

    bridge = resolve_memory_writer(context, cfg)
    if bridge is None:
        return {
            "ok": False,
            "remembered": False,
            "memory_id": "",
            "deduplicated": False,
            "error_code": "memory_provider_unavailable",
        }

    user_id = str(user_id or cfg.get("memory_owner_user_id") or "").strip()
    if not user_id:
        return {
            "ok": False,
            "remembered": False,
            "memory_id": "",
            "deduplicated": False,
            "error_code": "memory_user_id_missing",
        }

    record_id = str(record_id or "").strip()
    summary = str(summary or "").strip()
    if not record_id or not summary:
        return {
            "ok": False,
            "remembered": False,
            "memory_id": "",
            "deduplicated": False,
            "error_code": "record_id_or_summary_missing",
        }

    meta = {
        "record_id": record_id,
        "source": "seele_desktop",
        "kind": "screen_observation",
    }
    if metadata:
        meta.update(metadata)

    values = {
        "user_id": user_id,
        "content": summary,
        "summary": summary,
        "memory_type": MEMORY_TYPE,
        "source_plugin": SOURCE_PLUGIN,
        "idempotency_key": f"screen_record:{record_id}",
        "durability": "short",
        "metadata": meta,
        "long_term": True,
    }
    try:
        result = await bridge.record_external_memory(**_call_kwargs(
            bridge.record_external_memory, values
        ))
    except Exception as exc:
        logger.warning(f"[SeeleDesktop] memory write failed: {exc}")
        return {
            "ok": False,
            "remembered": False,
            "memory_id": "",
            "deduplicated": False,
            "error_code": "memory_write_exception",
            "error": str(exc),
        }

    if not isinstance(result, dict):
        return {
            "ok": bool(result),
            "remembered": bool(result),
            "memory_id": "",
            "deduplicated": False,
            "error_code": None if result else "invalid_memory_result",
        }

    ok = bool(result.get("ok"))
    memory_id = str(result.get("memory_id") or "")
    return {
        "ok": ok,
        "remembered": ok and bool(memory_id),
        "memory_id": memory_id,
        "deduplicated": bool(result.get("deduplicated")),
        "error_code": result.get("error_code"),
        "raw": result,
    }
