/* Automated device tests page. Reuses app.js helpers (api, appUrl, toast,
 * panelIsVisible) and receives live progress as "z3-test-event" events from
 * the shared SSE connection, so it pauses with the rest of the panel. */
(() =>
{
  const view = document.getElementById("view-tests");
  const $t = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LOG_LIMIT = 200;
  const SCRIPT_KEY = "z3-tests-script";
  const VERDICTS = {
    pass: ["通过", "v-pass"], fail: ["不通过", "v-fail"], error: ["错误", "v-error"], inconclusive: ["无法判定", "v-inconclusive"],
    skipped: ["已跳过", ""], "not-run": ["未运行", ""], aborted: ["已中止", ""], running: ["运行中", "v-running"], pending: ["待运行", ""],
  };
  const RUN_STATUS = {
    idle: ["空闲", ""], setup: ["绑定与上报配置", "info"], running: ["运行中", "info"], completed: ["已完成", "ok"],
    aborted: ["已中止", "warn"], error: ["出错", "bad"],
  };
  const tests = { scripts: [], scriptId: null, script: null, selected: new Set(), devices: [], status: null, results: [], log: [], promptId: null, loading: false };

  function visible()
  {
    return panelIsVisible() && !view.hidden;
  }

  function chip(map, key)
  {
    const [label, tone] = map[key] || [key || "-", ""];
    return `<span class="chip ${tone}">${esc(label)}</span>`;
  }

  function summaryChips(summary)
  {
    const order = ["pass", "fail", "error", "inconclusive", "skipped", "aborted", "not-run", "running", "pending"];
    return Object.entries(summary || {})
      .sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]))
      .map(([key, count]) => `<span class="chip ${(VERDICTS[key] || ["", ""])[1]}">${esc((VERDICTS[key] || [key])[0])} ${count}</span>`)
      .join("");
  }

  function describeStep(step)
  {
    if (step.label) return step.label;
    const names = {
      write_attributes: () => "写入属性 " + Object.entries(step.attributes || {}).map(([k, v]) => `${k}=${v}`).join(", "),
      read: () => "读取 " + (step.attributes || []).join(", "),
      set_state: () => `前置状态：${step.on ? "开" : "关"}，亮度 ${step.level}`,
      command: () => "发送 " + step.command + (step.level !== undefined ? ` L=${step.level}` : "") + (step.transition !== undefined ? ` T=${step.transition}` : "") + (step.mode !== undefined ? ` ${step.mode}` : "") + (step.rate !== undefined ? ` rate=${step.rate}` : ""),
      burst: () => `连续发送 ${(step.frames || []).length} 帧，间隔 ${step.gap_ms || 0} ms`,
      expect: () => `检查上报（${(step.matchers || []).length} 项）`,
      wait: () => (step.ms !== undefined ? `等待 ${step.ms} ms` : "等待上报静默"),
      operator: () => "操作员：" + step.prompt,
      confirm: () => "人工确认：" + step.prompt,
    };
    return (names[step.type] || (() => step.type))();
  }

  // ------------------------------------------------------------ data

  async function loadScripts()
  {
    const data = await api("/api/tests/scripts");
    tests.scripts = data.scripts || [];
    let wanted = tests.scriptId;
    if (!wanted)
    {
      try
      {
        wanted = localStorage.getItem(SCRIPT_KEY);
      }
      catch
      {
        wanted = null;
      }
    }
    if (!tests.scripts.some((item) => item.id === wanted && !item.error))
    {
      wanted = (tests.scripts.find((item) => !item.error) || {}).id || null;
    }
    renderScripts();
    if (wanted && (wanted !== tests.scriptId || !tests.script))
    {
      await selectScript(wanted);
    }
  }

  async function selectScript(id)
  {
    const data = await api("/api/tests/scripts/" + encodeURIComponent(id));
    const sameScript = tests.script && tests.script.id === id;
    tests.scriptId = id;
    tests.script = data.script;
    try
    {
      localStorage.setItem(SCRIPT_KEY, id);
    }
    catch
    {
      // Remembering the last script is only a convenience.
    }
    if (!sameScript)
    {
      tests.selected = new Set(tests.script.cases.map((c) => c.id));
    }
    renderScripts();
    renderCases();
  }

  async function loadDevices()
  {
    const data = await api("/api/device-center");
    tests.devices = (data.devices || []).filter((d) => d.role !== "gateway" && Object.values(d.endpoints || {}).some((ep) => (ep.inClusters || []).includes(6)));
    renderDevices();
  }

  async function loadStatus()
  {
    applyStatus(await api("/api/tests/status"), { replaceLog: true });
  }

  async function loadResults()
  {
    const data = await api("/api/tests/results");
    tests.results = data.results || [];
    renderResults();
  }

  async function refreshAll()
  {
    if (!visible() || tests.loading) return;
    tests.loading = true;
    try
    {
      await Promise.all([loadScripts(), loadDevices(), loadStatus(), loadResults()]);
    }
    catch (err)
    {
      toast(err.message);
    }
    finally
    {
      tests.loading = false;
    }
  }

  // ------------------------------------------------------------ render

  function renderScripts()
  {
    const list = $t("tests-script-list");
    if (!tests.scripts.length)
    {
      list.innerHTML = '<p class="muted">暂无脚本，请上传 JSON 测试脚本。</p>';
      return;
    }
    list.innerHTML = tests.scripts.map((item) =>
    {
      const meta = item.error
        ? `<small class="danger-text">${esc(item.error.split("\n")[0])}</small>`
        : `<small>${item.cases} 个用例${item.operator_cases ? ` · ${item.operator_cases} 个需旋钮` : ""}</small>`;
      return `<button type="button" class="tests-script ${item.id === tests.scriptId ? "active" : ""}" data-script="${esc(item.id)}" title="${esc(item.source || item.id)}" ${item.error ? "disabled" : ""}>`
        + `<strong>${esc(item.title || item.id)}</strong><span class="chip ${item.builtin ? "" : "info"}">${item.builtin ? "内置" : "已上传"}</span>${meta}</button>`;
    }).join("");
  }

  function caseVerdicts()
  {
    const status = tests.status;
    if (!status || !status.cases || !status.script || status.script.id !== tests.scriptId) return new Map();
    return new Map(status.cases.map((c) => [c.id, c.verdict]));
  }

  function renderCases()
  {
    const list = $t("tests-case-list");
    const script = tests.script;
    const deleteButton = ensureDeleteButton();
    if (!script)
    {
      $t("tests-script-title").textContent = "用例";
      $t("tests-script-meta").textContent = "选择左侧脚本";
      list.innerHTML = "";
      deleteButton.hidden = true;
      updateSelectionCount();
      return;
    }
    $t("tests-script-title").textContent = script.title || script.id;
    $t("tests-script-meta").textContent = [script.source, script.description].filter(Boolean).join(" · ") || script.id;
    deleteButton.hidden = Boolean(script.builtin);
    const term = $t("tests-case-filter").value.trim().toLowerCase();
    const verdicts = caseVerdicts();
    const currentCase = tests.status?.active ? tests.status.current?.case_id : null;
    const items = script.cases.filter((c) => !term || `${c.number} ${c.title} ${c.notes || ""}`.toLowerCase().includes(term));
    list.innerHTML = items.map((c) =>
    {
      const flags = c.flags || {};
      const badges = [
        flags.operator ? '<span class="chip warn">需要操作旋钮</span>' : "",
        flags.confirm ? '<span class="chip">人工确认</span>' : "",
        flags.timing ? '<span class="chip info">时序敏感</span>' : "",
      ].join("");
      const verdict = verdicts.get(c.id);
      const steps = c.steps.map((step) => `<li>${esc(describeStep(step))}</li>`).join("");
      return `<div class="tests-case ${c.id === currentCase ? "current" : ""}" data-case="${esc(c.id)}">`
        + `<input type="checkbox" data-case-check="${esc(c.id)}" aria-label="选择 ${esc(c.number)}" ${tests.selected.has(c.id) ? "checked" : ""}>`
        + `<div class="tests-case-main"><strong>${esc(c.number)}</strong><span>${esc(c.title)}</span>${badges ? `<div class="tests-case-badges">${badges}</div>` : ""}</div>`
        + (verdict ? chip(VERDICTS, verdict) : "<span></span>")
        + `<details><summary>${c.steps.length} 个步骤${c.notes ? " · " + esc(c.notes) : ""}</summary><ol>${steps}</ol></details></div>`;
    }).join("") || '<p class="muted">没有匹配的用例。</p>';
    updateSelectionCount();
  }

  function ensureDeleteButton()
  {
    let button = $t("tests-delete-script");
    if (!button)
    {
      button = document.createElement("button");
      button.id = "tests-delete-script";
      button.type = "button";
      button.className = "ghost danger-text";
      button.textContent = "删除脚本";
      button.hidden = true;
      button.addEventListener("click", () => deleteScript().catch((err) => toast(err.message)));
      $t("tests-select-none").after(button);
    }
    return button;
  }

  function updateSelectionCount()
  {
    const script = tests.script;
    const active = Boolean(tests.status?.active);
    if (!script)
    {
      $t("tests-selection-count").textContent = "未选择脚本";
      $t("tests-start").disabled = true;
      return;
    }
    const chosen = script.cases.filter((c) => tests.selected.has(c.id));
    const knob = chosen.filter((c) => c.flags?.operator).length;
    $t("tests-selection-count").textContent = `已选 ${chosen.length} / ${script.cases.length} 个用例` + (knob ? `，其中 ${knob} 个需要操作旋钮` : "");
    $t("tests-start").disabled = active || !chosen.length || !$t("tests-device").value;
  }

  function renderDevices()
  {
    const select = $t("tests-device");
    const previous = select.value;
    if (!tests.devices.length)
    {
      select.innerHTML = '<option value="">暂无可测试的设备</option>';
    }
    else
    {
      select.innerHTML = tests.devices.map((d) =>
      {
        const ready = d.identity === "known" && d.nodeId && d.addressVerified;
        const label = `${d.name || d.model || d.nodeId || d.id} · ${d.nodeId || "地址待确认"}${ready ? "" : "（待确认）"}`;
        return `<option value="${esc(d.id)}" ${ready ? "" : "disabled"}>${esc(label)}</option>`;
      }).join("");
      const keep = tests.devices.find((d) => d.id === previous && d.addressVerified);
      const first = tests.devices.find((d) => d.identity === "known" && d.addressVerified);
      select.value = keep ? keep.id : first ? first.id : "";
    }
    renderEndpoints();
  }

  function renderEndpoints()
  {
    const device = tests.devices.find((d) => d.id === $t("tests-device").value);
    const select = $t("tests-endpoint");
    const previous = select.value;
    const endpoints = Object.entries(device?.endpoints || {}).filter(([, ep]) => (ep.inClusters || []).includes(6));
    select.innerHTML = endpoints.map(([ep, info]) => `<option value="${esc(ep)}">端点 ${esc(ep)} · ${esc(info.type || "")}${(info.inClusters || []).includes(8) ? "" : "（无调光簇）"}</option>`).join("") || '<option value="">无</option>';
    const scriptEndpoint = String(tests.script?.endpoint ?? "");
    if (endpoints.some(([ep]) => ep === previous)) select.value = previous;
    else if (endpoints.some(([ep]) => ep === scriptEndpoint)) select.value = scriptEndpoint;
    updateSelectionCount();
  }

  function renderStatus()
  {
    const status = tests.status || { status: "idle" };
    const box = $t("tests-status");
    const active = Boolean(status.active);
    $t("tests-abort").disabled = !active;
    if (!status.run_id)
    {
      box.innerHTML = '<div class="tests-status-line">' + chip(RUN_STATUS, "idle") + '<span class="muted">选择脚本、设备与用例后点击“开始测试”。测试期间该设备由测试独占，自动发现暂停。</span></div>';
    }
    else
    {
      const total = status.total || 0;
      const done = Math.min(status.done || 0, total);
      const current = status.current;
      const percent = total ? Math.round((done / total) * 100) : 0;
      const stepText = current
        ? `<div class="tests-current"><strong>${esc(current.number)}</strong> ${esc(current.title || "")}`
          + `<p class="muted">步骤 ${current.step_index + 1}${current.step_total ? " / " + current.step_total : ""}：${esc(current.step_label)}</p>`
          + (current.observe ? `<p class="tests-observe">${esc(current.observe)}</p>` : "") + "</div>"
        : "";
      box.innerHTML = `<div class="tests-status-line">${chip(RUN_STATUS, status.status)}<strong>${esc(status.script?.title || "")}</strong>`
        + `<span class="muted">${esc(status.device?.name || status.device?.eui64 || "")} · 端点 ${esc(status.device?.endpoint ?? "")}</span></div>`
        + `<div class="tests-progress" role="progressbar" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}"><div style="width:${percent}%"></div></div>`
        + `<div class="tests-status-line"><span class="muted">${done} / ${total} 个用例</span><div class="tests-summary">${summaryChips(status.summary)}</div></div>`
        + (status.message ? `<p class="muted warning-text">${esc(status.message)}</p>` : "")
        + stepText;
    }
    renderPrompt(status.prompt);
    updateSelectionCount();
  }

  function renderPrompt(prompt)
  {
    const box = $t("tests-prompt");
    if (!prompt)
    {
      box.hidden = true;
      box.innerHTML = "";
      tests.promptId = null;
      return;
    }
    if (prompt.id === tests.promptId) return;  // keep a comment being typed
    tests.promptId = prompt.id;
    const confirm = prompt.kind === "confirm";
    box.className = "tests-prompt" + (confirm ? " confirm" : "");
    const actions = confirm
      ? '<button type="button" class="tests-ok" data-respond="pass">通过</button><button type="button" class="danger" data-respond="fail">不通过</button><button type="button" class="ghost" data-respond="skip">跳过</button>'
      : '<button type="button" class="primary" data-respond="continue">继续</button><button type="button" class="ghost" data-respond="skip-case">跳过本用例</button>';
    box.innerHTML = `<span class="tests-prompt-kind">${confirm ? "人工确认" : "需要操作员"} · ${esc(prompt.case || "")} ${esc(prompt.case_title || "")}</span>`
      + `<div class="tests-prompt-text">${esc(prompt.text)}</div>`
      + (prompt.observe && !confirm ? `<p class="tests-observe">${esc(prompt.observe)}</p>` : "")
      + (confirm ? '<textarea id="tests-prompt-comment" placeholder="备注（可选），例如：亮度跳变、时长约 2 秒"></textarea>' : "")
      + `<div class="tests-prompt-actions">${actions}</div>`;
    box.hidden = false;
    box.querySelector("[data-respond]")?.focus({ preventScroll: true });
  }

  function renderLog()
  {
    const output = $t("tests-log");
    const followTail = output.scrollHeight - output.scrollTop - output.clientHeight < 40;
    output.textContent = tests.log.join("\n");
    if (followTail) output.scrollTop = output.scrollHeight;
  }

  function renderResults()
  {
    const list = $t("tests-result-list");
    if (!tests.results.length)
    {
      list.innerHTML = '<p class="muted">暂无结果。</p>';
      return;
    }
    list.innerHTML = tests.results.slice(0, 100).map((r) =>
    {
      const base = "/api/tests/results/" + encodeURIComponent(r.id);
      const when = r.started_at ? new Date(r.started_at).toLocaleString() : "";
      return `<div class="tests-result"><div class="tests-result-head"><strong>${esc(r.script?.title || r.script?.id)}</strong>${chip(RUN_STATUS, r.status)}</div>`
        + `<small>${esc(when)} · ${esc(r.device?.name || r.device?.model || r.device?.eui64 || "")}${r.device?.swBuildId ? " · " + esc(r.device.swBuildId) : ""} · ${r.cases} 个用例</small>`
        + `<div class="tests-summary">${summaryChips(r.summary)}</div>`
        + `<div class="tests-result-actions"><a href="${esc(appUrl(base + "/report.html"))}" target="_blank" rel="noopener">查看报告</a>`
        + `<a href="${esc(appUrl(base + "/report.html?download=1"))}">下载 HTML</a>`
        + `<a href="${esc(appUrl(base))}" target="_blank" rel="noopener">JSON</a>`
        + `<button type="button" class="danger-text" data-delete-result="${esc(r.id)}">删除</button></div></div>`;
    }).join("");
  }

  // ------------------------------------------------------------ state

  function applyStatus(status, { replaceLog = false } = {})
  {
    const wasActive = Boolean(tests.status?.active);
    const previousCases = tests.status?.cases;
    tests.status = { ...status, cases: status.cases || (status.run_id === tests.status?.run_id ? previousCases : undefined) };
    if (replaceLog && Array.isArray(status.log))
    {
      tests.log = status.log.slice(-LOG_LIMIT);
      renderLog();
    }
    renderStatus();
    renderCases();
    if (wasActive && !status.active)
    {
      loadResults().catch(() => {});
      if (!status.cases) loadStatus().catch(() => {});
    }
  }

  function onTestEvent(event)
  {
    const message = event.detail || {};
    if (message.type === "log" && message.line)
    {
      tests.log.push(message.line);
      if (tests.log.length > LOG_LIMIT) tests.log.splice(0, tests.log.length - LOG_LIMIT);
      if (!view.hidden) renderLog();
    }
    else if (message.type === "state" && message.state)
    {
      if (message.state.run_id && message.state.run_id !== tests.status?.run_id)
      {
        tests.log = [];
        renderLog();
        loadStatus().catch(() => {});
        return;
      }
      applyStatus(message.state);
    }
    else if (message.type === "case" && message.case && tests.status?.cases)
    {
      const item = tests.status.cases.find((c) => c.id === message.case.id);
      if (item) item.verdict = message.case.verdict;
      renderCases();
    }
  }

  // ------------------------------------------------------------ actions

  async function startRun()
  {
    const script = tests.script;
    const cases = script.cases.filter((c) => tests.selected.has(c.id)).map((c) => c.id);
    const device = tests.devices.find((d) => d.id === $t("tests-device").value);
    if (!device || !cases.length) return;
    const knob = script.cases.filter((c) => tests.selected.has(c.id) && c.flags?.operator).length;
    const manual = $t("tests-opt-manual").checked;
    const text = `在 ${device.name || device.model || device.eui64} 上运行 ${cases.length} 个用例？\n\n`
      + (knob ? `其中 ${knob} 个用例需要现场操作旋钮。\n` : "")
      + (manual ? "灯光变化需要人工确认。\n" : "已关闭人工确认，只检查上报属性。\n")
      + "测试期间该设备被测试独占，其他设备的自动发现会暂停。";
    if (!window.confirm(text)) return;
    tests.log = [];
    renderLog();
    const status = await api("/api/tests/run", {
      method: "POST",
      body: JSON.stringify({
        script: script.id, device: device.id, endpoint: Number($t("tests-endpoint").value || script.endpoint || 1), cases,
        setup: $t("tests-opt-setup").checked, manual: manual ? "ask" : "skip", stop_on_fail: $t("tests-opt-stop").checked,
      }),
    });
    applyStatus(status, { replaceLog: true });
    toast("测试已开始");
  }

  async function abortRun()
  {
    if (!window.confirm("中止当前测试？正在发送的命令会先完成。")) return;
    applyStatus(await api("/api/tests/abort", { method: "POST", body: "{}" }));
  }

  async function respond(action)
  {
    const prompt = tests.status?.prompt;
    if (!prompt) return;
    const comment = $t("tests-prompt-comment")?.value || "";
    $t("tests-prompt").querySelectorAll("button").forEach((button) => { button.disabled = true; });
    try
    {
      await api("/api/tests/respond", { method: "POST", body: JSON.stringify({ action, comment, prompt_id: prompt.id }) });
    }
    catch (err)
    {
      tests.promptId = null;
      toast(err.message);
      await loadStatus();
    }
  }

  async function uploadScript(file)
  {
    const content = await file.text();
    const send = async (overwrite) =>
    {
      const response = await fetch(appUrl("/api/tests/scripts"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content, overwrite }),
      });
      const data = await response.json().catch(() => ({}));
      return { response, data };
    };
    let { response, data } = await send(false);
    if (response.status === 409 && !String(data.error || "").includes("内置") && window.confirm(`${data.error}\n\n覆盖已上传的同名脚本？`))
    {
      ({ response, data } = await send(true));
    }
    if (!response.ok)
    {
      throw new Error(data.error || `${response.status} ${response.statusText}`);
    }
    tests.scriptId = data.script.id;
    tests.script = null;
    await loadScripts();
    toast(`脚本 ${data.script.title} 已上传（${data.script.cases} 个用例）`);
  }

  async function deleteScript()
  {
    const script = tests.script;
    if (!script || script.builtin || !window.confirm(`删除已上传的脚本 ${script.title || script.id}？`)) return;
    await api("/api/tests/scripts/" + encodeURIComponent(script.id), { method: "DELETE" });
    tests.scriptId = null;
    tests.script = null;
    await loadScripts();
    if (!tests.script) renderCases();
    toast("脚本已删除");
  }

  async function deleteResult(id)
  {
    if (!window.confirm("删除这条测试结果？")) return;
    await api("/api/tests/results/" + encodeURIComponent(id), { method: "DELETE" });
    await loadResults();
  }

  function selectCases(predicate)
  {
    if (!tests.script) return;
    tests.selected = new Set(tests.script.cases.filter(predicate).map((c) => c.id));
    renderCases();
  }

  // ------------------------------------------------------------ wiring

  $t("tests-script-list").addEventListener("click", (event) =>
  {
    const button = event.target.closest("[data-script]");
    if (button) selectScript(button.dataset.script).catch((err) => toast(err.message));
  });
  $t("tests-case-list").addEventListener("change", (event) =>
  {
    const box = event.target.closest("[data-case-check]");
    if (!box) return;
    if (box.checked) tests.selected.add(box.dataset.caseCheck);
    else tests.selected.delete(box.dataset.caseCheck);
    updateSelectionCount();
  });
  $t("tests-prompt").addEventListener("click", (event) =>
  {
    const button = event.target.closest("[data-respond]");
    if (button) respond(button.dataset.respond).catch((err) => toast(err.message));
  });
  $t("tests-result-list").addEventListener("click", (event) =>
  {
    const button = event.target.closest("[data-delete-result]");
    if (button) deleteResult(button.dataset.deleteResult).catch((err) => toast(err.message));
  });
  $t("tests-case-filter").addEventListener("input", renderCases);
  $t("tests-select-all").addEventListener("click", () => selectCases(() => true));
  $t("tests-select-none").addEventListener("click", () => selectCases(() => false));
  $t("tests-select-auto").addEventListener("click", () => selectCases((c) => !c.flags?.operator));
  $t("tests-device").addEventListener("change", renderEndpoints);
  $t("tests-device").addEventListener("focus", () => loadDevices().catch(() => {}));
  $t("tests-start").addEventListener("click", () => startRun().catch((err) => toast(err.message)));
  $t("tests-abort").addEventListener("click", () => abortRun().catch((err) => toast(err.message)));
  $t("tests-results-refresh").addEventListener("click", () => loadResults().catch((err) => toast(err.message)));
  $t("tests-upload").addEventListener("click", () => $t("tests-upload-input").click());
  $t("tests-upload-input").addEventListener("change", (event) =>
  {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) uploadScript(file).catch((err) => toast(err.message));
  });
  document.addEventListener("z3-test-event", onTestEvent);
  document.addEventListener("z3-test-resync", () =>
  {
    if (visible()) loadStatus().catch(() => {});
  });
  $t("tab-tests").addEventListener("click", () => setTimeout(refreshAll, 0));
  // SSE carries progress; this slow poll only repairs a missed event while visible.
  setInterval(() =>
  {
    if (visible() && tests.status?.active) loadStatus().catch(() => {});
  }, 5000);
  renderStatus();
})();
