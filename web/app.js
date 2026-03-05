const consumerHeaders = () => {
  const key = byId("consumerKey").value.trim();
  return key ? { "x-api-key": key } : {};
};

const reviewerHeaders = () => {
  const key = byId("reviewerKey").value.trim();
  return key ? { "x-api-key": key } : {};
};

const api = (path, options = {}) => fetch(path, {
  headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  ...options,
}).then(async (res) => {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || JSON.stringify(body));
  return body;
});

const byId = (id) => document.getElementById(id);

const parseNum = (id) => {
  const value = byId(id).value;
  return value === "" ? null : Number(value);
};

async function refresh() {
  const [dashboard, metrics] = await Promise.all([
    api("/api/dashboard", { headers: consumerHeaders() }),
    api("/api/metrics", { headers: consumerHeaders() }),
  ]);

  byId("metricsBox").textContent = JSON.stringify(metrics, null, 2);
  byId("dashboardBox").textContent = JSON.stringify({
    users: dashboard.users,
    issues: dashboard.issues,
    branches: dashboard.branches,
    commits: dashboard.commits.slice(0, 5),
    prs: dashboard.prs.slice(0, 5),
    merges: dashboard.merges.slice(0, 5),
    event_counts: dashboard.event_counts,
  }, null, 2);
}

byId("seedBtn").onclick = async () => {
  await api("/api/seed", { method: "POST", headers: consumerHeaders() });
  await refresh();
};

byId("commitBtn").onclick = async () => {
  await api("/api/commits", {
    method: "POST",
    headers: consumerHeaders(),
    body: JSON.stringify({
      branch_id: parseNum("branchId"),
      user_id: parseNum("userId"),
      task_type: byId("taskType").value,
      evidence_text: byId("evidenceText").value,
      metric_value: parseNum("metricValue"),
      adherence_score: parseNum("adherence"),
    }),
  });
  await refresh();
};

byId("prBtn").onclick = async () => {
  const result = await api("/api/prs", {
    method: "POST",
    headers: consumerHeaders(),
    body: JSON.stringify({
      branch_id: parseNum("branchId"),
      requested_by: parseNum("userId"),
      summary: byId("prSummary").value,
      risk_level: byId("riskLevel").value,
    }),
  });
  byId("reviewPrId").value = result.id;
  await refresh();
};

byId("outcomeBtn").onclick = async () => {
  await api("/api/outcomes", {
    method: "POST",
    headers: consumerHeaders(),
    body: JSON.stringify({
      issue_id: parseNum("issueId"),
      metric_name: byId("metricName").value,
      metric_value: parseNum("outcomeValue"),
      note: byId("outcomeNote").value,
    }),
  });
  await refresh();
};

async function review(action) {
  await api(`/api/prs/${parseNum("reviewPrId")}/review`, {
    method: "POST",
    headers: reviewerHeaders(),
    body: JSON.stringify({
      reviewer_id: parseNum("reviewerId"),
      action,
      review_note: byId("reviewNote").value,
      force_override: byId("forceOverride").checked,
    }),
  });
  await refresh();
}

byId("approveBtn").onclick = () => review("approve");
byId("rejectBtn").onclick = () => review("reject");
byId("refreshBtn").onclick = refresh;
byId("eventsBtn").onclick = async () => {
  const result = await api("/api/events?limit=30", { headers: reviewerHeaders() });
  byId("eventsBox").textContent = JSON.stringify(result, null, 2);
};

refresh().catch((e) => {
  byId("dashboardBox").textContent = `请先点“初始化示例数据”\n${e.message}`;
});
