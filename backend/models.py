"""
weekly-push-tool 数据模型
借鉴 Horizon 的 ContentItem 设计，统一抓取 → AI处理 → 过滤的数据结构
"""

from datetime import datetime, timezone
from typing import Optional, List
from pydantic import BaseModel, Field


class RawArticle(BaseModel):
    """抓取到的原始文章"""
    title: str
    url: str
    source_id: str = ""                         # 来源 ID，如 "kjdsnews"
    source_name: str = ""                       # 来源名称，如 "跨境电商新闻"
    module: str = ""                            # 所属模块: ai-weekly / logistics-daily 等
    content_snippet: Optional[str] = None       # 页面抓到的正文片段

    class Config:
        frozen = False  # 允许后续修改（如补充正文）


class CandidateItem(BaseModel):
    """经过 AI 处理的候选条目 — 对应 Horizon ContentItem 的 ai_* 字段"""
    
    # 原始信息
    title: str
    source_url: str
    source_id: str = ""
    source_name: str = ""
    
    # AI 结构化结果
    ai_summary: str = ""                # AI 摘要
    ai_analysis: str = ""               # AI 行业分析
    ai_tags: List[str] = Field(default_factory=list)
    
    # AI 打分（借鉴 Horizon）
    ai_score: float = 5.0               # 0-10，对目标业务的影响程度
    ai_reason: str = ""                 # 打分理由
    
    # 状态
    selected: bool = False              # 前端勾选状态
    processed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class FetchResult(BaseModel):
    """一次抓取+AI处理的完整结果"""
    task_type: str
    sources_used: int
    total_raw: int                      # 原始抓取条数
    after_dedup: int                    # 去重后条数
    candidates: List[CandidateItem]     # AI 处理后的候选列表
    errors: List[str] = Field(default_factory=list)
    from_cache: bool = False            # 是否因本次无新内容而回退到最近候选
    cached_count: int = 0               # 本次被 URL 缓存过滤的数量
    draft_filename: str = ""            # 回退读取的草稿文件名
