import fs from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";

const DEFAULTS = {
  enabled: true,
  workspaceRoot: "",
  engramDir: "",
  pythonBin: "",
  agentsDir: "",
  topK: 8,
  maxChars: 6000,
  maxMemories: 8,
  debug: false,
  storeAssistantMessages: true,
  storeUserMessages: true,
  includeSystemPromptAddition: true,
  ownsCompaction: false,
  keepRecentMessages: 12,
  // Cache settings (new)
  assembleCacheTtlMs: 3 * 60 * 1000,   // re-query Neo4j at most once every 3 min per session
  pinnedCacheTtlMs: 10 * 60 * 1000,    // pinned facts change rarely — cache 10 min per session
};

// ─── Session-scoped assembly cache ───────────────────────────────────────────
// Keyed by sessionId. Stores { termsHash, result, expiresAt } so we skip
// redundant Neo4j subprocess calls when the conversation topic hasn't changed.
const _assembleCache = new Map();  // sessionId → { termsHash, result, expiresAt }
const _pinnedCache   = new Map();  // sessionId → { facts, expiresAt }

function hashTerms(terms) {
  // Stable hash of sorted search terms — cheap change detector.
  return Array.isArray(terms) ? [...terms].sort().join("|") : "";
}

function getCachedAssembly(sessionId, termsHash, ttlMs) {
  const entry = _assembleCache.get(sessionId);
  if (!entry) return null;
  if (Date.now() > entry.expiresAt) { _assembleCache.delete(sessionId); return null; }
  if (entry.termsHash !== termsHash) return null;  // topic shifted — miss
  return entry.result;
}

function setCachedAssembly(sessionId, termsHash, result, ttlMs) {
  _assembleCache.set(sessionId, { termsHash, result, expiresAt: Date.now() + ttlMs });
}

function getCachedPinned(sessionId, ttlMs) {
  const entry = _pinnedCache.get(sessionId);
  if (!entry) return null;
  if (Date.now() > entry.expiresAt) { _pinnedCache.delete(sessionId); return null; }
  return entry.facts;
}

function setCachedPinned(sessionId, facts, ttlMs) {
  _pinnedCache.set(sessionId, { facts, expiresAt: Date.now() + ttlMs });
}

function log(cfg, level, message, extra = undefined) {
  if (!cfg?.debug && level === "debug") return;
  const suffix = extra ? ` ${JSON.stringify(extra)}` : "";
  console[level === "error" ? "error" : "log"](`[engram-context-engine] ${message}${suffix}`);
}

function safeRun(cfg, label, fallback, fn) {
  try {
    return fn();
  } catch (err) {
    log(cfg, "error", `${label} failed`, { error: String(err?.stack || err) });
    return fallback;
  }
}

function loadEngramConfig(engramDir) {
  return safeRun({}, "loadEngramConfig", {}, () => {
    const cfgPath = path.join(engramDir, "config.json");
    if (fs.existsSync(cfgPath)) return JSON.parse(fs.readFileSync(cfgPath, "utf-8"));
    return {};
  });
}

function getConfig(api) {
  // api.config may be the full openclaw.json or the plugin-scoped config block.
  // Handle both: extract the plugin entry config if we detect the full config shape.
  let pluginCfg = api?.config ?? {};
  if (pluginCfg.plugins?.entries?.["engram-context-engine"]?.config) {
    pluginCfg = pluginCfg.plugins.entries["engram-context-engine"].config;
  } else if (pluginCfg.gateway || pluginCfg.channels || pluginCfg.agents) {
    // This is clearly the full config, but our entry is missing — use defaults only
    pluginCfg = {};
  }
  const merged = { ...DEFAULTS, ...pluginCfg };
  const homeDir = process.env.HOME || process.env.USERPROFILE || "";

  if (!merged.workspaceRoot) merged.workspaceRoot = path.join(homeDir, "clawd");
  if (!merged.engramDir) merged.engramDir = path.join(merged.workspaceRoot, "engram");
  if (!merged.pythonBin) merged.pythonBin = path.join(merged.workspaceRoot, ".venv-memory", "bin", "python");
  if (!merged.agentsDir) merged.agentsDir = path.join(homeDir, ".openclaw", "agents");

  // Engram's own config is fallback-only. OpenClaw plugin config remains authoritative.
  const ce = loadEngramConfig(merged.engramDir)?.context_engine || {};
  if (!pluginCfg.workspaceRoot && ce.workspace_root) merged.workspaceRoot = ce.workspace_root;
  if (!pluginCfg.engramDir && ce.engram_dir) merged.engramDir = ce.engram_dir;
  if (!pluginCfg.pythonBin && ce.python_bin) merged.pythonBin = ce.python_bin;
  if (!pluginCfg.agentsDir && ce.agents_dir) merged.agentsDir = ce.agents_dir;

  return merged;
}

const _liveSessionAgentCache = new Map();

function extractAgentFromPath(sessionFile) {
  if (!sessionFile) return null;
  return String(sessionFile).match(/[\\/]agents[\\/]([^\\/]+)[\\/]sessions[\\/]/)?.[1] || null;
}

function extractAgentFromSessionKey(sessionId) {
  // OpenClaw session keys often look like "agent:<agentId>:discord:channel:xxx"
  // or contain the agent ID after "agent:" prefix
  const match = String(sessionId || "").match(/^agent:([^:]+):/);
  return match ? match[1] : null;
}

function resolveAgentId(cfg, sessionId, sessionFile) {
  const cached = _liveSessionAgentCache.get(sessionId);
  if (cached) return cached;

  // 1. Try extracting from session file path (most reliable)
  const fromPath = extractAgentFromPath(sessionFile);
  if (fromPath) {
    _liveSessionAgentCache.set(sessionId, fromPath);
    return fromPath;
  }

  // 2. Try extracting from session key format (agent:<id>:...)
  const fromKey = extractAgentFromSessionKey(sessionId);
  if (fromKey) {
    _liveSessionAgentCache.set(sessionId, fromKey);
    return fromKey;
  }

  // 3. Scan agent dirs for the session file (UUID-based sessions)
  return safeRun(cfg, "resolveAgentId", "main", () => {
    const agents = fs.readdirSync(cfg.agentsDir, { withFileTypes: true });
    for (const entry of agents) {
      if (!entry.isDirectory()) continue;
      const sessDir = path.join(cfg.agentsDir, entry.name, "sessions");
      // Check exact .jsonl match
      const sessFile = path.join(sessDir, `${sessionId}.jsonl`);
      if (fs.existsSync(sessFile)) {
        _liveSessionAgentCache.set(sessionId, entry.name);
        return entry.name;
      }
      // Also check sessions.json index for UUID mapping
      const indexFile = path.join(sessDir, "sessions.json");
      if (fs.existsSync(indexFile)) {
        try {
          const index = JSON.parse(fs.readFileSync(indexFile, "utf8"));
          // sessions.json maps channel keys to session UUIDs or vice versa
          const sessions = Array.isArray(index) ? index : Object.values(index);
          for (const s of sessions) {
            const sid = typeof s === "string" ? s : s?.id || s?.sessionId || "";
            if (sid === sessionId) {
              _liveSessionAgentCache.set(sessionId, entry.name);
              return entry.name;
            }
          }
        } catch { /* ignore parse errors */ }
      }
    }
    log(cfg, "warn", `resolveAgentId: could not resolve agent for session ${sessionId}, defaulting to main`);
    return "main";
  });
}

function normalizeMessages(messages) {
  return (Array.isArray(messages) ? messages : [])
    .map((msg) => {
      const role = msg?.role || "user";
      let text = "";
      if (typeof msg?.text === "string") text = msg.text;
      else if (typeof msg?.content === "string") text = msg.content;
      else if (Array.isArray(msg?.content)) {
        text = msg.content
          .map((part) => typeof part?.text === "string" ? part.text : "")
          .filter(Boolean)
          .join("\n");
      }
      return { role, text: String(text || "").trim() };
    })
    .filter((m) => m.text.length > 0);
}

function appendJsonl(file, record) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, `${JSON.stringify(record)}\n`, "utf8");
}

function summarizeRecord(msg) {
  const text = String(msg.text || "").replace(/\s+/g, " ").trim();
  return text.length > 240 ? `${text.slice(0, 237)}...` : text;
}

function shouldStoreLiveTurn(msg) {
  const role = String(msg?.role || "").trim();
  const text = String(msg?.text || "").trim();
  if (!text || text.length < 20) return false;
  if (role !== "user") return false;
  if (text === "NO_REPLY" || text === "HEARTBEAT_OK") return false;
  const lower = text.toLowerCase();
  if (lower.includes("heartbeat") && text.length < 80) return false;
  // Don't filter based on envelope metadata here — Python side strips it.
  // Only filter pure noise that has no human content at all.
  if (lower.includes("toolcall") || lower.includes("toolresult")) return false;
  return true;
}

function storeLiveTurn(cfg, sessionId, agentId, msg) {
  return safeRun(cfg, "storeLiveTurn", undefined, () => {
    if (!shouldStoreLiveTurn(msg)) return undefined;
    const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
    const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: agentId };
    const res = spawnSync(
      cfg.pythonBin,
      [script, "store_live", "--text", msg.text, "--agent", agentId, "--session", sessionId, "--role", msg.role],
      { encoding: "utf8", env, timeout: 5000 }
    );
    if (res.error) {
      log(cfg, "error", "store_live spawn failed", { error: String(res.error) });
      return undefined;
    }
    if (res.status !== 0) {
      log(cfg, "error", "store_live exited non-zero", { status: res.status, stderr: String(res.stderr || "").trim().slice(0, 500) });
    }
    const out = String(res.stdout || "").trim();
    if (!out) return undefined;
    try {
      return JSON.parse(out);
    } catch (err) {
      log(cfg, "error", "store_live returned invalid JSON", { error: String(err), stdout: out.slice(0, 500) });
      return undefined;
    }
  });
}

function extractSpeakerFromText(text) {
  // Try to extract speaker name from OpenClaw Discord envelope metadata embedded in the message
  const patterns = [
    /"sender"\s*:\s*"([^"]+)"/,
    /"name"\s*:\s*"([^"]+)"/,
    /"label"\s*:\s*"([^"(]+)/,
  ];
  for (const pat of patterns) {
    const m = pat.exec(String(text || ""));
    if (m) {
      const name = m[1].trim();
      if (name && name.length >= 2 && !["user","assistant","system","true","false","untrusted"].includes(name.toLowerCase())) {
        return name;
      }
    }
  }
  return null;
}

function storeLiveLLM(cfg, sessionId, agentId, msg) {
  return safeRun(cfg, "storeLiveLLM", undefined, () => {
    if (msg?.role !== "user") return undefined;
    if (!shouldStoreLiveTurn(msg)) return undefined;
    if (String(msg?.text || "").length <= 30) return undefined;
    const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
    const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: agentId };
    // Extract speaker from envelope metadata so facts are attributed to the real human name
    const speaker = extractSpeakerFromText(msg.text);
    const args = [script, "extract_llm", "--text", msg.text, "--agent", agentId, "--session", sessionId];
    if (speaker) args.push("--speaker", speaker);
    const child = spawn(
      cfg.pythonBin,
      args,
      { env, stdio: "ignore", detached: true }
    );
    child.on("error", (err) => {
      log(cfg, "error", "extract_llm spawn failed", { error: String(err) });
    });
    child.unref();
    return { started: true };
  });
}

function looksDurable(text) {
  const lower = String(text || "").toLowerCase();
  return ["remember this", "remember ", "favorite", "prefers", "likes", "birthday", "policy", "always", "never", "project", "important", "working on", "best friend", "aka"].some((n) => lower.includes(n));
}

function storageBase(cfg) {
  const base = path.join(cfg.workspaceRoot, ".engram-live-memory");
  fs.mkdirSync(path.join(base, "session"), { recursive: true });
  fs.mkdirSync(path.join(base, "shared"), { recursive: true });
  return base;
}

function safeKey(value) {
  return String(value || "unknown").replace(/[^a-zA-Z0-9._-]+/g, "_");
}

function sessionStorePath(cfg, sessionId) {
  return path.join(storageBase(cfg), "session", `${safeKey(sessionId)}.jsonl`);
}

function sharedStorePath(cfg, agentId) {
  return path.join(storageBase(cfg), "shared", `${safeKey(agentId)}.jsonl`);
}

function persistMessages(cfg, sessionId, agentId, messages, source = "turn") {
  return safeRun(cfg, "persistMessages", 0, () => {
    const normalized = normalizeMessages(messages);
    const sessionFile = sessionStorePath(cfg, sessionId);
    const sharedFile = sharedStorePath(cfg, agentId);
    let count = 0;
    for (const msg of normalized) {
      if (msg.role === "assistant" && !cfg.storeAssistantMessages) continue;
      if (msg.role === "user" && !cfg.storeUserMessages) continue;
      const record = { ts: Date.now(), sessionId, agentId, role: msg.role, source, text: msg.text, summary: summarizeRecord(msg) };
      appendJsonl(sessionFile, record);
      if (looksDurable(msg.text)) appendJsonl(sharedFile, record);
      count += 1;
    }
    return count;
  });
}

function buildSearchTerms(messages) {
  const recent = normalizeMessages(messages).slice(-6);
  const text = recent.map((m) => m.text).join(" \n ").toLowerCase();
  return [...new Set(text.split(/[^a-z0-9_#@.-]+/).filter((t) => t.length >= 4))].slice(0, 24);
}

function queryEngramMulti(cfg, searchTerms, agentId) {
  return safeRun(cfg, "queryEngramMulti", [], () => {
    const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
    const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: agentId || "main" };
    const res = spawnSync(cfg.pythonBin, [script, "query", searchTerms.join(" "), "--agent", agentId || "main", "--limit", String(cfg.topK || 8), "--json"], { encoding: "utf8", env, timeout: 15000 });
    if (res.status !== 0) return [];
    const out = String(res.stdout || "").trim();
    if (!out) return [];
    return JSON.parse(out);
  });
}

function getPinnedConfig(cfg) {
  const engramCfg = loadEngramConfig(cfg.engramDir || path.join(cfg.workspaceRoot, "engram"));
  const pinned = engramCfg?.context_engine?.pinned_injection || {};
  return {
    enabled: pinned.enabled !== false,
    maxPinned: Number(pinned.max_pinned || 5),
    minImportance: Number(pinned.min_importance || 0.9),
  };
}

function extractChannelId(sessionId) {
  const match = String(sessionId || "").match(/^[^:]+:[^:]+:[^:]+:channel:([^:]+)$/);
  return match ? match[1] : null;
}

function queryPinnedFacts(cfg, agentId, scope = {}) {
  return safeRun(cfg, "queryPinnedFacts", [], () => {
    const pinnedCfg = getPinnedConfig(cfg);
    if (!pinnedCfg.enabled) return [];
    const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
    const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: agentId || "main" };
    const args = [script, "pinned", "--agent", agentId || "main", "--limit", String(pinnedCfg.maxPinned)];
    if (scope.channelId) args.push("--channel", String(scope.channelId));
    if (scope.sessionId) args.push("--session", String(scope.sessionId));
    const res = spawnSync(cfg.pythonBin, args, { encoding: "utf8", env, timeout: 5000 });
    if (res.status !== 0) return [];
    const out = String(res.stdout || "").trim();
    if (!out) return [];
    try {
      const parsed = JSON.parse(out);
      return Array.isArray(parsed.facts) ? parsed.facts : [];
    } catch {
      return [];
    }
  });
}

function formatEngramResults(results) {
  if (!results || typeof results !== "object") return "";
  // Handle structured response: {ok, entities, facts, episodes}
  const entities = Array.isArray(results.entities) ? results.entities : [];
  const facts = Array.isArray(results.facts) ? results.facts : [];
  const episodes = Array.isArray(results.episodes) ? results.episodes : [];
  // Also handle legacy flat array
  const flatItems = Array.isArray(results) ? results : [];
  const allItems = [...facts, ...entities, ...episodes, ...flatItems];
  if (!allItems.length) return "";
  const lines = ["Relevant Engram memory:"];
  const seen = new Set();
  for (const r of allItems.slice(0, 12)) {
    const text = String(r?.content || r?.text || r?.description || r?.summary || "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    const name = r?.name ? `[${r.name}] ` : "";
    lines.push(`- ${name}${text}`);
  }
  return lines.length > 1 ? lines.join("\n") : "";
}

function sessionFileFor(cfg, sessionId, sessionFile) {
  if (sessionFile && fs.existsSync(sessionFile)) return sessionFile;
  const agentId = resolveAgentId(cfg, sessionId, sessionFile);
  const byAgent = path.join(cfg.agentsDir, agentId, "sessions", `${sessionId}.jsonl`);
  if (fs.existsSync(byAgent)) return byAgent;
  return null;
}

function loadSessionTranscript(cfg, sessionId, sessionFile) {
  return safeRun(cfg, "loadSessionTranscript", [], () => {
    const file = sessionFileFor(cfg, sessionId, sessionFile);
    if (!file || !fs.existsSync(file)) return [];
    return fs.readFileSync(file, "utf8")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => { try { return JSON.parse(line); } catch { return null; } })
      .filter(Boolean);
  });
}

function normalizeTranscriptMessages(entries) {
  const out = [];
  for (const entry of Array.isArray(entries) ? entries : []) {
    const msg = entry?.message;
    if (!msg) continue;
    const role = msg.role || "user";
    let text = "";
    if (typeof msg.content === "string") text = msg.content;
    else if (Array.isArray(msg.content)) {
      text = msg.content
        .map((part) => typeof part?.text === "string" ? part.text : "")
        .filter(Boolean)
        .join("\n");
    }
    text = String(text || "").trim();
    if (!text) continue;
    out.push({ role, text, timestamp: entry.timestamp || null });
  }
  return out;
}

function stripCompactionNoise(messages) {
  return (Array.isArray(messages) ? messages : []).filter((m) => {
    const text = String(m.text || "").trim();
    if (!text) return false;
    if (text.startsWith("Compaction skipped: Engram")) return false;
    if (text.length > 12000 && (text.includes("toolCall") || text.includes("toolResult"))) return false;
    return true;
  });
}

function splitTranscriptForCompaction(messages, opts = {}) {
  const keepRecent = Math.max(8, Number(opts.keepRecentMessages || 12));
  if (!Array.isArray(messages) || messages.length <= keepRecent + 4) return { olderMessages: [], recentTail: messages || [] };
  return { olderMessages: messages.slice(0, -keepRecent), recentTail: messages.slice(-keepRecent) };
}

function isEnvelopeNoise(text) {
  // Filter out Discord message envelopes, raw JSON blobs, tool outputs
  const t = String(text || "").trim();
  if (t.startsWith("{") || t.startsWith("[")) return true;           // raw JSON
  if (t.includes('"message_id"') || t.includes('"sender_id"')) return true;  // Discord envelope
  if (t.includes('"sender_label"') || t.includes('"has_reply_context"')) return true;
  if (t.includes("Conversation info (untrusted metadata)")) return true;
  if (t.includes("Config warnings:") || t.includes("plugins.entries")) return true;
  if (t.includes("toolCall") || t.includes("toolResult")) return true;
  if (t.includes("Process exited with code")) return true;
  if (t.length > 800) return true;  // anything this long in durable memory is probably noise
  return false;
}

function extractExplicitDurableMemories(messages) {
  const needles = ["remember this", "remember ", "always ", "never ", "favorite", "prefers", "likes ", "birthday", "anniversary", "policy", "working on", "project is"];
  return (Array.isArray(messages) ? messages : [])
    .filter((m) => m.role === "user" || m.role === "assistant")
    .map((m) => ({ ...m, lower: String(m.text || "").toLowerCase() }))
    .filter((m) => needles.some((n) => m.lower.includes(n)))
    .filter((m) => !isEnvelopeNoise(m.text))  // strip raw metadata/JSON blobs
    .map((m) => ({ role: m.role, text: m.text, summary: summarizeRecord(m) }));
}

function dedupeDurableRecords(records) {
  const seen = new Set();
  const out = [];
  for (const rec of Array.isArray(records) ? records : []) {
    const key = String(rec.summary || rec.text || "").toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(rec);
  }
  return out;
}

function estimateTokenCountFromText(text) {
  return Math.ceil(String(text || "").length / 4);
}

function bulletize(lines) {
  const clean = lines.map((s) => String(s || "").trim()).filter(Boolean);
  if (!clean.length) return ["- None"];
  return clean.slice(0, 8).map((s) => `- ${s}`);
}

function buildStructuredCompactionSummary(messages) {
  const items = Array.isArray(messages) ? messages : [];
  // Filter envelope noise before building summary bullets
  const clean = (msgs) => msgs.filter((t) => !isEnvelopeNoise(t));
  const userMsgs = clean(items.filter((m) => m.role === "user").map((m) => m.text));
  const asstMsgs = clean(items.filter((m) => m.role === "assistant").map((m) => m.text));
  const objective = bulletize(userMsgs.slice(-3).map((t) => summarizeRecord({ text: t })));
  const established = bulletize(asstMsgs.slice(-5).map((t) => summarizeRecord({ text: t })));
  const decisions = bulletize(
    clean(items.map((m) => m.text))
      .filter((t) => /decid|will do|going to|fixed|changed|disabled|enabled/i.test(t))
      .map((t) => summarizeRecord({ text: t }))
      .slice(-6)
  );
  const openLoops = bulletize(
    clean(items.map((m) => m.text))
      .filter((t) => /todo|next|need to|follow up|pending|block|later/i.test(t))
      .map((t) => summarizeRecord({ text: t }))
      .slice(-6)
  );
  return ["Compacted session state", "", "Objective:", ...objective, "", "Established facts:", ...established, "", "Decisions made:", ...decisions, "", "Open loops:", ...openLoops].join("\n");
}

function buildCompactedMessages(summary, recentTail) {
  const msgs = [{ role: "system", content: `Compacted context generated by Engram. Use it as working memory summary for this session.\n\n${String(summary || "")}` }];
  for (const m of Array.isArray(recentTail) ? recentTail : []) msgs.push({ role: m.role || "user", content: m.text || "" });
  return msgs;
}

export default function register(api) {
  const cfg = getConfig(api);
  log(cfg, "log", "register called", { ownsCompaction: !!cfg.ownsCompaction });

  // ─── engram_search tool: on-demand Neo4j graph memory search ───
  api.registerTool(
    {
      name: "engram_search",
      label: "Engram Search",
      description:
        "Search Engram's Neo4j graph memory for facts, entities, and episodes. " +
        "Use this for recall questions about people, preferences, past events, decisions, or any stored knowledge. " +
        "Returns structured results from the knowledge graph including facts, entities, and temporal episodes. " +
        "Preferred over memory_search for personal/contextual recall.",
      parameters: {
        type: "object",
        properties: {
          query: {
            type: "string",
            description: "Natural language search query (e.g. 'TheDev favorite soda', 'Lady2good kids', 'car insurance policy')",
          },
          agent: {
            type: "string",
            description: "Agent ID to search within. Defaults to 'main'.",
          },
          maxResults: {
            type: "number",
            description: "Maximum number of results to return. Defaults to 8.",
          },
        },
        required: ["query"],
      },
      async execute(_toolCallId, params) {
        const query = String(params?.query || "").trim();
        if (!query) {
          return {
            content: [{ type: "text", text: JSON.stringify({ ok: false, error: "query parameter is required" }) }],
            details: { ok: false },
          };
        }
        const agentId = String(params?.agent || "main").trim();
        const limit = Number(params?.maxResults) || cfg.topK || 8;

        try {
          const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
          const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: agentId };
          const res = spawnSync(
            cfg.pythonBin,
            [script, "query", query, "--agent", agentId, "--limit", String(limit), "--json"],
            { encoding: "utf8", env, timeout: 15000 }
          );

          if (res.status !== 0) {
            const stderr = String(res.stderr || "").trim().slice(0, 500);
            log(cfg, "error", "engram_search query failed", { status: res.status, stderr });
            return {
              content: [{ type: "text", text: JSON.stringify({ ok: false, error: "query failed", stderr }) }],
              details: { ok: false },
            };
          }

          const out = String(res.stdout || "").trim();
          if (!out) {
            return {
              content: [{ type: "text", text: JSON.stringify({ ok: true, results: [], message: "No results found" }) }],
              details: { ok: true, empty: true },
            };
          }

          const parsed = JSON.parse(out);
          const formatted = formatEngramResults(parsed);
          const resultCount =
            (Array.isArray(parsed?.facts) ? parsed.facts.length : 0) +
            (Array.isArray(parsed?.entities) ? parsed.entities.length : 0) +
            (Array.isArray(parsed?.episodes) ? parsed.episodes.length : 0);

          return {
            content: [{ type: "text", text: formatted || "No relevant memories found." }],
            details: { ok: true, resultCount, raw: parsed },
          };
        } catch (err) {
          log(cfg, "error", "engram_search execute error", { error: String(err?.stack || err) });
          return {
            content: [{ type: "text", text: JSON.stringify({ ok: false, error: String(err) }) }],
            details: { ok: false },
          };
        }
      },
    },
    { names: ["engram_search"], optional: true }
  );

  // ─── engram_store tool: explicitly store a fact/memory into Neo4j ───
  api.registerTool(
    {
      name: "engram_store",
      label: "Engram Store",
      description:
        "Store a specific fact, preference, or memory into Engram's Neo4j graph. " +
        "Use when someone explicitly says 'remember this' or when you identify durable information worth persisting. " +
        "Facts are stored with agent scope and can be recalled cross-session.",
      parameters: {
        type: "object",
        properties: {
          text: {
            type: "string",
            description: "The fact or memory to store (e.g. 'TheDev\\'s favorite soda is RootBeer')",
          },
          agent: {
            type: "string",
            description: "Agent ID to store under. Defaults to 'main'.",
          },
        },
        required: ["text"],
      },
      async execute(_toolCallId, params) {
        const text = String(params?.text || "").trim();
        if (!text) {
          return {
            content: [{ type: "text", text: JSON.stringify({ ok: false, error: "text parameter is required" }) }],
            details: { ok: false },
          };
        }
        const agentId = String(params?.agent || "main").trim();

        try {
          const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
          const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: agentId };
          const res = spawnSync(
            cfg.pythonBin,
            [script, "store_live", "--text", text, "--agent", agentId, "--session", "manual", "--role", "user"],
            { encoding: "utf8", env, timeout: 10000 }
          );

          if (res.status !== 0) {
            // Fallback to LLM extraction for richer parsing
            const llmRes = spawnSync(
              cfg.pythonBin,
              [script, "extract_llm", "--text", text, "--agent", agentId, "--session", "manual"],
              { encoding: "utf8", env, timeout: 15000 }
            );
            const llmOk = llmRes.status === 0;
            return {
              content: [{ type: "text", text: JSON.stringify({ ok: llmOk, method: "llm_extract", text }) }],
              details: { ok: llmOk, method: "llm_extract" },
            };
          }

          const out = String(res.stdout || "").trim();
          let result = {};
          try { result = out ? JSON.parse(out) : {}; } catch { result = { raw: out }; }

          return {
            content: [{ type: "text", text: JSON.stringify({ ok: true, stored: true, ...result }) }],
            details: { ok: true, stored: true },
          };
        } catch (err) {
          log(cfg, "error", "engram_store execute error", { error: String(err?.stack || err) });
          return {
            content: [{ type: "text", text: JSON.stringify({ ok: false, error: String(err) }) }],
            details: { ok: false },
          };
        }
      },
    },
    { names: ["engram_store"], optional: true }
  );

  log(cfg, "log", "registered engram_search and engram_store tools");

  // ─── Memory Plugin: replace memory-core with Engram Neo4j backend ────────
  // Registers memory_search and memory_get tools backed by the knowledge graph.
  // memory-core must be disabled in config so Engram wins the exclusive memory slot.

  api.registerMemoryPromptSection(({ availableTools }) => {
    const hasSearch = availableTools.has("memory_search");
    const hasGet = availableTools.has("memory_get");
    if (!hasSearch && !hasGet) return [];
    const lines = [
      "## Memory Recall",
      "Before answering questions about prior work, people, preferences, decisions, or past events: " +
      "run memory_search to query the Engram knowledge graph (Neo4j). " +
      "Results include facts, entities, and temporal episodes with importance scores. " +
      "Prefer memory_search over guessing from context alone.",
    ];
    if (hasGet) lines.push("Use memory_get to read specific memory files when a path is known.");
    lines.push("");
    return lines;
  });

  // memory_search — Neo4j graph-backed semantic search
  api.registerTool(
    {
      name: "memory_search",
      label: "Memory Search (Engram)",
      description:
        "Mandatory recall step: search Engram's Neo4j knowledge graph for facts, people, preferences, " +
        "decisions, and past events before answering memory questions. Returns ranked results with importance scores.",
      parameters: {
        type: "object",
        properties: {
          query: {
            type: "string",
            description: "Natural language search query (e.g. 'TheDev favorite drink', 'Lady2good kids', 'car insurance')",
          },
          maxResults: {
            type: "number",
            description: "Maximum results to return (default: 8)",
          },
        },
        required: ["query"],
      },
      async execute(_toolCallId, params) {
        const query = String(params?.query || "").trim();
        if (!query) {
          return {
            content: [{ type: "text", text: JSON.stringify({ ok: false, error: "query parameter is required", disabled: false }) }],
          };
        }
        const limit = Number(params?.maxResults) || cfg.topK || 8;

        try {
          const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
          const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: "main" };
          const res = spawnSync(
            cfg.pythonBin,
            [script, "query", query, "--agent", "main", "--limit", String(limit), "--json"],
            { encoding: "utf8", env, timeout: 15000 }
          );

          if (res.status !== 0) {
            const stderr = String(res.stderr || "").trim().slice(0, 300);
            log(cfg, "error", "memory_search (engram) failed", { stderr });
            return {
              content: [{ type: "text", text: JSON.stringify({ ok: false, error: "search failed", disabled: false, stderr }) }],
            };
          }

          const raw = String(res.stdout || "").trim();
          const jsonStart = raw.lastIndexOf("\n{");
          const jsonStr = jsonStart >= 0 ? raw.slice(jsonStart + 1) : raw;
          let parsed;
          try { parsed = JSON.parse(jsonStr); } catch { parsed = { facts: [], entities: [], episodes: [], ok: false }; }

          // Merge facts + entities + episodes into unified results array
          const facts = (parsed.facts || []).map((r) => ({
            path: "engram://facts",
            score: r.importance ?? 0.5,
            snippet: r.content || r.fact || String(r),
            source: r.source_type || "graph",
            agent: r.agent_id || "main",
          }));
          const entities = (parsed.entities || []).map((r) => ({
            path: "engram://entities",
            score: r.importance ?? 0.6,
            snippet: r.name ? `${r.name}${r.summary ? ": " + r.summary : ""}` : String(r),
            source: "entity",
            agent: "main",
          }));
          const episodes = (parsed.episodes || []).map((r) => ({
            path: `engram://episodes/${r.source_file || ""}`,
            score: r.importance ?? 0.4,
            snippet: r.summary || String(r),
            source: "episode",
            agent: "main",
          }));
          // Interleave: facts first (most relevant), then entities, then episodes
          const results = [...facts, ...entities, ...episodes].slice(0, 15);

          return {
            content: [{ type: "text", text: JSON.stringify({ results, provider: "engram-neo4j", ok: true }) }],
          };
        } catch (err) {
          return {
            content: [{ type: "text", text: JSON.stringify({ results: [], ok: false, error: String(err), disabled: false }) }],
          };
        }
      },
    },
    { names: ["memory_search"] }
  );

  // memory_get — read a specific memory file (falls back to file system, same as memory-core)
  api.registerTool(
    {
      name: "memory_get",
      label: "Memory Get (Engram)",
      description:
        "Read a specific memory file (e.g. MEMORY.md, memory/2026-03-23.md). " +
        "Use after memory_search when you need the full content of a specific file.",
      parameters: {
        type: "object",
        properties: {
          path: {
            type: "string",
            description: "Relative path to the memory file (e.g. 'MEMORY.md', 'memory/2026-03-23.md')",
          },
          from: {
            type: "number",
            description: "Line number to start reading from (1-indexed, optional)",
          },
          lines: {
            type: "number",
            description: "Number of lines to read (optional, reads all if omitted)",
          },
        },
        required: ["path"],
      },
      async execute(_toolCallId, params) {
        const relPath = String(params?.path || "").trim();
        if (!relPath) {
          return {
            content: [{ type: "text", text: JSON.stringify({ path: "", text: "", disabled: true, error: "path is required" }) }],
          };
        }

        try {
          const workspaceRoot = cfg.workspaceRoot || process.env.HOME + "/clawd";
          const fullPath = path.isAbsolute(relPath) ? relPath : path.join(workspaceRoot, relPath);
          const rawContent = fs.readFileSync(fullPath, "utf8");
          const allLines = rawContent.split("\n");

          const fromLine = params?.from ? Number(params.from) : 1;
          const lineCount = params?.lines ? Number(params.lines) : allLines.length;
          const sliced = allLines.slice(fromLine - 1, fromLine - 1 + lineCount).join("\n");

          return {
            content: [{ type: "text", text: JSON.stringify({ path: relPath, text: sliced, disabled: false }) }],
          };
        } catch (err) {
          return {
            content: [{ type: "text", text: JSON.stringify({ path: relPath, text: "", disabled: true, error: String(err) }) }],
          };
        }
      },
    },
    { names: ["memory_get"] }
  );

  log(cfg, "log", "registered memory_search and memory_get (Engram Neo4j backend)");

  api.registerContextEngine("engram-context-engine", () => {
    const engine = {
      info: {
        id: "engram-context-engine",
        name: "Engram Context Engine",
        version: "1.0.2",
        ownsCompaction: Boolean(cfg.ownsCompaction)
      },

      async bootstrap({ sessionId, sessionFile }) {
        return safeRun(cfg, "bootstrap", { bootstrapped: true, importedMessages: 0, reason: "engram-fallback" }, () => {
          const agentId = resolveAgentId(cfg, sessionId, sessionFile);
          // Warm pinned cache at session start so first assemble() is instant
          if (!getCachedPinned(sessionId, cfg.pinnedCacheTtlMs)) {
            const channelId = extractChannelId(sessionId);
            const pinned = queryPinnedFacts(cfg, agentId, { channelId, sessionId });
            setCachedPinned(sessionId, pinned, cfg.pinnedCacheTtlMs);
            log(cfg, "debug", "bootstrap: warmed pinned cache", { agentId, count: pinned?.length || 0 });
          }
          return { bootstrapped: true, importedMessages: 0, reason: "engram-direct" };
        });
      },

      async ingest({ sessionId, message }) {
        return safeRun(cfg, "ingest", { ingested: false }, () => {
          const agentId = resolveAgentId(cfg, sessionId, null);
          const count = persistMessages(cfg, sessionId, agentId, [message], "ingest");
          return { ingested: count > 0 };
        });
      },

      async ingestBatch({ sessionId, messages }) {
        return safeRun(cfg, "ingestBatch", { ingestedCount: 0 }, () => {
          const agentId = resolveAgentId(cfg, sessionId, null);
          const count = persistMessages(cfg, sessionId, agentId, messages, "ingest-batch");
          return { ingestedCount: count };
        });
      },

      async afterTurn({ sessionId, sessionFile, messages, prePromptMessageCount }) {
        return safeRun(cfg, "afterTurn", undefined, () => {
          const agentId = resolveAgentId(cfg, sessionId, sessionFile);
          const newMessages = Array.isArray(messages) ? messages.slice(Math.max(0, prePromptMessageCount || 0)) : [];
          const normalized = normalizeMessages(newMessages);
          if (normalized.length) {
            persistMessages(cfg, sessionId, agentId, normalized, "afterTurn");
            for (const msg of normalized) {
              // Only store user messages via live extraction — assistant messages are our own output
              if (msg?.role !== "user") continue;
              // Skip regex path — it produces low-quality facts (timestamps, decisions, noise).
              // Go straight to LLM extraction for all substantial user messages.
              if (String(msg?.text || "").length > 30 && shouldStoreLiveTurn(msg)) {
                storeLiveLLM(cfg, sessionId, agentId, msg);
              }
            }
          }
          return undefined;
        });
      },

      async assemble({ sessionId, messages }) {
        return safeRun(cfg, "assemble", { messages, estimatedTokens: 0 }, () => {
          const agentId = resolveAgentId(cfg, sessionId, null);
          const channelId = extractChannelId(sessionId);

          // ── 1. Pinned facts: cached per-session, TTL 10 min ──────────────
          let pinned = getCachedPinned(sessionId, cfg.pinnedCacheTtlMs);
          if (!pinned) {
            pinned = queryPinnedFacts(cfg, agentId, { channelId, sessionId });
            setCachedPinned(sessionId, pinned, cfg.pinnedCacheTtlMs);
            log(cfg, "debug", "assemble: pinned cache miss — queried Neo4j");
          } else {
            log(cfg, "debug", "assemble: pinned cache hit");
          }

          const pinnedLines = [];
          const pinnedSeen = new Set();
          for (const p of (pinned || [])) {
            const text = String(p?.content || "").trim();
            if (text && !pinnedSeen.has(text)) {
              pinnedSeen.add(text);
              pinnedLines.push(`- ${text}`);
            }
          }

          // ── 2. Context query: skip if same terms & within TTL ────────────
          const searchTerms = buildSearchTerms(messages);
          const termsHash = hashTerms(searchTerms);
          let queryAddition = "";

          if (searchTerms && searchTerms.length >= 3) {
            const cached = getCachedAssembly(sessionId, termsHash, cfg.assembleCacheTtlMs);
            if (cached !== null) {
              log(cfg, "debug", "assemble: context cache hit", { termsHash });
              queryAddition = cached;
            } else {
              log(cfg, "debug", "assemble: context cache miss — querying Neo4j", { termsHash });
              const results = queryEngramMulti(cfg, searchTerms, agentId);
              if (results && typeof results === "object") {
                const facts = Array.isArray(results.facts)
                  ? results.facts.filter((f) => !pinnedSeen.has(String(f?.content || "").trim()))
                  : [];
                results.facts = facts;
              }
              queryAddition = formatEngramResults(results);
              setCachedAssembly(sessionId, termsHash, queryAddition, cfg.assembleCacheTtlMs);
            }
          }

          const parts = [];
          if (pinnedLines.length) {
            parts.push("Standing rules:\n" + pinnedLines.join("\n"));
          }
          if (queryAddition) {
            parts.push(queryAddition);
          }
          const addition = parts.join("\n\n");

          return { messages, estimatedTokens: 0, systemPromptAddition: cfg.includeSystemPromptAddition && addition ? addition : undefined };
        });
      },

      async compact({ sessionId, sessionFile }) {
        return safeRun(cfg, "compact", { ok: true, compacted: false, reason: `Engram compaction fallback for ${sessionId}.` }, () => {
          if (!cfg.ownsCompaction) {
            return { ok: true, compacted: false, reason: `Engram compaction ownership disabled for ${sessionId}; defer to default runtime compaction.` };
          }
          const agentId = resolveAgentId(cfg, sessionId, sessionFile || null);
          const transcript = loadSessionTranscript(cfg, sessionId, sessionFile || null);
          const normalized = stripCompactionNoise(normalizeTranscriptMessages(transcript));
          const { olderMessages, recentTail } = splitTranscriptForCompaction(normalized, { keepRecentMessages: cfg.keepRecentMessages || 12 });
          if (!olderMessages.length) {
            return { ok: true, compacted: false, reason: `Engram found too little older history to compact for ${sessionId}.` };
          }
          const durable = dedupeDurableRecords(extractExplicitDurableMemories(olderMessages));
          if (durable.length) persistMessages(cfg, sessionId, agentId, durable.map((d) => ({ role: d.role, text: d.text })), "compaction-durable");
          const summary = buildStructuredCompactionSummary(olderMessages);
          const messages = buildCompactedMessages(summary, recentTail);
          const tokensBefore = estimateTokenCountFromText(olderMessages.map((m) => m.text).join("\n"));
          const tokensAfter = estimateTokenCountFromText(messages.map((m) => String(m.content || "")).join("\n"));

          // Write compaction summary to memory/*.md so cron pipeline can ingest it
          try {
            const memoryDir = path.join(cfg.workspaceRoot, "memory");
            fs.mkdirSync(memoryDir, { recursive: true });
            const now = new Date();
            const dateStr = now.toISOString().slice(0, 10);
            const hash = sessionId.replace(/[^a-zA-Z0-9]/g, "").slice(0, 8);
            const memFile = path.join(memoryDir, `${dateStr}-${agentId}-${hash}.md`);

            const lines = [];
            lines.push(`# ${dateStr} — Compaction flush (${agentId})`);
            lines.push(`Session: ${sessionId}`);
            lines.push(`Agent: ${agentId}`);
            lines.push(`Compacted: ${olderMessages.length} older messages → ${recentTail.length} recent kept`);
            lines.push(`Tokens: ${tokensBefore} → ${tokensAfter}`);
            lines.push("");

            if (durable.length) {
              lines.push("## Durable Memories");
              for (const d of durable) {
                const text = String(d.text || d.summary || "").trim();
                if (text) lines.push(`- ${text.slice(0, 300)}`);
              }
              lines.push("");
            }

            lines.push("## Session Summary");
            lines.push(String(summary || "No summary generated."));
            lines.push("");

            // Include key user messages for richer context (skip envelope noise)
            const userMsgs = olderMessages.filter((m) => m.role === "user" && !isEnvelopeNoise(m.text));
            if (userMsgs.length) {
              lines.push("## Key Messages");
              for (const m of userMsgs.slice(-8)) {
                const text = String(m.text || "").trim();
                if (text && text.length > 20 && text.length < 400) {
                  lines.push(`- [${m.role}] ${text.slice(0, 240)}`);
                }
              }
              lines.push("");
            }

            fs.writeFileSync(memFile, lines.join("\n"), "utf8");
            log(cfg, "log", `compact: wrote memory flush to ${memFile}`, { bytes: lines.join("\n").length, durable: durable.length });
          } catch (flushErr) {
            log(cfg, "error", "compact: memory flush write failed", { error: String(flushErr) });
          }

          return { ok: true, compacted: true, result: { tokensBefore, tokensAfter, summary, messages, durableMemoriesPersisted: durable.length, recentTailMessages: recentTail.length, compactedOlderMessages: olderMessages.length } };
        });
      },

      async prepareSubagentSpawn() {
        return { rollback: async () => {} };
      },

      async onSubagentEnded() { return; }
    };

    return engine;
  });
}
