import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { build } from "esbuild";

async function loadTypeScriptModule(relativePath) {
  const result = await build({
    entryPoints: [fileURLToPath(new URL(relativePath, import.meta.url))],
    bundle: true,
    format: "esm",
    platform: "node",
    target: "node20",
    write: false,
  });
  const source = Buffer.from(result.outputFiles[0].contents).toString("base64");
  return import(`data:text/javascript;base64,${source}`);
}

function jsonResponse(status, payload = null) {
  return new Response(status === 204 ? null : JSON.stringify(payload), {
    status,
    headers: status === 204 ? undefined : { "Content-Type": "application/json" },
  });
}

test("generates the basic Studio project and Runtime delivery workflow in frontend", async () => {
  const [{ buildBasicTemplateFiles }, { buildRuntimeDeliveryWorkflow }] = await Promise.all([
    loadTypeScriptModule("../src/automations/templateProject.ts"),
    loadTypeScriptModule("../src/automations/runtimeDelivery.ts"),
  ]);
  const files = buildBasicTemplateFiles("basic-agent");
  assert.match(files["app.py"], /create_agentkit_app\(/);
  assert.match(files["app.py"], /enable_feishu=True/);
  assert.match(files["app.py"], /enable_studio_tools=True/);
  assert.match(files["app.py"], /run_agentkit_app\(app\)/);
  assert.doesNotMatch(files["app.py"], /AgentkitAgentServerApp/);
  assert.match(files["assistant/agent.py"], /root_agent = Agent\(/);
  assert.equal(
    files["requirements.txt"],
    [
      "veadk-python==1.1.5",
      "agentkit-sdk-python==0.8.4",
      "google-adk==2.1.0",
      "lark-channel-sdk==1.2.0",
      "lark-oapi==1.7.3",
      "starlette==0.52.1",
      "",
    ].join("\n"),
  );
  assert.match(files["README.md"], /python app\.py/);

  const workflow = buildRuntimeDeliveryWorkflow({
    baseBranch: "main",
    projectPath: "examples/basic-agent",
    runtimeName: "basic-agent",
    runtimeId: "rt-basic-agent",
    region: "cn-beijing",
  });
  assert.match(workflow, /Publish to AgentKit Runtime/);
  assert.match(workflow, /\$\{\{ secrets\.VOLCENGINE_ACCESS_KEY \}\}/);
  assert.match(workflow, /AgentkitRuntimeClient/);
  assert.match(workflow, /"runtime_role_name": runtime_role_name/);
  assert.match(workflow, /"image_tag": f"veadk-v\{next_version\}"/);
  assert.match(workflow, /working-directory: "examples\/basic-agent"/);
  assert.match(workflow, /group: "agentkit-runtime-rt-basic-agent"/);
  assert.doesNotMatch(workflow, /__[A-Z_]+__/);
});

test("generates the isolated pull request review workflow in frontend", async () => {
  const { buildPullRequestReviewWorkflow } = await loadTypeScriptModule(
    "../src/automations/pullRequestReview.ts",
  );
  const workflow = buildPullRequestReviewWorkflow({
    sandboxToolId: "tool-code-review",
    region: "cn-beijing",
  });
  assert.doesNotMatch(workflow, /pull_request_target/);
  assert.match(workflow, /github\.event\.pull_request\.head\.repo\.full_name == github\.repository/);
  assert.match(workflow, /GH_TOKEN secret is required for PR review/);
  assert.match(workflow, /secret must not contain spaces or newlines/);
  assert.match(workflow, /Install AgentKit SDK/);
  assert.match(workflow, /websockets>=12,<16/);
  assert.match(workflow, /Start review in Sandbox Session/);
  assert.match(workflow, /CreateSessionRequest/);
  assert.match(workflow, /GetSessionRequest/);
  assert.match(workflow, /client\.get_session/);
  assert.match(workflow, /AGENTKIT_SANDBOX_TOOL_ID: "tool-code-review"/);
  assert.match(workflow, /tool_id = os\.environ\["AGENTKIT_SANDBOX_TOOL_ID"\]\.strip\(\)/);
  assert.doesNotMatch(workflow, /list_tools|Ready CodeEnv Sandbox Tool|FiltersItemForListTools/);
  assert.match(workflow, /env_item\(Key="GH_TOKEN", Value=github_token\)/);
  assert.doesNotMatch(workflow, /CODEX_CONFIG_TOML|CODEX_MODEL_CATALOG_JSON/);
  assert.match(workflow, /authoritative = _get_authoritative_session\(client, tool_id, instance_id\)/);
  assert.match(workflow, /endpoint = _session_value\(authoritative, "endpoint", "Endpoint"\) or created_endpoint/);
  assert.doesNotMatch(workflow, /model_catalog_json|env_key = \\"CODEX_API_KEY\\"|experimental_supported_tools/);
  assert.match(workflow, /websockets\.connect/);
  assert.match(workflow, /"thread\/start"/);
  assert.match(workflow, /"turn\/start"/);
  assert.match(workflow, /"turn\/completed"/);
  assert.match(workflow, /"experimentalApi": False/);
  assert.doesNotMatch(workflow, /"experimentalApi": True/);
  assert.match(workflow, /def _runtime_permission_params/);
  assert.match(workflow, /"approvalPolicy": "on-request"/);
  assert.match(workflow, /"approvalsReviewer": "user"/);
  assert.match(workflow, /"sandboxPolicy": \{/);
  assert.match(workflow, /"type": "workspaceWrite"/);
  assert.match(workflow, /"networkAccess": False/);
  assert.match(workflow, /"excludeTmpdirEnvVar": False/);
  assert.match(workflow, /item\/permissions\/requestApproval/);
  assert.match(workflow, /"result": \{"decision": "accept"\}/);
  assert.doesNotMatch(workflow, /unsupported server request|invalid params for/);
  assert.doesNotMatch(workflow, /modelProvider\/capabilities\/read|namespaceTools|imageGeneration|webSearch/);
  assert.match(workflow, /"thread\/start",\n\s+_runtime_permission_params\(\),/);
  assert.match(workflow, /\*\*_runtime_permission_params\(cwd\)/);
  assert.match(workflow, /请评审这个 Pull Request：\$\{\{ github\.event\.pull_request\.html_url \}\}/);
  assert.match(workflow, /\$\{\{ secrets\.GH_TOKEN \}\}/);
  const pythonScript = workflow.match(/python <<'PY'\n([\s\S]*?)\n\s+PY/)?.[1];
  assert.ok(pythonScript);
  const nonBlankPythonLines = pythonScript.split("\n").filter((line) => line.trim());
  const commonIndent = Math.min(
    ...nonBlankPythonLines.map((line) => line.match(/^ */)?.[0].length ?? 0),
  );
  const normalizedPythonScript = pythonScript
    .split("\n")
    .map((line) => line.slice(commonIndent))
    .join("\n");
  const tempDir = mkdtempSync(join(tmpdir(), "pr-review-workflow-"));
  try {
    const scriptPath = join(tempDir, "review.py");
    writeFileSync(scriptPath, normalizedPythonScript);
    execFileSync("python", ["-m", "py_compile", scriptPath]);
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
  assert.doesNotMatch(workflow, /agentkit sandbox delete \\/);
  assert.doesNotMatch(workflow, /actions\/checkout|actions\/setup-node|agentkit sandbox exec|--copy|CODEX_MODEL_API_KEY|--model-name|--model-base-url|--model-api-key|gh pr review|review\.md|tee/);
  assert.doesNotMatch(workflow, /__GH__|__[A-Z_]+__/);
});

test("rejects invalid Runtime and review settings before generating workflows", async () => {
  const [{ buildRuntimeDeliveryWorkflow }, { buildPullRequestReviewWorkflow }] = await Promise.all([
    loadTypeScriptModule("../src/automations/runtimeDelivery.ts"),
    loadTypeScriptModule("../src/automations/pullRequestReview.ts"),
  ]);
  assert.throws(
    () => buildRuntimeDeliveryWorkflow({
      baseBranch: "main",
      projectPath: ".",
      runtimeName: "invalid runtime",
      runtimeId: "rt-agent",
      region: "cn-beijing",
    }),
    /Runtime 名称/,
  );
  assert.throws(
    () => buildPullRequestReviewWorkflow({
      sandboxToolId: "bad tool id",
      region: "cn-beijing",
    }),
    /Codex 沙箱工具 ID/,
  );
  assert.doesNotThrow(() => buildPullRequestReviewWorkflow({
    sandboxToolId: "tool-code-review",
    region: "cn-beijing",
  }));
});

test("normalizes supported GitHub repository forms and rejects unsafe paths", async () => {
  const { normalizeGitHubRepository, normalizeRepositoryPath } = await loadTypeScriptModule(
    "../src/adk/githubIntegration.ts",
  );
  assert.equal(normalizeGitHubRepository("https://www.github.com/acme/agent.git"), "acme/agent");
  assert.equal(normalizeGitHubRepository("git@github.com:acme/agent.git"), "acme/agent");
  assert.throws(() => normalizeGitHubRepository("https://example.com/acme/agent"), /github\.com/);
  assert.throws(() => normalizeRepositoryPath("../escape"), /安全相对路径/);
});

test("creates a GitHub pull request directly without persisting the token", async () => {
  const { createGitHubPullRequest } = await loadTypeScriptModule(
    "../src/adk/githubIntegration.ts",
  );
  const calls = [];
  const originalFetch = globalThis.fetch;
  const originalCrypto = globalThis.crypto;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    const method = init.method || "GET";
    if (String(url).endsWith("/repos/acme/agent")) return jsonResponse(200, {});
    if (String(url).includes("/git/ref/heads/main")) {
      return jsonResponse(200, { object: { sha: "base-sha" } });
    }
    if (method === "POST" && String(url).endsWith("/git/refs")) {
      return jsonResponse(201, { ref: "refs/heads/feat/test" });
    }
    if (method === "GET" && String(url).includes("/contents/")) {
      return jsonResponse(404, { message: "Not Found" });
    }
    if (method === "PUT" && String(url).includes("/contents/")) {
      return jsonResponse(201, { content: { sha: "file-sha" } });
    }
    if (method === "POST" && String(url).endsWith("/pulls")) {
      return jsonResponse(201, {
        number: 42,
        html_url: "https://github.com/acme/agent/pull/42",
      });
    }
    throw new Error(`Unexpected request: ${method} ${url}`);
  };
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: { randomUUID: () => "12345678-1234-1234-1234-123456789012" },
  });

  try {
    const result = await createGitHubPullRequest(
      {
        repository: "https://github.com/acme/agent.git",
        baseBranch: "main",
        token: "github-secret-token",
        files: [{
          path: ".github/workflows/test.yml",
          content: "hello 世界",
          commitMessage: "chore: add workflow",
        }],
        branchPrefix: "feat/test",
        title: "chore: test",
        description: "test",
      },
      new AbortController().signal,
    );
    assert.equal(result.number, 42);
    assert.equal(result.url, "https://github.com/acme/agent/pull/42");
    assert.match(result.branch, /^feat\/test-\d{14}-12345678$/);
    assert.equal(calls.every(({ url }) => url.startsWith("https://api.github.com/")), true);
    assert.equal(
      calls.every(({ init }) => init.headers.Authorization === "Bearer github-secret-token"),
      true,
    );
    const putCall = calls.find(({ init }) => init.method === "PUT");
    const putBody = JSON.parse(putCall.init.body);
    assert.equal(Buffer.from(putBody.content, "base64").toString("utf8"), "hello 世界");
    assert.equal(calls.every(({ init }) => !String(init.body).includes("github-secret-token")), true);
  } finally {
    globalThis.fetch = originalFetch;
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: originalCrypto,
    });
  }
});

test("removes the temporary GitHub branch when file creation fails", async () => {
  const { createGitHubPullRequest } = await loadTypeScriptModule(
    "../src/adk/githubIntegration.ts",
  );
  const calls = [];
  const originalFetch = globalThis.fetch;
  const originalCrypto = globalThis.crypto;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    const method = init.method || "GET";
    if (String(url).endsWith("/repos/acme/agent")) return jsonResponse(200, {});
    if (String(url).includes("/git/ref/heads/main")) {
      return jsonResponse(200, { object: { sha: "base-sha" } });
    }
    if (method === "POST" && String(url).endsWith("/git/refs")) return jsonResponse(201, {});
    if (method === "GET" && String(url).includes("/contents/")) return jsonResponse(404, {});
    if (method === "PUT") return jsonResponse(500, { message: "write failed" });
    if (method === "DELETE") return jsonResponse(204);
    throw new Error(`Unexpected request: ${method} ${url}`);
  };
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: { randomUUID: () => "12345678-1234-1234-1234-123456789012" },
  });

  try {
    await assert.rejects(
      createGitHubPullRequest(
        {
          repository: "acme/agent",
          baseBranch: "main",
          token: "github-secret-token",
          files: [{ path: "test.txt", content: "test", commitMessage: "test" }],
          branchPrefix: "feat/test",
          title: "test",
          description: "test",
        },
        new AbortController().signal,
      ),
      /write failed/,
    );
    assert.equal(calls.some(({ init }) => init.method === "DELETE"), true);
  } finally {
    globalThis.fetch = originalFetch;
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: originalCrypto,
    });
  }
});

test("reports missing GitHub workflow permission clearly", async () => {
  const { createGitHubPullRequest } = await loadTypeScriptModule(
    "../src/adk/githubIntegration.ts",
  );
  const originalFetch = globalThis.fetch;
  const originalCrypto = globalThis.crypto;
  globalThis.fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    if (String(url).endsWith("/repos/acme/agent")) return jsonResponse(200, {});
    if (String(url).includes("/git/ref/heads/main")) {
      return jsonResponse(200, { object: { sha: "base-sha" } });
    }
    if (method === "POST" && String(url).endsWith("/git/refs")) return jsonResponse(201, {});
    if (method === "GET" && String(url).includes("/contents/")) return jsonResponse(404, {});
    if (method === "PUT") {
      return jsonResponse(403, {
        message: "refusing to allow a Personal Access Token to create or update workflow `.github/workflows/test.yml` without workflow scope",
      });
    }
    if (method === "DELETE") return jsonResponse(204);
    throw new Error(`Unexpected request: ${method} ${url}`);
  };
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: { randomUUID: () => "12345678-1234-1234-1234-123456789012" },
  });

  try {
    await assert.rejects(
      createGitHubPullRequest(
        {
          repository: "acme/agent",
          baseBranch: "main",
          token: "github-secret-token",
          files: [{
            path: ".github/workflows/test.yml",
            content: "test",
            commitMessage: "test",
          }],
          branchPrefix: "feat/test",
          title: "test",
          description: "test",
        },
        new AbortController().signal,
      ),
      /缺少 Workflows 写权限/,
    );
  } finally {
    globalThis.fetch = originalFetch;
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: originalCrypto,
    });
  }
});
