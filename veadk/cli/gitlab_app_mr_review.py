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

"""GitLab helpers for Studio MR review automation."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx

from veadk.cli.github_app_pr_review import PageRequest, PageResult, _status_code


GITLAB_DEFAULT_BASE_URL = "https://gitlab.com"
GITLAB_BASE_URL_ENV = "VEADK_GITLAB_BASE_URL"
GITLAB_TOKEN_ENV = "VEADK_GITLAB_TOKEN"
GITLAB_WEBHOOK_SECRET_ENV = "VEADK_GITLAB_WEBHOOK_SECRET"
GITLAB_GROUP_ID_OR_PATH_ENV = "VEADK_GITLAB_GROUP_ID_OR_PATH"
GITLAB_REVIEW_OWNER_ID_ENV = "VEADK_GITLAB_REVIEW_OWNER_ID"
GITLAB_REVIEW_CREATOR_ENV = "VEADK_GITLAB_REVIEW_CREATOR"
STUDIO_PUBLIC_BASE_URL_ENV = "VEADK_STUDIO_PUBLIC_BASE_URL"
GITLAB_WEBHOOK_PATH = "/web/gitlab/app/webhook"
GITLAB_REVIEW_PROJECTS_KEY = "veadk-studio/v1/gitlab-mr-review/projects.json"
GITLAB_REVIEW_HISTORY_KEY = "veadk-studio/v1/gitlab-mr-review/history.json"
_MAX_REVIEW_PROJECTS_BYTES = 128 * 1024
_MAX_REVIEW_HISTORY_BYTES = 256 * 1024
_MAX_REVIEW_HISTORY_ITEMS = 50


class GitLabAppReviewError(RuntimeError):
    """GitLab review integration failed with a user-safe message."""


class GitLabAppReviewStorageUnavailable(GitLabAppReviewError):
    """GitLab review enablement cannot be read or written."""


@dataclass(frozen=True)
class GitLabAppConfig:
    base_url: str
    token: str
    webhook_secret: str
    group_id_or_path: str = ""
    review_owner_id: str = "gitlab-app"
    review_creator_name: str = "GitLab App"
    studio_public_base_url: str = ""
    instance_id: str = "default"

    @property
    def api_root(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v4"

    @property
    def webhook_url(self) -> str:
        if not self.studio_public_base_url:
            return ""
        return f"{self.studio_public_base_url.rstrip('/')}{GITLAB_WEBHOOK_PATH}"


@dataclass(frozen=True)
class GitLabProject:
    instance_id: str
    base_url: str
    project_id: int
    path_with_namespace: str
    name: str
    namespace: str
    web_url: str
    private: bool
    permissions_note: str = ""

    def to_public_dict(self, *, review_enabled: bool) -> dict[str, object]:
        return {
            "instanceId": self.instance_id,
            "baseUrl": self.base_url,
            "projectId": self.project_id,
            "pathWithNamespace": self.path_with_namespace,
            "name": self.name,
            "namespace": self.namespace,
            "webUrl": self.web_url,
            "private": self.private,
            "reviewEnabled": review_enabled,
            "permissionsNote": self.permissions_note,
        }


@dataclass(frozen=True)
class GitLabMergeRequestEvent:
    delivery_id: str
    action: str
    instance_id: str
    base_url: str
    project_id: int
    path_with_namespace: str
    merge_request_url: str
    merge_request_iid: int
    source_project_id: int
    target_project_id: int
    draft: bool
    head_sha: str

    @property
    def should_review(self) -> bool:
        return (
            self.action in {"open", "reopen", "update"}
            and not self.draft
            and self.source_project_id == self.target_project_id == self.project_id
        )


@dataclass(frozen=True)
class GitLabMergeRequestReviewRecord:
    record_id: str
    instance_id: str
    base_url: str
    project_id: int
    path_with_namespace: str
    merge_request_url: str
    merge_request_iid: int
    status: str
    trigger: str
    created_at: str
    delivery_id: str = ""
    action: str = ""
    session_id: str = ""
    display_name: str = ""
    reason: str = ""

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.record_id,
            "instanceId": self.instance_id,
            "baseUrl": self.base_url,
            "projectId": self.project_id,
            "pathWithNamespace": self.path_with_namespace,
            "mergeRequestUrl": self.merge_request_url,
            "mergeRequestIid": self.merge_request_iid,
            "status": self.status,
            "trigger": self.trigger,
            "createdAt": self.created_at,
            "deliveryId": self.delivery_id,
            "action": self.action,
            "sessionId": self.session_id,
            "displayName": self.display_name,
            "reason": self.reason,
        }


class TosGitLabAppReviewProjectStore:
    """Persist GitLab MR review enablement in Studio's private TOS bucket."""

    def __init__(
        self,
        *,
        bucket: str,
        client_factory: Any,
        key: str = GITLAB_REVIEW_PROJECTS_KEY,
        history_key: str = GITLAB_REVIEW_HISTORY_KEY,
    ) -> None:
        if not bucket.strip():
            raise ValueError("GitLab review storage requires a bucket.")
        self._bucket = bucket.strip()
        self._client_factory = client_factory
        self._key = key.strip("/")
        self._history_key = history_key.strip("/")

    async def enabled_projects(self) -> set[str]:
        return await asyncio.to_thread(self._enabled_projects)

    async def save_enabled_projects(
        self, projects: list[GitLabProject]
    ) -> list[dict[str, object]]:
        return await asyncio.to_thread(self._save_enabled_projects, projects)

    async def review_records_page(
        self,
        page_request: PageRequest,
    ) -> tuple[list[GitLabMergeRequestReviewRecord], PageResult]:
        return await asyncio.to_thread(self._review_records_page, page_request)

    async def append_review_record(
        self,
        record: GitLabMergeRequestReviewRecord,
    ) -> GitLabMergeRequestReviewRecord:
        return await asyncio.to_thread(self._append_review_record, record)

    async def update_review_record_status(
        self,
        record_id: str,
        *,
        status: str,
        reason: str = "",
    ) -> GitLabMergeRequestReviewRecord | None:
        return await asyncio.to_thread(
            self._update_review_record_status,
            record_id,
            status=status,
            reason=reason,
        )

    def _enabled_projects(self) -> set[str]:
        payload = self._read_json_object(
            self._key,
            max_bytes=_MAX_REVIEW_PROJECTS_BYTES,
            not_found={},
            invalid_message="MR 自动评审项目配置格式无效。",
        )
        projects = payload.get("projects")
        if projects is None:
            return set()
        if not isinstance(projects, list):
            raise GitLabAppReviewStorageUnavailable("MR 自动评审项目配置格式无效。")
        enabled: set[str] = set()
        for item in projects:
            if not isinstance(item, dict):
                raise GitLabAppReviewStorageUnavailable("MR 自动评审项目配置格式无效。")
            instance_id = _payload_text(item, "instanceId") or "default"
            project_id = item.get("projectId")
            if not isinstance(project_id, int) or project_id <= 0:
                raise GitLabAppReviewStorageUnavailable("MR 自动评审项目配置格式无效。")
            enabled.add(project_key(instance_id, project_id))
        return enabled

    def _save_enabled_projects(
        self, projects: list[GitLabProject]
    ) -> list[dict[str, object]]:
        deduped = {
            project_key(item.instance_id, item.project_id): item for item in projects
        }
        ordered = sorted(
            deduped.values(),
            key=lambda item: (
                item.instance_id.casefold(),
                item.path_with_namespace.casefold(),
            ),
        )
        content = json.dumps(
            {"projects": [_project_storage_dict(item) for item in ordered]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(content) > _MAX_REVIEW_PROJECTS_BYTES:
            raise GitLabAppReviewStorageUnavailable("MR 自动评审项目配置过大。")
        try:
            self._client_factory().put_object(
                bucket=self._bucket,
                key=self._key,
                content=content,
                content_length=len(content),
                content_type="application/json",
            )
        except Exception as error:
            raise GitLabAppReviewStorageUnavailable(
                "无法保存 MR 自动评审项目配置。"
            ) from error
        return [_project_storage_dict(item) for item in ordered]

    def _review_records(self) -> list[GitLabMergeRequestReviewRecord]:
        payload = self._read_json_object(
            self._history_key,
            max_bytes=_MAX_REVIEW_HISTORY_BYTES,
            not_found={},
            invalid_message="MR 评审记录格式无效。",
        )
        records = payload.get("records")
        if records is None:
            return []
        if not isinstance(records, list):
            raise GitLabAppReviewStorageUnavailable("MR 评审记录格式无效。")
        return [
            _review_record_from_payload(item)
            for item in records
            if isinstance(item, dict)
        ][:_MAX_REVIEW_HISTORY_ITEMS]

    def _review_records_page(
        self,
        page_request: PageRequest,
    ) -> tuple[list[GitLabMergeRequestReviewRecord], PageResult]:
        records = self._review_records()
        start = page_request.offset
        end = start + page_request.page_size
        return records[start:end], PageResult(
            page_request.page, page_request.page_size, end < len(records)
        )

    def _append_review_record(
        self,
        record: GitLabMergeRequestReviewRecord,
    ) -> GitLabMergeRequestReviewRecord:
        records = [record, *self._review_records()]
        deduped: list[GitLabMergeRequestReviewRecord] = []
        seen: set[str] = set()
        for item in records:
            if item.record_id in seen:
                continue
            seen.add(item.record_id)
            deduped.append(item)
            if len(deduped) >= _MAX_REVIEW_HISTORY_ITEMS:
                break
        self._write_review_records(deduped)
        return record

    def _update_review_record_status(
        self,
        record_id: str,
        *,
        status: str,
        reason: str = "",
    ) -> GitLabMergeRequestReviewRecord | None:
        normalized_status = _review_record_status(status)
        records = self._review_records()
        updated: GitLabMergeRequestReviewRecord | None = None
        output: list[GitLabMergeRequestReviewRecord] = []
        for item in records:
            if item.record_id == record_id:
                updated = replace(
                    item, status=normalized_status, reason=reason.strip()[:240]
                )
                output.append(updated)
            else:
                output.append(item)
        if updated is None:
            return None
        self._write_review_records(output)
        return updated

    def _write_review_records(
        self, records: list[GitLabMergeRequestReviewRecord]
    ) -> None:
        content = json.dumps(
            {"records": [item.to_public_dict() for item in records]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(content) > _MAX_REVIEW_HISTORY_BYTES:
            raise GitLabAppReviewStorageUnavailable("MR 评审记录过大。")
        try:
            self._client_factory().put_object(
                bucket=self._bucket,
                key=self._history_key,
                content=content,
                content_length=len(content),
                content_type="application/json",
            )
        except Exception as error:
            raise GitLabAppReviewStorageUnavailable("无法保存 MR 评审记录。") from error

    def _read_json_object(
        self,
        key: str,
        *,
        max_bytes: int,
        not_found: dict[str, Any],
        invalid_message: str,
    ) -> dict[str, Any]:
        client = self._client_factory()
        try:
            response = client.get_object(bucket=self._bucket, key=key)
        except Exception as error:
            if _status_code(error) == 404:
                return dict(not_found)
            raise GitLabAppReviewStorageUnavailable(invalid_message) from error
        content = response.read(max_bytes + 1)
        if not isinstance(content, bytes) or len(content) > max_bytes:
            raise GitLabAppReviewStorageUnavailable(invalid_message)
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise GitLabAppReviewStorageUnavailable(invalid_message) from error
        if not isinstance(payload, dict):
            raise GitLabAppReviewStorageUnavailable(invalid_message)
        return payload


class GitLabAppClient:
    def __init__(self, config: GitLabAppConfig, *, timeout: float = 20.0) -> None:
        self._config = config
        self._timeout = timeout

    async def projects(self) -> list[GitLabProject]:
        if self._config.group_id_or_path:
            path = f"/groups/{quote(self._config.group_id_or_path, safe='')}/projects?include_subgroups=true"
        else:
            path = "/projects?membership=true"
        payloads = await self._request_pages(path)
        projects = [
            _project_from_payload(self._config, item)
            for item in payloads
            if isinstance(item, dict)
        ]
        return sorted(projects, key=lambda item: item.path_with_namespace.casefold())

    async def project(self, project_id: int) -> GitLabProject:
        payload = await self._request("GET", f"/projects/{project_id}")
        if not isinstance(payload, dict):
            raise GitLabAppReviewError("GitLab App 响应格式无效。")
        return _project_from_payload(self._config, payload)

    async def ensure_project_webhook(self, project_id: int) -> None:
        webhook_url = self._config.webhook_url
        if not webhook_url:
            raise GitLabAppReviewError(
                "管理员未配置 VEADK_STUDIO_PUBLIC_BASE_URL，无法自动创建 GitLab webhook。"
            )
        hooks = await self._request_pages(f"/projects/{project_id}/hooks")
        matching = [
            item
            for item in hooks
            if isinstance(item, dict)
            and str(item.get("url") or "").rstrip("/") == webhook_url.rstrip("/")
        ]
        if len(matching) > 1:
            raise GitLabAppReviewError(
                "GitLab 项目存在重复 Studio webhook，请管理员清理后重试。"
            )
        body = {
            "url": webhook_url,
            "token": self._config.webhook_secret,
            "merge_requests_events": True,
            "push_events": False,
            "enable_ssl_verification": True,
        }
        if matching:
            hook_id = matching[0].get("id")
            if not isinstance(hook_id, int) or hook_id <= 0:
                raise GitLabAppReviewError("GitLab webhook 响应格式无效。")
            await self._request(
                "PUT", f"/projects/{project_id}/hooks/{hook_id}", json=body
            )
            return
        await self._request("POST", f"/projects/{project_id}/hooks", json=body)

    async def _request(
        self, method: str, path: str, *, json: dict[str, object] | None = None
    ) -> Any:
        headers = {"Accept": "application/json", "PRIVATE-TOKEN": self._config.token}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method, f"{self._config.api_root}{path}", headers=headers, json=json
                )
        except httpx.HTTPError as error:
            raise GitLabAppReviewError(
                "连接 GitLab 失败，请检查网络后重试。"
            ) from error
        payload = response.json() if response.content else {}
        if not response.is_success:
            message = payload.get("message") if isinstance(payload, dict) else ""
            detail = str(message or "").strip()
            raise GitLabAppReviewError(
                detail[:240] or f"GitLab App 请求失败（HTTP {response.status_code}）。"
            )
        if not isinstance(payload, (dict, list)):
            raise GitLabAppReviewError("GitLab App 响应格式无效。")
        return payload

    async def _request_pages(self, path: str) -> list[Any]:
        items: list[Any] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 101):
            payload = await self._request(
                "GET", f"{path}{separator}per_page=100&page={page}"
            )
            if not isinstance(payload, list):
                raise GitLabAppReviewError("GitLab App 响应格式无效。")
            items.extend(payload)
            if len(payload) < 100:
                break
        return items


def load_gitlab_app_config() -> GitLabAppConfig | None:
    base_url = (
        (os.getenv(GITLAB_BASE_URL_ENV) or GITLAB_DEFAULT_BASE_URL).strip().rstrip("/")
    )
    token = (os.getenv(GITLAB_TOKEN_ENV) or "").strip()
    webhook_secret = (os.getenv(GITLAB_WEBHOOK_SECRET_ENV) or "").strip()
    explicit_base_url = (os.getenv(GITLAB_BASE_URL_ENV) or "").strip()
    if not any((explicit_base_url, token, webhook_secret)):
        return None
    missing = [
        name
        for name, value in (
            (GITLAB_TOKEN_ENV, token),
            (GITLAB_WEBHOOK_SECRET_ENV, webhook_secret),
        )
        if not value
    ]
    if missing:
        raise GitLabAppReviewError("GitLab App 配置不完整：" + "、".join(missing))
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise GitLabAppReviewError(
            "VEADK_GITLAB_BASE_URL 必须是不含路径参数的 HTTPS 地址。"
        )
    return GitLabAppConfig(
        base_url=base_url,
        token=token,
        webhook_secret=webhook_secret,
        group_id_or_path=(os.getenv(GITLAB_GROUP_ID_OR_PATH_ENV) or "")
        .strip()
        .strip("/"),
        review_owner_id=(os.getenv(GITLAB_REVIEW_OWNER_ID_ENV) or "gitlab-app").strip()
        or "gitlab-app",
        review_creator_name=(
            os.getenv(GITLAB_REVIEW_CREATOR_ENV) or "GitLab App"
        ).strip()
        or "GitLab App",
        studio_public_base_url=(os.getenv(STUDIO_PUBLIC_BASE_URL_ENV) or "")
        .strip()
        .rstrip("/"),
    )


def gitlab_app_public_config() -> dict[str, object]:
    try:
        config = load_gitlab_app_config()
    except GitLabAppReviewError as error:
        return {
            "configured": False,
            "baseUrl": "",
            "webhookUrl": "",
            "reason": str(error),
        }
    if config is None:
        return {
            "configured": False,
            "baseUrl": "",
            "webhookUrl": "",
            "reason": "管理员未配置 GitLab App。",
        }
    return {
        "configured": True,
        "baseUrl": config.base_url,
        "webhookUrl": config.webhook_url,
        "reason": "",
    }


def verify_gitlab_webhook_token(received: str, secret: str) -> bool:
    return bool(received) and hmac.compare_digest(received, secret)


def parse_merge_request_event(
    payload: dict[str, Any],
    *,
    event_name: str,
    delivery_id: str,
    config: GitLabAppConfig,
) -> GitLabMergeRequestEvent | None:
    if event_name != "Merge Request Hook":
        return None
    project = payload.get("project")
    attrs = payload.get("object_attributes")
    if not isinstance(project, dict) or not isinstance(attrs, dict):
        raise GitLabAppReviewError("GitLab webhook 缺少 project 或 object_attributes。")
    project_id = _positive_int(project.get("id"), "GitLab webhook project id 无效。")
    mr_iid = _positive_int(attrs.get("iid"), "GitLab webhook Merge Request IID 无效。")
    action = _payload_text(attrs, "action")
    if not action:
        raise GitLabAppReviewError("GitLab webhook action 无效。")
    path = _payload_text(project, "path_with_namespace")
    if not path:
        raise GitLabAppReviewError("GitLab webhook project path 无效。")
    url = (
        _payload_text(attrs, "url")
        or f"{config.base_url}/{path}/-/merge_requests/{mr_iid}"
    )
    source_project_id = _positive_int(
        attrs.get("source_project_id"), "GitLab webhook source project id 无效。"
    )
    target_project_id = _positive_int(
        attrs.get("target_project_id"), "GitLab webhook target project id 无效。"
    )
    title = _payload_text(attrs, "title")
    work_in_progress = bool(attrs.get("work_in_progress")) or title.lower().startswith(
        ("draft:", "wip:")
    )
    last_commit = attrs.get("last_commit")
    head_sha = ""
    if isinstance(last_commit, dict):
        head_sha = _payload_text(last_commit, "id")
    return GitLabMergeRequestEvent(
        delivery_id=delivery_id,
        action=action,
        instance_id=config.instance_id,
        base_url=config.base_url,
        project_id=project_id,
        path_with_namespace=path,
        merge_request_url=url,
        merge_request_iid=mr_iid,
        source_project_id=source_project_id,
        target_project_id=target_project_id,
        draft=work_in_progress,
        head_sha=head_sha,
    )


def create_review_record(
    *,
    instance_id: str,
    base_url: str,
    project_id: int,
    path_with_namespace: str,
    merge_request_url: str,
    merge_request_iid: int,
    status: str,
    trigger: str,
    delivery_id: str = "",
    action: str = "",
    session_id: str = "",
    display_name: str = "",
    reason: str = "",
) -> GitLabMergeRequestReviewRecord:
    return GitLabMergeRequestReviewRecord(
        record_id=uuid4().hex,
        instance_id=instance_id.strip() or "default",
        base_url=base_url.strip().rstrip("/"),
        project_id=project_id,
        path_with_namespace=path_with_namespace.strip().strip("/"),
        merge_request_url=merge_request_url.strip(),
        merge_request_iid=merge_request_iid,
        status=_review_record_status(status),
        trigger=_review_record_trigger(trigger),
        created_at=datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        delivery_id=delivery_id.strip(),
        action=action.strip(),
        session_id=session_id.strip(),
        display_name=display_name.strip(),
        reason=reason.strip()[:240],
    )


def project_key(instance_id: str, project_id: int) -> str:
    return f"{instance_id.strip() or 'default'}:{project_id}"


def merge_request_url_for(
    config: GitLabAppConfig, project: GitLabProject, iid: int
) -> str:
    return f"{config.base_url}/{project.path_with_namespace}/-/merge_requests/{iid}"


def parse_merge_request_url(config: GitLabAppConfig, value: str) -> tuple[str, int]:
    candidate = value.strip()
    base = config.base_url.rstrip("/") + "/"
    if not candidate.startswith(base):
        raise GitLabAppReviewError("请输入当前 GitLab 实例下的 Merge Request URL。")
    suffix = candidate[len(base) :].strip("/")
    marker = "/-/merge_requests/"
    if marker not in suffix:
        raise GitLabAppReviewError("请输入完整的 GitLab Merge Request URL。")
    project_path, iid_text = suffix.split(marker, 1)
    iid_text = iid_text.strip("/")
    if not project_path or not iid_text.isdigit() or int(iid_text) <= 0:
        raise GitLabAppReviewError("请输入完整的 GitLab Merge Request URL。")
    return project_path, int(iid_text)


def _project_from_payload(
    config: GitLabAppConfig, payload: dict[str, Any]
) -> GitLabProject:
    project_id = _positive_int(payload.get("id"), "GitLab App 项目响应格式无效。")
    path = _payload_text(payload, "path_with_namespace")
    name = _payload_text(payload, "name")
    web_url = _payload_text(payload, "web_url") or f"{config.base_url}/{path}"
    namespace_payload = payload.get("namespace")
    namespace = ""
    if isinstance(namespace_payload, dict):
        namespace = _payload_text(namespace_payload, "full_path") or _payload_text(
            namespace_payload, "path"
        )
    if not path:
        raise GitLabAppReviewError("GitLab App 项目响应格式无效。")
    permissions_note = ""
    permissions = payload.get("permissions")
    if isinstance(permissions, dict):
        project_access = permissions.get("project_access")
        group_access = permissions.get("group_access")
        access_levels = [
            item.get("access_level")
            for item in (project_access, group_access)
            if isinstance(item, dict)
        ]
        if access_levels and max(int(level or 0) for level in access_levels) < 30:
            permissions_note = "Token 权限可能不足，至少需要 Developer 权限。"
    return GitLabProject(
        instance_id=config.instance_id,
        base_url=config.base_url,
        project_id=project_id,
        path_with_namespace=path,
        name=name or path.rsplit("/", 1)[-1],
        namespace=namespace or path.rsplit("/", 1)[0],
        web_url=web_url,
        private=str(payload.get("visibility") or "").lower() == "private",
        permissions_note=permissions_note,
    )


def _project_storage_dict(project: GitLabProject) -> dict[str, object]:
    return {
        "instanceId": project.instance_id,
        "baseUrl": project.base_url,
        "projectId": project.project_id,
        "pathWithNamespace": project.path_with_namespace,
    }


def _review_record_from_payload(
    payload: dict[str, Any],
) -> GitLabMergeRequestReviewRecord:
    project_id = payload.get("projectId")
    mr_iid = payload.get("mergeRequestIid")
    if (
        not isinstance(project_id, int)
        or project_id <= 0
        or not isinstance(mr_iid, int)
        or mr_iid <= 0
    ):
        raise GitLabAppReviewStorageUnavailable("MR 评审记录格式无效。")
    return GitLabMergeRequestReviewRecord(
        record_id=_required_text(payload, "id", "MR 评审记录格式无效。"),
        instance_id=_payload_text(payload, "instanceId") or "default",
        base_url=_required_text(payload, "baseUrl", "MR 评审记录格式无效。").rstrip(
            "/"
        ),
        project_id=project_id,
        path_with_namespace=_required_text(
            payload, "pathWithNamespace", "MR 评审记录格式无效。"
        ),
        merge_request_url=_required_text(
            payload, "mergeRequestUrl", "MR 评审记录格式无效。"
        ),
        merge_request_iid=mr_iid,
        status=_review_record_status(_payload_text(payload, "status")),
        trigger=_review_record_trigger(_payload_text(payload, "trigger")),
        created_at=_required_text(payload, "createdAt", "MR 评审记录格式无效。"),
        delivery_id=_payload_text(payload, "deliveryId"),
        action=_payload_text(payload, "action"),
        session_id=_payload_text(payload, "sessionId"),
        display_name=_payload_text(payload, "displayName"),
        reason=_payload_text(payload, "reason")[:240],
    )


def _payload_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _required_text(payload: dict[str, Any], key: str, message: str) -> str:
    value = _payload_text(payload, key)
    if not value:
        raise GitLabAppReviewStorageUnavailable(message)
    return value


def _positive_int(value: object, message: str) -> int:
    if isinstance(value, int) and value > 0:
        return value
    raise GitLabAppReviewError(message)


def _review_record_status(value: str) -> str:
    if value not in {"started", "completed", "ignored", "failed"}:
        raise GitLabAppReviewStorageUnavailable("MR 评审记录状态无效。")
    return value


def _review_record_trigger(value: str) -> str:
    if value not in {"manual", "webhook"}:
        raise GitLabAppReviewStorageUnavailable("MR 评审记录触发方式无效。")
    return value
