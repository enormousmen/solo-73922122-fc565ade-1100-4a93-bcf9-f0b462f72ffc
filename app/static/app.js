(() => {
  "use strict";

  const STATUS_LABELS = {
    pending: "等待",
    running: "进行中",
    succeeded: "已选中",
    failed: "失败",
    aborted: "已放弃",
    composing: "合成中",
    ready: "可下载",
  };

  const ATTEMPT_LABELS = {
    queued: "排队中",
    running: "转码中",
    cancelling: "取消中（仍占槽）",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
    superseded: "迟到产物已丢弃",
    skipped: "未启动",
  };

  const KIND_LABELS = { first: "首跑", backup: "补派" };

  const form = document.getElementById("create-form");
  const createButton = document.getElementById("create-button");
  const createStatus = document.getElementById("create-status");
  const batchesRoot = document.getElementById("batches");
  const slotUsedEl = document.getElementById("slots-used");
  const slotTotalEl = document.getElementById("slots-total");
  const template = document.getElementById("batch-template");

  let polling = null;
  let inFlight = false;

  function fmtSeconds(ms) {
    if (ms == null) return "—";
    return (ms / 1000).toFixed(1) + "s";
  }

  function fmtClock(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function statusLabel(batch) {
    if (batch.status === "ready") return `可下载`;
    if (batch.status === "failed") return "无成片";
    if (batch.status === "composing") return "合成中";
    return "运行中";
  }

  async function createBatch(event) {
    event.preventDefault();
    const chosen = form.querySelector('input[name="slow_clip"]:checked');
    if (!chosen) return;
    createButton.disabled = true;
    createStatus.classList.remove("error");
    createStatus.textContent = "正在启动…";
    try {
      const res = await fetch("/api/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slow_clip: chosen.value }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
      createStatus.textContent = `已启动批次 ${body.id.slice(0, 8)}`;
      await refresh();
    } catch (err) {
      createStatus.classList.add("error");
      createStatus.textContent = `启动失败：${err.message}`;
    } finally {
      createButton.disabled = false;
    }
  }

  function renderAttempt(attempt, selectedId) {
    const li = document.createElement("li");
    li.className = "attempt" + (attempt.id === selectedId ? " selected" : "");
    const left = document.createElement("span");
    left.className = "attempt-kind";
    left.innerHTML = `<span class="dot s-${attempt.status}"></span>${KIND_LABELS[attempt.kind] || attempt.kind}`;
    const mid = document.createElement("span");
    mid.className = "attempt-time";
    const timing = attempt.elapsed_ms == null ? "未开始" : fmtSeconds(attempt.elapsed_ms);
    mid.textContent = `${ATTEMPT_LABELS[attempt.status] || attempt.status} · ${timing} · ${fmtClock(attempt.started_at) || "—"}`;
    const right = document.createElement("span");
    right.className = "attempt-time";
    right.textContent = attempt.slot_held ? "占槽" : "";
    li.append(left, mid, right);
    if (attempt.error) {
      const err = document.createElement("span");
      err.className = "attempt-error";
      err.textContent = attempt.error;
      li.append(err);
    }
    if (attempt.cancel_reason) {
      const reason = document.createElement("span");
      reason.className = "attempt-error";
      reason.style.color = "var(--cancel)";
      reason.textContent = `取消原因：${attempt.cancel_reason}`;
      li.append(reason);
    }
    li.title = [
      attempt.id,
      attempt.started_at ? `开始 ${fmtClock(attempt.started_at)}` : null,
      attempt.finished_at ? `结束 ${fmtClock(attempt.finished_at)}` : null,
      attempt.exit_code != null ? `退出码 ${attempt.exit_code}` : null,
    ].filter(Boolean).join(" · ");
    return li;
  }

  function renderSegment(segment) {
    const card = document.createElement("div");
    card.className = "segment";
    const head = document.createElement("div");
    head.className = "segment-head";
    const name = document.createElement("span");
    name.className = "segment-name";
    name.textContent = `片段 ${segment.name}`;
    if (segment.slow_first) {
      const tag = document.createElement("span");
      tag.className = "slow-tag";
      tag.textContent = segment.backup_launched
        ? "模拟慢速 · 已补派"
        : segment.backup_eligible
          ? "模拟慢速 · 达到补派阈值"
          : "模拟慢速";
      name.append(tag);
    }
    const status = document.createElement("span");
    status.className = "segment-status";
    status.textContent = STATUS_LABELS[segment.status] || segment.status;
    head.append(name, status);

    const list = document.createElement("ul");
    list.className = "attempts";
    for (const attempt of segment.attempts) {
      list.append(renderAttempt(attempt, segment.selected_attempt_id));
    }
    card.append(head, list);
    return card;
  }

  function renderBatch(batch) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.batchId = batch.id;
    node.querySelector(".batch-id").textContent = batch.id.slice(0, 8);
    const selectedSegments = batch.segments.filter((s) => s.status === "succeeded").length;
    node.querySelector(".batch-meta").textContent =
      `慢速片段 ${batch.slow_clip} · ${selectedSegments}/${batch.segments.length} 段已选定 · 用时 ${fmtSeconds(batch.elapsed_ms)} · 槽 ${batch.slots_used}/${batch.slots_total}`;
    const badge = node.querySelector(".batch-status");
    badge.classList.add(batch.status);
    badge.textContent = statusLabel(batch);
    node.querySelector(".progress-bar").style.width = `${(selectedSegments / batch.segments.length) * 100}%`;

    const grid = node.querySelector(".segment-grid");
    for (const segment of batch.segments) grid.append(renderSegment(segment));

    const download = node.querySelector(".download");
    if (batch.download_ready && batch.download_url) {
      download.hidden = false;
      download.href = batch.download_url;
    }
    const error = node.querySelector(".batch-error");
    if (batch.error) {
      error.hidden = false;
      error.textContent = batch.error;
    }
    return node;
  }

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    try {
      const [batchesRes, configRes] = await Promise.all([
        fetch("/api/batches", { cache: "no-store" }),
        fetch("/api/config", { cache: "no-store" }),
      ]);
      if (!batchesRes.ok) throw new Error(`HTTP ${batchesRes.status}`);
      const data = await batchesRes.json();
      const config = configRes.ok ? await configRes.json() : null;
      if (config) {
        slotUsedEl.textContent = config.slots_used;
        slotTotalEl.textContent = config.slots_total;
      }

      const batches = data.batches || [];
      if (batches.length === 0) {
        batchesRoot.innerHTML = '<p class="empty">还没有批次。选择一个片段变慢，然后启动。</p>';
        return;
      }
      const fragment = document.createDocumentFragment();
      for (const batch of batches) fragment.append(renderBatch(batch));
      batchesRoot.replaceChildren(fragment);
    } catch (err) {
      // Keep the last rendered state; surface the issue quietly.
      console.warn("刷新失败", err);
    } finally {
      inFlight = false;
    }
  }

  form.addEventListener("submit", createBatch);

  refresh();
  polling = setInterval(refresh, 400);
  window.addEventListener("beforeunload", () => clearInterval(polling));
})();
