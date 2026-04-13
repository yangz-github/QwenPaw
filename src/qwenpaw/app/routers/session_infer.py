# -*- coding: utf-8 -*-
"""Structured infer API for planner-like orchestrators."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...agents.model_factory import create_model_and_formatter
from ...app.agent_context import set_current_agent_id, set_current_session_id
from ...config.config import load_agent_config
from ...providers.provider_manager import ProviderManager
from ..agent_context import get_agent_for_request

logger = logging.getLogger(__name__)

PROMPT_VERSION = "qwenpaw-session-infer-v1"

router = APIRouter(prefix="/qwenpaw", tags=["qwenpaw"])


class SessionInferIntent(BaseModel):
    model_config = ConfigDict(extra="allow")

    intentCode: str = Field(default="")
    executionMode: str = Field(default="")
    description: str = Field(default="")
    roleCode: Optional[str] = None
    sqlTemplateCode: Optional[str] = None
    selectedTableId: Optional[int] = None
    slotKeys: list[str] = Field(default_factory=list)
    slotSchema: dict[str, Any] = Field(default_factory=dict)


class SessionInferRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    question: str = Field(..., min_length=1)
    traceId: str = Field(default="")
    intents: list[SessionInferIntent] = Field(default_factory=list)
    sessionId: Optional[str] = None
    conversationId: Optional[str] = None
    chatId: Optional[str] = None
    agentId: Optional[str] = None


class CandidatePlan(BaseModel):
    intentCode: str
    executionMode: str
    confidence: float
    slots: dict[str, Any] = Field(default_factory=dict)
    roleCode: Optional[str] = None
    sqlTemplateCode: Optional[str] = None
    selectedTableId: Optional[int] = None


class ModelMeta(BaseModel):
    provider: str
    model: str
    promptVersion: str
    requestId: str


class SessionInferData(BaseModel):
    candidatePlan: CandidatePlan
    modelMeta: ModelMeta


class SessionInferResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: Optional[SessionInferData] = None


def _extract_text_from_chunk(chunk: Any) -> str:
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_text_from_response(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                item_text = item.get("text")
                if isinstance(item_text, str):
                    parts.append(item_text)
        return "".join(parts)
    if isinstance(response, str):
        return response
    return ""


async def _collect_model_text(response: Any) -> str:
    if hasattr(response, "__aiter__"):
        accumulated = ""
        async for chunk in response:  # type: ignore[union-attr]
            text = _extract_text_from_chunk(chunk)
            if not text:
                continue
            # Some providers emit cumulative text on each chunk.
            if len(text) >= len(accumulated) and text.startswith(accumulated):
                accumulated = text
            else:
                accumulated += text
        return accumulated
    return _extract_text_from_response(response)


def _extract_first_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Model output is not a valid JSON object")


def _build_messages(payload: SessionInferRequest) -> list[dict[str, Any]]:
    intents_json = json.dumps(
        [intent.model_dump() for intent in payload.intents],
        ensure_ascii=False,
        indent=2,
    )
    system_prompt = (
        "You are a query planner.\n"
        "Pick exactly one intent from the provided intents.\n"
        "Return JSON only, no markdown fences, no explanation.\n"
        "JSON schema:\n"
        "{\n"
        '  "intentCode": "string, must be one of intents.intentCode",\n'
        '  "executionMode": "string",\n'
        '  "confidence": "number between 0 and 1",\n'
        '  "slots": {"any":"json object"},\n'
        '  "roleCode": "string|null",\n'
        '  "sqlTemplateCode": "string|null",\n'
        '  "selectedTableId": "number|null"\n'
        "}"
    )
    user_prompt = (
        f"traceId: {payload.traceId}\n"
        f"question: {payload.question}\n"
        f"intents: {intents_json}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _resolve_effective_model_meta(agent_id: str, trace_id: str) -> ModelMeta:
    manager = ProviderManager.get_instance()
    provider_id = "unknown"
    model_name = "unknown"
    try:
        agent_config = load_agent_config(agent_id)
        active = agent_config.active_model
        if active and active.provider_id and active.model:
            provider_id = active.provider_id
            model_name = active.model
        else:
            global_model = manager.get_active_model()
            if global_model and global_model.provider_id and global_model.model:
                provider_id = global_model.provider_id
                model_name = global_model.model
    except Exception:
        logger.debug("Failed to resolve effective model meta", exc_info=True)

    request_id = trace_id.strip() or f"qwenpaw-{uuid4().hex[:12]}"
    return ModelMeta(
        provider=provider_id,
        model=model_name,
        promptVersion=PROMPT_VERSION,
        requestId=request_id,
    )


def _build_candidate_plan(
    output_raw: dict[str, Any],
    intents: list[SessionInferIntent],
) -> CandidatePlan:
    candidate_raw = output_raw.get("candidatePlan")
    if not isinstance(candidate_raw, dict):
        candidate_raw = output_raw

    intent_map = {
        str(intent.intentCode).strip(): intent
        for intent in intents
        if str(intent.intentCode).strip()
    }
    if not intent_map:
        raise ValueError("No intents provided")

    intent_code = str(candidate_raw.get("intentCode") or "").strip()
    if not intent_code:
        raise ValueError("Missing intentCode in model output")
    if intent_code not in intent_map:
        raise ValueError(
            f"intentCode '{intent_code}' is not in provided intents",
        )

    matched = intent_map[intent_code]
    execution_mode = str(
        candidate_raw.get("executionMode") or matched.executionMode or "",
    ).strip()
    if not execution_mode:
        raise ValueError("Missing executionMode in model output")

    raw_conf = candidate_raw.get("confidence", 0.0)
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid confidence in model output") from exc
    confidence = max(0.0, min(1.0, confidence))

    slots = candidate_raw.get("slots")
    if not isinstance(slots, dict):
        slots = {}

    return CandidatePlan(
        intentCode=intent_code,
        executionMode=execution_mode,
        confidence=confidence,
        slots=slots,
        roleCode=(candidate_raw.get("roleCode") or matched.roleCode),
        sqlTemplateCode=(
            candidate_raw.get("sqlTemplateCode") or matched.sqlTemplateCode
        ),
        selectedTableId=(
            candidate_raw.get("selectedTableId") or matched.selectedTableId
        ),
    )


async def _resolve_target_agent_id(
    request: Request,
    payload: SessionInferRequest,
    header_agent_id: Optional[str],
) -> str:
    target = (
        (payload.agentId or "").strip()
        or (header_agent_id or "").strip()
        or None
    )
    workspace = await get_agent_for_request(request, agent_id=target)
    return workspace.agent_id


@router.post(
    "/session/infer",
    response_model=SessionInferResponse,
    summary="Infer candidate plan using current agent effective model",
)
async def post_session_infer(
    payload: SessionInferRequest,
    request: Request,
    x_agent_id: Optional[str] = Header(default=None, alias="X-Agent-Id"),
) -> SessionInferResponse:
    try:
        target_agent_id = await _resolve_target_agent_id(
            request=request,
            payload=payload,
            header_agent_id=x_agent_id,
        )
        set_current_agent_id(target_agent_id)
        if payload.sessionId:
            set_current_session_id(payload.sessionId.strip())

        if not payload.intents:
            return SessionInferResponse(code=1, message="No intents provided")

        model, _ = create_model_and_formatter(agent_id=target_agent_id)
        response = await model(_build_messages(payload))
        response_text = await _collect_model_text(response)
        response_json = _extract_first_json_object(response_text)

        candidate = _build_candidate_plan(response_json, payload.intents)
        model_meta = _resolve_effective_model_meta(
            target_agent_id,
            payload.traceId,
        )
        return SessionInferResponse(
            code=0,
            message="ok",
            data=SessionInferData(
                candidatePlan=candidate,
                modelMeta=model_meta,
            ),
        )
    except HTTPException as exc:
        return SessionInferResponse(code=exc.status_code, message=str(exc.detail))
    except Exception as exc:
        logger.exception("Session infer failed")
        return SessionInferResponse(code=1, message=str(exc))
