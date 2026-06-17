"""
工作区保存接口

保存用户在浏览器内完成的编辑状态，避免刷新、重新登录或换设备后丢失。
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
WORKSPACES_DIR = DATA_DIR / "workspaces"

router = APIRouter(prefix="/api/workspace", tags=["工作区"])


class WorkspacePayload(BaseModel):
    state: dict[str, Any] = Field(default_factory=dict)
    ui_state: dict[str, Any] = Field(default_factory=dict)
    source_state: dict[str, Any] = Field(default_factory=dict)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    candidate_meta: dict[str, Any] = Field(default_factory=dict)
    client_updated_at: str = ""


def _safe_workspace_id(workspace_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", workspace_id):
        raise HTTPException(400, "工作区 ID 只能包含字母、数字、下划线和短横线")
    return workspace_id


def _workspace_path(workspace_id: str) -> Path:
    return WORKSPACES_DIR / f"{_safe_workspace_id(workspace_id)}.json"


@router.get("/{workspace_id}")
def get_workspace(workspace_id: str):
    path = _workspace_path(workspace_id)
    if not path.exists():
        return {"success": True, "exists": False, "workspace": None}

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return {
        "success": True,
        "exists": True,
        "workspace": payload,
        "updated_at": payload.get("updated_at", ""),
    }


@router.post("/{workspace_id}")
def save_workspace(workspace_id: str, payload: WorkspacePayload):
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    data = payload.model_dump(mode="json")
    data["workspace_id"] = _safe_workspace_id(workspace_id)
    data["updated_at"] = now

    path = _workspace_path(workspace_id)
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)

    return {
        "success": True,
        "workspace_id": workspace_id,
        "updated_at": now,
        "path": str(path),
    }
