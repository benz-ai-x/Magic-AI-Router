"""Per-provider request body normalization before forwarding to upstream.

Anthropic-compatible providers (DeepSeek, GLM, KIMI) reject certain fields
that Claude Code sends in the request body — system-as-array, document
content blocks, beta tool-schema fields. This module strips or converts
those fields so the gateway's forwarding layer is transparent to clients
while remaining compatible with every known provider.

All transformations are safe for every provider: we only remove or flatten
fields that no provider recognises. If a provider later adds support for a
field, a conditional branch can be added here.
"""
from __future__ import annotations

from typing import Any


def extract_system_text(body: dict[str, Any]) -> str:
    """Extract the ``system`` prompt as a string, regardless of format.

    Handles three shapes: plain string (returned as-is), content-block
    array (text blocks joined with newlines), and missing/None ("").
    This is the single source of truth for "read system from an
    Anthropic Messages body" — used by routing decisions and by
    ``_flatten_system`` for provider compatibility.
    """
    system = body.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for item in system:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def normalize_body(body: dict[str, Any], provider: str,
                   *, anthropic_native: bool = False) -> None:
    """Normalize request body in-place before forwarding to *provider*.

    The default path applies uniformly to all providers (no per-provider
    branching) because every transformation removes a field no known
    provider supports. ``anthropic_native=True`` opts a provider out of all
    stripping: the backend natively accepts Anthropic body shapes, so the
    ``cache_control`` markers Claude Code relies on for prompt caching
    (they live on the system content blocks), document blocks, and beta
    tool fields must survive the hop.
    """
    if anthropic_native:
        return
    _flatten_system(body)
    _strip_document_blocks(body)
    _strip_beta_tool_fields(body)


def _flatten_system(body: dict[str, Any]) -> None:
    """Convert ``system`` from content-block array to plain string.

    Claude Code v2.1.154+ sends ``system`` as an array of content blocks
    with ``cache_control`` markers::

        [{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}]

    DeepSeek rejects this format (deepseek-ai/DeepSeek-V3#1369). We flatten
    it to a plain string, preserving all text content, dropping
    ``cache_control``.
    """
    if isinstance(body.get("system"), list):
        body["system"] = extract_system_text(body)


def _strip_document_blocks(body: dict[str, Any]) -> None:
    """Remove ``document`` content blocks from messages.

    KIMI rejects ``document`` content blocks outright
    (MoonshotAI/Kimi-K2#129). We remove them from every message's
    ``content`` array. If a message would be left with empty content,
    we insert a single empty text block as a placeholder to avoid
    provider errors on empty content arrays.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        filtered = [b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "document")]
        if len(filtered) != len(content):
            if not filtered:
                filtered = [{"type": "text", "text": ""}]
            msg["content"] = filtered


def _strip_beta_tool_fields(body: dict[str, Any]) -> None:
    """Remove Anthropic beta tool-schema fields from tools.

    Claude Code 5 sends ``defer_loading`` and ``eager_input_streaming``
    on tool objects. These are Anthropic-specific beta fields whose
    compatibility with every upstream provider is unknown. We strip
    them defensively — this is the gateway-side complement to the
    client-side ``CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`` env var.
    """
    tools = body.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if isinstance(tool, dict):
            tool.pop("defer_loading", None)
            tool.pop("eager_input_streaming", None)
