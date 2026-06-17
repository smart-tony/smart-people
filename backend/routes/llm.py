"""
LLM 路由 — Phase 2
POST /api/llm/process   → 发送原文给 DeepSeek，返回结构化条目
POST /api/llm/batch     → 批量处理多条
"""

import json
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from openai import OpenAI

from settings import load_config, load_llm_config

router = APIRouter(prefix="/api/llm", tags=["LLM"])
VALID_TASK_TYPES = (
    "ai-weekly",
    "global-logistics-risk",
    "cn-logistics-industry",
    "exchange-rate",
    # 别名：sources.config.json 模块名，自动映射到对应 prompt
    "logistics-daily",
    "weather-alert",
)

# 别名映射：将 sources.config 模块名转为标准 prompt key
TASK_TYPE_ALIASES: dict[str, str] = {
    "logistics-daily": "cn-logistics-industry",
    "weather-alert": "global-logistics-risk",
}


# ── 数据模型 ──────────────────────────────────────────────

class ProcessRequest(BaseModel):
    raw_text: str = Field(..., description="原始文章内容（HTML 或纯文本）")
    task_type: str = Field(
        default="ai-weekly",
        description="任务类型: ai-weekly | global-logistics-risk | cn-logistics-industry | exchange-rate"
    )
    source_url: str = Field(default="", description="原文链接")

class ProcessResponse(BaseModel):
    success: bool
    task_type: str
    result: dict = Field(default_factory=dict)
    tokens_used: int = 0
    error: Optional[str] = None


def get_llm_client() -> Optional[OpenAI]:
    """初始化 OpenAI 兼容客户端"""
    llm = load_llm_config()
    api_key = llm.get("api_key", "")
    if not api_key or api_key == "***":
        return None
    return OpenAI(
        api_key=api_key,
        base_url=llm.get("base_url", "https://api.deepseek.com"),
    )


def process_article_impl(req: ProcessRequest) -> ProcessResponse:
    """核心处理逻辑，供单条和批量接口复用。"""
    prompts = load_config("prompts.config.json")
    llm_config = load_llm_config()

    if req.task_type not in VALID_TASK_TYPES:
        raise HTTPException(400, f"未知任务类型: {req.task_type}，可用: {list(VALID_TASK_TYPES)}")

    # 别名映射：logistics-daily → cn-logistics-industry, weather-alert → global-logistics-risk
    resolved_task_type = TASK_TYPE_ALIASES.get(req.task_type, req.task_type)

    task_prompt = prompts.get(resolved_task_type)
    if not task_prompt:
        raise HTTPException(500, f"prompts.config.json 缺少任务配置: {req.task_type}")

    client = get_llm_client()
    if not client:
        return ProcessResponse(
            success=False,
            task_type=req.task_type,
            error="未配置 LLM API Key。请设置 LLM_API_KEY 或在 config/llm.config.json 中填入 api_key。",
        )

    system_prompt = task_prompt["system"]
    user_prompt = task_prompt["user_template"].replace("{raw_text}", req.raw_text)

    overrides = llm_config.get("task_overrides", {})
    task_overrides = overrides.get(req.task_type, {})
    temperature = task_overrides.get(
        "temperature",
        llm_config.get("temperature", 0.3),
    )

    try:
        response = client.chat.completions.create(
            model=llm_config.get("model", "deepseek-chat"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=llm_config.get("max_tokens", 2048),
        )
    except Exception as e:
        return ProcessResponse(
            success=False,
            task_type=req.task_type,
            error=f"LLM 调用失败: {str(e)}",
        )

    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content.rsplit("```", 1)[0]

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return ProcessResponse(
            success=False,
            task_type=req.task_type,
            error=f"LLM 返回的不是合法 JSON。原始内容: {content[:300]}",
        )

    if isinstance(result, list):
        dict_items = [item for item in result if isinstance(item, dict)]
        if dict_items:
            result = dict_items[0]
        else:
            return ProcessResponse(
                success=False,
                task_type=req.task_type,
                error=f"LLM 返回的是数组，但里面没有可用的条目对象。原始内容: {content[:300]}",
            )

    if not isinstance(result, dict):
        return ProcessResponse(
            success=False,
            task_type=req.task_type,
            error=f"LLM 返回的 JSON 不是条目对象。原始内容: {content[:300]}",
        )

    if req.source_url and not result.get("source_url"):
        result["source_url"] = req.source_url

    return ProcessResponse(
        success=True,
        task_type=req.task_type,
        result=result,
        tokens_used=response.usage.total_tokens if response.usage else 0,
    )


# ── API ───────────────────────────────────────────────────

@router.post("/process", response_model=ProcessResponse)
def process_article(req: ProcessRequest):
    """
    将一篇原始文章交给 LLM 处理，返回结构化条目。
    
    task_type 决定了用哪套 system prompt：
    - ai-weekly          → AI 技术周报条目
    - global-logistics-risk → 国际物流风险分析
    - cn-logistics-industry → 中国跨境物流行业分析
    - exchange-rate      → 汇率分析
    """
    return process_article_impl(req)


@router.post("/batch")
async def batch_process(requests: list[ProcessRequest]):
    """批量处理（并行），单条失败不会中断整批。"""

    async def run_one(req: ProcessRequest) -> ProcessResponse:
        try:
            return await run_in_threadpool(process_article_impl, req)
        except HTTPException as exc:
            return ProcessResponse(
                success=False,
                task_type=req.task_type,
                error=str(exc.detail),
            )

    results = await asyncio.gather(*(run_one(req) for req in requests))
    return {"count": len(results), "results": [r.model_dump() for r in results]}
