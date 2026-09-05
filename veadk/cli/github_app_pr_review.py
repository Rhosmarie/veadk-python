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

"""GitHub App helpers for Studio PR review automation."""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import httpx


GITHUB_API_ROOT = "https://api.github.com"
GITHUB_APP_ID_ENV = "VEADK_GITHUB_APP_ID"
GITHUB_APP_SLUG_ENV = "VEADK_GITHUB_APP_SLUG"
GITHUB_APP_PRIVATE_KEY_ENV = "VEADK_GITHUB_APP_PRIVATE_KEY"
GITHUB_APP_PRIVATE_KEY_B64_ENV = "VEADK_GITHUB_APP_PRIVATE_KEY_B64"
GITHUB_APP_PRIVATE_KEY_PATH_ENV = "VEADK_GITHUB_APP_PRIVATE_KEY_PATH"
GITHUB_APP_WEBHOOK_SECRET_ENV = "VEADK_GITHUB_APP_WEBHOOK_SECRET"
GITHUB_APP_REVIEW_OWNER_ID_ENV = "VEADK_GITHUB_APP_REVIEW_OWNER_ID"
GITHUB_APP_REVIEW_CREATOR_ENV = "VEADK_GITHUB_APP_REVIEW_CREATOR"


class GitHubAppReviewError(RuntimeError):
    """GitHub App review integration failed with a user-safe message."""


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: str
    app_slug: str
    private_key: str
    webhook_secret: str
    review_owner_id: str = "github-app"
    review_creator_name: str = "GitHub App"

    @property
    def install_url(self) -> str:
        return f"https://github.com/apps/{self.app_slug}/installations/new"


@dataclass(frozen=True)
class GitHubPullRequestEvent:
    delivery_id: str
    action: str
    installation_id: int
    repository: str
    pull_request_url: str
    pull_request_number: int
    head_repository: str
    draft: bool

    @property
    def should_review(self) -> bool:
        return (
            self.action in {"opened", "synchronize", "reopened", "ready_for_review"}
            and not self.draft
            and self.head_repository == self.repository
        )


def load_github_app_config() -> GitHubAppConfig | None:
    """Return GitHub App config when the center-service integration is enabled."""
    app_id = (os.getenv(GITHUB_APP_ID_ENV) or "").strip()
    app_slug = (os.getenv(GITHUB_APP_SLUG_ENV) or "").strip()
    webhook_secret = (os.getenv(GITHUB_APP_WEBHOOK_SECRET_ENV) or "").strip()
    private_key = _load_private_key()
    if not any((app_id, app_slug, webhook_secret, private_key)):
        return None
    missing = [
        name
        for name, value in (
            (GITHUB_APP_ID_ENV, app_id),
            (GITHUB_APP_SLUG_ENV, app_slug),
            (GITHUB_APP_WEBHOOK_SECRET_ENV, webhook_secret),
            ("GitHub App private key", private_key),
        )
        if not value
    ]
    if missing:
        raise GitHubAppReviewError("GitHub App 配置不完整：" + "、".join(missing))
    return GitHubAppConfig(
        app_id=app_id,
        app_slug=app_slug,
        private_key=private_key,
        webhook_secret=webhook_secret,
        review_owner_id=(
            os.getenv(GITHUB_APP_REVIEW_OWNER_ID_ENV) or "github-app"
        ).strip()
        or "github-app",
        review_creator_name=(
            os.getenv(GITHUB_APP_REVIEW_CREATOR_ENV) or "GitHub App"
        ).strip()
        or "GitHub App",
    )


def github_app_public_config() -> dict[str, object]:
    """Return browser-safe GitHub App setup state."""
    try:
        config = load_github_app_config()
    except GitHubAppReviewError as error:
        slug = (os.getenv(GITHUB_APP_SLUG_ENV) or "").strip()
        return {
            "configured": False,
            "appSlug": slug,
            "installUrl": (
                f"https://github.com/apps/{slug}/installations/new" if slug else ""
            ),
            "reason": str(error),
        }
    if config is None:
        slug = (os.getenv(GITHUB_APP_SLUG_ENV) or "").strip()
        return {
            "configured": False,
            "appSlug": slug,
            "installUrl": (
                f"https://github.com/apps/{slug}/installations/new" if slug else ""
            ),
            "reason": "管理员未配置 GitHub App。",
        }
    return {
        "configured": True,
        "appSlug": config.app_slug,
        "installUrl": config.install_url,
        "reason": "",
    }


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_pull_request_event(
    payload: dict[str, Any],
    *,
    event_name: str,
    delivery_id: str,
) -> GitHubPullRequestEvent | None:
    if event_name != "pull_request":
        return None
    installation = payload.get("installation")
    repository = payload.get("repository")
    pull_request = payload.get("pull_request")
    if not isinstance(installation, dict) or not isinstance(repository, dict):
        raise GitHubAppReviewError("GitHub webhook 缺少 installation 或 repository。")
    if not isinstance(pull_request, dict):
        raise GitHubAppReviewError("GitHub webhook 缺少 pull_request。")

    installation_id = installation.get("id")
    repository_full_name = repository.get("full_name")
    pull_request_url = pull_request.get("html_url")
    pull_request_number = pull_request.get("number")
    head = pull_request.get("head")
    head_repo = head.get("repo") if isinstance(head, dict) else None
    head_repository = head_repo.get("full_name") if isinstance(head_repo, dict) else ""
    action = payload.get("action")
    if not isinstance(installation_id, int) or installation_id <= 0:
        raise GitHubAppReviewError("GitHub webhook installation id 无效。")
    if not isinstance(repository_full_name, str) or "/" not in repository_full_name:
        raise GitHubAppReviewError("GitHub webhook repository 无效。")
    if not isinstance(pull_request_url, str) or not pull_request_url:
        raise GitHubAppReviewError("GitHub webhook Pull Request URL 无效。")
    if not isinstance(pull_request_number, int) or pull_request_number <= 0:
        raise GitHubAppReviewError("GitHub webhook Pull Request 编号无效。")
    if not isinstance(action, str):
        raise GitHubAppReviewError("GitHub webhook action 无效。")
    return GitHubPullRequestEvent(
        delivery_id=delivery_id,
        action=action,
        installation_id=installation_id,
        repository=repository_full_name,
        pull_request_url=pull_request_url,
        pull_request_number=pull_request_number,
        head_repository=head_repository,
        draft=bool(pull_request.get("draft")),
    )


class GitHubAppClient:
    def __init__(
        self,
        config: GitHubAppConfig,
        *,
        api_root: str = GITHUB_API_ROOT,
        timeout: float = 20.0,
    ) -> None:
        self._config = config
        self._api_root = api_root.rstrip("/")
        self._timeout = timeout

    async def installation_token(self, installation_id: int) -> str:
        payload = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=self._app_jwt(),
        )
        token = payload.get("token")
        if not isinstance(token, str) or not token.strip():
            raise GitHubAppReviewError("GitHub 未返回 installation token。")
        return token

    async def repository_installation_id(self, owner: str, repo: str) -> int:
        payload = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/installation",
            token=self._app_jwt(),
        )
        installation_id = payload.get("id")
        if not isinstance(installation_id, int) or installation_id <= 0:
            raise GitHubAppReviewError("GitHub 未返回有效 installation id。")
        return installation_id

    async def _request(self, method: str, path: str, *, token: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    f"{self._api_root}{path}",
                    headers=headers,
                )
        except httpx.HTTPError as error:
            raise GitHubAppReviewError(
                "连接 GitHub 失败，请检查网络后重试。"
            ) from error
        payload = response.json() if response.content else {}
        if not response.is_success:
            message = payload.get("message") if isinstance(payload, dict) else ""
            detail = str(message or "").strip()
            raise GitHubAppReviewError(
                detail[:240] or f"GitHub App 请求失败（HTTP {response.status_code}）。"
            )
        if not isinstance(payload, dict):
            raise GitHubAppReviewError("GitHub App 响应格式无效。")
        return payload

    def _app_jwt(self) -> str:
        try:
            import jwt
        except ImportError as error:
            raise GitHubAppReviewError(
                "缺少 PyJWT 依赖，无法生成 GitHub App JWT。"
            ) from error
        issued_at = int(time.time()) - 60
        expires_at = issued_at + 9 * 60
        return jwt.encode(
            {"iat": issued_at, "exp": expires_at, "iss": self._config.app_id},
            self._config.private_key,
            algorithm="RS256",
        )


def _load_private_key() -> str:
    inline = (os.getenv(GITHUB_APP_PRIVATE_KEY_ENV) or "").strip()
    if inline:
        return inline.replace("\\n", "\n")
    encoded = (os.getenv(GITHUB_APP_PRIVATE_KEY_B64_ENV) or "").strip()
    if encoded:
        try:
            return base64.b64decode(encoded).decode().strip()
        except (binascii.Error, UnicodeDecodeError) as error:
            raise GitHubAppReviewError(
                "GitHub App private key base64 无效。"
            ) from error
    path = (os.getenv(GITHUB_APP_PRIVATE_KEY_PATH_ENV) or "").strip()
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as file:
            return file.read().strip()
    except OSError as error:
        raise GitHubAppReviewError("无法读取 GitHub App private key 文件。") from error
