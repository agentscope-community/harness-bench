/*
 * Copyright 2024-2026 the original author or authors.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package io.agentscope.examples.eval;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.agentscope.core.agent.RuntimeContext;
import io.agentscope.core.event.AgentEvent;
import io.agentscope.core.event.AgentEventType;
import io.agentscope.core.message.Msg;
import io.agentscope.core.model.ChatResponse;
import io.agentscope.core.model.ChatUsage;
import io.agentscope.core.model.GenerateOptions;
import io.agentscope.core.model.Model;
import io.agentscope.core.model.ToolSchema;
import io.agentscope.core.permission.PermissionContextState;
import io.agentscope.core.permission.PermissionMode;
import io.agentscope.core.state.JsonFileAgentStateStore;
import io.agentscope.core.tool.Toolkit;
import io.agentscope.core.tool.mcp.McpClientBuilder;
import io.agentscope.core.tool.mcp.McpClientWrapper;
import io.agentscope.extensions.model.openai.OpenAIChatModel;
import io.agentscope.harness.agent.HarnessAgent;
import io.agentscope.harness.agent.context.ContextPolicy;
import io.agentscope.harness.agent.context.ContextTokenEstimator;
import io.agentscope.harness.agent.filesystem.spec.LocalFilesystemSpec;
import io.agentscope.harness.agent.memory.compaction.CompactionConfig;
import io.agentscope.harness.agent.tools.ToolsConfig;
import java.io.BufferedWriter;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import reactor.core.publisher.Flux;

/** Runs the production Harness with its workspace tools; records native evidence for the adapter. */
public final class HarnessEvalCli {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String ADAPTER_VERSION = "1.4.0";
    private static final String IDENTITY_PROMPT_VERSION = "coding-agent-v1";
    // Harness is a general framework and ships no root role prompt. Evaluation treats all
    // three harnesses as terminal coding agents, so the adapter supplies only that product
    // configuration; it contains no task, benchmark, or dataset material.
    static final String IDENTITY_PROMPT =
            "You are a coding agent working in a terminal workspace. "
                    + "Inspect files, run commands, and modify code to complete the user's task. "
                    + "Work only through the function tools provided in each request; "
                    + "run command-line programs with the execute tool.";

    private HarnessEvalCli() {}

    public static void main(String[] args) throws IOException {
        if (args.length == 1 && args[0].equals("--version")) {
            System.out.println("agentscope-harness-eval " + ADAPTER_VERSION);
            return;
        }
        if (args.length != 3) {
            throw new IllegalArgumentException(
                    "Usage: HarnessEvalCli REQUEST_JSON NATIVE_JSONL WORKSPACE");
        }
        JsonNode request = JSON.readTree(Files.readString(Path.of(args[0])));
        JsonNode events = request.path("events");
        if (!events.isArray() || events.isEmpty()) {
            throw new IllegalArgumentException("Expected a non-empty events array");
        }
        // Validate the entire request before executing any input.
        for (JsonNode event : events) {
            if (!event.path("type").asText().equals("user.message")
                    || !event.path("content").isArray()
                    || event.path("content").isEmpty()) {
                throw new IllegalArgumentException("This batch CLI accepts text user.message only");
            }
            for (JsonNode block : event.path("content")) {
                if (!block.path("type").asText().equals("text")
                        || !block.path("text").isTextual()) {
                    throw new IllegalArgumentException("Only text content is supported");
                }
            }
        }
        Path workspace = Path.of(args[2]).toAbsolutePath().normalize();
        Files.createDirectories(workspace);
        String sessionId =
                System.getenv().getOrDefault("EVAL_SESSION_ID", UUID.randomUUID().toString());
        Path state =
                Path.of(
                        System.getenv()
                                .getOrDefault(
                                        "EVAL_STATE_DIR",
                                        Path.of(args[1])
                                                .toAbsolutePath()
                                                .getParent()
                                                .resolve("workspace-state")
                                                .toString()));
        String apiKey = System.getenv("MODEL_API_KEY");
        if (apiKey == null || apiKey.isBlank()) apiKey = System.getenv("DASHSCOPE_API_KEY");
        // Defaults keep the historical behaviour; the Harbor adapter overrides them per run.
        Map<String, Object> extraBody =
                JSON.readValue(
                        System.getenv()
                                .getOrDefault(
                                        "EVAL_MODEL_EXTRA_BODY", "{\"enable_thinking\":false}"),
                        new TypeReference<Map<String, Object>>() {});
        GenerateOptions.Builder options =
                GenerateOptions.builder()
                        .maxTokens(
                                Integer.parseInt(
                                        System.getenv()
                                                .getOrDefault("EVAL_MODEL_MAX_TOKENS", "16384")));
        String temperature = System.getenv().getOrDefault("EVAL_MODEL_TEMPERATURE", "");
        if (!temperature.isBlank()) options.temperature(Double.parseDouble(temperature));
        if (!extraBody.isEmpty()) options.additionalBodyParams(extraBody);
        var modelBuilder =
                OpenAIChatModel.builder()
                        .apiKey(apiKey)
                        .baseUrl(System.getenv("MODEL_BASE_URL"))
                        .modelName(System.getenv("MODEL_ID"))
                        .stream(true)
                        .generateOptions(options.build());
        // Unknown Bailian model names infer no window; the harness then falls back to
        // 64000 input tokens while compaction triggers on a different fallback. Reporting
        // the real window keeps both thresholds consistent and avoids false budget stops.
        int contextWindowTokens =
                Integer.parseInt(System.getenv().getOrDefault("EVAL_MODEL_CONTEXT_WINDOW", "0"));
        if (contextWindowTokens > 0) modelBuilder.contextWindowSize(contextWindowTokens);
        var model = modelBuilder.build();
        Toolkit toolkit = mcpToolkit(System.getenv("EVAL_MCP_SERVERS"));
        AtomicBoolean exceeded = new AtomicBoolean();
        int maxIters = configuredMaxIters();
        boolean iterationLimited = false;
        boolean failed = false;
        try (BufferedWriter output =
                Files.newBufferedWriter(
                        Path.of(args[1]),
                        StandardCharsets.UTF_8,
                        StandardOpenOption.CREATE,
                        StandardOpenOption.APPEND)) {
            var builder =
                    HarnessAgent.builder()
                            .name("HarnessAgent")
                            .model(observe(model, output))
                            .toolkit(toolkit)
                            .workspace(state)
                            .stateStore(new JsonFileAgentStateStore(state.resolve("sessions")))
                            .contextPolicy(
                                    new ContextPolicy(
                                            0,
                                            0,
                                            0,
                                            ContextTokenEstimator.approximate(),
                                            manifest ->
                                                    write(
                                                            output,
                                                            Map.of(
                                                                    "type",
                                                                    "eval.context",
                                                                    "createdAt",
                                                                    Instant.now().toString(),
                                                                    "manifest",
                                                                    manifest))))
                            .filesystem(
                                    new LocalFilesystemSpec()
                                            .project(workspace)
                                            .projectWritable(true))
                            .maxIters(maxIters)
                            // Unattended evaluation has no user to confirm HITL prompts.
                            // Without this, any ASK-gated tool (e.g. tau3's
                            // start_conversation) stops the run instead of executing it.
                            .permissionContext(
                                    PermissionContextState.builder()
                                            .mode(PermissionMode.BYPASS)
                                            .build());
            String systemContext = System.getenv("EVAL_SYSTEM_PROMPT");
            // Always set the identity prompt; a supplied system context is appended, never
            // substituted, matching Pi's --append-system-prompt semantics.
            builder.sysPrompt(
                    systemContext == null || systemContext.isBlank()
                            ? IDENTITY_PROMPT
                            : IDENTITY_PROMPT + "\n\n" + systemContext);
            String skillsDir = System.getenv("EVAL_SKILLS_DIR");
            if (skillsDir != null && !skillsDir.isBlank()) {
                builder.projectGlobalSkillsDir(Path.of(skillsDir));
            }
            if (System.getenv("EVAL_COMPACTION_MESSAGES") != null) {
                builder.compaction(
                        CompactionConfig.builder()
                                .triggerMessages(
                                        Integer.parseInt(System.getenv("EVAL_COMPACTION_MESSAGES")))
                                .triggerTokens(
                                        Integer.parseInt(
                                                System.getenv()
                                                        .getOrDefault(
                                                                "EVAL_COMPACTION_TOKENS", "12000")))
                                .keepMessages(2)
                                .keepTokens(1000)
                                .flushBeforeCompact(
                                        Boolean.parseBoolean(
                                                System.getenv()
                                                        .getOrDefault(
                                                                "EVAL_FLUSH_BEFORE_COMPACT",
                                                                "true")))
                                .build());
            }
            if (System.getenv("EVAL_DENY_TOOL") != null) {
                ToolsConfig toolsConfig = new ToolsConfig();
                toolsConfig.setDeny(List.of(System.getenv("EVAL_DENY_TOOL")));
                builder.toolsConfig(toolsConfig);
            }
            if (Boolean.parseBoolean(System.getenv("EVAL_DISABLE_MEMORY_HOOKS"))) {
                builder.disableMemoryHooks();
            }
            HarnessAgent agent = builder.build();
            try {
                Map<String, Object> binding = new LinkedHashMap<>();
                binding.put("type", "eval.binding");
                binding.put("createdAt", Instant.now().toString());
                binding.put("session_id", sessionId);
                binding.put(
                        "candidate_sha", System.getenv().getOrDefault("CANDIDATE_SHA", "unknown"));
                binding.put(
                        "candidate_source_sha256",
                        System.getenv().getOrDefault("CANDIDATE_SOURCE_SHA256", "unknown"));
                binding.put("tools", agent.getToolkit().getToolNames());
                binding.put("profile", "local-project-writable");
                binding.put("adapter_version", ADAPTER_VERSION);
                binding.put("identity_prompt_version", IDENTITY_PROMPT_VERSION);
                binding.put("identity_prompt_sha256", sha256(IDENTITY_PROMPT));
                write(output, binding);
                for (JsonNode input : events) {
                    StringBuilder text = new StringBuilder();
                    for (JsonNode block : input.path("content")) {
                        if (!text.isEmpty()) text.append('\n');
                        text.append(block.path("text").asText());
                    }
                    write(
                            output,
                            Map.of(
                                    "type",
                                    "eval.input",
                                    "id",
                                    UUID.randomUUID().toString(),
                                    "createdAt",
                                    Instant.now().toString(),
                                    "content",
                                    input.path("content")));
                    var stream =
                            agent.streamEvents(
                                            text.toString(),
                                            RuntimeContext.builder()
                                                    .sessionId(sessionId)
                                                    .userId(
                                                            System.getenv()
                                                                    .getOrDefault(
                                                                            "EVAL_USER_ID",
                                                                            sessionId))
                                                    .build())
                                    .doOnNext(event -> record(output, event, exceeded));
                    long timeoutSeconds =
                            Long.parseLong(
                                    System.getenv().getOrDefault("TURN_TIMEOUT_SECONDS", "0"));
                    // Zero means the Harbor trial budget is the only deadline. A positive
                    // value bounds one input's whole event stream (all iterations).
                    if (timeoutSeconds > 0) {
                        stream.blockLast(Duration.ofSeconds(timeoutSeconds));
                    } else {
                        stream.blockLast();
                    }
                    write(
                            output,
                            Map.of(
                                    "type",
                                    "eval.tools_after",
                                    "createdAt",
                                    Instant.now().toString(),
                                    "tools",
                                    agent.getToolkit().getToolNames()));
                    if (exceeded.get()) {
                        // A step cap is a normal stop, not an infrastructure failure: other
                        // harnesses stop at their turn limit and let the verifier score the
                        // workspace. Exiting 1 here erased trials whose work was complete.
                        iterationLimited = true;
                        write(
                                output,
                                Map.of(
                                        "type",
                                        "eval.iteration_limit",
                                        "createdAt",
                                        Instant.now().toString(),
                                        "max_iters",
                                        maxIters));
                        break;
                    }
                }
                Map<String, Object> completed = new LinkedHashMap<>();
                completed.put("type", "eval.completed");
                completed.put("createdAt", Instant.now().toString());
                if (iterationLimited) completed.put("stopReason", "iteration_limit");
                write(output, completed);
            } catch (Exception error) {
                // Provider exceptions can contain request details; retain only their class here.
                write(
                        output,
                        Map.of(
                                "type",
                                "eval.failed",
                                "createdAt",
                                Instant.now().toString(),
                                "error_type",
                                error.getClass().getSimpleName(),
                                "iteration_limit",
                                exceeded.get()));
                System.err.println(
                        "Harness evaluation failed: " + error.getClass().getSimpleName());
                failed = true;
            } finally {
                agent.close();
            }
        } finally {
            toolkit.closeMcpClients();
        }
        if (failed) System.exit(1);
    }

    /**
     * Builds the toolkit Harness registers its workspace tools onto, adding task MCP servers.
     *
     * <p>EVAL_MCP_SERVERS is a JSON list of Harbor MCP server configs: {name, transport, url,
     * headers} for sse/streamable-http, or {name, transport: "stdio", command, args}.
     */
    static Toolkit mcpToolkit(String config) throws IOException {
        Toolkit toolkit = new Toolkit();
        McpClientWrapper pending = null;
        try {
            if (config == null || config.isBlank()) return toolkit;
            for (JsonNode server : JSON.readTree(config)) {
                String name = server.path("name").asText();
                String transport = server.path("transport").asText("sse");
                McpClientBuilder client = McpClientBuilder.create(name);
                switch (transport) {
                    case "streamable-http" ->
                            client.streamableHttpTransport(server.path("url").asText());
                    case "sse" -> client.sseTransport(server.path("url").asText());
                    case "stdio" ->
                            client.stdioTransport(
                                    server.path("command").asText(),
                                    JSON.convertValue(
                                            server.path("args"),
                                            new TypeReference<List<String>>() {}),
                                    Map.of());
                    default ->
                            throw new IllegalArgumentException(
                                    "Unsupported MCP transport for " + name + ": " + transport);
                }
                if (server.path("headers").isObject()) {
                    client.headers(
                            JSON.convertValue(
                                    server.path("headers"),
                                    new TypeReference<Map<String, String>>() {}));
                }
                pending = client.timeout(Duration.ofSeconds(120)).buildAsync().block();
                toolkit.registerMcpClient(pending).block(Duration.ofSeconds(120));
                // The toolkit owns the wrapper only after registration completes.
                pending = null;
            }
            return toolkit;
        } catch (IOException | RuntimeException | Error error) {
            try {
                if (pending != null) {
                    // Wrapper close is idempotent, including after failed registration cleanup.
                    try {
                        pending.close();
                    } catch (RuntimeException cleanupError) {
                        error.addSuppressed(cleanupError);
                    }
                }
            } finally {
                toolkit.closeMcpClients();
            }
            throw error;
        }
    }

    private static void record(BufferedWriter output, AgentEvent event, AtomicBoolean exceeded) {
        // Forwarded subagent events carry a slash-separated source path. A subagent's cap is
        // part of that subagent's own result; only the root agent's cap ends this input.
        if (event.getType() == AgentEventType.EXCEED_MAX_ITERS && event.getSource() == null) {
            exceeded.set(true);
        }
        write(output, event);
    }

    /** Absent MAX_TURNS relies on the Harbor trial budget; an explicit value is validated. */
    private static int configuredMaxIters() {
        String configured = System.getenv("MAX_TURNS");
        if (configured == null || configured.isBlank()) return Integer.MAX_VALUE;
        return Integer.parseInt(configured);
    }

    private static String sha256(String value) {
        try {
            byte[] digest =
                    MessageDigest.getInstance("SHA-256")
                            .digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte b : digest) hex.append(String.format("%02x", b));
            return hex.toString();
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is unavailable", error);
        }
    }

    private static Model observe(Model delegate, BufferedWriter output) {
        return new Model() {
            @Override
            public String getModelName() {
                return delegate.getModelName();
            }

            @Override
            public Flux<ChatResponse> stream(
                    List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                return Flux.defer(
                        () -> {
                            String id = UUID.randomUUID().toString();
                            AtomicReference<ChatUsage> usage = new AtomicReference<>();
                            write(
                                    output,
                                    Map.of(
                                            "type",
                                            "eval.model_start",
                                            "id",
                                            id,
                                            "createdAt",
                                            Instant.now().toString(),
                                            "messages",
                                            messages,
                                            "tools",
                                            tools == null ? List.of() : tools));
                            return delegate.stream(messages, tools, options)
                                    .doOnNext(
                                            response -> {
                                                if (response.getUsage() != null)
                                                    usage.set(response.getUsage());
                                            })
                                    .doOnComplete(
                                            () ->
                                                    write(
                                                            output,
                                                            Map.of(
                                                                    "type",
                                                                    "eval.model_end",
                                                                    "id",
                                                                    id,
                                                                    "createdAt",
                                                                    Instant.now().toString(),
                                                                    "usage",
                                                                    usage.get() == null
                                                                            ? Map.of()
                                                                            : usage.get())))
                                    .doOnError(
                                            error ->
                                                    write(
                                                            output,
                                                            Map.of(
                                                                    "type",
                                                                    "eval.model_error",
                                                                    "id",
                                                                    id,
                                                                    "createdAt",
                                                                    Instant.now().toString(),
                                                                    "error_type",
                                                                    error.getClass()
                                                                            .getSimpleName())));
                        });
            }
        };
    }

    private static synchronized void write(BufferedWriter output, Object value) {
        try {
            output.write(JSON.writeValueAsString(value));
            output.newLine();
            output.flush();
        } catch (IOException error) {
            throw new UncheckedIOException(error);
        }
    }
}
