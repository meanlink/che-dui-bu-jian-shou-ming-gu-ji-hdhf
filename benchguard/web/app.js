/* 行业对标发布管控系统前端（原生 ES，无构建步骤） */
"use strict";

const state = { user: null };

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ---------- 标签页 ----------
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`#tab-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "public") loadPublic();
    if (tab.dataset.tab === "mine") loadMine();
  });
});

// ---------- 会话 ----------
function renderSession() {
  const u = state.user;
  $("#session-info").textContent = u
    ? `${u.display_name}（${u.role === "maintainer" ? "机务核验" : "申请单位"}）`
    : "未登录（公开页可匿名访问）";
  $("#nav-login").classList.toggle("hidden", !!u);
  $("#nav-logout").classList.toggle("hidden", !u);
  $$(".maintainer-only").forEach((el) =>
    el.classList.toggle("hidden", !(u && u.role === "maintainer"))
  );
}

$("#nav-login").addEventListener("click", () => $("#login-dialog").showModal());
$("#nav-logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  state.user = null;
  renderSession();
  loadMine();
});

$("#login-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const fd = new FormData($("#login-form"));
  const errBox = $("#login-error");
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: { username: fd.get("username"), password: fd.get("password") },
    });
    state.user = data.user;
    renderSession();
    errBox.classList.add("hidden");
    $("#login-dialog").close();
    $("#login-form").reset();
  } catch (err) {
    errBox.textContent = err.message;
    errBox.classList.remove("hidden");
  }
});

// ---------- 公开页 ----------
async function loadPublic() {
  const data = await api("/api/public");
  $("#public-rows").innerHTML = data.listed_releases.length
    ? data.listed_releases
        .map(
          (r) =>
            `<tr><td>${r.code}</td><td>${r.sample_size}</td><td>${r.failure_count}</td><td>${r.created_at}</td></tr>`
        )
        .join("")
    : '<tr><td colspan="4" class="muted">暂无在架发布</td></tr>';
  $("#withdrawn-rows").innerHTML = data.withdrawn_releases.length
    ? data.withdrawn_releases
        .map((r) => `<li>${r.code} · 撤下时间 ${r.withdrawn_at} · 数字已撤，记录仍参与核对</li>`)
        .join("")
    : '<li class="muted">无撤下发布</li>';
}

// ---------- 申请 ----------
function addItemRow(partId = "", failed = 0) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td><input class="part-id" value="${partId}" inputmode="numeric" /></td>
    <td><input class="failed" value="${failed}" inputmode="numeric" maxlength="1" /></td>
    <td><button type="button" class="btn btn-danger btn-remove">删除</button></td>`;
  tr.querySelector(".btn-remove").addEventListener("click", () => tr.remove());
  $("#item-rows").appendChild(tr);
}
$("#add-row").addEventListener("click", () => addItemRow());

$("#fill-serial").addEventListener("click", async () => {
  // 通过机务/申请身份拉名单辅助录入；非机务被服务端拒绝时提示手填 ID
  try {
    const data = await api("/api/parts");
    const wanted = prompt(
      `输入资产号（逗号分隔）。可选资产号：\n${data.parts
        .map((p) => `${p.id}:${p.serial}`)
        .join(", ")}`
    );
    if (!wanted) return;
    const serials = wanted.split(/[，,\s]+/).filter(Boolean);
    const map = new Map(data.parts.map((p) => [p.serial, p.id]));
    for (const s of serials) {
      const id = map.get(s.trim());
      if (id) addItemRow(id, 0);
      else alert(`资产号 ${s} 不在冻结批次内，已跳过`);
    }
  } catch (err) {
    alert(`无法拉取名单（${err.message}），请直接填写部件 ID`);
  }
});

$("#apply-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const items = $$("#item-rows tr")
    .map((tr) => ({
      part_id: Number(tr.querySelector(".part-id").value),
      failed: Number(tr.querySelector(".failed").value),
    }))
    .filter((it) => Number.isInteger(it.part_id) && it.part_id > 0);
  const out = $("#apply-result");
  try {
    const data = await api("/api/applications", {
      method: "POST",
      body: {
        title: fd.get("title"),
        idempotency_key: fd.get("idempotency_key"),
        items,
      },
    });
    out.className = "result";
    out.textContent = `已登记申请 #${data.application.id}，状态：${data.application.status}（裁决串行处理，请在"我的申请"刷新）`;
    out.classList.remove("hidden");
  } catch (err) {
    out.className = "result error";
    out.textContent = err.message;
    out.classList.remove("hidden");
  }
});

// ---------- 我的申请 ----------
async function loadMine() {
  const tbody = $("#mine-rows");
  if (!state.user) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">请先登录</td></tr>';
    $("#detail-box").classList.add("hidden");
    return;
  }
  const data = await api("/api/applications");
  $("#mine-hint").textContent = `共 ${data.applications.length} 份`;
  tbody.innerHTML = data.applications.length
    ? data.applications
        .map((a) => {
          const code = a.status === "approved" ? "见详情" : "—";
          return `<tr>
            <td>${a.id}</td><td>${escapeHtml(a.title)}</td>
            <td><span class="status status-${a.status}">${a.status}</span></td>
            <td>${code}</td><td>${a.created_at}</td>
            <td>
              <button class="btn btn-ghost" data-detail="${a.id}">详情/拒发原因</button>
            </td></tr>`;
        })
        .join("")
    : '<tr><td colspan="6" class="muted">暂无申请</td></tr>';
  $$("[data-detail]").forEach((btn) =>
    btn.addEventListener("click", () => loadDetail(btn.dataset.detail))
  );
}

$("#refresh-mine").addEventListener("click", loadMine);

async function loadDetail(id) {
  const box = $("#detail-box");
  box.classList.remove("hidden");
  const data = await api(`/api/applications/${id}`);
  const a = data.application;
  const rel = data.release;
  box.innerHTML = `
    <h3>申请 #${a.id}：${escapeHtml(a.title)}</h3>
    <p>状态：<span class="status status-${a.status}">${a.status}</span>
       ${rel ? `｜发布编码：${rel.code}｜发布状态：<span class="status status-${rel.status}">${rel.status}</span>` : ""}</p>
    <table class="grid">
      <thead><tr><th>部件 ID</th><th>资产号</th><th>型号</th><th>失效</th></tr></thead>
      <tbody>${data.items
        .map((i) => `<tr><td>${i.part_id}</td><td>${i.serial}</td><td>${i.model}</td><td>${i.failed}</td></tr>`)
        .join("")}</tbody>
    </table>
    ${
      a.status === "rejected"
        ? `<h4>拒发原因</h4><div class="reason-box">${escapeHtml(a.reject_reason)}</div>`
        : ""
    }
    ${
      rel && rel.status === "listed"
        ? `<div class="row"><button class="btn btn-danger" id="withdraw-btn" data-rel="${rel.id}">撤下该发布</button>
           <span class="muted small">撤下后公开页不再展示数字；入选记录保留并继续参与匿名核对。</span></div>`
        : ""
    }`;
  const wb = $("#withdraw-btn");
  if (wb)
    wb.addEventListener("click", async () => {
      if (!confirm("确认撤下该发布？撤下不可恢复。")) return;
      try {
        await api(`/api/releases/${wb.dataset.rel}/withdraw`, { method: "POST" });
        await loadDetail(id);
        loadPublic();
      } catch (err) {
        alert(err.message);
      }
    });
}

// ---------- 部件名单（机务） ----------
$("#load-roster").addEventListener("click", async () => {
  const tbody = $("#roster-rows");
  try {
    const data = await api("/api/parts");
    tbody.innerHTML = data.parts
      .map((p) => `<tr><td>${p.id}</td><td>${p.serial}</td><td>${p.model}</td></tr>`)
      .join("");
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="3" class="muted">${err.message}</td></tr>`;
  }
});

// ---------- 审计 ----------
$("#load-audit").addEventListener("click", async () => {
  const data = await api("/api/audit");
  $("#audit-rows").innerHTML = data.audit_logs
    .map((l) => `<tr><td>${l.ts}</td><td>${l.action}</td><td>${escapeHtml(l.detail)}</td></tr>`)
    .join("");
});

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---------- 启动 ----------
(async function boot() {
  addItemRow("", 0);
  addItemRow("", 0);
  try {
    const me = await api("/api/auth/me");
    state.user = me.user;
  } catch {
    state.user = null;
  }
  renderSession();
  loadPublic();
})();
