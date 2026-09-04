import {
  createGitHubPullRequest,
  type GitHubAutomationRegion,
} from "../adk/githubIntegration";
import {
  baseBranchField,
  commonGitHubInput,
  initialAutomationValues,
  repositoryField,
} from "./githubFields";
import type { GitHubAutomationDefinition } from "./types";

interface PullRequestReviewWorkflowInput {
  sandboxToolId: string;
  region: GitHubAutomationRegion;
}

const SANDBOX_TOOL_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export function validatePullRequestReviewSettings(
  input: PullRequestReviewWorkflowInput,
): void {
  if (!SANDBOX_TOOL_ID_PATTERN.test(input.sandboxToolId)) {
    throw new Error("Codex 沙箱工具 ID 格式不正确");
  }
  if (!input.region) {
    throw new Error("地域不能为空");
  }
}

export function buildPullRequestReviewPrompt(pullRequestUrl: string): string {
  return `请评审这个 Pull Request：${pullRequestUrl}

要求：
1.你需要遵守GitHub Skill，通过GitHubCLI获取PR信息、diff和必要的上下文
2.遵守Code-Review Skill的规范，对PR进行CodeReview
3.不要修改仓库文件，不要执行破坏性命令。
4.执行结束后，请告知我你都进行了哪些操作，给出明确且清晰的反馈`;
}

export function buildPullRequestReviewWorkflow(input: PullRequestReviewWorkflowInput): string {
  validatePullRequestReviewSettings(input);
  const template = String.raw`name: PR Automated Review

"on":
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: pr-review-__GH__ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    if: >-
      github.event.pull_request.draft == false &&
      github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    timeout-minutes: 30
    env:
      VOLCENGINE_ACCESS_KEY: __GH__ secrets.VOLCENGINE_ACCESS_KEY }}
      VOLCENGINE_SECRET_KEY: __GH__ secrets.VOLCENGINE_SECRET_KEY }}
      VOLCENGINE_REGION: __REGION__
      AGENTKIT_SANDBOX_TOOL_ID: __SANDBOX_TOOL_ID__
      GH_TOKEN: __GH__ secrets.GH_TOKEN }}
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install AgentKit SDK
        run: python -m pip install "agentkit-sdk-python>=0.8.1,<0.9.0" "websockets>=12,<16"
      - name: Start review in Sandbox Session
        shell: bash
        run: |
          set -euo pipefail
          if [ -z "__GH_TOKEN_VALUE__" ]; then
            echo "GH_TOKEN secret is required for PR review." >&2
            exit 1
          fi

          SESSION_ID="pr-review-__GH__ github.run_id }}-__GH__ github.run_attempt }}"
          export SESSION_ID
          export PR_REVIEW_PROMPT=__PROMPT_JSON__
          python <<'PY'
          import asyncio
          import json
          import os
          from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

          from agentkit.sdk.tools import types as tools_types
          from agentkit.sdk.tools.client import AgentkitToolsClient
          from pydantic import Field
          import websockets

          def _required_secret(name):
              value = os.environ.get(name, "").strip()
              if not value:
                  raise RuntimeError(f"{name} secret is required.")
              if any(character.isspace() for character in value):
                  raise RuntimeError(f"{name} secret must not contain spaces or newlines.")
              return value

          def _model_supports_alias(model_type, alias):
              fields = getattr(model_type, "model_fields", None) or getattr(model_type, "__fields__", {})
              return any(getattr(field, "alias", None) == alias for field in fields.values())

          class _SessionEnv(tools_types.ToolsBaseModel):
              key: str = Field(alias="Key")
              value: str = Field(alias="Value")

          class _CreateSessionRequestCompat(tools_types.ToolsBaseModel):
              tool_id: str = Field(alias="ToolId")
              ttl: int = Field(alias="Ttl")
              ttl_unit: str = Field(alias="TtlUnit")
              user_session_id: str = Field(alias="UserSessionId")
              envs: list[_SessionEnv] = Field(alias="Envs")

          class _GetSessionRequestCompat(tools_types.ToolsBaseModel):
              tool_id: str = Field(alias="ToolId")
              session_id: str = Field(alias="SessionId")

          access_key = _required_secret("VOLCENGINE_ACCESS_KEY")
          secret_key = _required_secret("VOLCENGINE_SECRET_KEY")
          region = os.environ["VOLCENGINE_REGION"].strip()
          user_session_id = os.environ["SESSION_ID"].strip()
          github_token = _required_secret("GH_TOKEN")
          prompt = os.environ["PR_REVIEW_PROMPT"].strip()

          def _sandbox_service_url(endpoint, pathname, websocket=False):
              parsed = urlsplit(endpoint)
              if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
                  raise RuntimeError("CreateSession returned an invalid Endpoint.")
              scheme = (
                  ("wss" if parsed.scheme in {"https", "wss"} else "ws")
                  if websocket
                  else ("https" if parsed.scheme in {"https", "wss"} else "http")
              )
              base_path = parsed.path.rstrip("/")
              if base_path.endswith("/v1/codex/app-server"):
                  base_path = base_path.removesuffix("/v1/codex/app-server")
              values = list(parse_qsl(parsed.query, keep_blank_values=True))
              return urlunsplit((scheme, parsed.netloc, f"{base_path}{pathname}", urlencode(values), ""))

          def _runtime_permission_params(cwd=""):
              return {
                  "approvalPolicy": "on-request",
                  "approvalsReviewer": "user",
                  "sandboxPolicy": {
                      "type": "workspaceWrite",
                      "writableRoots": [cwd] if cwd else [],
                      "networkAccess": False,
                      "excludeTmpdirEnvVar": False,
                  },
              }

          async def _send_jsonrpc(websocket, request_id, method, params=None, timeout=60):
              payload = {"id": request_id, "method": method}
              if params is not None:
                  payload["params"] = params
              await websocket.send(json.dumps(payload, ensure_ascii=False))
              while True:
                  raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                  message = json.loads(raw)
                  if isinstance(message.get("method"), str) and "id" in message:
                      await _handle_server_request(websocket, message)
                      continue
                  if message.get("id") != request_id:
                      continue
                  if "error" in message:
                      raise RuntimeError(f"{method} failed: {message['error']}")
                  result = message.get("result")
                  if not isinstance(result, dict):
                      raise RuntimeError(f"{method} returned an invalid response.")
                  return result

          async def _handle_server_request(websocket, message):
              request_id = message.get("id")
              method = message.get("method")
              if method == "item/permissions/requestApproval":
                  await websocket.send(json.dumps({
                      "id": request_id,
                      "result": {"permissions": {}, "scope": "turn"},
                  }))
                  return
              if method in {
                  "item/commandExecution/requestApproval",
                  "item/fileChange/requestApproval",
              }:
                  await websocket.send(json.dumps({
                      "id": request_id,
                      "result": {"decision": "accept"},
                  }))
                  return
              await websocket.send(json.dumps({
                  "id": request_id,
                  "result": {},
              }))

          async def _wait_turn_completed(websocket, timeout=1800):
              def _error_detail(value):
                  if isinstance(value, dict):
                      for key in ("message", "detail", "error"):
                          detail = value.get(key)
                          if isinstance(detail, str) and detail.strip():
                              return detail.strip()
                          if detail is not None:
                              return json.dumps(detail, ensure_ascii=False)
                      return json.dumps(value, ensure_ascii=False)
                  if value is not None:
                      return str(value)
                  return "Codex app-server returned an error."

              while True:
                  raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
                  message = json.loads(raw)
                  method = message.get("method")
                  params = message.get("params")
                  if isinstance(method, str) and "id" in message:
                      await _handle_server_request(websocket, message)
                      continue
                  if method == "item/agentMessage/delta" and isinstance(params, dict):
                      delta = params.get("delta")
                      if isinstance(delta, str) and delta:
                          print(delta, end="", flush=True)
                      continue
                  if method == "error" and isinstance(params, dict):
                      raise RuntimeError(_error_detail(params))
                  if method != "turn/completed" or not isinstance(params, dict):
                      continue
                  turn = params.get("turn")
                  if not isinstance(turn, dict):
                      raise RuntimeError("turn/completed returned an invalid payload.")
                  status = str(turn.get("status") or "completed").lower()
                  if status in {"failed", "cancelled", "canceled"}:
                      raise RuntimeError(f"Codex review turn failed: {_error_detail(turn.get('error') or turn)}")
                  return turn

          async def _start_codex_review(endpoint, prompt):
              app_server_url = _sandbox_service_url(endpoint, "/v1/codex/app-server/", websocket=True)
              async with websockets.connect(
                  app_server_url,
                  open_timeout=30,
                  close_timeout=5,
                  ping_timeout=60,
                  max_size=20 * 1024 * 1024,
              ) as websocket:
                  await _send_jsonrpc(
                      websocket,
                      1,
                      "initialize",
                      {
                          "clientInfo": {
                              "name": "github_actions_pr_review",
                              "title": "GitHub Actions PR Review",
                              "version": "1",
                          },
                          "capabilities": {"experimentalApi": False},
                      },
                  )
                  await websocket.send(json.dumps({"method": "initialized"}))
                  thread = await _send_jsonrpc(
                      websocket,
                      2,
                      "thread/start",
                      _runtime_permission_params(),
                  )
                  thread_id = thread.get("thread", {}).get("id") or thread.get("threadId")
                  if not isinstance(thread_id, str) or not thread_id:
                      raise RuntimeError("thread/start did not return a Thread ID.")
                  cwd = thread.get("cwd")
                  if not isinstance(cwd, str):
                      cwd = ""
                  turn_start = await _send_jsonrpc(
                      websocket,
                      3,
                      "turn/start",
                      {
                          "threadId": thread_id,
                          "input": [{"type": "text", "text": prompt}],
                          **_runtime_permission_params(cwd),
                      },
                  )
                  turn_id = turn_start.get("turn", {}).get("id")
                  print(f"Submitted PR review message to Codex thread {thread_id}, turn {turn_id}")
                  await _wait_turn_completed(websocket)
                  print("Codex PR review completed.")

          def _session_value(session, *names):
              for name in names:
                  value = getattr(session, name, None)
                  if isinstance(value, str) and value.strip():
                      return value.strip()
              return ""

          def _get_authoritative_session(client, tool_id, instance_id):
              request_type = getattr(tools_types, "GetSessionRequest", None)
              if request_type is None:
                  request_type = _GetSessionRequestCompat
              return client.get_session(request_type(ToolId=tool_id, SessionId=instance_id))

          client = AgentkitToolsClient(
              access_key=access_key,
              secret_key=secret_key,
              region=region,
          )

          tool_id = os.environ["AGENTKIT_SANDBOX_TOOL_ID"].strip()

          env_item = getattr(tools_types, "EnvsItemForCreateSession", None)
          if env_item is None:
              env_item = _SessionEnv

          request_type = tools_types.CreateSessionRequest
          if not _model_supports_alias(request_type, "Envs"):
              request_type = _CreateSessionRequestCompat

          request = request_type(
              ToolId=tool_id,
              UserSessionId=user_session_id,
              Ttl=28800,
              TtlUnit="second",
              Envs=[
                  env_item(Key="GH_TOKEN", Value=github_token),
              ],
          )
          session = client.create_session(request)
          instance_id = _session_value(session, "session_id", "SessionId")
          created_endpoint = _session_value(session, "endpoint", "Endpoint")
          if not instance_id:
              raise RuntimeError("CreateSession response is missing SessionId.")

          authoritative = _get_authoritative_session(client, tool_id, instance_id)
          endpoint = _session_value(authoritative, "endpoint", "Endpoint") or created_endpoint
          if not endpoint:
              raise RuntimeError("GetSession response is missing Endpoint.")

          print(f"Created Sandbox Session {user_session_id}")
          asyncio.run(_start_codex_review(endpoint, prompt))
          PY
`;
  const replacements: Record<string, string> = {
    "__GH__": "${{",
    __REGION__: JSON.stringify(input.region),
    __SANDBOX_TOOL_ID__: JSON.stringify(input.sandboxToolId),
    __GH_TOKEN_VALUE__: "${GH_TOKEN:-}",
    __PROMPT_JSON__: JSON.stringify(buildPullRequestReviewPrompt("${{ github.event.pull_request.html_url }}")),
  };
  return Object.entries(replacements).reduce(
    (workflow, [key, value]) => workflow.split(key).join(value),
    template,
  );
}

export const pullRequestReviewAutomation: GitHubAutomationDefinition = {
  id: "review",
  kind: "github",
  category: "development",
  icon: "github",
  name: "PR 自动评审",
  description: "在隔离 Sandbox 中评审代码变更，并将结果发布到 Pull Request。",
  title: "PR 自动评审",
  subtitle: "在隔离 Sandbox 中检查代码变更并把结果发布到 Pull Request",
  panel: "工作流仅评审同仓库的非草稿 PR；fork PR 不会读取仓库 Secrets。",
  submitLabel: "添加评审并提交 PR",
  fields: [
    repositoryField,
    baseBranchField,
    {
      name: "sandboxToolId",
      label: "Codex 沙箱工具 ID",
      placeholder: "tool-xxxxxxxx",
      help: "选择 Studio 中可与 Codex 智能体对话的 Sandbox Tool，不要使用普通 CodeEnv Tool。",
      required: true,
    },
  ],
  initialValues: initialAutomationValues(),
  regionHelp: "用于 GitHub Actions 创建评审 Sandbox Session",
  secrets: [
    "GH_TOKEN：GitHub fine-grained PAT，Repository access 选择目标仓库；Permissions 至少配置 Contents: Read and write、Pull requests: Read and write、Workflows: Read and write。Workflows 权限用于创建 .github/workflows/codex-pr-review.yml；运行评审时它只会在创建本次 Sandbox Session 时作为 GH_TOKEN 注入，供 Codex 通过 GitHub CLI 读取 PR、获取 diff、写回 review 评论。",
    "VOLCENGINE_ACCESS_KEY：火山引擎访问密钥，用于 GitHub Actions 调用 AgentKit 创建隔离的评审 Sandbox Session。",
    "VOLCENGINE_SECRET_KEY：与 VOLCENGINE_ACCESS_KEY 配套的火山引擎密钥。不要填入模型 API Key 或 Studio API Token。",
  ],
  submit(values, signal) {
    const input = commonGitHubInput(values);
    return createGitHubPullRequest(
      {
        ...input,
        files: [
          {
            path: ".github/workflows/codex-pr-review.yml",
            content: buildPullRequestReviewWorkflow({
              sandboxToolId: values.sandboxToolId.trim(),
              region: input.region,
            }),
            commitMessage: "chore: configure PR automated review",
          },
        ],
        branchPrefix: "chore/pr-automated-review",
        title: "chore: 配置 PR 自动评审",
        description: "新增 GitHub Actions 工作流，在隔离 Sandbox 中评审同仓库 PR，并由 Sandbox 内 Codex 写回评审评论。合并前请配置工作流所需 Secrets。",
      },
      signal,
    );
  },
};
