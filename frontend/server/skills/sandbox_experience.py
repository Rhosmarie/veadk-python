# Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd. and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SkillSpace-backed Skill Sandbox sessions for Studio Skill experience."""

from __future__ import annotations

import asyncio
import io
import json
import re
import secrets
import shlex
import time
import unicodedata
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
import requests

from frontend.server.sandbox_remote import SandboxRemoteTransport
from veadk.cli.frontend_sandbox import (
    SandboxConversationService,
    SandboxError,
    SandboxSessionUnavailableError,
    SandboxValidationError,
    _require_session_access,
)


OwnerResolver = Callable[[Any], str]
CreatorResolver = Callable[[Any], str]


class MaterializeSkill(Protocol):
    def __call__(
        self,
        space_id: str,
        skill_id: str,
        version: str | None,
        region: str | None,
        *,
        skill_space_name: str | None = None,
        skill_name: str | None = None,
    ) -> Awaitable[str | list[Any]]: ...


def _slug(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or fallback)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", normalized).strip(".-")
    return (slug or fallback or "skill")[:64]


def _archive(
    materialized: str | list[Any], *, skill_id: str, skill_name: str
) -> tuple[bytes, str]:
    folder = _slug(skill_name, skill_id)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        if isinstance(materialized, str):
            archive.writestr(f"{folder}/SKILL.md", materialized)
        else:
            wrote = False
            for item in materialized:
                raw_path = str(getattr(item, "path", "") or "").strip()
                content = getattr(item, "content", None)
                if not raw_path or not isinstance(content, str):
                    continue
                path = Path(raw_path)
                if path.is_absolute() or ".." in path.parts:
                    continue
                archive.writestr(path.as_posix(), content)
                wrote = True
            if not wrote:
                raise HTTPException(
                    status_code=422,
                    detail="Skill 包没有可安装的文本文件。",
                )
    return output.getvalue(), folder


_INSTALL_SCRIPT = r"""
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import zipfile

archive_path = sys.argv[1]
requested_folder = sys.argv[2]
folder = re.sub(r"[^A-Za-z0-9_.-]+", "-", requested_folder).strip(".-")[:64] or "skill"
skills_root = os.environ.get("VEADK_LOCAL_SKILLS_PATH") or "/home/gem/veadk_skills/my_skills"
target = os.path.join(skills_root, folder)
tmp = os.path.join(os.path.dirname(archive_path), "extract")
if os.path.exists(tmp):
    shutil.rmtree(tmp)
os.makedirs(tmp, exist_ok=True)
with zipfile.ZipFile(archive_path) as archive:
    members = [info for info in archive.infolist() if not info.is_dir()]
    if not members:
        raise RuntimeError("Skill ZIP is empty")
    for info in members:
        name = info.filename.replace("\\", "/")
        normalized = posixpath.normpath(name)
        if (
            normalized.startswith("../")
            or normalized.startswith("/")
            or normalized in {"", "."}
            or "/../" in f"/{normalized}/"
        ):
            raise RuntimeError("Skill ZIP contains an unsafe path")
        mode = (info.external_attr >> 16) & 0o777777
        if (
            stat.S_ISLNK(mode)
            or stat.S_ISCHR(mode)
            or stat.S_ISBLK(mode)
            or stat.S_ISFIFO(mode)
            or stat.S_ISSOCK(mode)
        ):
            raise RuntimeError("Skill ZIP contains unsupported file types")
        destination = os.path.abspath(os.path.join(tmp, normalized))
        root = os.path.abspath(tmp)
        if not destination.startswith(root + os.sep):
            raise RuntimeError("Skill ZIP escapes the extraction root")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with archive.open(info) as source, open(destination, "wb") as output:
            shutil.copyfileobj(source, output)
skill_roots = []
for current, _dirs, files in os.walk(tmp):
    if "SKILL.md" in files:
        skill_roots.append(current)
if not skill_roots:
    raise RuntimeError("Skill ZIP has no SKILL.md")
skill_root = sorted(skill_roots, key=lambda value: (value.count(os.sep), value))[0]
os.makedirs(skills_root, exist_ok=True)
if os.path.exists(target):
    shutil.rmtree(target)
shutil.copytree(skill_root, target)
manifest = os.path.join(target, "SKILL.md")
if not os.path.isfile(manifest):
    raise RuntimeError("Installed Skill is missing SKILL.md")
print(json.dumps({"installed": True, "skillDir": target, "manifest": manifest}, ensure_ascii=False))
""".strip()

_A2A_TERMINAL_STATES = {
    "completed",
    "failed",
    "canceled",
    "cancelled",
    "rejected",
    "input-required",
    "auth-required",
}
_A2A_READY_RETRIES = 12
_A2A_READY_DELAY_SECONDS = 5.0
_A2A_POLL_RETRIES = 120
_A2A_POLL_DELAY_SECONDS = 1.0


async def _install(
    endpoint: str, archive_content: bytes, folder: str
) -> dict[str, object]:
    remote = SandboxRemoteTransport(endpoint)
    staging = f"/home/gem/.veadk-skill-experience/{secrets.token_hex(8)}"
    archive_path = f"{staging}/skill.zip"
    await remote.exec_text(f"mkdir -p {shlex.quote(staging)}", timeout=12)
    await remote.upload(archive_path, archive_content, media_type="application/zip")
    result = await remote.exec_json(
        "python3 -c "
        + shlex.quote(_INSTALL_SCRIPT)
        + " "
        + shlex.quote(archive_path)
        + " "
        + shlex.quote(folder),
        timeout=60,
    )
    if result.get("installed") is not True:
        raise HTTPException(status_code=502, detail="Skill 安装到沙箱失败。")
    return result


def _public_session(session: Any, owner_id: str) -> dict[str, object]:
    return {
        "sessionId": session.instance_id,
        "userSessionId": session.user_session_id,
        "status": session.status,
        "createdAt": session.created_at,
        "expireAt": session.expire_at,
        "toolType": session.tool_type,
        "createdBy": session.creator_name or session.created_by,
        "region": session.region,
        "isMine": bool(owner_id and session.created_by == owner_id),
        "displayName": session.display_name,
        "persistent": session.persistent,
    }


def _extract_text(value: object) -> str:
    parts: list[str] = []

    def walk(item: object) -> None:
        if isinstance(item, dict):
            kind = item.get("kind") or item.get("type")
            text = item.get("text")
            if (
                isinstance(text, str)
                and text.strip()
                and (kind in {None, "text"} or "text" in item)
            ):
                parts.append(text)
            for key in ("parts", "artifacts", "messages", "history"):
                child = item.get(key)
                if isinstance(child, list):
                    for entry in child:
                        walk(entry)
        elif isinstance(item, list):
            for entry in item:
                walk(entry)

    walk(value)
    return "\n".join(dict.fromkeys(part.strip() for part in parts if part.strip()))


def _task_id(task: object) -> str:
    if not isinstance(task, dict):
        return ""
    for key in ("id", "taskId"):
        value = task.get(key)
        if isinstance(value, str) and value:
            return value
    nested = task.get("task")
    if isinstance(nested, dict):
        return _task_id(nested)
    return ""


def _task_state(task: object) -> str:
    if not isinstance(task, dict):
        return ""
    status = task.get("status")
    if isinstance(status, dict):
        state = status.get("state")
        if isinstance(state, str):
            return state.lower()
    state = task.get("state")
    return state.lower() if isinstance(state, str) else ""


def _post_a2a(
    endpoint: str, payload: dict[str, object], *, timeout: int
) -> dict[str, object]:
    url = urljoin(endpoint.rstrip("/") + "/", "a2a")
    last_error: Exception | None = None
    for attempt in range(_A2A_READY_RETRIES):
        try:
            response = requests.post(url, json=payload, timeout=(5, timeout))
            if (
                response.status_code in {502, 503, 504}
                and attempt + 1 < _A2A_READY_RETRIES
            ):
                last_error = RuntimeError(response.text[:500])
                time_to_sleep = _A2A_READY_DELAY_SECONDS
            else:
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise RuntimeError("A2A response is not a JSON object")
                if data.get("error"):
                    raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
                return data
        except Exception as error:
            last_error = error
            if attempt + 1 >= _A2A_READY_RETRIES:
                break
            time_to_sleep = _A2A_READY_DELAY_SECONDS
        time.sleep(time_to_sleep)
    raise RuntimeError(f"Skill Sandbox A2A request failed: {last_error}")


async def _run_a2a(endpoint: str, prompt: str) -> str:
    request_id = secrets.token_hex(8)
    message_id = secrets.token_hex(16)
    start = await asyncio.to_thread(
        _post_a2a,
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "messageId": message_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": prompt}],
                },
                "configuration": {"blocking": False, "historyLength": 20},
            },
        },
        timeout=60,
    )
    task = start.get("result")
    task_id = _task_id(task)
    if not task_id:
        return _extract_text(task) or json.dumps(task, ensure_ascii=False)
    last_task: object = task
    for _ in range(_A2A_POLL_RETRIES):
        state = _task_state(last_task)
        if state in _A2A_TERMINAL_STATES:
            break
        await asyncio.sleep(_A2A_POLL_DELAY_SECONDS)
        polled = await asyncio.to_thread(
            _post_a2a,
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": secrets.token_hex(8),
                "method": "tasks/get",
                "params": {"id": task_id, "historyLength": 20},
            },
            timeout=60,
        )
        last_task = polled.get("result")
    text = _extract_text(last_task)
    if text:
        return text
    return json.dumps(last_task, ensure_ascii=False, indent=2)


def mount_skill_sandbox_experience_routes(
    app: Any,
    service: SandboxConversationService,
    owner_resolver: OwnerResolver,
    creator_resolver: CreatorResolver,
    materialize_skill: MaterializeSkill,
) -> None:
    """Mount Skill-specific session creation with server-side installation."""

    @app.post("/web/skills/sandbox/sessions")
    async def _start_skill_experience_session(request: Request):
        owner_id = owner_resolver(request)
        creator_name = creator_resolver(request)
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=422, detail="请求必须是 JSON 对象。")
        skill_space_id = str(data.get("skillSpaceId") or "").strip()
        skill_id = str(data.get("skillId") or "").strip()
        version = str(data.get("version") or "").strip() or None
        region = str(data.get("region") or "").strip() or None
        skill_name = str(data.get("skillName") or "").strip() or skill_id
        skill_space_name = str(data.get("skillSpaceName") or "").strip() or None
        display_name = (
            str(data.get("displayName") or "").strip() or f"{skill_name} 体验"
        )
        if not skill_space_id or not skill_id:
            raise HTTPException(status_code=422, detail="缺少 Skill 标识。")

        session = None
        try:
            materialized = await materialize_skill(
                skill_space_id,
                skill_id,
                version,
                region,
                skill_space_name=skill_space_name,
                skill_name=skill_name,
            )
            archive_content, folder = _archive(
                materialized,
                skill_id=skill_id,
                skill_name=skill_name,
            )
            session = await service.create(owner_id, display_name, creator_name, False)
            if not session.endpoint:
                raise HTTPException(status_code=503, detail="Skill 沙箱尚未就绪。")
            installed = await _install(session.endpoint, archive_content, folder)
            payload = _public_session(session, owner_id)
            payload["installedSkill"] = {
                "skillId": skill_id,
                "skillName": skill_name,
                "version": version or "",
                "skillDir": installed.get("skillDir", ""),
            }
            return payload
        except Exception as error:
            if session is not None:
                try:
                    await service.delete(session.instance_id, owner_id, is_admin=True)
                except Exception:
                    pass
            if isinstance(error, HTTPException):
                raise
            raise HTTPException(
                status_code=502,
                detail=f"Skill 安装到沙箱失败：{error}",
            ) from error

    @app.post("/web/skills/sandbox/sessions/{session_id}/messages")
    async def _send_skill_experience_message(
        session_id: str,
        request: Request,
    ) -> StreamingResponse:
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise SandboxValidationError("请求必须是 JSON 对象。")
            prompt = data.get("message")
            if not isinstance(prompt, str) or not prompt.strip():
                raise SandboxValidationError("message must not be empty")
            if len(prompt) > 100_000:
                raise SandboxValidationError("message is too large")
            owner_id = owner_resolver(request)
            cloud = await service._cloud_session(session_id)  # pyright: ignore[reportPrivateUsage]
            _require_session_access(cloud, owner_id, is_admin=False)
            if cloud.status.lower() != "ready" or not cloud.endpoint:
                status = cloud.status or "Unknown"
                raise SandboxSessionUnavailableError(
                    f"Skill Sandbox Session 尚未就绪，当前状态：{status}。"
                )
            endpoint = cloud.endpoint
            prompt = prompt.strip()
        except SandboxError as error:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                },
            ) from error

        async def stream():
            try:
                text = await _run_a2a(endpoint, prompt)
                yield (
                    "event: delta\n"
                    f"data: {json.dumps({'text': text}, ensure_ascii=False)}\n\n"
                )
                yield "event: done\ndata: {}\n\n"
            except Exception as error:
                payload = {"message": f"Skill Sandbox 对话失败：{error}"}
                yield (
                    f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
                yield 'event: done\ndata: {"reason": "failed"}\n\n'

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
