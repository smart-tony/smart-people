// ═══════════════════════════════════════════════════════════════
//  weekly-push-tool 前端 — 晨报推送工作台
// ═══════════════════════════════════════════════════════════════
//
//  目录:
//    §1  API 配置 & 常量
//    §2  默认状态 & 数据结构
//    §3  初始化 & 生命周期 (init, loadState, saveState)
//    §4  工作区持久化 (workspace load/save/schedule)
//    §5  数据工具函数 (hydrateState, normalizeItem, createEmptyItem)
//    §6  UI 状态 & 导航 (expand/collapse, tabs, sections)
//    §7  控件绑定 & 表单同步 (bindControls, writeStateToForm, readFormToForm)
//    §8  条目编辑器 (renderItemsEditor, AI 处理, 文件上传)
//    §9  内容刷新 & 渲染 (refreshAll, summarizeText)
//    §10 来源管理 & 候选池 (loadSourcesConfig, renderSourcesPanel, simulateFetch, renderCandidateList)
//    §11 草稿管理 (loadDraftsList, loadSelectedDraft, clearUrlCache)
//    §12 输出构建 (syncCardDefaults, buildWeeklyHtml, buildPushPayload, buildChecklist)
//    §13 导入解析 (parseMarkdownIntoState)
//    §14 工具函数 (downloadHtml, copyText, showToast, apiFetch, escapeHtml, paragraphs)
//

// ── §1 API 配置 ──────────────────────────────────────────────
let API_BASE = window.location.origin + "/api";
const WORKSPACE_ID = "default";

// ── 默认状态 ──────────────────────────────────────────────
const defaultState = {
  title: "AI 热点技术周报",
  brand: "百运科技",
  startDate: "2026-06-01",
  endDate: "2026-06-07",
  vol: "VOL. 2026.06.07",
  countLabel: "",
  topic: "AI / 大模型 / Agent / 机器人 / 企业应用",
  logoUrl: "https://wework.qpic.cn/wwpic3az/473299_2JxzjfdIQnixsSL_1780245047/0",
  summary: "本期主线是“Agent 平台化 + 模型工程化”：OpenAI 把 Codex 从编程助手推向覆盖产品、设计、数据和知识工作的协作入口；Microsoft 在 Build 2026 上集中补齐企业 Agent 平台、MAI 自研模型、Foundry 运行层、Windows 本地安全执行和 AI 安全治理；Google Research 用 Agentic RAG 强调企业知识回答的可验证性；NVIDIA 则把长推理与内容安全能力放进 Nemotron 系列。模型与应用侧，xAI、Anthropic、H Company、JetBrains、Magenta、MiniMax 的更新集中在多模态创作、科学研究、电脑操作、本地化和高效推理，说明竞争重点不只是“模型更强”，而是“模型能不能进入真实工作流”。",
  ending: "这一周的共同趋势是：模型能力正在被拆进更具体的工作流。OpenAI 和 Microsoft 强调跨角色协作与企业平台，Google 和 NVIDIA 强调可靠 RAG、长任务推理与安全治理，xAI、Anthropic、H Company、JetBrains、Magenta 和 MiniMax 则从创作、科学研究、电脑操作、本地化和高效推理补足实际使用场景。下阶段值得关注的，不只是模型排行榜，而是这些模型能否稳定接入工具、知识、权限和业务流程。",
  pageUrl: "",
  coverUrl: "",
  userId: "18820271886",
  sourceDesc: "百运科技 · AI热点周报",
  cardTitle: "AI热点技术周报｜16条精选",
  cardDesc: "2026.06.01 - 2026.06.07｜Agent / 模型 / 机器人 / 企业应用",
  items: [
    {
      title: "OpenAI 重返机器人赛道，四大核心岗位开招（2026-06-01）",
      tags: ["OpenAI", "机器人", "具身智能"],
      sources: ["https://news.pedaily.cn/202606/564707.shtml"],
      image: "",
      imageAlt: "",
      desc: "OpenAI Robotics 一口气放出电气工程师、仿真环境工程师、执行器设计工程师、控制系统软件工程师四大核心岗位，指向从底层电路板、执行器到仿真环境和控制系统的完整机器人研发链路。"
    },
    {
      title: "H Company 发布 Holo3.1，电脑操作 Agent 强化本地部署与多框架适配（2026-06-01）",
      tags: ["HCompany", "Holo3.1", "ComputerUse"],
      sources: ["https://hcompany.ai/holo3.1"],
      image: "",
      imageAlt: "",
      desc: "Holo3.1 定位为 fast & local computer use agents，面向 Web、桌面和移动环境，新增函数调用协议支持，并发布面向本地推理的模型适配方案。"
    },
    {
      title: "JetBrains 开源 Mellum2，IDE 场景小模型向本地化与低延迟靠近（2026-06-01）",
      tags: ["JetBrains", "Mellum2", "编程模型"],
      sources: ["https://blog.jetbrains.com/ai/2026/06/mellum2-goes-open-source-a-fast-model-for-ai-workflows/"],
      image: "",
      imageAlt: "",
      desc: "Mellum2 是 12B 总参数、2.5B active per token 的 MoE 模型，Apache 2.0 许可开源。面向代码补全、Agent 工具调用等 IDE 内场景，强调本地运行的低延迟与高吞吐。"
    },
    {
      title: "MiniMax M3 发布引发关注，长上下文与 Agent 编码仍需独立验证（2026-06-01）",
      tags: ["MiniMax", "M3", "开源模型"],
      sources: ["https://venturebeat.com/technology/minimax-m3-debuts-eclipsing-gpt-5-5-and-gemini-3-1-pro-on-key-benchmark-performance-for-just-5-10-of-the-cost"],
      image: "",
      imageAlt: "",
      desc: "MiniMax M3 面向 coding、agentic performance、1M context 和 native multimodality。多家媒体报道其基准测试表现，但部分声称缺乏独立验证。"
    },
    {
      title: "OpenAI 发布 Codex for every role，Coding Agent 扩展到跨职能工作流（2026-06-02）",
      tags: ["OpenAI", "Codex", "Agent"],
      sources: ["https://openai.com/index/codex-for-every-role-tool-workflow/"],
      image: "",
      imageAlt: "",
      desc: "Codex 正从单纯面向软件开发的编程助手，扩展为可被分析、营销、运营、设计、研究、投资和银行等角色使用的工作流入口。新增插件、可分享的 Sites、annotations。每周超过 500 万用户，非开发者约占 20%。"
    },
    {
      title: "Microsoft Build 2026 发布 Agent Platform 与 Microsoft IQ（2026-06-02）",
      tags: ["Microsoft", "AgentPlatform", "Build2026"],
      sources: ["https://blogs.microsoft.com/blog/2026/06/02/microsoft-build-2026-be-yourself-at-work/"],
      image: "",
      imageAlt: "",
      desc: "Agent Platform、Microsoft IQ、Work IQ、Fabric IQ、Foundry IQ、Web IQ、Scout 等能力集中发布。微软正在把 AI 能力拆进不同角色和流程。"
    },
    {
      title: "Microsoft AI 发布七个 MAI 自研模型，覆盖推理/代码/图像/语音（2026-06-02）",
      tags: ["MicrosoftAI", "MAI", "模型家族"],
      sources: ["https://microsoft.ai/news/building-a-hillclimbing-machine-launching-seven-new-mai-models/"],
      image: "",
      imageAlt: "",
      desc: "七个内部自研 MAI 模型：MAI-Thinking-1、MAI-Code-1-Flash、MAI-Image-2.5、MAI Transcribe-1.5、MAI-Voice-2 等。微软正在建立不依赖 OpenAI 的自研模型能力。"
    },
    {
      title: "Microsoft Foundry 更新 Agent Service，从原型到生产的运行与评估（2026-06-02）",
      tags: ["MicrosoftFoundry", "AgentService", "生产化"],
      sources: ["https://devblogs.microsoft.com/foundry/agent-service-build2026/"],
      image: "",
      imageAlt: "",
      desc: "Agent Service 构建、部署、运行和优化新能力。Agent 离开原型后遇到知识接入、工具授权、会话隔离、可观测性等问题，Foundry 从平台层统一解决。"
    },
    {
      title: "Windows 11 推出 MXC 与 Aion 本地模型，Agent 需系统级安全边界（2026-06-02）",
      tags: ["Windows", "MXC", "本地Agent"],
      sources: ["https://blogs.windows.com/windowsdeveloper/2026/06/02/build-2026-furthering-windows-as-the-trusted-platform-for-development/"],
      image: "",
      imageAlt: "",
      desc: "Microsoft Execution Containers（MXC）SDK、Agent 365 集成、Aion 本地模型发布。OS 层面的 AI 安全执行边界，为 Agent 提供沙箱运行能力。"
    },
    {
      title: "xAI 发布 Grok Imagine 1.5 Preview，图生视频进入 API 预览（2026-06-03）",
      tags: ["xAI", "Grok", "视频生成"],
      sources: ["https://x.ai/news/grok-imagine-1-5"],
      image: "",
      imageAlt: "",
      desc: "grok-imagine-video-1.5-preview 通过 xAI API 以 preview 形式提供，可以把静态图片转成视频。"
    },
    {
      title: "NVIDIA 推出 Nemotron 3 Ultra，长任务 Agent 推理向高效化推进（2026-06-04）",
      tags: ["NVIDIA", "Nemotron", "推理模型"],
      sources: ["https://developer.nvidia.com/blog/nvidia-nemotron-3-ultra-powers-faster-more-efficient-reasoning-for-long-running-agents/"],
      image: "",
      imageAlt: "",
      desc: "Nemotron 3 Ultra 面向长运行 Agent 的高效推理，强调多步骤规划、工具调用和长链路执行的企业 Agent 场景。"
    },
    {
      title: "Google Magenta 推出 RealTime 2，实时音乐生成进入本地可交互创作（2026-06-04）",
      tags: ["GoogleMagenta", "音频生成", "本地模型"],
      sources: ["https://magenta.withgoogle.com/magenta-realtime-2"],
      image: "",
      imageAlt: "",
      desc: "Magenta RealTime 2 开源权重实时音乐模型，支持 MIDI 控制、低延迟实时交互，标志音频 AI 从离线进入本地实时创作。"
    },
    {
      title: "Google Research 发布 Agentic RAG 方案，企业知识问答强调可追溯（2026-06-05）",
      tags: ["Google", "Gemini", "RAG"],
      sources: ["https://research.google/blog/unlocking-dependable-responses-with-gemini-enterprise-agent-platforms-agentic-rag/"],
      image: "",
      imageAlt: "",
      desc: "Gemini Enterprise Agent Platform 的 Agentic RAG，提升企业知识检索、推理可靠性。强调可追溯性、多步推理验证和引用锚定。"
    },
    {
      title: "Anthropic 发布 Claude 化学研究能力，探索 AI 在谱图理解中的科学应用（2026-06-05）",
      tags: ["Anthropic", "Claude", "AIforScience"],
      sources: ["https://www.anthropic.com/research/making-claude-a-chemist"],
      image: "",
      imageAlt: "",
      desc: "Anthropic 与化学专家合作，让 Claude 服务化学研究。聚焦 NMR 谱图任务，前沿模型正在进入需要领域专家判断的分析场景。"
    },
    {
      title: "DeepSeek V4 做数学证明，500 倍成本优势刷新多项纪录（2026-06-06）",
      tags: ["DeepSeek", "数学证明", "Agent"],
      sources: ["https://www.huxiu.com/article/4864984.html"],
      image: "",
      imageAlt: "",
      desc: "DeepSeek V4 通过智能体系统在多个数学证明基准上刷新纪录，成本为闭源模型的 1/500，中国模型在非生成式任务中找到差异化路径。"
    },
    {
      title: "京东与腾讯深化 AI Agent 合作，电商物流进入多 Agent 协作（2026-06-07）",
      tags: ["京东", "腾讯", "Agent合作"],
      sources: ["https://www.36kr.com/"],
      image: "",
      imageAlt: "",
      desc: "京东与腾讯在 AI Agent 领域深化合作，聚焦电商与物流场景中的多 Agent 协作，覆盖智能客服、仓储调度、配送路径优化等实际业务链路。"
    }
  ]
};

const fields = {
  title: document.querySelector("#titleInput"),
  brand: document.querySelector("#brandInput"),
  startDate: document.querySelector("#startDateInput"),
  endDate: document.querySelector("#endDateInput"),
  vol: document.querySelector("#volInput"),
  countLabel: document.querySelector("#countLabelInput"),
  topic: document.querySelector("#topicInput"),
  logoUrl: document.querySelector("#logoInput"),
  summary: document.querySelector("#summaryInput"),
  ending: document.querySelector("#endingInput"),
  pageUrl: document.querySelector("#pageUrlInput"),
  coverUrl: document.querySelector("#coverUrlInput"),
  userId: document.querySelector("#userIdInput"),
  sourceDesc: document.querySelector("#sourceDescInput"),
  cardTitle: document.querySelector("#cardTitleInput"),
  cardDesc: document.querySelector("#cardDescInput")
};

const sourceFields = {
  module: document.querySelector("#sourceModuleSelect"),
  fetchLimit: document.querySelector("#fetchLimitInput"),
  fetchHint: document.querySelector("#sourceFetchHint"),
  name: document.querySelector("#sourceNameInput"),
  id: document.querySelector("#sourceIdInput"),
  url: document.querySelector("#sourceUrlInput"),
  note: document.querySelector("#sourceNoteInput"),
  output: document.querySelector("#sourcesOutput")
};

const sourceState = {
  config: null,
  currentModule: "ai-weekly"
};

const state = loadState();
const itemsEditor = document.querySelector("#itemsEditor");
const itemTemplate = document.querySelector("#itemTemplate");
const previewFrame = document.querySelector("#previewFrame");
const pushJsonOutput = document.querySelector("#pushJsonOutput");
const saveStatusEl = document.querySelector("#saveStatus");
const uiState = {
  expandedItems: new Set(),
  cardTitleTouched: false,
  cardDescTouched: false,
  serverLoaded: false,
  serverSaveStatus: "idle",
  lastSavedAt: ""
};

init();

async function init() {
  bindTabs();
  bindControls();
  updateSaveStatus();
  await loadWorkspaceFromServer();
  ensureDefaultExpandedItem();
  writeStateToForm();
  renderItemsEditor();
  renderCandidateList();
  refreshAll();
  await loadSourcesConfigToPanel();
  await loadDraftsList();
}

function loadState() {
  const saved = localStorage.getItem("weeklyPushToolState");
  if (!saved) return hydrateStateShape(structuredClone(defaultState));
  try {
    return hydrateStateShape({ ...structuredClone(defaultState), ...JSON.parse(saved) });
  } catch {
    return hydrateStateShape(structuredClone(defaultState));
  }
}

function saveState() {
  localStorage.setItem("weeklyPushToolState", JSON.stringify(state));
  scheduleWorkspaceSave();
}

function buildWorkspacePayload() {
  return {
    state,
    ui_state: {
      expandedItems: [...uiState.expandedItems],
      cardTitleTouched: uiState.cardTitleTouched,
      cardDescTouched: uiState.cardDescTouched
    },
    source_state: {
      currentModule: sourceState.currentModule,
      selectedModule: sourceFields.module?.value || sourceState.currentModule,
      fetchLimit: sourceFields.fetchLimit?.value || "8"
    },
    candidates: normalizeCandidates(window._candidates || []),
    candidate_meta: window._candidateMeta || {},
    client_updated_at: new Date().toISOString()
  };
}

async function loadWorkspaceFromServer() {
  try {
    const resp = await apiFetch(`/workspace/${encodeURIComponent(WORKSPACE_ID)}`);
    const data = await resp.json();
    if (!resp.ok || !data.exists || !data.workspace) return;

    const workspace = data.workspace;
    if (workspace.state && typeof workspace.state === "object") {
      Object.assign(state, hydrateStateShape({ ...structuredClone(defaultState), ...workspace.state }));
    }
    if (workspace.ui_state && typeof workspace.ui_state === "object") {
      uiState.cardTitleTouched = Boolean(workspace.ui_state.cardTitleTouched);
      uiState.cardDescTouched = Boolean(workspace.ui_state.cardDescTouched);
      uiState.expandedItems = new Set(Array.isArray(workspace.ui_state.expandedItems) ? workspace.ui_state.expandedItems : []);
    }
    if (workspace.source_state && typeof workspace.source_state === "object") {
      sourceState.currentModule = workspace.source_state.currentModule || sourceState.currentModule;
      if (sourceFields.module && workspace.source_state.selectedModule) {
        sourceFields.module.value = workspace.source_state.selectedModule;
        sourceState.currentModule = sourceFields.module.value;
      }
      if (sourceFields.fetchLimit && workspace.source_state.fetchLimit) {
        sourceFields.fetchLimit.value = workspace.source_state.fetchLimit;
      }
    }
    window._candidates = normalizeCandidates(workspace.candidates || []);
    window._candidateMeta = workspace.candidate_meta || {};
    uiState.serverLoaded = true;
    uiState.lastSavedAt = data.updated_at || workspace.updated_at || "";
    localStorage.setItem("weeklyPushToolState", JSON.stringify(state));
    updateSaveStatus();
  } catch (e) {
    console.warn("读取服务器工作区失败，继续使用本地缓存:", e);
    uiState.serverSaveStatus = "error";
    updateSaveStatus();
  }
}

function scheduleWorkspaceSave() {
  if (scheduleWorkspaceSave._paused) return;
  window.clearTimeout(scheduleWorkspaceSave._timer);
  uiState.serverSaveStatus = "pending";
  updateSaveStatus();
  scheduleWorkspaceSave._timer = window.setTimeout(saveWorkspaceToServer, 800);
}

async function saveWorkspaceToServer() {
  try {
    uiState.serverSaveStatus = "saving";
    updateSaveStatus();
    const resp = await apiFetch(`/workspace/${encodeURIComponent(WORKSPACE_ID)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildWorkspacePayload())
    });
    const data = await resp.json();
    if (!resp.ok || !data.success) {
      throw new Error(data.detail || data.error || `HTTP ${resp.status}`);
    }
    uiState.serverSaveStatus = "saved";
    uiState.lastSavedAt = data.updated_at || "";
    updateSaveStatus();
  } catch (e) {
    uiState.serverSaveStatus = "error";
    updateSaveStatus();
    console.warn("保存服务器工作区失败:", e);
  }
}

function updateSaveStatus() {
  if (!saveStatusEl) return;
  const status = uiState.serverSaveStatus;
  saveStatusEl.classList.remove("is-saving", "is-saved", "is-error");
  if (status === "saving" || status === "pending") {
    saveStatusEl.textContent = status === "pending" ? "等待自动保存" : "保存中...";
    saveStatusEl.classList.add("is-saving");
    return;
  }
  if (status === "saved") {
    const savedText = uiState.lastSavedAt ? new Date(uiState.lastSavedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
    saveStatusEl.textContent = savedText ? `已保存 ${savedText}` : "已保存";
    saveStatusEl.classList.add("is-saved");
    return;
  }
  if (status === "error") {
    saveStatusEl.textContent = "保存失败";
    saveStatusEl.classList.add("is-error");
    return;
  }
  saveStatusEl.textContent = "自动保存";
}

window.addEventListener("beforeunload", () => {
  try {
    const payload = JSON.stringify(buildWorkspacePayload());
    const url = `${API_BASE}/workspace/${encodeURIComponent(WORKSPACE_ID)}`;
    if (navigator.sendBeacon) {
      navigator.sendBeacon(url, new Blob([payload], { type: "application/json" }));
    }
  } catch {}
});

function hydrateStateShape(target) {
  target.items = Array.isArray(target.items) ? target.items.map(normalizeItem) : [];
  return target;
}

function normalizeItem(item = {}) {
  return {
    id: item.id || createItemId(),
    title: item.title || "",
    tags: Array.isArray(item.tags) ? item.tags : splitTags(item.tags || ""),
    sources: Array.isArray(item.sources) ? item.sources : String(item.sources || "").split(/\n+/).map((x) => x.trim()).filter(Boolean),
    image: item.image || "",
    imageAlt: item.imageAlt || "",
    desc: item.desc || ""
  };
}

function createItemId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `item-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
}

function createEmptyItem(overrides = {}) {
  return normalizeItem({
    title: "新条目标题",
    tags: [],
    sources: [],
    image: "",
    imageAlt: "",
    desc: "",
    ...overrides
  });
}

function isItemExpanded(itemId) {
  return uiState.expandedItems.has(itemId);
}

function setItemExpanded(itemId, expanded) {
  if (!itemId) return;
  if (expanded) uiState.expandedItems.add(itemId);
  else uiState.expandedItems.delete(itemId);
}

function toggleItemExpanded(itemId) {
  setItemExpanded(itemId, !isItemExpanded(itemId));
}

function ensureDefaultExpandedItem() {
  if (!uiState.expandedItems.size && state.items.length) {
    uiState.expandedItems.add(state.items[0].id);
  }
}

function openSection(sectionName) {
  switchToTab(sectionName);
  const panel = document.querySelector(`#tab-${sectionName}`);
  if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function bindTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      switchToTab(tab.dataset.tab);
    });
  });
}

function switchToTab(tabName) {
  document.querySelectorAll(".tab").forEach((el) => el.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach((el) => el.classList.remove("active"));
  const tab = document.querySelector(`.tab[data-tab="${tabName}"]`);
  const panel = document.querySelector(`#tab-${tabName}`);
  if (tab) tab.classList.add("active");
  if (panel) panel.classList.add("active");
}

function bindControls() {
  Object.entries(fields).forEach(([key, input]) => {
    input.addEventListener("input", () => {
      if (key === "cardTitle") uiState.cardTitleTouched = true;
      if (key === "cardDesc") uiState.cardDescTouched = true;
      state[key] = input.value;
      if (key === "title" || key === "startDate" || key === "endDate" || key === "topic") syncCardDefaults();
      refreshAll();
    });
  });

  document.querySelector("#openImportBtn").addEventListener("click", () => openSection("import"));
  document.querySelector("#openSourcesBtn").addEventListener("click", () => openSection("sources"));
  document.querySelector("#openPushBtn").addEventListener("click", () => openSection("push"));
  document.querySelector("#expandAllItemsBtn").addEventListener("click", () => {
    state.items.forEach((item) => setItemExpanded(item.id, true));
    renderItemsEditor();
    scheduleWorkspaceSave();
  });
  document.querySelector("#collapseAllItemsBtn").addEventListener("click", () => {
    uiState.expandedItems.clear();
    renderItemsEditor();
    scheduleWorkspaceSave();
  });
  document.querySelectorAll(".add-item-action").forEach((button) => button.addEventListener("click", () => {
    const nextItem = createEmptyItem();
    state.items.push(nextItem);
    setItemExpanded(nextItem.id, true);
    syncCardDefaults();
    renderItemsEditor();
    refreshAll();
    openSection("items");
  }));
  document.querySelectorAll(".renumber-action").forEach((button) => button.addEventListener("click", refreshAll));
  document.querySelector("#refreshPreviewBtn").addEventListener("click", refreshAll);
  document.querySelector("#downloadHtmlBtn").addEventListener("click", downloadHtml);
  document.querySelector("#loadSampleBtn").addEventListener("click", () => {
    Object.assign(state, structuredClone(defaultState));
    hydrateStateShape(state);
    uiState.expandedItems.clear();
    uiState.cardTitleTouched = false;
    uiState.cardDescTouched = false;
    ensureDefaultExpandedItem();
    syncCardDefaults(true);
    writeStateToForm();
    renderItemsEditor();
    window._candidates = [];
    window._candidateMeta = {};
    renderCandidateList();
    refreshAll();
    showToast("已恢复示例内容");
  });
  document.querySelector("#parseMarkdownBtn").addEventListener("click", parseMarkdownIntoState);
  document.querySelector("#clearMarkdownBtn").addEventListener("click", () => {
    document.querySelector("#markdownInput").value = "";
  });
  document.querySelector("#copyPushJsonBtn").addEventListener("click", (event) => copyText(pushJsonOutput.value, event.currentTarget));
  document.querySelector("#copyChecklistBtn").addEventListener("click", (event) => copyText(buildChecklist(), event.currentTarget));
  document.querySelector("#reloadSourcesBtn").addEventListener("click", loadSourcesConfigToPanel);
  document.querySelector("#addSourceBtn").addEventListener("click", () => {
    addSourceToConfig().catch((e) => alert("新增来源失败: " + e.message));
  });
  document.querySelector("#simulateFetchBtn").addEventListener("click", simulateFetchFromSources);
  document.querySelector("#selectAllCandidatesBtn").addEventListener("click", () => {
    if (window._candidates) {
      window._candidates.forEach(c => c.selected = true);
      renderCandidateList();
      scheduleWorkspaceSave();
    }
  });
  document.querySelector("#deselectAllCandidatesBtn").addEventListener("click", () => {
    if (window._candidates) {
      window._candidates.forEach(c => c.selected = false);
      renderCandidateList();
      scheduleWorkspaceSave();
    }
  });
  document.querySelector("#importCandidatesBtn").addEventListener("click", () => {
    const selected = (window._candidates || []).filter(c => c.selected);
    if (!selected.length) { alert("请先勾选要导入的条目"); return; }
    selected.forEach(c => {
      const nextItem = createEmptyItem({
        title: c.title || "未命名候选条目",
        tags: Array.isArray(c.ai_tags) ? c.ai_tags : [],
        sources: c.source_url ? [c.source_url] : [],
        desc: [c.ai_summary || "", c.ai_analysis || ""].filter(Boolean).join("\n\n"),
      });
      state.items.push(nextItem);
    });
    syncCardDefaults();
    renderItemsEditor();
    refreshAll();
    openSection("items");
    showToast(`已导入 ${selected.length} 条到周报条目`);
  });
  // 草稿加载
  document.querySelector("#loadDraftBtn").addEventListener("click", loadSelectedDraft);
  document.querySelector("#clearCacheBtn").addEventListener("click", clearUrlCache);
  sourceFields.module.addEventListener("change", () => {
    sourceState.currentModule = sourceFields.module.value;
    renderSourcesPanel();
    loadDraftsList();
    scheduleWorkspaceSave();
  });
  sourceFields.fetchLimit.addEventListener("input", () => {
    renderFetchHint();
    scheduleWorkspaceSave();
  });
}

function writeStateToForm() {
  Object.entries(fields).forEach(([key, input]) => {
    input.value = state[key] || "";
  });
}

function readFormToState() {
  Object.entries(fields).forEach(([key, input]) => {
    state[key] = input.value;
  });
}

function renderItemsEditor() {
  if (!uiState.expandedItems.size && state.items.length) {
    uiState.expandedItems.add(state.items[0].id);
  }
  itemsEditor.innerHTML = "";
  state.items.forEach((item, index) => {
    const node = itemTemplate.content.firstElementChild.cloneNode(true);
    const expanded = isItemExpanded(item.id);
    const summaryText = summarizeText(item.desc);
    node.querySelector(".item-number").textContent = `条目 ${index + 1}`;
    node.querySelector(".item-tag-count").textContent = `${(item.tags || []).length} 个标签`;
    node.querySelector(".item-source-count").textContent = `${(item.sources || []).length} 个来源`;
    node.querySelector(".item-image-state").textContent = item.image ? "已带图" : "无图片";
    node.querySelector(".item-title").value = item.title || "";
    node.querySelector(".item-tags").value = (item.tags || []).join(" ");
    node.querySelector(".item-sources").value = (item.sources || []).join("\n");
    node.querySelector(".item-image").value = item.image || "";
    node.querySelector(".item-image-alt").value = item.imageAlt || "";
    node.querySelector(".item-desc").value = item.desc || "";
    node.querySelector(".item-brief").textContent = summaryText || "展开后可编辑正文、来源和图片。";
    node.querySelector(".item-body").hidden = !expanded;
    node.querySelector(".item-toggle").textContent = expanded ? "收起" : "展开";

    node.querySelector(".remove-item").addEventListener("click", () => {
      state.items.splice(index, 1);
      uiState.expandedItems.delete(item.id);
      ensureDefaultExpandedItem();
      syncCardDefaults();
      renderItemsEditor();
      refreshAll();
    });

    node.querySelector(".item-toggle").addEventListener("click", () => {
      toggleItemExpanded(item.id);
      renderItemsEditor();
    });

    node.querySelector(".ai-process-item").addEventListener("click", async () => {
      const btn = node.querySelector(".ai-process-item");
      const taskSelect = node.querySelector(".ai-task-type");
      const taskType = taskSelect ? taskSelect.value : "ai-weekly";
      console.log("🚀 AI处理 — task_type:", taskType, "| select:", taskSelect);
      const rawText = node.querySelector(".item-desc").value.trim();
      if (!rawText) { alert("请先在正文框中粘贴文章内容"); return; }

      btn.textContent = "⏳ 处理中...";
      btn.disabled = true;

      try {
        const resp = await apiFetch("/llm/process", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            raw_text: rawText,
            task_type: taskType,
            source_url: item.sources[0] || "",
          }),
        });
        const text = await resp.text();
        let data;
        try {
          data = JSON.parse(text);
        } catch {
          throw new Error("服务器返回异常（可能崩溃了），请检查后端是否正常运行。原始返回：" + text.slice(0, 100));
        }
        if (!resp.ok) {
          throw new Error(data.detail || data.error || `HTTP ${resp.status}`);
        }
        if (!data.success) { alert("AI 处理失败: " + data.error); return; }

        const r = data.result;
        // 根据 task_type 映射 LLM 字段到条目
        item.title = r.title || item.title;
        if (r.tags) {
          item.tags = Array.isArray(r.tags) ? r.tags : splitTags(r.tags);
        }
        if (taskType === "ai-weekly") {
          item.desc = [r.summary, r.analysis].filter(Boolean).join("\n\n");
        } else if (taskType === "global-logistics-risk") {
          item.desc = [
            `风险等级: ${r.risk_level || "N/A"}`,
            `影响区域: ${r.affected_regions || "N/A"}`,
            `影响链路: ${r.affected_routes || "N/A"}`,
            `物流影响: ${r.impact_on_logistics || ""}`,
            r.suggestion ? `建议:\n${Array.isArray(r.suggestion) ? r.suggestion.map((s,i) => `${i+1}. ${s}`).join("\n") : r.suggestion}` : "",
            `原文摘要: ${r.source_summary || ""}`
          ].filter(Boolean).join("\n\n");
        } else if (taskType === "cn-logistics-industry") {
          const points = r.key_points ? (Array.isArray(r.key_points) ? r.key_points.map((p,i) => `${i+1}. ${p}`).join("\n") : r.key_points) : "";
          item.desc = [
            `分类: ${r.category || "N/A"}`,
            points ? `要点:\n${points}` : "",
            `业务影响: ${r.impact_on_business || ""}`,
            r.suggestion ? `建议:\n${Array.isArray(r.suggestion) ? r.suggestion.map((s,i) => `${i+1}. ${s}`).join("\n") : r.suggestion}` : "",
            `原文摘要: ${r.source_summary || ""}`
          ].filter(Boolean).join("\n\n");
        } else if (taskType === "exchange-rate") {
          item.desc = [
            `货币对: ${r.currency_pair || "N/A"}`,
            `汇率变化: ${r.rate_change || "N/A"}`,
            `影响方向: ${r.impact_direction || "N/A"}`,
            `成本影响: ${r.impact_on_cost || ""}`,
            r.suggestion ? `建议:\n${Array.isArray(r.suggestion) ? r.suggestion.map((s,i) => `${i+1}. ${s}`).join("\n") : r.suggestion}` : "",
            `原文摘要: ${r.source_summary || ""}`
          ].filter(Boolean).join("\n\n");
        }
        if (r.source_url && !item.sources.includes(r.source_url)) {
          item.sources.push(r.source_url);
        }
        setItemExpanded(item.id, true);
        renderItemsEditor();
        refreshAll();
      } catch (e) {
        alert("请求失败: " + e.message);
      } finally {
        btn.textContent = "AI 处理";
        btn.disabled = false;
      }
    });

    [
      [".item-title", "title"],
      [".item-image", "image"],
      [".item-image-alt", "imageAlt"],
      [".item-desc", "desc"]
    ].forEach(([selector, key]) => {
      node.querySelector(selector).addEventListener("input", (event) => {
        item[key] = event.target.value;
        refreshAll();
      });
    });
    node.querySelector(".item-tags").addEventListener("input", (event) => {
      item.tags = splitTags(event.target.value);
      refreshAll();
    });
    node.querySelector(".item-sources").addEventListener("input", (event) => {
      item.sources = event.target.value.split(/\n+/).map((x) => x.trim()).filter(Boolean);
      refreshAll();
    });
    node.querySelector(".item-file").addEventListener("change", (event) => {
      const file = event.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        item.image = reader.result;
        if (!item.imageAlt) item.imageAlt = file.name.replace(/\.[^.]+$/, "");
        renderItemsEditor();
        refreshAll();
      };
      reader.readAsDataURL(file);
    });
    node.querySelector(".clear-image").addEventListener("click", () => {
      item.image = "";
      item.imageAlt = "";
      renderItemsEditor();
      refreshAll();
    });
    itemsEditor.appendChild(node);
  });
}

function summarizeText(text = "") {
  return String(text).replace(/\s+/g, " ").trim().slice(0, 140);
}

function refreshAll() {
  readFormToState();
  saveState();
  document.querySelector("#previewName").textContent = state.title || "周报预览";
  previewFrame.srcdoc = buildWeeklyHtml();
  pushJsonOutput.value = JSON.stringify(buildPushPayload(), null, 2);
}

async function loadSourcesConfigToPanel() {
  try {
    const resp = await apiFetch("/config");
    const data = await resp.json();
    sourceState.config = data.sources || {};
    sourceState.currentModule = sourceFields.module.value;
    renderSourcesPanel();
  } catch (e) {
    sourceFields.output.value = "读取来源配置失败: " + e.message;
  }
}

function renderSourcesPanel() {
  const cfg = sourceState.config || {};
  const sourcesMap = cfg.sources || {};
  const currentList = sourcesMap[sourceState.currentModule] || [];
  sourceFields.output.value = JSON.stringify(
    {
      fetch_rules: cfg.fetch_rules || {},
      [sourceState.currentModule]: currentList
    },
    null,
    2
  );
  renderFetchHint();
}

function getEnabledSourcesForCurrentModule() {
  const cfg = sourceState.config || {};
  const sourcesMap = cfg.sources || {};
  return (sourcesMap[sourceState.currentModule] || []).filter((x) => x.enabled);
}

function getFetchLimit() {
  const value = Number(sourceFields.fetchLimit?.value || 8);
  return Math.min(15, Math.max(1, Number.isFinite(value) ? value : 8));
}

function renderFetchHint() {
  if (!sourceFields.fetchHint) return;
  const enabledSources = getEnabledSourcesForCurrentModule();
  const limit = getFetchLimit();
  const names = enabledSources.map((x) => x.name || x.id).join("、") || "暂无启用来源";
  sourceFields.fetchHint.textContent = `当前启用 ${enabledSources.length} 个来源：${names}。每个来源最多抓取 ${limit} 条，候选池上限约 ${enabledSources.length * limit} 条。`;
}

async function addSourceToConfig() {
  const moduleKey = sourceFields.module.value;
  const item = {
    id: sourceFields.id.value.trim(),
    name: sourceFields.name.value.trim(),
    url: sourceFields.url.value.trim(),
    enabled: true,
    note: sourceFields.note.value.trim()
  };
  if (!item.id || !item.name || !item.url) {
    alert("请至少填写来源 ID、名称和网址");
    return;
  }

  if (!sourceState.config) {
    sourceState.config = { fetch_rules: {}, sources: {} };
  }
  if (!sourceState.config.sources) {
    sourceState.config.sources = {};
  }
  if (!Array.isArray(sourceState.config.sources[moduleKey])) {
    sourceState.config.sources[moduleKey] = [];
  }

  sourceState.config.sources[moduleKey].push(item);

  const resp = await apiFetch("/config/sources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sourceState.config)
  });
  const result = await resp.json();
  if (!resp.ok || !result.success) {
    throw new Error(result.detail || result.error || "保存来源配置失败");
  }

  sourceState.currentModule = moduleKey;
  renderSourcesPanel();
  sourceFields.name.value = "";
  sourceFields.id.value = "";
  sourceFields.url.value = "";
  sourceFields.note.value = "";
}

async function simulateFetchFromSources() {
  const btn = document.querySelector("#simulateFetchBtn");
  const listEl = document.querySelector("#candidateList");
  const toolbar = document.querySelector(".candidate-toolbar");
  const errEl = document.querySelector("#fetchErrors");

  const perSource = getFetchLimit();
  const currentList = getEnabledSourcesForCurrentModule();
  const sourceIds = currentList.map((x) => x.id);
  const forceRefresh = Boolean(document.querySelector("#forceRefreshInput")?.checked);

  if (!sourceIds.length) {
    toolbar.style.display = "flex";
    document.querySelector("#candidateCount").textContent = "没有启用来源";
    errEl.style.display = "none";
    listEl.innerHTML = `
      <div class="candidate-empty">
        <strong>当前模块没有启用来源</strong>
        <p>请在“来源配置”中新增或启用来源，再重新获取内容。</p>
      </div>
    `;
    return;
  }

  btn.textContent = "抓取中...";
  btn.disabled = true;
  listEl.innerHTML = '<div class="candidate-empty"><strong>正在获取内容</strong><p>正在并发抓取并 AI 分析中，请稍候...</p></div>';
  errEl.style.display = "none";

  try {
    const resp = await apiFetch("/scrape/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_type: sourceState.currentModule,
        source_ids: sourceIds,
        limit: perSource,
        score_threshold: 0,  // 不过滤，让人工审核
        force_refresh: forceRefresh
      })
    });
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); }
    catch { throw new Error(explainHtmlError(text, "获取内容失败")); }
    if (!resp.ok) throw new Error(data.detail || data.error || `HTTP ${resp.status}`);

    // 显示管道统计信息
    const stats = [
      `📊 来源 ${data.sources_used} 个`,
      `📥 原始抓取 ${data.total_raw} 条`,
      `🔗 去重后 ${data.after_dedup} 条`,
      data.candidates ? `✅ 候选 ${data.candidates.length} 条` : "",
      data.from_cache ? "最近候选回退" : ""
    ].filter(Boolean).join(" · ");
    console.log("管道统计:", stats);

    // 显示错误（如有）
    if (data.errors && data.errors.length) {
      errEl.style.display = "block";
      errEl.innerHTML = `<strong>${data.from_cache ? "缓存提示" : "抓取/处理问题"} (${data.errors.length})：</strong><ul>${
        data.errors.map(e => `<li>${escHtml(String(e))}</li>`).join("")
      }</ul>`;
    }

    // 存储候选数据
    window._candidates = normalizeCandidates(data.candidates || []);
    window._candidateMeta = {
      fromCache: Boolean(data.from_cache),
      cachedCount: Number(data.cached_count || 0),
      draftFilename: data.draft_filename || ""
    };
    renderCandidateList();
    scheduleWorkspaceSave();
    // 刷新草稿列表
    loadDraftsList();

  } catch (e) {
    listEl.innerHTML = `<p style="color:red">获取失败: ${e.message}</p>`;
    errEl.style.display = "block";
    errEl.innerHTML = `<strong>❌ 管道异常：</strong> ${escHtml(String(e.message))}`;
  } finally {
    btn.textContent = "一键获取内容";
    btn.disabled = false;
  }
}

function renderCandidateList() {
  const listEl = document.querySelector("#candidateList");
  const toolbar = document.querySelector(".candidate-toolbar");
  const countEl = document.querySelector("#candidateCount");
  const candidates = normalizeCandidates(window._candidates || []);
  const meta = window._candidateMeta || {};
  window._candidates = candidates;

  if (!candidates.length) {
    toolbar.style.display = "flex";
    countEl.textContent = "候选池为空";
    listEl.innerHTML = `
      <div class="candidate-empty">
        <strong>这次没有可选候选</strong>
        <p>常见原因是缓存过滤、来源暂时没有新内容，或抓取被站点拦截。你可以忽略缓存重新抓取，或加载历史草稿继续选择。</p>
        <div class="button-row">
          <button type="button" class="retry-force-fetch">忽略缓存重抓</button>
          <button type="button" class="load-latest-draft">加载最近候选</button>
        </div>
      </div>
    `;
    listEl.querySelector(".retry-force-fetch")?.addEventListener("click", () => {
      document.querySelector("#forceRefreshInput").checked = true;
      simulateFetchFromSources();
    });
    listEl.querySelector(".load-latest-draft")?.addEventListener("click", loadLatestDraftForCurrentModule);
    return;
  }

  toolbar.style.display = "flex";
  const selectedCount = candidates.filter((c) => c.selected).length;
  countEl.textContent = `共 ${candidates.length} 条候选，已选 ${selectedCount} 条${meta.fromCache ? " · 来自最近候选" : ""}`;

  listEl.innerHTML = candidates.map((c, i) => `
    <article class="candidate-item ${c.selected ? "is-selected" : ""}">
      <input type="checkbox" class="candidate-check" data-index="${i}"
        ${c.selected ? "checked" : ""} aria-label="选择候选 ${i + 1}">
      <div class="candidate-info">
        <div class="candidate-title">
          <span class="candidate-score ${scoreClass(c.ai_score)}">${Number(c.ai_score || 0).toFixed(1)}</span>
          <span class="candidate-title-text">${escHtml(c.title || "未命名候选条目")}</span>
          ${c.selected ? '<span class="candidate-selected-badge">已选</span>' : ""}
        </div>
        <div class="candidate-meta">
          <span>来源：${escHtml(c.source_name || "未知来源")}</span>
          ${c.source_url ? `<a href="${escHtml(c.source_url)}" target="_blank" rel="noopener">打开原文</a>` : ""}
          ${c.ai_tags.length ? `<span>· ${c.ai_tags.map(t => `<em>${escHtml(String(t))}</em>`).join(" ")}</span>` : ""}
        </div>
        <div class="candidate-summary">${escHtml(String(c.ai_summary || c.ai_analysis || c.title || "")).slice(0, 360) || "暂无摘要"}</div>
        ${c.ai_reason ? `<div class="candidate-reason"><strong>入选理由：</strong>${escHtml(String(c.ai_reason)).slice(0, 220)}</div>` : ""}
      </div>
    </article>
  `).join("");

  listEl.querySelectorAll(".candidate-check").forEach((checkbox) => {
    checkbox.addEventListener("change", (event) => {
      toggleCandidate(Number(event.target.dataset.index), event.target.checked);
    });
  });
}

function toggleCandidate(index, checked) {
  if (window._candidates && window._candidates[index]) {
    window._candidates[index].selected = checked;
    renderCandidateList();
    scheduleWorkspaceSave();
  }
}

function scoreClass(score) {
  if (score >= 8) return "score-high";
  if (score >= 6) return "score-mid";
  return "score-low";
}

function normalizeCandidates(list) {
  if (!Array.isArray(list)) return [];

  return list.map((item) => {
    if (typeof item === "string") {
      return {
        title: item,
        source_name: "候选池",
        source_url: "",
        ai_tags: [],
        ai_summary: item,
        ai_analysis: "",
        ai_score: 5,
        ai_reason: "",
        selected: false
      };
    }

    const obj = item && typeof item === "object" ? item : {};
    const tags = Array.isArray(obj.ai_tags)
      ? obj.ai_tags
      : typeof obj.ai_tags === "string"
        ? splitTags(obj.ai_tags)
        : [];

    const scoreNum = Number(obj.ai_score);

    return {
      ...obj,
      title: String(obj.title || obj.name || obj.headline || obj.raw_title || "未命名候选条目"),
      source_name: String(obj.source_name || obj.source || obj.module || "未知来源"),
      source_url: String(obj.source_url || obj.url || ""),
      ai_tags: tags,
      ai_summary: String(obj.ai_summary || obj.summary || obj.desc || obj.snippet || ""),
      ai_analysis: String(obj.ai_analysis || obj.analysis || ""),
      ai_score: Number.isFinite(scoreNum) ? scoreNum : 5,
      ai_reason: String(obj.ai_reason || obj.reason || ""),
      selected: Boolean(obj.selected)
    };
  });
}

// ── 草稿管理 ──────────────────────────────────────────────

async function loadDraftsList() {
  const select = document.querySelector("#draftSelect");
  try {
    const resp = await apiFetch(`/scrape/drafts?task_type=${encodeURIComponent(sourceState.currentModule)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const drafts = await resp.json();
    const options = ['<option value="">📂 加载历史草稿...</option>'];
    drafts.sort((a, b) => {
      const aHasCandidates = Number(a.candidate_count || 0) > 0 ? 1 : 0;
      const bHasCandidates = Number(b.candidate_count || 0) > 0 ? 1 : 0;
      if (aHasCandidates !== bHasCandidates) return bHasCandidates - aHasCandidates;
      return new Date(b.created || 0) - new Date(a.created || 0);
    }).forEach(d => {
      const date = d.created ? new Date(d.created).toLocaleString("zh-CN") : "";
      const count = Number(d.candidate_count || 0);
      const errors = Number(d.error_count || 0);
      const label = count ? `${count} 条候选` : `无候选${errors ? ` · ${errors} 个问题` : ""}`;
      options.push(`<option value="${escHtml(d.filename)}" data-count="${count}">${escHtml(d.filename)} (${label} · ${(d.size/1024).toFixed(1)}KB · ${date})</option>`);
    });
    select.innerHTML = options.join("");
  } catch (e) {
    console.warn("加载草稿列表失败:", e);
    select.innerHTML = '<option value="">历史草稿加载失败</option>';
  }
}

function loadSelectedDraft() {
  const select = document.querySelector("#draftSelect");
  const filename = select.value;
  if (!filename) return;

  apiFetch(`/scrape/drafts/${filename}`)
    .then(async r => {
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || data.error || `HTTP ${r.status}`);
      return data;
    })
    .then(data => {
      window._candidates = normalizeCandidates(data.candidates || []);
      window._candidateMeta = {};
      renderCandidateList();
      scheduleWorkspaceSave();
      // 显示统计信息
      const stats = [
        data.sources_used ? `📊 来源 ${data.sources_used} 个` : "",
        data.total_raw ? `📥 原始 ${data.total_raw} 条` : "",
        data.after_dedup ? `🔗 去重后 ${data.after_dedup} 条` : "",
        data.candidates ? `✅ 候选 ${data.candidates.length} 条` : ""
      ].filter(Boolean).join(" · ");
      console.log("加载草稿:", filename, stats);
      // 显示草稿错误
      const errEl = document.querySelector("#fetchErrors");
      if (data.errors && data.errors.length) {
        errEl.style.display = "block";
        errEl.innerHTML = `<strong>⚠️ 保存时的抓取问题 (${data.errors.length})：</strong><ul>${
          data.errors.map(e => `<li>${escHtml(String(e))}</li>`).join("")
        }</ul>`;
      } else {
        errEl.style.display = "none";
      }
    })
    .catch(e => alert("加载草稿失败: " + e.message));
}

function loadLatestDraftForCurrentModule() {
  const select = document.querySelector("#draftSelect");
  const firstOption = [...select.options].find((option) => option.value && Number(option.dataset.count || 0) > 0);
  if (!firstOption) {
    alert("还没有可用候选草稿，请先忽略缓存重新抓取一次。");
    return;
  }
  select.value = firstOption.value;
  loadSelectedDraft();
}

async function clearUrlCache() {
  if (!confirm("确定清除 URL 处理缓存？下次抓取时将重新分析所有文章（包括已处理过的）。")) return;
  try {
    const resp = await apiFetch("/scrape/clear-cache", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || data.error || `HTTP ${resp.status}`);
    const forceRefreshInput = document.querySelector("#forceRefreshInput");
    if (forceRefreshInput) forceRefreshInput.checked = true;
    showToast(data.message || "缓存已清除");
  } catch (e) {
    alert("清除缓存失败: " + e.message);
  }
}

function syncCardDefaults(force = false) {
  const start = dateWithDots(state.startDate);
  const end = dateWithDots(state.endDate);
  if (force || !uiState.cardTitleTouched) {
    state.cardTitle = `${state.title || "AI热点技术周报"}｜${state.items.length}条精选`;
  }
  if (force || !uiState.cardDescTouched) {
    state.cardDesc = `${start} - ${end}｜${state.topic || ""}`;
  }
  fields.cardTitle.value = state.cardTitle;
  fields.cardDesc.value = state.cardDesc;
}

function buildWeeklyHtml() {
  const count = state.items.length;
  const countLabel = state.countLabel || `${count} 条精选`;
  const period = `${state.startDate || ""} ~ ${state.endDate || ""}`;
  const cards = state.items.map((item, index) => {
    const tags = (item.tags || []).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
    const sources = (item.sources || []).map((url, i) => `<a href="${escapeAttr(url)}" target="_blank">来源 ${i + 1}</a>`).join("");
    const image = item.image ? `<figure class="pic"><img src="${escapeAttr(item.image)}" alt="${escapeAttr(item.imageAlt || item.title || "")}">${item.imageAlt ? `<figcaption>${escapeHtml(item.imageAlt)}</figcaption>` : ""}</figure>` : "";
    return `<div class="card"><div class="top"><div class="n">${index + 1}</div><h2>${escapeHtml(item.title || "")}</h2></div><div class="tags">${tags}</div><div class="src">${sources}</div>${image}<div class="desc">${paragraphs(item.desc || "")}</div></div>`;
  }).join("");

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>${escapeHtml(state.title)} - ${escapeHtml(state.brand)}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}body{background:#f5f7fa;color:#333;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.8;-webkit-text-size-adjust:100%}
.w{max-width:800px;margin:0 auto;padding:20px 12px 40px}.hd{text-align:center;padding:36px 20px 32px;border-bottom:2px solid #1A5FDC;margin-bottom:16px;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.04)}.hd img{height:32px;margin-bottom:14px}.hd .vol{font-size:12px;color:#999;letter-spacing:2px;margin-bottom:8px}.hd .vol b{color:#1A5FDC;font-weight:700}.hd h1{font-size:26px;font-weight:700;color:#1A5FDC;margin-bottom:6px}.hd .meta{font-size:13px;color:#666;line-height:1.6}.hd .meta i{color:#1A5FDC;font-style:normal;font-weight:600}.summary{background:#fff;border-radius:10px;padding:18px 16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,0.04);font-size:13px;color:#555;line-height:1.85}.card{background:#fff;border-radius:10px;padding:20px 16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,0.04);word-wrap:break-word;overflow-wrap:break-word}.card:active{box-shadow:0 3px 12px rgba(26,95,220,0.12)}.card .top{display:flex;align-items:flex-start;gap:10px;margin-bottom:10px}.card .n{flex-shrink:0;width:26px;height:26px;background:#1A5FDC;color:#fff;font-size:11px;font-weight:700;border-radius:6px;display:flex;align-items:center;justify-content:center}.card h2{font-size:16px;font-weight:600;line-height:1.5;flex:1;word-break:break-word;color:#222}.card .tags{margin-bottom:8px;display:flex;flex-wrap:wrap;gap:4px}.card .tags span{display:inline-block;font-size:10px;padding:2px 8px;border-radius:8px;background:rgba(26,95,220,0.08);color:#1A5FDC;border:1px solid rgba(26,95,220,0.15)}.card .src{margin-bottom:8px;display:flex;gap:6px;flex-wrap:wrap}.card .src a{color:#1A5FDC;text-decoration:none;padding:3px 8px;background:rgba(26,95,220,0.06);border-radius:4px;font-size:11px}.card .src a:active{background:rgba(26,95,220,0.2)}.card .pic{margin:10px 0 12px}.card .pic img{display:block;width:100%;max-height:360px;object-fit:cover;border-radius:8px;background:#eef3f8}.card .pic figcaption{margin-top:5px;text-align:center;font-size:11px;color:#888;line-height:1.5}.card .desc{font-size:13px;color:#555;line-height:1.8;word-break:break-word;overflow-wrap:break-word;white-space:normal}.end{background:#fff;border-radius:10px;padding:18px 16px;margin-top:12px;box-shadow:0 1px 4px rgba(0,0,0,0.04);font-size:13px;color:#555;line-height:1.85}.ft{text-align:center;padding:32px 0 20px;color:#bbb;font-size:12px}.ft img{height:24px;margin-bottom:8px;opacity:0.5}.ft strong{color:#999;font-weight:500}@media(min-width:600px){.w{padding:40px 20px}.hd{padding:50px 40px 40px}.hd h1{font-size:32px}.summary,.end{font-size:14px;padding:22px}.card{padding:24px}.card h2{font-size:17px}.card .desc{font-size:14px}}
</style>
</head>
<body><div class="w"><div class="hd"><img src="${escapeAttr(state.logoUrl)}" alt="${escapeAttr(state.brand)}"><div class="vol">${escapeHtml(state.vol)} - <b>${escapeHtml(countLabel)}</b></div><h1>${escapeHtml(state.title)}</h1><div class="meta">${escapeHtml(period)}<br><i>${escapeHtml(state.topic)}</i></div></div><div class="summary">${paragraphs(state.summary)}</div>${cards}<div class="end"><strong>结语</strong><br>${paragraphs(state.ending)}</div><div class="ft"><img src="${escapeAttr(state.logoUrl)}" alt="${escapeAttr(state.brand)}"><br><strong>${escapeHtml(state.brand)}</strong> - ${escapeHtml(state.title)}</div></div></body></html>`;
}

function buildPushPayload() {
  const pageUrl = state.pageUrl || "HTML页面地址";
  const coverUrl = state.coverUrl || "封面图地址";
  const start = dateWithDots(state.startDate).slice(5);
  const end = dateWithDots(state.endDate).slice(5);
  const firstTitles = state.items.slice(0, 3).map((item) => item.title.replace(/（[^）]+）$/, "")).join("、");
  return {
    user_id: state.userId || "",
    msgtype: "template_card",
    template_card: {
      card_type: "news_notice",
      source: {
        icon_url: state.logoUrl,
        desc: state.sourceDesc || state.brand
      },
      main_title: {
        title: state.cardTitle || `${state.title}｜${state.items.length}条精选`,
        desc: state.cardDesc || `${dateWithDots(state.startDate)} - ${dateWithDots(state.endDate)}｜${state.topic}`
      },
      card_image: {
        url: coverUrl,
        aspect_ratio: 1.78
      },
      image_text_area: {
        type: 1,
        url: pageUrl,
        title: "点击查看",
        desc: `本周AI热点速览：共${state.items.length}篇精选资讯`,
        image_url: coverUrl
      },
      quote_area: {
        type: 1,
        url: pageUrl,
        title: "本期导读",
        quote_text: state.summary.slice(0, 80)
      },
      horizontal_content_list: [
        { keyname: "周期", value: `${start} - ${end}` },
        { keyname: "数量", value: `${state.items.length}条精选` },
        { keyname: "趋势", value: state.topic.split("/")[0].trim() || "AI热点" },
        { keyname: "重点", value: firstTitles.slice(0, 28) || "点击查看" }
      ],
      jump_list: [
        {
          type: 1,
          title: "阅读完整周报",
          url: pageUrl
        }
      ],
      card_action: {
        type: 1,
        url: pageUrl
      }
    }
  };
}

function buildChecklist() {
  const payload = buildPushPayload();
  return [
    `接收人：${payload.user_id}`,
    "卡片类型：news_notice（图文展示）",
    `主标题：${payload.template_card.main_title.title}`,
    `描述：${payload.template_card.main_title.desc}`,
    `卡片图片：${payload.template_card.card_image.url}`,
    `HTML地址：${payload.template_card.card_action.url}`,
    "纵向内容列表：留空",
    "跳转列表：阅读完整周报",
    "卡片动作：URL 跳转到 HTML 地址"
  ].join("\n");
}

function parseMarkdownIntoState() {
  const md = document.querySelector("#markdownInput").value.trim();
  if (!md) return;
  const period = md.match(/周期[:：]\s*([0-9-]+)\s*[~—-]+\s*([0-9-]+)/);
  const topic = md.match(/主题[:：]\s*(.+)/);
  if (period) {
    state.startDate = period[1];
    state.endDate = period[2];
    state.vol = `VOL. ${period[2].replaceAll("-", ".")}`;
  }
  if (topic) state.topic = topic[1].trim();

  const beforeFirst = md.split(/\n##\s+\d+[｜|]/)[0] || "";
  const summary = beforeFirst.split(/\n\n/).map((x) => x.trim()).filter(Boolean).find((x) => !x.startsWith("#") && !x.startsWith("- "));
  if (summary) state.summary = summary;

  const sections = md.split(/\n(?=##\s+\d+[｜|])/).filter((part) => /^##\s+\d+[｜|]/.test(part.trim()));
  const parsedItems = sections.map(parseSection).filter(Boolean);
  if (parsedItems.length) {
    state.items = parsedItems.map(normalizeItem);
    uiState.expandedItems.clear();
    ensureDefaultExpandedItem();
  }
  const conclusion = md.match(/##\s*(结语|总结)[\s\S]*?\n([\s\S]+)$/);
  if (conclusion) state.ending = conclusion[2].trim();

  syncCardDefaults();
  writeStateToForm();
  renderItemsEditor();
  refreshAll();
}

function parseSection(section) {
  const lines = section.trim().split(/\n/);
  const heading = lines.shift().replace(/^##\s+\d+[｜|]\s*/, "").trim();
  if (!heading) return null;
  const body = lines.join("\n").trim();
  const sourceMatches = [...body.matchAll(/来源[:：]\s*(https?:\/\/\S+)/g)];
  const sources = sourceMatches.map((match) => match[1].trim());
  const imageMatch = body.match(/图片[:：]\s*(https?:\/\/\S+)/);
  const imageTextMatch = body.match(/图片[:：]\s*(.+)/);
  const cleanedLines = body.split(/\n/).map((line) => line.trim()).filter(Boolean)
    .filter((line) => !line.startsWith("图片：") && !line.startsWith("来源："));
  const tagLine = cleanedLines.shift() || "";
  return {
    title: heading,
    tags: splitTags(tagLine),
    sources,
    image: imageMatch ? imageMatch[1].trim() : "",
    imageAlt: imageTextMatch && !imageMatch ? imageTextMatch[1].trim() : "",
    desc: cleanedLines.join("\n").trim()
  };
}

function splitTags(text) {
  return text.split(/[\s,，、]+/).map((x) => x.trim()).filter(Boolean);
}

function downloadHtml() {
  const html = buildWeeklyHtml();
  const name = `ai_weekly_${state.startDate || "start"}_${state.endDate || "end"}_white.html`;
  const blob = new Blob([html], { type: "text/html;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

async function copyText(text, button) {
  const originalText = button ? button.textContent : "";
  try {
    await navigator.clipboard.writeText(text);
    if (button) {
      button.textContent = "已复制";
      window.setTimeout(() => { button.textContent = originalText; }, 1200);
    }
    showToast("已复制到剪贴板");
  } catch (e) {
    window.prompt("浏览器不允许自动复制，请手动复制以下内容：", text);
  }
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(showToast._timer);
  showToast._timer = window.setTimeout(() => {
    toast.classList.remove("is-visible");
  }, 1800);
}

async function apiFetch(path, options) {
  await ensureApiBase();
  return fetch(`${API_BASE}${path}`, options);
}

async function ensureApiBase() {
  const candidates = [API_BASE];
  if (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1") {
    candidates.push(`${window.location.protocol}//${window.location.hostname}:8001/api`);
  }
  for (const base of candidates) {
    try {
      const resp = await fetch(`${base}/health`);
      if (resp.ok) {
        API_BASE = base;
        return;
      }
    } catch {}
  }
}

function explainHtmlError(text, fallback) {
  if (/<title>Error response<\/title>/i.test(text) || /Error code:\s*501/i.test(text)) {
    return `${fallback}：当前 8000 端口返回的不是后端 API 响应，请重启真实 FastAPI 服务后继续使用 http://localhost:8000/`;
  }
  return `${fallback}：${String(text).replace(/\s+/g, " ").slice(0, 180)}`;
}

function paragraphs(text) {
  return escapeHtml(text).replace(/\n{2,}/g, "<br><br>").replace(/\n/g, "<br>");
}

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value = "") {
  return escapeHtml(value).replaceAll("`", "&#96;");
}

function dateWithDots(value = "") {
  return value.replaceAll("-", ".");
}

const escHtml = escapeHtml;
