// Exam to HTML — M2 UI 前端 (Workbench 暖橙版)
// 单文件 ~180 行, 无外部依赖
// 契约：DOM id 与 /api/* 端点必须与后端 (backend/server.py) 对齐

(function () {
  "use strict";

  // ============================================================
  // DOM refs (与 index.html 同步)
  // ============================================================
  const $ = (id) => document.getElementById(id);

  const dz = $("dropzone");
  const picker = $("filepicker");
  const pickLink = $("pick-link");
  const filePill = $("file-pill");
  const pickedName = $("picked-name");
  const pickedSize = $("picked-size");
  const startBtn = $("start");

  const tokenInput = $("token");
  const saveTokenBtn = $("save-token");
  const outputDirEl = $("output-dir");

  const statusEl = $("status");
  const resultEl = $("result");
  const resultPath = $("result-path");
  const openBtn = $("open-btn");
  const revealBtn = $("reveal-btn");

  const statusDot = $("status-dot");
  const progressPct = $("progress-pct");
  const progressBar = $("progress-bar");
  const progressTask = $("progress-task");
  const progressEta = $("progress-eta");
  const logsEl = $("logs");

  const checkUpdateBtn = $("check-update");   // optional, may be missing
  const updateStatusEl = $("update-status");

  const incompleteBanner = $("incomplete-banner");
  const incompleteDetail = $("incomplete-detail");
  const dismissBannerBtn = $("dismiss-banner");
  const clearIncompleteBtn = $("clear-incomplete");

  const stepDrop = $("step-drop");
  const stepOutput = $("step-output");
  const stepAdvanced = $("step-advanced");

  // ============================================================
  // State
  // ============================================================
  let currentFile = null;
  let currentJobId = null;
  let pollTimer = null;
  let startedAt = 0;

  // ============================================================
  // 拖拽 / 选择文件
  // ============================================================
  function fmtSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function setFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      showStatus("请选择 .pdf 文件", true);
      return;
    }
    currentFile = file;
    pickedName.textContent = file.name;
    pickedSize.textContent = fmtSize(file.size);
    filePill.classList.remove("hidden");
    dz.classList.add("has-file");
    startBtn.disabled = false;
    stepDrop.classList.add("done");
    stepOutput.classList.add("active");
    hideResult();
    hideStatus();
    setStatusDot("等待点击", "");
    appendLog(`已选择：${file.name}（${fmtSize(file.size)}）`, "now");
  }

  function clearFile() {
    currentFile = null;
    filePill.classList.add("hidden");
    dz.classList.remove("has-file");
    startBtn.disabled = true;
    picker.value = "";
    setStatusDot("等待中", "");
  }

  // 让 dropzone 内点 "点击选择文件" 链接时不触发 label 重复触发
  dz.addEventListener("click", (e) => {
    if (e.target.tagName === "A") return;
    picker.click();
  });
  pickLink.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    picker.click();
  });
  // label 默认就会打开 picker, 这里不再重复
  picker.addEventListener("change", (e) => {
    if (e.target.files && e.target.files[0]) setFile(e.target.files[0]);
  });

  ["dragenter", "dragover"].forEach((evt) =>
    dz.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.add("dragging");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dz.addEventListener(evt, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.remove("dragging");
    })
  );
  dz.addEventListener("drop", (e) => {
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files[0]) setFile(files[0]);
  });

  // ============================================================
  // 状态显示 (状态条 + 日志流)
  // ============================================================
  function setStatusDot(text, kind) {
    if (!statusDot) return;
    statusDot.textContent = text;
    statusDot.className = "status-dot";
    if (kind) statusDot.classList.add(kind);
  }

  function appendLog(text, kind) {
    if (!logsEl) return;
    // 清掉占位 "等待文件..."
    const placeholder = logsEl.querySelector(".log-line.muted");
    if (placeholder) placeholder.remove();

    const line = document.createElement("div");
    line.className = "log-line" + (kind ? " " + kind : "");
    line.textContent = text;
    logsEl.appendChild(line);
    // 自动滚到底
    logsEl.scrollTop = logsEl.scrollHeight;
  }

  function showStatus(msg, isError) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.classList.remove("hidden", "error", "success");
    if (isError) statusEl.classList.add("error");
  }
  function hideStatus() {
    if (!statusEl) return;
    statusEl.classList.add("hidden");
  }

  function setProgress(pct, taskText, etaText) {
    if (progressBar) progressBar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    if (progressPct) progressPct.textContent = pct > 0 ? `${pct}% · ${taskText || "进行中"}` : (taskText || "待开始");
    if (progressTask) progressTask.textContent = `当前任务：${taskText || "—"}`;
    if (progressEta) progressEta.textContent = etaText || "";
  }

  function showResult(htmlPath) {
    resultPath.textContent = htmlPath;
    resultEl.classList.remove("hidden");
  }
  function hideResult() {
    resultEl.classList.add("hidden");
  }

  // M3-3 错误码 → UI
  const ERROR_ACTIONS = {
    retry:        { label: null, action: null },
    retry_button: { label: "🔄 重试", action: "retry" },
    open_settings:{ label: "⚙️ 打开高级设置", action: "open_settings" },
    change_output:{ label: "📁 改输出位置", action: "change_output" },
    free_space:   { label: "💾 释放磁盘后重试", action: null },
  };
  function showJobError(job) {
    const code = job.error_code || "UNKNOWN";
    const recovery = job.error_recovery || "retry_button";
    const msg = job.error || "未知错误";
    setStatusDot("失败", "error");
    setProgress(0, "失败", "");
    appendLog(`[${code}] ${msg}`, "err");
    statusEl.classList.remove("hidden", "error", "success");
    statusEl.classList.add("error");
    const act = ERROR_ACTIONS[recovery] || ERROR_ACTIONS.retry_button;
    if (act.label) {
      statusEl.innerHTML = "";
      const span = document.createElement("span");
      span.textContent = `❌ ${msg} `;
      statusEl.appendChild(span);
      const btn = document.createElement("button");
      btn.className = "btn-link";
      btn.textContent = act.label;
      btn.addEventListener("click", () => {
        if (act.action === "retry") clearFile();
        else if (act.action === "open_settings") {
          const adv = document.getElementById("advanced");
          if (adv) { adv.open = true; adv.scrollIntoView({ behavior: "smooth" }); }
        } else if (act.action === "change_output") {
          alert("请在高级设置或下次拖文件时指定其他输出位置");
        }
      });
      statusEl.appendChild(btn);
    } else {
      statusEl.textContent = `❌ ${msg}`;
    }
  }

  // ============================================================
  // 提交转换
  // ============================================================
  startBtn.addEventListener("click", async () => {
    if (!currentFile) return;
    if (!currentFile.name.toLowerCase().endsWith(".pdf")) {
      showStatus("❌ 请拖入 PDF 文件（.pdf 后缀）", true);
      return;
    }
    if (currentFile.size > 100 * 1024 * 1024) {
      showStatus(`❌ 文件过大（${(currentFile.size / 1024 / 1024).toFixed(1)}MB > 100MB），请压缩或拆分`, true);
      return;
    }

    startBtn.disabled = true;
    hideResult();
    showStatus("正在解析 PDF, 请稍候...");
    setStatusDot("处理中", "processing");
    setProgress(8, "正在上传 PDF");
    startedAt = Date.now();
    appendLog("正在上传 PDF...", "now");

    const form = new FormData();
    form.append("file", currentFile);

    try {
      const res = await fetch("/api/convert", { method: "POST", body: form });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      currentJobId = data.job_id;
      appendLog(`job_id: ${data.job_id}`, "muted");
      setProgress(18, "已入队, 等待处理");
      pollJob();
    } catch (e) {
      showStatus(`❌ ${e.message}`, true);
      appendLog(`上传失败：${e.message}`, "err");
      setStatusDot("失败", "error");
      startBtn.disabled = false;
    }
  });

  function pollJob() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (!currentJobId) return;
      try {
        const res = await fetch(`/api/jobs/${currentJobId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const job = await res.json();

        if (job.status === "done") {
          const stats = job.stats || {};
          const drafts = stats.drafts ?? 0;
          const inTopic = stats.questions_in_topic ?? 0;
          const dur = ((stats.duration_ms || (Date.now() - startedAt)) / 1000).toFixed(1);
          setProgress(100, `完成 · ${drafts} 题解析 · ${inTopic} 入册 · ${dur}s`);
          setStatusDot("完成", "done");
          showStatus(`✅ 完成！${drafts} 题解析，${inTopic} 题入册（${dur}s）`);
          statusEl.classList.remove("error");
          statusEl.classList.add("success");
          appendLog(`✅ 完成 → ${job.html_path}`, "ok");
          showResult(job.html_path);
          startBtn.disabled = false;
          currentJobId = null;
        } else if (job.status === "failed") {
          showJobError(job);
          startBtn.disabled = false;
          currentJobId = null;
        } else {
          // queued / processing → 继续轮询, 推个估算进度
          const elapsed = (Date.now() - startedAt) / 1000;
          const estimate = Math.min(85, 18 + elapsed * 6);
          setProgress(Math.round(estimate), "正在解析");
          pollJob();
        }
      } catch (e) {
        appendLog(`查询失败：${e.message}`, "err");
        showStatus(`查询失败: ${e.message}`, true);
        setStatusDot("失败", "error");
        startBtn.disabled = false;
        currentJobId = null;
      }
    }, 800);
  }

  // ============================================================
  // 打开 HTML
  // ============================================================
  async function openHtml(path) {
    try {
      const res = await fetch("/api/open-html", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
    } catch (e) {
      showStatus(`打开失败: ${e.message}`, true);
    }
  }

  if (openBtn) openBtn.addEventListener("click", () => {
    const path = resultPath && resultPath.textContent;
    if (path) openHtml(path);
  });
  if (revealBtn) revealBtn.addEventListener("click", () => {
    const path = resultPath && resultPath.textContent;
    if (!path) {
      showStatus("暂未生成 HTML", true);
      return;
    }
    // 没专门 reveal 端点, 先复用 open-html (Win 上 start "" 会打开默认应用)
    openHtml(path);
  });

  // ============================================================
  // Token 保存
  // ============================================================
  if (saveTokenBtn) {
    saveTokenBtn.addEventListener("click", async () => {
      const token = tokenInput.value.trim();
      try {
        const res = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mineru_token: token || null }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        showStatus(token ? "✅ Token 已保存" : "✅ Token 已清空");
        statusEl.classList.remove("error");
        statusEl.classList.add("success");
      } catch (e) {
        showStatus(`保存失败: ${e.message}`, true);
      }
    });
  }

  // ============================================================
  // 初始化：拉 config + 检查更新
  // ============================================================
  (async function init() {
    try {
      const res = await fetch("/api/config");
      if (res.ok) {
        const cfg = await res.json();
        if (cfg.output_dir) outputDirEl.value = cfg.output_dir;
        if (cfg.mineru_token) tokenInput.value = cfg.mineru_token;
      }
    } catch (e) {
      console.warn("config load failed:", e);
    }
    if (typeof checkUpdate === "function") checkUpdate(false);
  })();

  // ============================================================
  // 自动更新 (设计文档 §7)
  // ============================================================
  function renderUpdateStatus(data, isManual) {
    if (!updateStatusEl) return;
    updateStatusEl.className = "update-status";
    if (!data) { updateStatusEl.textContent = ""; return; }
    if (data.status === "update_available") {
      const url = data.download_url;
      updateStatusEl.classList.add("has-update");
      updateStatusEl.innerHTML = `🆕 v${data.latest_version} 可用 ` +
        (url ? `<a href="${url}" target="_blank">前往下载</a>` : "(无下载链接)");
      if (checkUpdateBtn) checkUpdateBtn.textContent = "🆕 前往下载 v" + data.latest_version;
      const adv = document.getElementById("advanced");
      if (adv && !adv.open) adv.open = true;
    } else if (data.status === "up_to_date") {
      updateStatusEl.textContent = `✅ 已是最新 v${data.current_version}`;
      if (checkUpdateBtn) checkUpdateBtn.textContent = "📦 检查更新";
    } else if (data.status === "throttled") {
      if (isManual) updateStatusEl.textContent = `⏱ 24h 内已检查过 (当前 v${data.current_version})`;
      else updateStatusEl.textContent = "";
      if (checkUpdateBtn) checkUpdateBtn.textContent = "📦 检查更新";
    } else if (data.status === "check_failed") {
      updateStatusEl.classList.add("check-failed");
      updateStatusEl.textContent = `❌ 检查失败: ${data.error || "未知错误"}`;
      if (checkUpdateBtn) checkUpdateBtn.textContent = "📦 重试检查";
    } else {
      updateStatusEl.textContent = `❓ 未知状态: ${data.status}`;
    }
  }

  async function checkUpdate(force) {
    const url = force ? "/api/version/check" : "/api/version";
    if (force && updateStatusEl) {
      updateStatusEl.className = "update-status";
      updateStatusEl.textContent = "⏳ 检查中...";
      if (checkUpdateBtn) checkUpdateBtn.disabled = true;
    }
    try {
      const res = await fetch(url, force ? { method: "POST" } : undefined);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderUpdateStatus(data, force);
    } catch (e) {
      if (updateStatusEl) {
        updateStatusEl.classList.add("check-failed");
        updateStatusEl.textContent = `❌ 检查失败: ${e.message}`;
      }
    } finally {
      if (checkUpdateBtn) checkUpdateBtn.disabled = false;
    }
  }

  if (checkUpdateBtn) {
    checkUpdateBtn.addEventListener("click", () => {
      if (checkUpdateBtn.textContent.includes("前往下载")) {
        const a = updateStatusEl && updateStatusEl.querySelector("a");
        if (a) a.click();
      } else {
        checkUpdate(true);
      }
    });
  }

  // ============================================================
  // M3-2 启动恢复 banner
  // ============================================================
  async function checkIncompleteUploads() {
    try {
      const res = await fetch("/api/incomplete-uploads?within_hours=24");
      if (!res.ok) return;
      const data = await res.json();
      if (data.count > 0) {
        const names = data.uploads.slice(0, 3).map((u) => u.filename).join("、");
        const more = data.count > 3 ? ` 等 ${data.count} 份` : "";
        incompleteDetail.textContent = `${names}${more}未处理。请重新拖入。`;
        incompleteBanner.classList.remove("hidden");
        if (clearIncompleteBtn) clearIncompleteBtn.classList.remove("hidden");
        if (clearIncompleteBtn) {
          clearIncompleteBtn.onclick = async () => {
            const paths = data.uploads.map((u) => u.path);
            try {
              await fetch("/api/incomplete-uploads/clear", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ paths }),
              });
              incompleteBanner.classList.add("hidden");
            } catch (e) {
              console.warn("clear incomplete failed:", e);
            }
          };
        }
      }
    } catch (e) {
      console.warn("incomplete-uploads check failed:", e);
    }
  }

  if (dismissBannerBtn) {
    dismissBannerBtn.addEventListener("click", () => {
      incompleteBanner.classList.add("hidden");
    });
  }
  checkIncompleteUploads();
})();
