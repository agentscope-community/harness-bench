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
import io.agentscope.core.event.ToolResultEndEvent;
import io.agentscope.core.journal.AtomicThreadJournal;
import io.agentscope.core.journal.ThreadJournalStore;
import io.agentscope.core.message.Msg;
import io.agentscope.core.message.ToolResultBlock;
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
import io.agentscope.core.tool.search.ToolSearchMode;
import io.agentscope.extensions.model.openai.OpenAIChatModel;
import io.agentscope.harness.agent.HarnessAgent;
import io.agentscope.harness.agent.context.ContextPolicy;
import io.agentscope.harness.agent.context.ContextTokenEstimator;
import io.agentscope.harness.agent.filesystem.spec.LocalFilesystemSpec;
import io.agentscope.harness.agent.journal.LocalJournalStorage;
import io.agentscope.harness.agent.memory.compaction.CompactionConfig;
import io.agentscope.harness.agent.tools.ToolsConfig;
import java.io.BufferedWriter;
import java.io.IOException;
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
    private static final String ADAPTER_VERSION = "1.5.0";
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

    public static void main(String[] args) {
        try {
            run(args);
        } catch (Exception error) {
            // Setup/cleanup can fail outside the stream. Never print request-bearing messages.
            if (args.length == 3) {
                try (BufferedWriter output =
                        Files.newBufferedWriter(
                                Path.of(args[1]),
                                StandardCharsets.UTF_8,
                                StandardOpenOption.CREATE,
                                StandardOpenOption.APPEND)) {
                    write(
                            output,
                            Map.of(
                                    "type",
                                    "eval.failed",
                                    "createdAt",
                                    Instant.now().toString(),
                                    "error_type",
                                    error.getClass().getSimpleName(),
                                    "phase",
                                    "adapter_setup_or_cleanup"));
                } catch (Exception evidenceError) {
                    System.err.println(
                            "Could not record adapter failure: "
                                    + evidenceError.getClass().getSimpleName());
                }
            }
            System.err.println("Harness evaluation failed: " + error.getClass().getSimpleName());
            System.exit(1);
        }
    }

    private static void run(String[] args) throws IOException {
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
                        env(
                                "EVAL_MODEL_EXTRA_BODY_SECRET",
                                env("EVAL_MODEL_EXTRA_BODY", "{\"enable_thinking\":false}")),
                        new TypeReference<Map<String, Object>>() {});
        int maxTokens = Integer.parseInt(env("EVAL_MODEL_MAX_TOKENS", "16384"));
        GenerateOptions.Builder options = GenerateOptions.builder().maxTokens(maxTokens);
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
        // An explicit window overrides name inference. The observer below must pass it
        // through unchanged so native compaction and budget checks use the same capacity.
        int contextWindowTokens =
                Integer.parseInt(System.getenv().getOrDefault("EVAL_MODEL_CONTEXT_WINDOW", "0"));
        if (contextWindowTokens > 0) modelBuilder.contextWindowSize(contextWindowTokens);
        var model = modelBuilder.build();
        Toolkit toolkit = mcpToolkit(System.getenv("EVAL_MCP_SERVERS"));
        // Kernel defaults are evaluated as shipped; the switches exist for A/B runs and for
        // reproducing results recorded with the 1.4 adapter.
        boolean journalEnabled = Boolean.parseBoolean(env("EVAL_JOURNAL_ENABLED", "true"));
        boolean repeatedFailureGuard =
                !Boolean.parseBoolean(env("EVAL_DISABLE_REPEATED_FAILURE_GUARD", "false"));
        boolean toolResultEviction =
                !Boolean.parseBoolean(env("EVAL_DISABLE_TOOL_RESULT_EVICTION", "false"));
        String readFileMaxChars = env("EVAL_READ_FILE_MAX_CHARS", "");
        String configuredTurns = env("MAX_TURNS", "");
        AtomicBoolean exceeded = new AtomicBoolean();
        ToolFailureStats toolFailures = new ToolFailureStats();
        int maxIters = configuredMaxIters();
        boolean iterationLimited = false;
        boolean failed = false;
        try (BufferedWriter output =
                Files.newBufferedWriter(
                        Path.of(args[1]),
                        StandardCharsets.UTF_8,
                        StandardOpenOption.CREATE,
                        StandardOpenOption.APPEND)) {
            // Journaling is the 2.1 session path. Keep its files under the evidence state
            // directory: the workspace default writes into the task workspace, which the
            // verifier scores and diffs.
            Path journalRoot = state.resolve("journal");
            ThreadJournalStore journalStore =
                    (key, context) ->
                            new AtomicThreadJournal(
                                    new LocalJournalStorage(journalRoot), key.storagePath());
            var builder =
                    HarnessAgent.builder()
                            .name("HarnessAgent")
                            .model(observe(model, output))
                            .toolkit(toolkit)
                            .workspace(state)
                            .stateStore(new JsonFileAgentStateStore(state.resolve("sessions")))
                            .journalEnabled(journalEnabled)
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
            if (journalEnabled) builder.threadJournalStore(journalStore);
            if (!repeatedFailureGuard) builder.disableRepeatedFailureGuard();
            if (!toolResultEviction) builder.disableToolResultEviction();
            if (!readFileMaxChars.isBlank()) {
                builder.readFileMaxChars(Integer.parseInt(readFileMaxChars));
            }
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
                binding.put("harness_version", packageVersion(HarnessAgent.class));
                binding.put("identity_prompt_version", IDENTITY_PROMPT_VERSION);
                binding.put("identity_prompt_sha256", sha256(IDENTITY_PROMPT));
                binding.put("journal_enabled", journalEnabled);
                binding.put("repeated_failure_guard", repeatedFailureGuard);
                binding.put("tool_result_eviction", toolResultEviction);
                binding.put("read_file_max_chars", readFileMaxChars);
                binding.put("max_turns", configuredTurns);
                binding.put("max_iters", maxIters);
                binding.put("context_window_tokens", model.getContextWindowSize());
                binding.put("max_tokens", maxTokens);
                binding.put(
                        "temperature",
                        temperature.isBlank() ? null : Double.parseDouble(temperature));
                Object thinking = extraBody.get("enable_thinking");
                if (thinking instanceof Boolean) binding.put("enable_thinking", thinking);
                binding.put(
                        "disable_memory_hooks",
                        Boolean.parseBoolean(env("EVAL_DISABLE_MEMORY_HOOKS", "false")));
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
                                    .doOnNext(
                                            event -> record(output, event, exceeded, toolFailures));
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
                completed.put("tool_failures", toolFailures.summary());
                write(output, completed);
            } catch (Exception error) {
                // Provider exceptions can contain request details; retain only their class here.
                Map<String, Object> failure = new LinkedHashMap<>();
                failure.put("type", "eval.failed");
                failure.put("createdAt", Instant.now().toString());
                failure.put("error_type", error.getClass().getSimpleName());
                failure.put("iteration_limit", exceeded.get());
                failure.put("tool_failures", toolFailures.summary());
                write(output, failure);
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

    private static void record(
            BufferedWriter output,
            AgentEvent event,
            AtomicBoolean exceeded,
            ToolFailureStats failures) {
        // Forwarded subagent events carry a slash-separated source path. A subagent's cap is
        // part of that subagent's own result; only the root agent's cap ends this input.
        if (event.getType() == AgentEventType.EXCEED_MAX_ITERS && event.getSource() == null) {
            exceeded.set(true);
        }
        if (event instanceof ToolResultEndEvent result) {
            Map<String, Object> metadata = result.getMetadata();
            Object kind =
                    metadata == null ? null : metadata.get(ToolResultBlock.METADATA_ERROR_KIND);
            if (kind instanceof String value && !value.isBlank()) {
                failures.record(value);
                Map<String, Object> failure = new LinkedHashMap<>();
                failure.put("type", "eval.tool_failure");
                failure.put("createdAt", event.getCreatedAt());
                failure.put("tool", result.getToolCallName());
                failure.put("error_kind", value);
                Object scope = metadata.get(ToolResultBlock.METADATA_ERROR_SCOPE);
                if (scope instanceof String text && !text.isBlank()) {
                    failure.put("error_scope", text);
                }
                Object retriable = metadata.get(ToolResultBlock.METADATA_RETRIABLE);
                if (retriable instanceof Boolean flag) failure.put("retriable", flag);
                if (event.getSource() != null) failure.put("source", event.getSource());
                write(output, failure);
            }
        }
        write(output, event);
    }

    /** Counts machine-readable tool failures so one trial row can show the dominant cause. */
    static final class ToolFailureStats {
        private final Map<String, Integer> byKind = new LinkedHashMap<>();
        private int total;

        void record(String kind) {
            total++;
            byKind.merge(kind, 1, Integer::sum);
        }

        Map<String, Object> summary() {
            Map<String, Object> summary = new LinkedHashMap<>();
            summary.put("total", total);
            summary.put("by_kind", new LinkedHashMap<>(byKind));
            return summary;
        }
    }

    private static String env(String name, String fallback) {
        String value = System.getenv(name);
        return value == null ? fallback : value;
    }

    private static String packageVersion(Class<?> type) {
        Package owner = type.getPackage();
        String version = owner == null ? null : owner.getImplementationVersion();
        return version == null || version.isBlank() ? "unknown" : version;
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

    /** SDK primitive zero also means absent upstream data; do not claim a measured zero. */
    static Map<String, Object> observedUsage(ChatUsage usage) {
        Map<String, Object> values = new LinkedHashMap<>();
        values.put("inputTokens", usage.getInputTokens() > 0 ? usage.getInputTokens() : null);
        values.put("outputTokens", usage.getOutputTokens() > 0 ? usage.getOutputTokens() : null);
        values.put("cachedTokens", usage.getCachedTokens() > 0 ? usage.getCachedTokens() : null);
        values.put(
                "cacheCreationTokens",
                usage.getCacheCreationTokens() > 0 ? usage.getCacheCreationTokens() : null);
        values.put(
                "reasoningTokens",
                usage.getReasoningTokens() > 0 ? usage.getReasoningTokens() : null);
        return values;
    }

    static Model observe(Model delegate, BufferedWriter output) {
        return new Model() {
            @Override
            public String getModelName() {
                return delegate.getModelName();
            }

            @Override
            public int getContextWindowSize() {
                return delegate.getContextWindowSize();
            }

            @Override
            public ToolSearchMode getToolSearchMode() {
                return delegate.getToolSearchMode();
            }

            @Override
            public boolean supportsNativeStructuredOutput() {
                return delegate.supportsNativeStructuredOutput();
            }

            @Override
            public boolean supportsNativeStructuredOutputWithTools() {
                return delegate.supportsNativeStructuredOutputWithTools();
            }

            @Override
            public boolean supportsToolChoiceSpecific() {
                return delegate.supportsToolChoiceSpecific();
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
                                                                            : observedUsage(
                                                                                    usage.get()))))
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
            // Evidence is a side path; a write failure must not interrupt native reasoning.
            System.err.println(
                    "Could not write native evidence: " + error.getClass().getSimpleName());
        }
    }
}
