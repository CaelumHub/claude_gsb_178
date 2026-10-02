/* ============================================================
   MiniLang 平台 —— 执行轨迹回放页
   功能：录制 -> 步进列表（虚拟滚动 / 过滤 / 折叠重复）-> 点击任意一步
        回放查看执行位置、局部/全局变量、调用栈、操作数栈与累计输出。
   口径：事件类型与后端 tracer 对齐（line/insn/call/enter/return/error），
        状态结构复用调试页（call_stack / globals / operand_stack / output）。
   ============================================================ */
(function () {
  "use strict";

  ML.Shell.render("trace");
  ML.Shell.setTitle("执行轨迹回放");

  const esc = ML.escapeHtml;
  const $ = (id) => document.getElementById(id);

  const inputEditor = new ML.CodeEditor($("editor"), {
    value: ML.store.get("draft_source", ML.DEFAULT_CODE), height: 220,
  });

  /* ----------------------------------------------------------
   * 事件类型元数据（与后端 tracer.py 常量一一对应）
   * ---------------------------------------------------------- */
  const I = { SEQ: 0, TYPE: 1, T: 2, DEPTH: 3, FUNC: 4, LINE: 5, OFF: 6, IC: 7,
    OP: 8, OPERAND: 9, CALLEE: 8, KIND: 9, ARGC: 10, ARGS: 11, RESULT: 12, NONDET: 13,
    VALUE: 8, MAIN: 9, DIAG: 8 };

  const TYPES = [
    { key: "line",   label: "行",     icon: "📝", cls: "ev-line",   on: true  },
    { key: "insn",   label: "指令",   icon: "🔢", cls: "ev-insn",   on: true  },
    { key: "call",   label: "调用",   icon: "📞", cls: "ev-call",   on: true  },
    { key: "call-b", label: "内建",   icon: "⚙️", cls: "ev-builtin",on: true  },
    { key: "enter",  label: "进入",   icon: "⬇️", cls: "ev-enter",  on: true  },
    { key: "return", label: "返回",   icon: "⬆️", cls: "ev-return", on: true  },
    { key: "error",  label: "错误",   icon: "💥", cls: "ev-error",  on: true  },
  ];
  const TYPE_MAP = Object.fromEntries(TYPES.map((t) => [t.key, t]));

  /* ----------------------------------------------------------
   * 页面状态
   * ---------------------------------------------------------- */
  let trace = null;          // 后端返回的 trace summary（含 events / source）
  let events = [];           // 原始事件数组
  let sourceLines = [];
  let viewRows = [];         // 过滤 / 折叠后用于渲染的行
  let selectedSeq = -1;
  let stateCache = new Map(); // seq -> 后端状态快照
  let stateLoading = null;
  let playTimer = null;
  let sourceViewer = null;

  const ROW_H = 26;
  const OVERSCAN = 8;

  /* ----------------------------------------------------------
   * 示例 / 项目源码 / 最近轨迹
   * ---------------------------------------------------------- */
  const sampleSel = $("sample");
  sampleSel.innerHTML = '<option value="">— 载入示例 —</option>' +
    ML.SAMPLES.map((s, i) => `<option value="${i}">${esc(s.name)}</option>`).join("");
  sampleSel.onchange = () => {
    if (sampleSel.value !== "") {
      inputEditor.setValue(ML.SAMPLES[+sampleSel.value].code);
      sampleSel.value = "";
    }
  };
  $("btn-loadproj").onclick = async () =>
    inputEditor.setValue(await ML.Shell.loadSource(ML.DEFAULT_CODE));

  async function loadRecent() {
    const data = await ML.api.traceList();
    const sel = $("recent");
    const list = (data.traces || []).slice(0, 20);
    sel.innerHTML = '<option value="">— 最近轨迹 —</option>' + list.map((t) => {
      const n = t.event_count || 0;
      return `<option value="${esc(t.id)}">${esc(String(n))} 步 · ${esc(t.granularity || "line")} · ${esc(t.timestamp || "")}</option>`;
    }).join("");
  }
  $("recent").onchange = async () => {
    const id = $("recent").value;
    if (!id) return;
    const data = await ML.api.traceGet(id);
    if (data.ok && data.trace) {
      bindTrace(data.trace);
    } else {
      ML.toast("轨迹不存在或已过期", "err");
    }
  };

  /* ----------------------------------------------------------
   * 录制
   * ---------------------------------------------------------- */
  $("btn-record").onclick = async () => {
    const btn = $("btn-record");
    btn.disabled = true;
    btn.textContent = "⏳ 录制中…";
    try {
      const granularity = $("granularity").value;
      const data = await ML.api.traceStart(inputEditor.getValue(), {
        trace_granularity: granularity,
      });
      const tr = data.trace;
      if (!data.ok || !tr) {
        ML.toast("编译失败，无法录制轨迹", "err");
        $("summary").innerHTML = ML.renderDiagnostics(data.diagnostics || [], inputEditor.getValue());
        return;
      }
      bindTrace(tr);
      ML.toast("轨迹录制完成：" + tr.event_count + " 步", "ok");
    } finally {
      btn.disabled = false;
      btn.textContent = "🎞️ 运行并录制轨迹";
      loadRecent();
    }
  };

  /* ----------------------------------------------------------
   * 绑定一条轨迹，构建过滤后的渲染行
   * ---------------------------------------------------------- */
  function typeKey(ev) {
    if (ev[I.TYPE] === "call" && ev[I.KIND] === "builtin") return "call-b";
    return ev[I.TYPE];
  }

  function bindTrace(tr) {
    stopPlay();
    trace = tr;
    events = tr.events || [];
    sourceLines = (tr.source_lines && tr.source_lines.length)
      ? tr.source_lines : (tr.source || "").split("\n");
    stateCache = new Map();
    selectedSeq = -1;
    renderSummary();
    buildSourceViewer();
    buildTypeFilters();
    rebuildRows(true);
    selectSeq(events.length ? 0 : -1, { jump: "first" });
  }

  function renderSummary() {
    const c = trace.type_counts || {};
    const badge = (text, cls) => text ? ML.badge(text, cls) : "";
    $("summary").innerHTML =
      ML.badge(trace.granularity === "instruction" ? "指令级粒度" : "行级粒度", "purple") +
      badge("行 " + (c.line || 0), "blue") +
      badge("指令 " + (c.insn || 0), "gray") +
      badge("调用 " + (c.call || 0), "cyan") +
      badge("进入 " + (c.enter || 0), "green") +
      badge("返回 " + (c.return || 0), "amber") +
      badge("错误 " + (c.error || 0), "red") +
      ML.badge("指令总数 " + trace.instruction_count, "gray") +
      ML.badge("⏱ " + ML.fmtMs(trace.elapsed_ms), "gray") +
      (trace.truncated ? ML.badge("⚠️ 已达事件上限截断（仅保留前 " + trace.event_count + " 步）", "red") : "") +
      (trace.error ? ML.badge("运行出错：" + trace.error.message, "red") : "") +
      (trace.finished ? ML.badge("程序已结束", "green") : "");
  }

  function buildTypeFilters() {
    $("type-filters").innerHTML = TYPES.map((t) =>
      `<label class="type-chip ${t.cls} ${t.on ? "on" : ""}" data-k="${t.key}">
        <span>${t.icon}</span>${esc(t.label)}<input type="checkbox" ${t.on ? "checked" : ""} style="display:none"></label>`
    ).join("");
    $("type-filters").querySelectorAll(".type-chip").forEach((el) => {
      el.onclick = () => {
        const t = TYPE_MAP[el.dataset.k];
        t.on = !t.on;
        el.classList.toggle("on", t.on);
        el.querySelector("input").checked = t.on;
        rebuildRows();
      };
    });
  }

  /* ----------------------------------------------------------
   * 源码只读查看器（复用 CodeEditor，语法高亮 + 当前行定位）
   * ---------------------------------------------------------- */
  function buildSourceViewer() {
    const host = $("trace-source");
    host.innerHTML = "";
    sourceViewer = new ML.CodeEditor(host, {
      value: trace.source || sourceLines.join("\n"), height: 240, readonly: true,
      autocomplete: false,
    });
  }

  /* ----------------------------------------------------------
   * 行过滤 / 折叠
   * ---------------------------------------------------------- */
  function rebuildRows(resetScroll) {
    const q = $("filter-text").value.trim().toLowerCase();
    const fold = $("fold-repeats").checked;
    const rows = [];
    let prevKey = null;
    for (const ev of events) {
      const tk = typeKey(ev);
      const meta = TYPE_MAP[tk];
      if (!meta || !meta.on) continue;
      if (q) {
        const hay = (ev[I.FUNC] + " " + ev[I.LINE] + " " + (ev[I.CALLEE] || "") + " "
          + (ev[I.OP] || "")).toLowerCase();
        if (!hay.includes(q)) continue;
      }
      // 折叠键：同类型 / 同函数 / 同行 / 同深度的相邻步骤压成一组
      const rk = tk + "|" + ev[I.DEPTH] + "|" + ev[I.FUNC] + "|" + ev[I.LINE]
        + "|" + (ev[I.CALLEE] || "");
      if (fold && q === "" && tk !== "error" && rk === prevKey) {
        rows[rows.length - 1].count++;
        rows[rows.length - 1].last = ev[I.SEQ];
        continue;
      }
      rows.push({ seq: ev[I.SEQ], ev, count: 1, first: ev[I.SEQ], last: ev[I.SEQ] });
      prevKey = rk;
    }
    viewRows = rows;
    renderVirtual(resetScroll);
    updateStepPos();
  }

  $("filter-text").addEventListener("input", ML.debounce(() => rebuildRows(), 150));
  $("fold-repeats").onchange = () => rebuildRows();

  /* ----------------------------------------------------------
   * 虚拟滚动列表
   * ---------------------------------------------------------- */
  const listEl = $("step-list");

  function renderVirtual(resetScroll) {
    const total = viewRows.length;
    const ph = document.createElement("div");
    ph.style.height = (total * ROW_H) + "px";
    ph.style.position = "relative";

    const scrollTop = resetScroll ? 0 : listEl.scrollTop;
    const viewH = listEl.clientHeight || 420;
    const start = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN);
    const end = Math.min(total, Math.ceil((scrollTop + viewH) / ROW_H) + OVERSCAN);

    for (let i = start; i < end; i++) {
      const row = viewRows[i];
      const ev = row.ev;
      const tk = typeKey(ev);
      const meta = TYPE_MAP[tk] || { icon: "•", cls: "" };
      const selected = ev[I.SEQ] === selectedSeq;
      const indent = Math.min(ev[I.DEPTH], 18);
      const summary = rowSummary(ev, tk);
      const el = document.createElement("div");
      el.className = "step-row " + meta.cls + (selected ? " sel" : "");
      el.style.top = (i * ROW_H) + "px";
      el.dataset.seq = ev[I.SEQ];
      el.innerHTML =
        `<span class="step-indent" style="width:${indent * 12}px"></span>` +
        `<span class="step-ico">${meta.icon}</span>` +
        `<span class="step-main"><span class="step-fn">${esc(ev[I.FUNC])}</span>` +
        `<span class="step-msg">${summary}</span></span>` +
        (row.count > 1 ? `<span class="step-count">×${row.count}</span>` : "") +
        `<span class="step-ln">L${ev[I.LINE]}</span>` +
        `<span class="step-seq">#${ev[I.SEQ]}</span>`;
      el.onclick = () => selectSeq(ev[I.SEQ]);
      ph.appendChild(el);
    }
    listEl.innerHTML = "";
    listEl.appendChild(ph);
    if (resetScroll) listEl.scrollTop = 0;
  }

  function rowSummary(ev, tk) {
    if (tk === "line") return `<span class="muted">${esc(opSnippet(ev[I.LINE]))}</span>`;
    if (tk === "insn") return `<span class="mono" style="color:var(--primary)">${esc(ev[I.OP])}</span>` +
      (ev[I.OPERAND] != null && ev[I.OPERAND] !== "" ? ` <span class="muted">${esc(String(ev[I.OPERAND]))}</span>` : "");
    if (tk === "call" || tk === "call-b") {
      const args = (ev[I.ARGS] || []).map((a) => ML.fmtValText(a)).join(", ");
      let tail = "";
      if (tk === "call-b" && ev[I.RESULT] != null) tail = " → " + ML.fmtVal(ev[I.RESULT]);
      return `调用 <b>${esc(ev[I.CALLEE])}</b>(${esc(args)})${tail}`;
    }
    if (tk === "enter") return `进入函数，调用点 L${ev[I.LINE]}`;
    if (tk === "return") return (ev[I.MAIN] ? "main 返回，程序结束" : "返回") +
      (ev[I.VALUE] != null ? " → " + ML.fmtVal(ev[I.VALUE]) : "");
    if (tk === "error") return `<span style="color:var(--danger)">${esc((ev[I.DIAG] || {}).message || "运行时错误")}</span>`;
    return "";
  }

  function opSnippet(line) {
    const txt = sourceLines[line - 1];
    return txt == null ? "" : txt.trim().slice(0, 46);
  }

  listEl.addEventListener("scroll", ML.debounce(() => renderVirtual(false), 12));

  /* ----------------------------------------------------------
   * 选中一步：滚动到可见 + 拉取回放状态
   * ---------------------------------------------------------- */
  function rowIndexOfSeq(seq) {
    return viewRows.findIndex((r) => r.seq === seq);
  }

  function ensureRowVisible(idx) {
    const top = idx * ROW_H;
    if (top < listEl.scrollTop) listEl.scrollTop = top;
    else if (top + ROW_H > listEl.scrollTop + listEl.clientHeight)
      listEl.scrollTop = top - listEl.clientHeight + ROW_H * 2;
  }

  async function selectSeq(seq, opts) {
    if (!events.length) return;
    seq = Math.max(-1, Math.min(seq, events.length - 1));
    selectedSeq = seq;
    const idx = rowIndexOfSeq(seq);
    if (idx >= 0) {
      ensureRowVisible(idx);
      renderVirtual(false);
    }
    updateStepPos();
    await loadState(seq);
  }

  function updateStepPos() {
    const total = viewRows.length;
    const idx = rowIndexOfSeq(selectedSeq);
    $("step-pos").textContent = total
      ? `事件 #${selectedSeq} · 列表 ${idx + 1}/${total}` : "";
  }

  async function loadState(seq) {
    renderEventPlaceholder(seq);
    if (stateCache.has(seq)) {
      renderState(seq, stateCache.get(seq));
      return;
    }
    if (stateLoading) stateLoading.aborted = true;
    const token = stateLoading = { aborted: false };
    const data = await ML.api.traceState(trace.id, seq);
    if (token.aborted || selectedSeq !== seq) return;
    if (!data.ok) {
      ML.toast(data.error || "状态回放失败", "err");
      return;
    }
    stateCache.set(seq, data);
    if (stateCache.size > 300) {
      const firstKey = stateCache.keys().next().value;
      stateCache.delete(firstKey);
    }
    renderState(seq, data);
  }

  /* ----------------------------------------------------------
   * 详情渲染
   * ---------------------------------------------------------- */
  function renderEventPlaceholder(seq) {
    const ev = seq >= 0 ? events[seq] : null;
    $("detail-title").innerHTML = "📍 执行位置";
    $("detail-event").innerHTML = ev
      ? `<span class="badge gray">#${ev[I.SEQ]}</span> 加载中…`
      : '<span class="muted">程序入口（任何指令执行前）</span>';
  }

  function renderState(seq, s) {
    const ev = seq >= 0 ? events[seq] : null;
    const tk = ev ? typeKey(ev) : null;
    const meta = tk ? TYPE_MAP[tk] : null;

    // 位置：enter/return/call/error 以事件自带行为准（指令执行中间），其余看快照
    const [posFn, posLine] = s.position || ["<main>", 0];
    $("detail-title").innerHTML =
      `📍 执行位置 ${meta ? meta.icon + " " + esc(meta.label) : "入口"}`;
    $("detail-event").innerHTML =
      (ev ? `<span class="badge purple">${esc(ev[I.TYPE])}</span> ` : "") +
      `<span style="color:var(--primary)">${esc(posFn)}</span> · 第 <b>${posLine}</b> 行 · ` +
      `<span class="muted">事件 #${seq} · 指令计数 ${s.instruction_count} · t=${s.elapsed_ms}ms</span>` +
      (s.next_instruction
        ? `<div style="margin-top:6px"><span class="badge purple">${esc(s.next_instruction.op)}</span> ` +
          `<span class="muted">@偏移 ${s.next_instruction.offset} · L${s.next_instruction.line}</span></div>`
        : '<div class="muted" style="margin-top:4px">（无下一指令）</div>');

    sourceViewer.setCurrentLine(posLine || null);
    sourceViewer.scrollToLine(posLine);

    // 调用栈
    const frames = s.call_stack || [];
    $("d-callstack").innerHTML = frames.length
      ? '<div class="callstack">' + frames.map((f, i) =>
          `<div class="frame ${i === 0 ? "top" : ""}">
            <span class="fn">${esc(f.function)}</span><span class="ln">L${f.line}</span>
            ${f.is_main ? '<span class="badge gray">main</span>' : ""}
          </div>`).join("") + "</div>"
      : ML.empty("调用栈为空（main 已返回）");

    // 局部 / 全局变量（与调试页同一渲染结构与值格式）
    const locals = frames.length ? frames[0].locals : {};
    $("d-locals").innerHTML = renderVars(locals);
    $("d-globals").innerHTML = renderVars(s.globals || {});

    // 事件负载
    $("d-payload").innerHTML = ev ? renderPayload(ev, tk, s) : '<span class="muted">—</span>';

    // 操作数栈
    const stack = s.operand_stack || [];
    $("d-operand").innerHTML = stack.length
      ? stack.slice().reverse().map((v, i) =>
          `<div class="mono" style="padding:2px 0">${i === 0 ? "栈顶 " : "     "}${ML.fmtVal(v)}</div>`).join("")
      : '<span class="muted">（空）</span>';

    // 输出
    const con = $("d-output");
    con.innerHTML = (s.output || []).length
      ? s.output.map((l) => `<span class="ln-out">${esc(l)}</span>`).join("<br>")
      : '<span class="muted">（暂无输出）</span>';
  }

  function renderVars(vars) {
    const keys = Object.keys(vars || {});
    if (!keys.length) return '<span class="muted">（无）</span>';
    return `<table class="tbl var-table"><thead><tr><th>名称</th><th>值</th></tr></thead><tbody>` +
      keys.map((k) => `<tr><td class="mono">${esc(k)}</td><td class="v">${ML.fmtVal(vars[k])}</td></tr>`).join("") +
      `</tbody></table>`;
  }

  function renderPayload(ev, tk, s) {
    const rows = [];
    const add = (k, v) => rows.push(`<tr><td class="muted">${esc(k)}</td><td class="mono">${v}</td></tr>`);
    add("类型", `<b>${esc(ev[I.TYPE])}</b>`);
    add("函数", esc(ev[I.FUNC]));
    add("源码行", "L" + ev[I.LINE]);
    add("帧深度", ev[I.DEPTH]);
    add("指令偏移", ev[I.OFF]);
    add("时刻", ev[I.T] + " ms");
    if (tk === "call" || tk === "call-b") {
      add("被调对象", esc(ev[I.CALLEE]));
      add("参数个数", ev[I.ARGC]);
      add("参数", (ev[I.ARGS] || []).length
        ? ev[I.ARGS].map((a) => ML.fmtVal(a)).join(" ") : "（无）");
      if (ev[I.RESULT] != null) add("返回值", ML.fmtVal(ev[I.RESULT]));
    }
    if (tk === "return") add("返回值", ev[I.VALUE] != null ? ML.fmtVal(ev[I.VALUE]) : "null");
    if (tk === "line") add("首条指令", esc(ev[I.OP] || ""));
    if (tk === "insn") add("操作数", ev[I.OPERAND] != null ? esc(JSON.stringify(ev[I.OPERAND])) : "—");
    if (tk === "error" && ev[I.DIAG]) {
      return `<table class="tbl">${rows.join("")}</table>` +
        ML.renderDiagnostic(ev[I.DIAG], trace.source);
    }
    return `<table class="tbl">${rows.join("")}</table>`;
  }

  /* ----------------------------------------------------------
   * 步进 / 自动播放 / 键盘
   * ---------------------------------------------------------- */
  function move(delta) {
    if (!events.length) return;
    const idx = rowIndexOfSeq(selectedSeq);
    let cur = idx;
    if (cur < 0) cur = delta > 0 ? -1 : viewRows.length;
    const next = viewRows[Math.max(0, Math.min(viewRows.length - 1, cur + delta))];
    if (next) selectSeq(next.seq);
  }

  $("btn-first").onclick = () => selectSeq(viewRows.length ? viewRows[0].first : -1);
  $("btn-last").onclick = () => selectSeq(viewRows.length ? viewRows[viewRows.length - 1].last : -1);
  $("btn-prev").onclick = () => move(-1);
  $("btn-next").onclick = () => move(1);

  $("btn-play").onclick = () => {
    if (playTimer) { stopPlay(); return; }
    const step = () => {
      if (!events.length) { stopPlay(); return; }
      const idx = rowIndexOfSeq(selectedSeq);
      if (idx < 0 || idx >= viewRows.length - 1) {
        stopPlay();
        return;
      }
      move(1);
    };
    step();
    playTimer = setInterval(step, Math.max(20, +$("play-speed").value || 220));
    $("btn-play").textContent = "⏸ 暂停";
  };
  function stopPlay() {
    if (playTimer) clearInterval(playTimer);
    playTimer = null;
    $("btn-play").textContent = "▶";
  }

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); move(-1)
    } else if (e.key === "Home") { e.preventDefault(); $("btn-first").click(); }
    else if (e.key === "End") { e.preventDefault(); $("btn-last").click(); }
    else if (e.key === " ") { e.preventDefault(); $("btn-play").click(); }
  });

  /* 初始空态 */
  listEl.innerHTML = '<div style="padding:40px 16px">' +
    ML.empty("点击右上角「运行并录制轨迹」，即可在此逐步回放执行过程") + "</div>";
  loadRecent();
})();
