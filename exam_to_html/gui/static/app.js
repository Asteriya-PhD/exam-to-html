// Exam to HTML — 前端交互
// 单文件 ~150 行, 无外部依赖

(function () {
  "use strict";

  // ============================================================
  // DOM refs
  // ============================================================
  const $ = (id) => document.getElementById(id);
  const dz = $("dropzone");
  const picker = $("filepicker");
  const pickLink = $("pick-link");
  const pickedName = $("picked-name");
  const startBtn = $("start");
  const statusEl = $("status");
  const resultEl = $("result");
  const resultPath = $("result-path");
  const openBtn = $("open-btn");
  const tokenInput = $("token");
  const saveTokenBtn = $("save-token");
  const outputDirEl = $("output-dir");
  const checkUpdateBtn = $("check-update");
  const updateStatusEl = $("update-status");
  const incompleteBanner = $("incomplete-banner");
  const incompleteDetail = $("incomplete-detail");
  const dismissBannerBtn = $("dismiss-banner");

  // ============================================================
  // State
  // ============================================================
  let currentFile = null;  // File 对象
  let currentJobId = null;  // 后端返回的 job_id
  let pollTimer = null;

  // ============================================================
  // 拖拽 / 选择文件
  // ============================================================
  function setFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      showStatus("请选择 .pdf 文件", true);
      return;
    }
    currentFile = file;
    pickedName.textContent = `已选择: ${file.name}`;
    dz.classList.add("has-file");
    startBtn.disabled = false;
    hideResult();
    hideStatus();
  }

  function clearFile() {
    currentFile = null;
    pickedName.textContent = "";
    dz.classList.remove("has-file");
    startBtn.disabled = true;
    picker.value = "";  // 允许重选同一文件
  }

  dz.addEventListener("click", (e) => {
    if (e.target.tagName === "A") return;  // 让 "点击选择文件" 链接自己处理
    picker.click();
  });

  pickLink.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    picker.click();
  });

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
  // 状态显示
  // ============================================================
  function showStatus(msg, isError) {
    statusEl.textContent = msg;
    statusEl.classList.remove("hidden");
    statusEl.classList.toggle("error", !!isError);
  }

  function hideStatus() {
    statusEl.classList.add("hidden");
  }

  // M3-3: 错误码 → UI (recovery hint 决定按钮)
  const ERROR_ACTIONS = {
    retry:        { label: null, action: null },  // 重新拖即可, 不加按钮
    retry_button: { label: "🔄 重试", action: "retry" },
    open_settings:{ label: "⚙️ 打开高级设置", action: "open_settings" },
    change_output:{ label: "📁 改输出位置", action: "change_output" },
    free_space:   { label: "💾 释放磁盘后重试", action: null },
  };

  function showJobError(job) {
    // job.error_code / job.error_recovery 来自 server.py PipelineError.to_dict()
    const code = job.error_code || "UNKNOWN";
    const recovery = job.error_recovery || "retry_button";
    const msg = job.error || "未知错误";

    statusEl.classList.remove("hidden");
    statusEl.classList.add("error");

    // 显示主消息 + 可选恢复按钮
    const act = ERROR_ACTIONS[recovery] || ERROR_ACTIONS.retry_button;
    if (act.label) {
      statusEl.innerHTML = "";  // clear text
      const span = document.createElement("span");
      span.textContent = `❌ ${msg} `;
      statusEl.appendChild(span);
      const btn = document.createElement("button");
      btn.className = "btn-link";
      btn.textContent = act.label;
      btn.addEventListener("click", () => {
        if (act.action === "retry") {
          // 重新拖
          clearFile();
        } else if (act.action === "open_settings") {
          const adv = document.getElementById("advanced");
          if (adv) { adv.open = true; adv.scrollIntoView({ behavior: "smooth" }); }
        } else if (act.action === "change_output") {
          // 提示用户改输出位置 (目前桌面是默认, 教师可手动改 config.json)
          alert("请在高级设置或下次拖文件时指定其他输出位置");
        }
      });
      statusEl.appendChild(btn);
    } else {
      // 无按钮, 只显示消息 (教师重新拖)
      statusEl.textContent = `❌ ${msg}`;
    }
  }

  function showResult(htmlPath) {
    resultPath.textContent = htmlPath;
    resultEl.classList.remove("hidden");
  }

  function hideResult() {
    resultEl.classList.add("hidden");
  }

  // ============================================================
  // 提交转换
  // ============================================================
  startBtn.addEventListener("click", async () => {
    if (!currentFile) return;

    startBtn.disabled = true;
    hideResult();
    showStatus("正在解析 PDF, 请稍候...");

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
      pollJob();
    } catch (e) {
      // 前端预校验失败 (非 PDF / > 100MB / etc) — 直接给友好提示
      showStatus(`❌ ${e.message}`, true);
      startBtn.disabled = false;
    }
  });

  // 上传前的预校验 (后端也会做, 但前端更早更省一轮)
  startBtn.addEventListener("click", () => {
    if (!currentFile) return;
    // 前端基础校验
    if (!currentFile.name.toLowerCase().endsWith(".pdf")) {
      showStatus("❌ 请拖入 PDF 文件（.pdf 后缀）", true);
      return;
    }
    if (currentFile.size > 100 * 1024 * 1024) {
      showStatus(`❌ 文件过大（${(currentFile.size / 1024 / 1024).toFixed(1)}MB > 100MB），请压缩或拆分`, true);
      return;
    }
    uploadFile();
  });

  function uploadFile() {
    startBtn.disabled = true;
    hideResult();
    showStatus("正在解析 PDF, 请稍候...");

    const form = new FormData();
    form.append("file", currentFile);

    fetch("/api/convert", { method: "POST", body: form })
      .then((res) => {
        if (!res.ok) {
          return res.json().then((err) => {
            throw new Error(err.detail || `HTTP ${res.status}`);
          });
        }
        return res.json();
      })
      .then((data) => {
        currentJobId = data.job_id;
        pollJob();
      })
      .catch((e) => {
        showStatus(`❌ ${e.message}`, true);
        startBtn.disabled = false;
      });
  }

  function pollJob() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (!currentJobId) return;
      try {
        const res = await fetch(`/api/jobs/${currentJobId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const job = await res.json();

        if (job.status === "done") {
          showStatus(`✅ 完成! ${job.stats.drafts} 题解析, ${job.stats.questions_in_topic} 题入册 (${(job.stats.duration_ms / 1000).toFixed(1)}s)`);
          showResult(job.html_path);
          startBtn.disabled = false;
          currentJobId = null;
        } else if (job.status === "failed") {
          showJobError(job);  // M3-3: 按 error_code / error_recovery 渲染
          startBtn.disabled = false;
          currentJobId = null;
        } else {
          // queued / processing → 继续轮询
          pollJob();
        }
      } catch (e) {
        showStatus(`查询失败: ${e.message}`, true);
        startBtn.disabled = false;
        currentJobId = null;
      }
    }, 800);
  }

  // ============================================================
  // 打开 HTML
  // ============================================================
  openBtn.addEventListener("click", async () => {
    const path = resultPath.textContent;
    if (!path) return;
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
  });

  // ============================================================
  // Token 保存 (高级设置)
  // ============================================================
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
    } catch (e) {
      showStatus(`保存失败: ${e.message}`, true);
    }
  });

  // ============================================================
  // 初始化: 拉 config 显示输出位置 + token
  // ============================================================
  (async function init() {
    try {
      const res = await fetch("/api/config");
      if (res.ok) {
        const cfg = await res.json();
        if (cfg.output_dir) {
          outputDirEl.textContent = cfg.output_dir;
        }
        if (cfg.mineru_token) {
          tokenInput.value = cfg.mineru_token;
        }
      }
    } catch (e) {
      console.warn("config load failed:", e);
    }
    // 自动检查更新 (节流: 后端默认 24h 内不重 fetch)
    checkUpdate(false);
  })();

  // ============================================================
  // 自动更新 (设计文档 §7)
  // ============================================================
  function renderUpdateStatus(data, isManual) {
    updateStatusEl.className = "update-status";  // reset
    if (!data) {
      updateStatusEl.textContent = "";
      return;
    }

    if (data.status === "update_available") {
      const url = data.download_url;
      updateStatusEl.classList.add("has-update");
      updateStatusEl.innerHTML =
        `🆕 v${data.latest_version} 可用 ` +
        (url ? `<a href="${url}" target="_blank">前往下载</a>` : "(无下载链接)");
      checkUpdateBtn.textContent = "🆕 前往下载 v" + data.latest_version;
      // 有新版本 → 强制打开高级设置 (展开 details)
      const adv = document.getElementById("advanced");
      if (adv && !adv.open) adv.open = true;
    } else if (data.status === "up_to_date") {
      updateStatusEl.textContent = `✅ 已是最新 v${data.current_version}`;
      checkUpdateBtn.textContent = "📦 检查更新";
    } else if (data.status === "throttled") {
      if (isManual) {
        updateStatusEl.textContent = `⏱ 24h 内已检查过 (当前 v${data.current_version})`;
      } else {
        // 启动时自动检查节流: 不打扰用户
        updateStatusEl.textContent = "";
      }
      checkUpdateBtn.textContent = "📦 检查更新";
    } else if (data.status === "check_failed") {
      updateStatusEl.classList.add("check-failed");
      updateStatusEl.textContent = `❌ 检查失败: ${data.error || "未知错误"}`;
      checkUpdateBtn.textContent = "📦 重试检查";
    } else {
      updateStatusEl.textContent = `❓ 未知状态: ${data.status}`;
    }
  }

  async function checkUpdate(force) {
    const url = force ? "/api/version/check" : "/api/version";
    if (force) {
      updateStatusEl.className = "update-status";
      updateStatusEl.textContent = "⏳ 检查中...";
      checkUpdateBtn.disabled = true;
    }
    try {
      const res = await fetch(url, force ? { method: "POST" } : undefined);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderUpdateStatus(data, force);
    } catch (e) {
      updateStatusEl.classList.add("check-failed");
      updateStatusEl.textContent = `❌ 检查失败: ${e.message}`;
    } finally {
      checkUpdateBtn.disabled = false;
    }
  }

  // 点 "前往下载" 按钮 (有新版本时按钮文本会变) → 强制检查
  checkUpdateBtn.addEventListener("click", () => {
    if (checkUpdateBtn.textContent.includes("前往下载")) {
      // 已经有新版本 → 直接打开下载 URL
      const a = updateStatusEl.querySelector("a");
      if (a) a.click();
    } else {
      checkUpdate(true);
    }
  });

  // ============================================================
  // M3-2 启动恢复: 显示 "上次有未完成" banner
  // ============================================================
  async function checkIncompleteUploads() {
    try {
      const res = await fetch("/api/incomplete-uploads?within_hours=24");
      if (!res.ok) return;
      const data = await res.json();
      if (data.count > 0) {
        const names = data.uploads
          .slice(0, 3)
          .map((u) => u.filename)
          .join("、");
        const more = data.count > 3 ? ` 等 ${data.count} 份` : "";
        incompleteDetail.textContent = `${names}${more}未处理。请重新拖入。`;
        incompleteBanner.classList.remove("hidden");
      }
    } catch (e) {
      console.warn("incomplete-uploads check failed:", e);
    }
  }

  dismissBannerBtn.addEventListener("click", () => {
    incompleteBanner.classList.add("hidden");
  });

  // 启动时调
  checkIncompleteUploads();
})();