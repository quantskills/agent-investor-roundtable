// 前端逻辑：A2A 投顾委员会 + 技术面 / 宏观面 / 筛选图表。

const $ = (id) => document.getElementById(id);
let charts = {};
let running = false;
let screenLoaded = false;

function initCharts() {
  if (typeof echarts === "undefined") {
    const banner = document.createElement("div");
    banner.className = "chart-error";
    banner.textContent = "图表组件加载失败，请刷新页面重试。";
    const panel = document.querySelector(".chart-panel");
    if (panel) panel.prepend(banner);
    return;
  }
  const ids = [
    "klineChart", "indChart", "indexChart",
    "buffettChart", "valChart", "screenChart",
    "committeeChart", "allocationChart",
  ];
  ids.forEach((id) => {
    const el = $(id);
    if (el) charts[id] = echarts.init(el, "dark");
  });
  window.addEventListener("resize", () => {
    Object.values(charts).forEach((c) => c && c.resize());
  });
}

function setOption(id, option) {
  const chart = charts[id];
  if (chart) chart.setOption(option, true);
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".tab-page").forEach((p) => {
    p.classList.toggle("active", p.id === "tab-" + name);
  });
  if (name === "screen" && !screenLoaded) {
    runScreen($("screenIndex") ? $("screenIndex").value : "000300", 20);
  }
  setTimeout(() => {
    Object.values(charts).forEach((c) => c && c.resize());
  }, 30);
}

function setChartSubject(name, symbol) {
  const el = $("chartSubject");
  if (el && name) el.textContent = `当前标的：${name}${symbol ? "（" + symbol + "）" : ""}`;
}

function resetRightPanel() {
  const subject = $("chartSubject");
  if (subject) subject.textContent = "当前标的：分析中…";
  const orc = $("orchestratorCard");
  if (orc) orc.innerHTML = '<div class="placeholder-sm">等待总览管家分配任务…</div>';
  const grid = $("masterGrid");
  if (grid) grid.innerHTML = "";
  const sub = $("buffettSub");
  if (sub) sub.style.display = "none";
  Object.keys(charts).forEach((id) => {
    const c = charts[id];
    if (c) c.clear();
  });
}

// ---------------- 技术图表 ---------------- //
function renderTechnical(d) {
  setChartSubject(d.name, d.symbol);
  setOption("klineChart", {
    backgroundColor: "transparent",
    title: { text: `${d.name} (${d.symbol})`, textStyle: { fontSize: 12, color: "#8b98b0" } },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    legend: { data: ["K线", "MA5", "MA20", "MA60"], top: 0, textStyle: { color: "#8b98b0" } },
    grid: { left: 50, right: 20, top: 30, bottom: 30 },
    xAxis: { type: "category", data: d.dates, axisLabel: { color: "#8b98b0" } },
    yAxis: { scale: true, axisLabel: { color: "#8b98b0" }, splitLine: { lineStyle: { color: "#26314a" } } },
    dataZoom: [{ type: "inside", start: 50 }, { type: "slider", height: 16, bottom: 6 }],
    series: [
      {
        name: "K线", type: "candlestick", data: d.kline,
        itemStyle: { color: "#ef5350", color0: "#26a69a", borderColor: "#ef5350", borderColor0: "#26a69a" },
      },
      { name: "MA5", type: "line", data: d.ma5, smooth: true, showSymbol: false, lineStyle: { width: 1, color: "#f0b90b" } },
      { name: "MA20", type: "line", data: d.ma20, smooth: true, showSymbol: false, lineStyle: { width: 1, color: "#42a5f5" } },
      { name: "MA60", type: "line", data: d.ma60, smooth: true, showSymbol: false, lineStyle: { width: 1, color: "#ab47bc" } },
    ],
  });

  const macdColors = d.macd_hist.map((v) => (v >= 0 ? "#ef5350" : "#26a69a"));
  setOption("indChart", {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" },
    legend: { data: ["DIF", "DEA", "MACD", "RSI12"], top: 0, textStyle: { color: "#8b98b0" } },
    grid: { left: 45, right: 20, top: 28, bottom: 24 },
    xAxis: { type: "category", data: d.dates, axisLabel: { color: "#8b98b0" } },
    yAxis: { scale: true, axisLabel: { color: "#8b98b0" }, splitLine: { lineStyle: { color: "#26314a" } } },
    series: [
      { name: "DIF", type: "line", data: d.dif, showSymbol: false, lineStyle: { width: 1, color: "#f0b90b" } },
      { name: "DEA", type: "line", data: d.dea, showSymbol: false, lineStyle: { width: 1, color: "#42a5f5" } },
      { name: "MACD", type: "bar", data: d.macd_hist, itemStyle: { color: (p) => macdColors[p.dataIndex] } },
      { name: "RSI12", type: "line", data: d.rsi12, showSymbol: false, lineStyle: { width: 1, color: "#ab47bc" } },
    ],
  });
}

// ---------------- 宏观图表 ---------------- //
function renderMacro(d) {
  const cards = $("macroCards");
  cards.innerHTML = "";
  (d.indicator_cards || []).forEach((c) => {
    const trendCls = c.trend === "上行" ? "up" : c.trend === "下行" ? "down" : "";
    const arrow = c.trend === "上行" ? "▲" : c.trend === "下行" ? "▼" : "—";
    cards.insertAdjacentHTML("beforeend", `
      <div class="macro-card">
        <div class="name">${c.name}</div>
        <div class="val">${c.value == null ? "—" : c.value}</div>
        <div class="trend ${trendCls}">${arrow} ${c.trend == null ? "" : c.trend}</div>
      </div>`);
  });

  const idx = d.indices || [];
  if (idx.length) {
    setOption("indexChart", {
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      legend: { data: idx.map((i) => i.name), top: 0, textStyle: { color: "#8b98b0" } },
      grid: { left: 50, right: 20, top: 28, bottom: 24 },
      xAxis: { type: "category", data: idx[0].dates, axisLabel: { color: "#8b98b0" } },
      yAxis: { scale: true, axisLabel: { color: "#8b98b0" }, splitLine: { lineStyle: { color: "#26314a" } } },
      series: idx.map((i) => ({ name: i.name, type: "line", data: i.closes, showSymbol: false, smooth: true })),
    });
  }
}

// ---------------- 巴菲特专项图表 ---------------- //
function renderBuffett(d) {
  setChartSubject(d.name, d.symbol);
  const sub = $("buffettSub");
  if (sub) sub.style.display = "block";
  const radarIndicators = (d.radar.indicators || []).map((name) => ({ name, max: 100, min: 0 }));
  setOption("buffettChart", {
    backgroundColor: "transparent",
    tooltip: {},
    radar: {
      indicator: radarIndicators,
      radius: "66%",
      splitArea: { areaStyle: { color: ["rgba(240,185,11,0.03)", "rgba(240,185,11,0.06)"] } },
      axisLine: { lineStyle: { color: "#26314a" } },
      splitLine: { lineStyle: { color: "#26314a" } },
      name: { textStyle: { color: "#8b98b0" } },
    },
    series: [{
      type: "radar",
      data: [{
        value: d.radar.values || [],
        name: `${d.name} 巴菲特评分`,
        areaStyle: { color: "rgba(240,185,11,0.25)" },
        lineStyle: { color: "#f0b90b", width: 2 },
        itemStyle: { color: "#f0b90b" },
      }],
    }],
  });

  $("buffettMetrics").innerHTML = (d.metrics || []).map((m) => `
    <div class="buffett-metric ${m.tone || ""}">
      <div class="name">${m.label}</div>
      <div class="val">${m.value}</div>
    </div>`).join("");

  const v = d.valuation || {};
  const vLabels = ["悲观", "基准", "乐观"];
  const vValues = [v.bear, v.base, v.bull].map((x) => (x == null ? null : Number(Number(x).toFixed(2))));
  setOption("valChart", {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 48, right: 12, top: 24, bottom: 20 },
    xAxis: { type: "category", data: vLabels, axisLabel: { color: "#8b98b0" } },
    yAxis: { scale: true, axisLabel: { color: "#8b98b0" }, splitLine: { lineStyle: { color: "#26314a" } } },
    series: [
      {
        name: "内在价值", type: "bar", data: vValues, barWidth: 24,
        itemStyle: { color: (p) => ["#f0b90b", "#42a5f5", "#26a69a"][p.dataIndex] },
      },
      {
        name: "现价", type: "line", data: [v.price, v.price, v.price],
        showSymbol: false, lineStyle: { color: "#ef5350", width: 2, type: "dashed" },
      },
    ],
  });
}

// ---------------- A2A 投顾委员会 ---------------- //
function renderCommittee(d) {
  setChartSubject(d.name, d.symbol);
  const o = d.orchestrator || {};
  $("orchestratorCard").innerHTML = `
    <div class="orc-head">
      <div class="orc-title">
        <span class="orc-avatar">总</span>
        <div>
          <h3>${o.name || "总览管家"}</h3>
          <p>${o.role || "A2A 协调者"}</p>
        </div>
      </div>
      <div class="orc-score">${(o.score == null ? 0 : o.score).toFixed(0)}<span>/100</span></div>
    </div>
    <div class="orc-body">
      <div class="orc-verdict ${o.tone || "mid"}">${o.verdict || "—"}</div>
      <div class="orc-metrics">
        ${(o.key_metrics || []).map((m) => `
          <div class="orc-metric"><span>${m.label}</span><b>${m.value}</b></div>`).join("")}
      </div>
      <p class="orc-reason">${o.reason || ""}</p>
      <div class="orc-conflicts">
        ${(d.conflicts || []).map((c) => `<div class="conflict">分歧：${c}</div>`).join("")}
      </div>
    </div>`;

  $("masterGrid").innerHTML = (d.agents || []).map((a) => `
    <div class="master-card tone-${a.tone || "mid"}">
      <div class="mc-head">
        <div class="mc-avatar">${(a.name || "").slice(0, 1)}</div>
        <div class="mc-title">
          <div class="mc-name">${a.name}</div>
          <div class="mc-school">${a.school}</div>
        </div>
        <div class="mc-score">${(a.score || 0).toFixed(0)}</div>
      </div>
      <div class="mc-verdict">${a.verdict}</div>
      <div class="mc-action">${a.action || ""}</div>
      <div class="mc-metrics">
        ${(a.key_metrics || []).map((m) => `<span>${m.label} ${m.value}</span>`).join("")}
      </div>
      <p class="mc-reason">${a.reason || ""}</p>
      <div class="mc-watch">
        ${(a.watch_items || []).slice(0, 3).map((w) => `<div>· ${w}</div>`).join("")}
      </div>
    </div>`).join("");

  const toneColor = { up: "#26a69a", mid: "#f0b90b", down: "#ef5350" };
  setOption("committeeChart", {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 60, right: 20, top: 24, bottom: 40 },
    xAxis: {
      type: "category",
      data: (d.agent_scores || []).map((a) => a.name),
      axisLabel: { color: "#8b98b0", interval: 0, rotate: 20 },
    },
    yAxis: { type: "value", min: 0, max: 100, axisLabel: { color: "#8b98b0" }, splitLine: { lineStyle: { color: "#26314a" } } },
    series: [{
      type: "bar",
      barMaxWidth: 34,
      label: { show: true, position: "top", color: "#8b98b0" },
      data: (d.agent_scores || []).map((a) => ({
        value: a.score,
        itemStyle: { color: toneColor[a.tone] || "#42a5f5" },
      })),
    }],
  });

  const alloc = d.allocation || { position: 0, cash: 1 };
  setOption("allocationChart", {
    backgroundColor: "transparent",
    tooltip: { trigger: "item" },
    legend: { bottom: 0, textStyle: { color: "#8b98b0" } },
    series: [{
      type: "pie",
      radius: ["42%", "70%"],
      center: ["50%", "44%"],
      label: { color: "#8b98b0", formatter: "{b}: {d}%" },
      data: [
        { name: "建议仓位", value: alloc.position, itemStyle: { color: "#42a5f5" } },
        { name: "现金保留", value: alloc.cash, itemStyle: { color: "#26314a" } },
      ],
    }],
  });

  renderDebate(d);
}

// ---------------- A2A 辩论实况 ---------------- //
function debateHtmlItem(m) {
  const isChallenge = m.role === "challenge";
  const tag = isChallenge ? "质询" : "辩护";
  const cls = isChallenge ? "debate-challenge" : "debate-defense";
  return `<div class="debate-item ${cls}">
    <div class="debate-meta"><span class="debate-tag">${tag}</span>
      <span class="debate-from">${m.sender}</span> → <span class="debate-to">${m.receiver}</span></div>
    <div class="debate-text">${m.text || ""}</div>
  </div>`;
}

function renderDebate(d) {
  const feed = $("debateFeed");
  const card = $("debateCard");
  const rounds = $("debateRounds");
  if (!feed || !card) return;
  const msgs = d.debate || [];
  if (!msgs.length) { card.style.display = "none"; return; }
  card.style.display = "";
  if (rounds) rounds.textContent = `${d.debate_rounds || 1} 轮 · ${msgs.length} 条交锋`;
  feed.innerHTML = msgs.map(debateHtmlItem).join("");
  feed.scrollTop = feed.scrollHeight;
}

function appendDebateMessages(list) {
  const feed = $("debateFeed");
  const card = $("debateCard");
  if (!feed || !card) return;
  card.style.display = "";
  const html = (list || []).map(debateHtmlItem).join("");
  feed.insertAdjacentHTML("beforeend", html);
  feed.scrollTop = feed.scrollHeight;
}

// ---------------- 筛选榜 ---------------- //
function renderScreen(d, master) {
  screenLoaded = true;
  const color = (d.color || (master === "all" ? "#f0b90b" : "#ef5350"));
  setOption("screenChart", {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 55, right: 16, top: 20, bottom: 40 },
    xAxis: {
      type: "category",
      data: d.labels || [],
      axisLabel: { color: "#8b98b0", interval: 0, rotate: 35, fontSize: 10 },
    },
    yAxis: { type: "value", min: 0, max: 100, axisLabel: { color: "#8b98b0" }, splitLine: { lineStyle: { color: "#26314a" } } },
    series: [{
      type: "bar",
      data: (d.values || []).map((v) => ({
        value: v,
        itemStyle: { color: v >= 75 ? "#26a69a" : v >= 65 ? "#f0b90b" : "#ef5350" },
      })),
      barMaxWidth: 18,
    }],
  });
  $("screenTableWrap").innerHTML = `
    <table class="screen-table">
      <thead>
        <tr>
          <th>#</th><th>代码</th><th>名称</th><th>行业</th>
          <th>现价</th><th>ROE</th><th>PE</th><th>PB</th><th>评分</th>
        </tr>
      </thead>
      <tbody>
        ${(d.detail || []).map((r, i) => `
          <tr>
            <td>${i + 1}</td>
            <td>${r.symbol}</td>
            <td>${r.name}</td>
            <td>${r.industry}</td>
            <td>${r.price == null ? "—" : r.price}</td>
            <td>${r.roe}</td>
            <td>${r.pe}</td>
            <td>${r.pb}</td>
            <td><b>${Number(r.score).toFixed(0)}</b></td>
          </tr>`).join("")}
      </tbody>
    </table>`;
  $("screenList").innerHTML = (d.detail || []).map((r) => `
    <div class="screen-row">
      <div class="sc-name">${r.name} <span>${r.symbol}</span></div>
      <div class="sc-meta">${r.industry} · ROE ${r.roe} · PE ${r.pe}</div>
      <div class="sc-score">${Number(r.score).toFixed(0)}</div>
    </div>`).join("");
}

// ---------------- 五位大师筛选并集 ---------------- //
function renderScreenAll(data) {
  screenLoaded = true;
  const listEl = $("screenList");
  const tableEl = $("screenTableWrap");
  const interEl = $("screenIntersection");
  if (tableEl) tableEl.innerHTML = "";
  if (listEl) listEl.innerHTML = "";
  if (interEl) interEl.style.display = "none";
  if (!listEl) return;

  const blocks = [];
  (data.masters || []).forEach((m) => {
    if (!m.ok) {
      blocks.push(`<div class="screen-master-block">
        <div class="screen-master-head" style="color:#8b98b0">${m.name}（${m.school}）：无符合标的</div>
      </div>`);
      return;
    }
    const chart = m.chart;
    const rows = (chart.detail || []).map((r, i) => `
      <div class="screen-row">
        <div class="sc-name">${r.name} <span>${r.symbol}</span></div>
        <div class="sc-meta">${r.industry} · ROE ${r.roe} · PE ${r.pe}</div>
        <div class="sc-score" style="color:${m.color}">${Number(r.score).toFixed(0)}</div>
      </div>`).join("");
    blocks.push(`<div class="screen-master-block">
      <div class="screen-master-head" style="color:${m.color}">${m.name} · ${m.school}（${m.summary.matched} 只符合，展示前 ${m.summary.returned} 只）</div>
      ${rows}
    </div>`);
  });
  listEl.innerHTML = blocks.join("");

  // 交集展示
  const inter = data.intersection || [];
  if (interEl) {
    if (inter.length) {
      interEl.style.display = "";
      interEl.innerHTML = `<div class="screen-inter-head">★ 多位大师共同选中（${inter.length} 只）</div>` +
        inter.map((r) => `<div class="screen-row inter">
          <div class="sc-name">${r.name} <span>${r.symbol}</span></div>
          <div class="sc-meta">${r.count} 位大师：${r.masters.join("、")}</div>
        </div>`).join("");
    }
  }
}

// ---------------- SSE 分析 ---------------- //
async function analyze() {
  if (running) return;
  const query = $("stock").value.trim();
  if (!query) { $("stock").focus(); return; }
  const question = $("question").value.trim();

  running = true;
  $("analyzeBtn").disabled = true;
  $("status").textContent = "";
  $("answer").innerHTML = "";
  resetRightPanel();
  switchTab("committee");
  let markdown = "";

  try {
    const resp = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, question }),
    });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const evt = JSON.parse(line.slice(5).trim());
        handleEvent(evt, () => markdown, (m) => (markdown = m));
      }
    }
  } catch (e) {
    $("status").textContent = "请求失败：" + e.message;
  } finally {
    running = false;
    $("analyzeBtn").disabled = false;
  }
}

async function runScreen(index, topN, master) {
  master = master || ($("screenMaster") ? $("screenMaster").value : "buffett");
  if (running) return;
  running = true;
  $("analyzeBtn").disabled = true;
  const statusEl = $("screenStatus");
  const masterName = { buffett: "巴菲特", lynch: "林奇", fisher: "费雪", livermore: "利弗莫尔", graham: "格雷厄姆", all: "五位大师" }[master] || "大师";
  if (statusEl) statusEl.textContent = "筛选中…正在拉取行情与财务数据，可能需要 10-30 秒。";
  try {
    let res, title;
    if (master === "all") {
      const resp = await fetch(`/api/buffett/screen_all?index=${encodeURIComponent(index)}&top_n=${topN || 20}`);
      res = await resp.json();
      if (!res.ok) throw new Error(res.error || "筛选失败");
      renderScreenAll(res.data);
      title = "五位大师筛选并集";
      if (statusEl) {
        const okM = (res.data.masters || []).filter((m) => m.ok).length;
        statusEl.textContent = `筛选完成：5 位大师中 ${okM} 位有结果（数据日期 ${index}）。下方为各流派 top 榜，底部为多位大师共同选中标的。`;
      }
    } else {
      const resp = await fetch(`/api/buffett/screen?index=${encodeURIComponent(index)}&top_n=${topN || 20}&master=${master}`);
      res = await resp.json();
      if (!res.ok) throw new Error(res.error || "筛选失败");
      renderScreen(res.data.chart, master);
      title = `${masterName}筛选榜`;
      if (statusEl) {
        statusEl.textContent =
          `筛选完成：${res.data.summary.matched} 只符合，展示前 ${res.data.summary.returned} 只（数据日期 ${res.data.summary.date}）。规则：${res.data.summary.method}`;
      }
    }
    if ($("screenTitle")) $("screenTitle").textContent = title + ` · ${index}`;
    switchTab("screen");
    const subject = $("chartSubject");
    if (subject) subject.textContent = `当前标的：${index} ${title}`;
    // 报告区展示并集结论（若有交集）
    if (master === "all") {
      const inter = (res.data.intersection || []);
      const lines = [`## 五位大师筛选并集（${index}）`, ""];
      lines.push("| 标的 | 被选中大师数 | 大师 |");
      lines.push("| --- | --- | --- |");
      inter.forEach((r) => lines.push(`| ${r.name} (${r.symbol}) | ${r.count} | ${r.masters.join("、")} |`));
      if (!inter.length) lines.push("| 暂无多位大师共同选中的标的 | | |");
      lines.push("", "仅供研究学习，不构成任何投资建议。");
      $("answer").innerHTML = marked.parse(lines.join("\n"));
    }
    $("status").textContent = "筛选完成 ✓";
  } catch (e) {
    $("status").textContent = "筛选失败：" + e.message;
    if (statusEl) statusEl.textContent = "筛选失败：" + e.message + "（可稍后重试或切换股票池）";
  } finally {
    running = false;
    $("analyzeBtn").disabled = false;
  }
}

function handleEvent(evt, getMd, setMd) {
  switch (evt.type) {
    case "status":
      $("status").textContent = "· " + evt.text;
      break;
    case "chart":
      if (evt.kind === "committee") renderCommittee(evt.data);
      else if (evt.kind === "debate") appendDebateMessages(evt.data);
      else if (evt.kind === "master") { /* 大师初判已在最终 committee 渲染中体现 */ }
      else if (evt.kind === "technical") renderTechnical(evt.data);
      else if (evt.kind === "macro") renderMacro(evt.data);
      else if (evt.kind === "buffett") renderBuffett(evt.data);
      else if (evt.kind === "screen") renderScreen(evt.data);
      break;
    case "token": {
      const md = getMd() + evt.text;
      setMd(md);
      $("answer").innerHTML = marked.parse(md);
      $("answer").scrollTop = $("answer").scrollHeight;
      break;
    }
    case "error":
      $("status").textContent = "错误：" + evt.text;
      break;
    case "done":
      $("status").textContent = "分析完成 ✓";
      break;
  }
}

// ---------------- 大模型 Key 配置 ---------------- //
function updateLlmBadge(configured, masked) {
  const el = $("llmMode");
  if (!el) return;
  el.textContent = configured ? "大模型模式" : "本地规则模式";
  el.classList.toggle("active", !!configured);
  if (configured && masked) el.title = "当前 Key：" + masked;
}

async function loadLlmConfig() {
  try {
    const resp = await fetch("/api/config/llm");
    const cfg = await resp.json();
    updateLlmBadge(cfg.configured, cfg.masked_key);
    if (cfg.base_url) $("cfgBaseUrl").value = cfg.base_url;
    if (cfg.model) $("cfgModel").value = cfg.model;
  } catch (e) {
    // 配置接口不可用时保持默认值
  }
}

function openConfigModal() {
  $("cfgStatus").textContent = "";
  $("configModal").style.display = "flex";
  $("cfgKey").focus();
}

function closeConfigModal() {
  $("configModal").style.display = "none";
}

async function saveLlmConfig(clear) {
  const apiKey = clear ? "" : $("cfgKey").value.trim();
  const payload = {
    api_key: apiKey,
    base_url: $("cfgBaseUrl").value.trim(),
    model: $("cfgModel").value.trim(),
  };
  $("cfgStatus").textContent = "保存中…";
  try {
    const resp = await fetch("/api/config/llm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const cfg = await resp.json();
    if (!cfg.ok) throw new Error(cfg.error || "保存失败");
    updateLlmBadge(cfg.configured, cfg.masked_key);
    $("cfgKey").value = "";
    $("cfgStatus").textContent = clear ? "已清除，切换回本地规则模式。" : "已保存并启用大模型模式。";
    setTimeout(closeConfigModal, 700);
  } catch (e) {
    $("cfgStatus").textContent = "保存失败：" + e.message;
  }
}

// ---------------- 事件绑定 ---------------- //
window.addEventListener("DOMContentLoaded", () => {
  initCharts();
  document.querySelectorAll(".tab").forEach((b) => {
    b.addEventListener("click", () => switchTab(b.dataset.tab));
  });
  $("analyzeBtn").addEventListener("click", analyze);
  $("stock").addEventListener("keydown", (e) => { if (e.key === "Enter") analyze(); });
  $("question").addEventListener("keydown", (e) => { if (e.key === "Enter") analyze(); });
  $("settingsBtn").addEventListener("click", openConfigModal);
  const screenRun = $("screenRunBtn");
  if (screenRun) {
    screenRun.addEventListener("click", () => {
      const index = $("screenIndex") ? $("screenIndex").value : "000300";
      const topN = $("screenTop") ? parseInt($("screenTop").value, 10) : 20;
      const master = $("screenMaster") ? $("screenMaster").value : "buffett";
      runScreen(index, topN, master);
    });
  }
  $("cfgSave").addEventListener("click", () => saveLlmConfig(false));
  $("cfgClear").addEventListener("click", () => saveLlmConfig(true));
  $("cfgCancel").addEventListener("click", closeConfigModal);
  $("configModal").addEventListener("click", (e) => {
    if (e.target === $("configModal")) closeConfigModal();
  });
  document.querySelectorAll(".quick a").forEach((a) => {
    a.addEventListener("click", () => {
      if (a.dataset.screen) { runScreen(a.dataset.screen); return; }
      $("stock").value = a.dataset.q; analyze();
    });
  });
  loadLlmConfig();
});
