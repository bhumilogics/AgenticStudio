// AgenticStudio dashboard -- plain JS, no build step, no framework.
// Talks to the FastAPI backend (interface/api.py) purely over fetch().
// Run it with any static file server, e.g.:  python -m http.server 8080
// then open http://localhost:8080 (make sure the API's CORS allows this
// origin -- interface/api.py currently allows "*").

const DEFAULT_API_BASE = "http://localhost:8000";

function apiBase() {
  return localStorage.getItem("agenticstudio_api_base") || DEFAULT_API_BASE;
}

function setApiBase(url) {
  localStorage.setItem("agenticstudio_api_base", url.replace(/\/+$/, ""));
}

// ------------------------------------------------------------------ auth
//
// A JWT from POST /auth/login or /auth/register, stored so it survives a
// page reload. Every api() call attaches it as a Bearer token; a 401 means
// it's missing/expired/invalid, so we drop it and fall back to the login
// screen rather than showing a raw error box on every panel.

function getToken() {
  return localStorage.getItem("agenticstudio_token") || "";
}
function setToken(token) {
  localStorage.setItem("agenticstudio_token", token);
}
function clearToken() {
  localStorage.removeItem("agenticstudio_token");
  currentUser = null;
}

let currentUser = null; // {id, email, role} -- populated by loadCurrentUser()

function isAdmin() {
  return !!currentUser && currentUser.role === "admin";
}

async function api(path, opts) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers["Authorization"] = "Bearer " + token;

  const res = await fetch(apiBase() + path, { headers, ...opts });
  let body = null;
  try { body = await res.json(); } catch (e) { /* no body */ }
  if (res.status === 401) {
    clearToken();
    renderAuthGate();
    throw new Error("session expired -- please sign in again");
  }
  if (!res.ok) {
    const detail = body && body.detail ? body.detail : res.statusText;
    throw new Error(`${res.status} ${detail}`);
  }
  return body;
}

function h(tag, attrs, children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue; // omit null/undefined/false entirely (boolean attrs: disabled, selected, ...)
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of [].concat(children || [])) {
    if (c == null) continue;
    el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return el;
}

function fmtTime(ts) {
  if (ts == null) return "-";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

function badge(text, cls) {
  return h("span", { class: `badge ${cls || text}` }, text);
}

// -------------------------------------------------------------- auth UI

function renderLogin(main, mode) {
  main.innerHTML = "";
  main.appendChild(h("h1", {}, mode === "register" ? "Create an account" : "Sign in"));
  main.appendChild(h("div", { class: "subtitle" }, mode === "register"
    ? "The very first account ever created on this instance becomes an admin automatically."
    : "Sign in to submit work, watch runs, and (if you're an admin) manage agents, jobs and schedules."));

  const panel = h("div", { class: "panel" });
  const emailInput = h("input", { type: "text", placeholder: "you@example.com" });
  const passwordInput = h("input", { type: "password", placeholder: "••••••••" });
  const output = h("div");
  panel.appendChild(h("label", {}, "Email"));
  panel.appendChild(emailInput);
  panel.appendChild(h("label", {}, "Password"));
  panel.appendChild(passwordInput);
  panel.appendChild(h("div", { class: "row", style: "margin-top:10px" }, [
    h("button", { class: "btn", onclick: submit }, mode === "register" ? "Create account" : "Sign in"),
    h("button", { class: "btn secondary", onclick: () => renderLogin(main, mode === "register" ? "login" : "register") },
      mode === "register" ? "I already have an account" : "Create an account instead"),
  ]));
  panel.appendChild(output);
  main.appendChild(panel);

  async function submit() {
    output.innerHTML = "";
    const email = emailInput.value.trim();
    const password = passwordInput.value;
    if (!email || !password) { output.appendChild(errorBox(new Error("email and password are required"))); return; }
    try {
      const path = mode === "register" ? "/auth/register" : "/auth/login";
      const resp = await api(path, { method: "POST", body: JSON.stringify({ email, password }) });
      setToken(resp.token);
      currentUser = resp.user;
      renderApp();
    } catch (err) {
      output.innerHTML = "";
      output.appendChild(errorBox(err));
    }
  }
}

async function loadCurrentUser() {
  currentUser = await api("/auth/me");
  return currentUser;
}

function renderAuthGate() {
  const main = document.getElementById("main");
  document.getElementById("app").classList.add("logged-out");
  renderLogin(main, "login");
}

async function renderApp() {
  document.getElementById("app").classList.remove("logged-out");
  renderUserBadge();
  router();
}

function renderUserBadge() {
  const el = document.getElementById("user-badge");
  if (!el) return;
  const usersNav = document.getElementById("users-nav");
  if (usersNav) usersNav.hidden = !isAdmin();
  el.innerHTML = "";
  if (!currentUser) return;
  el.appendChild(h("div", { class: "muted" }, currentUser.email));
  el.appendChild(h("span", { class: `badge ${currentUser.role === "admin" ? "done" : "manual"}` }, currentUser.role));
  el.appendChild(h("button", { class: "btn secondary", style: "margin-top:8px;width:100%", onclick: () => {
    clearToken();
    renderAuthGate();
  } }, "Sign out"));
}

// ------------------------------------------------------------- router

const routes = {
  "/jobs": renderJobsList,
  "/agents": renderAgentsList,
  "/logs": renderLogExplorer,
  "/schedules": renderScheduler,
};

function router() {
  if (!getToken()) { renderAuthGate(); return; }
  const hash = window.location.hash.replace(/^#/, "") || "/jobs";
  const [path, ...rest] = hash.split("/").filter(Boolean);
  const main = document.getElementById("main");
  main.innerHTML = "";

  document.querySelectorAll(".sidebar nav a").forEach((a) => a.classList.remove("active"));
  const navLink = document.querySelector(`.sidebar nav a[data-nav="${path}"]`);
  if (navLink) navLink.classList.add("active");

  if (path === "jobs" && rest[0] === "run" && rest[1]) return renderRunDetail(main, rest[1]);
  if (path === "jobs" && rest[0]) return renderJobDetail(main, rest[0]);
  if (path === "jobs") return renderJobsList(main);
  if (path === "agents") return renderAgentsList(main);
  if (path === "logs") return renderLogExplorer(main);
  if (path === "schedules") return renderScheduler(main);
  if (path === "users") return isAdmin() ? renderUsers(main) : renderJobsList(main);
  renderJobsList(main);
}

window.addEventListener("hashchange", router);

function errorBox(err) {
  return h("div", { class: "error-box" }, `Error: ${err.message || err}`);
}

// --------------------------------------------------------------- User admin

async function renderUsers(main) {
  main.appendChild(h("h1", {}, "User management"));
  main.appendChild(h("div", { class: "subtitle" }, "Create accounts, assign roles, update email addresses, reset passwords, and remove users."));

  const createPanel = h("div", { class: "panel" });
  createPanel.appendChild(h("h2", { style: "margin-top:0" }, "Add user"));
  const emailInput = h("input", { type: "text", placeholder: "user@example.com", autocomplete: "off" });
  const passwordInput = h("input", { type: "password", placeholder: "Temporary password", autocomplete: "new-password" });
  const roleInput = h("select", {}, [
    h("option", { value: "end_user" }, "End user"),
    h("option", { value: "admin" }, "Admin"),
  ]);
  const createMessage = h("div");
  createPanel.appendChild(h("div", { class: "user-create-form" }, [
    h("div", {}, [h("label", {}, "Email"), emailInput]),
    h("div", {}, [h("label", {}, "Password"), passwordInput]),
    h("div", {}, [h("label", {}, "Role"), roleInput]),
    h("button", { class: "btn", onclick: createUser }, "Add user"),
  ]));
  createPanel.appendChild(createMessage);
  main.appendChild(createPanel);

  const listPanel = h("div", { class: "panel" });
  listPanel.appendChild(h("div", { class: "muted" }, "Loading users..."));
  main.appendChild(listPanel);

  async function createUser() {
    createMessage.innerHTML = "";
    const email = emailInput.value.trim();
    const password = passwordInput.value;
    if (!email || !password) {
      createMessage.appendChild(errorBox(new Error("email and password are required")));
      return;
    }
    try {
      await api("/auth/users", {
        method: "POST",
        body: JSON.stringify({ email, password, role: roleInput.value }),
      });
      emailInput.value = "";
      passwordInput.value = "";
      createMessage.appendChild(h("div", { class: "ok-box" }, "User created."));
      await loadUsers();
    } catch (err) {
      createMessage.appendChild(errorBox(err));
    }
  }

  async function loadUsers() {
    listPanel.innerHTML = "";
    listPanel.appendChild(h("div", { class: "muted" }, "Loading users..."));
    try {
      const { users } = await api("/auth/users");
      listPanel.innerHTML = "";
      if (!users.length) {
        listPanel.appendChild(h("div", { class: "empty" }, "No users found."));
        return;
      }
      const table = h("table", { class: "users-table" }, [
        h("thead", {}, h("tr", {}, [
          h("th", {}, "Email"),
          h("th", {}, "Role"),
          h("th", {}, "Created"),
          h("th", {}, "Password reset"),
          h("th", {}, "Actions"),
        ])),
      ]);
      const tbody = h("tbody");
      users.forEach((user) => {
        const email = h("input", { type: "text", value: user.email, "aria-label": `Email for ${user.email}` });
        const role = h("select", { "aria-label": `Role for ${user.email}` }, [
          h("option", { value: "end_user", selected: user.role === "end_user" }, "End user"),
          h("option", { value: "admin", selected: user.role === "admin" }, "Admin"),
        ]);
        const newPassword = h("input", {
          type: "password",
          placeholder: "New password",
          autocomplete: "new-password",
          "aria-label": `New password for ${user.email}`,
        });
        const message = h("div");
        const row = h("tr", {}, [
          h("td", {}, email),
          h("td", {}, role),
          h("td", {}, fmtTime(user.created_at)),
          h("td", {}, [
            newPassword,
            h("button", { class: "btn secondary", onclick: resetPassword }, "Set password"),
          ]),
          h("td", {}, [
            h("button", { class: "btn", onclick: saveUser }, "Save"),
            h("button", {
              class: "btn danger",
              disabled: user.id === currentUser.id,
              title: user.id === currentUser.id ? "You cannot delete your own account" : "Delete user",
              onclick: removeUser,
            }, "Delete"),
            message,
          ]),
        ]);
        tbody.appendChild(row);

        async function saveUser() {
          message.innerHTML = "";
          try {
            await api(`/auth/users/${encodeURIComponent(user.id)}`, {
              method: "PATCH",
              body: JSON.stringify({ email: email.value.trim(), role: role.value }),
            });
            await loadCurrentUser();
            renderUserBadge();
            if (!isAdmin()) {
              window.location.hash = "#/jobs";
              return;
            }
            await loadUsers();
          } catch (err) {
            message.appendChild(errorBox(err));
          }
        }

        async function resetPassword() {
          message.innerHTML = "";
          if (!newPassword.value) {
            message.appendChild(errorBox(new Error("enter a new password")));
            return;
          }
          try {
            await api(`/auth/users/${encodeURIComponent(user.id)}/password`, {
              method: "PUT",
              body: JSON.stringify({ password: newPassword.value }),
            });
            newPassword.value = "";
            message.appendChild(h("div", { class: "ok-box" }, "Password updated."));
          } catch (err) {
            message.appendChild(errorBox(err));
          }
        }

        async function removeUser() {
          if (!confirm(`Delete ${user.email}? This cannot be undone.`)) return;
          message.innerHTML = "";
          try {
            await api(`/auth/users/${encodeURIComponent(user.id)}`, { method: "DELETE" });
            await loadUsers();
          } catch (err) {
            message.appendChild(errorBox(err));
          }
        }
      });
      table.appendChild(tbody);
      listPanel.appendChild(h("div", { class: "user-table-scroll" }, table));
    } catch (err) {
      listPanel.innerHTML = "";
      listPanel.appendChild(errorBox(err));
    }
  }

  await loadUsers();
}

// --------------------------------------------------------------- Jobs

async function renderJobsList(main) {
  main.appendChild(h("h1", {}, "Jobs"));
  main.appendChild(h("div", { class: "subtitle" }, "A Job is a team of agent roles wired into a workflow. Trigger one manually, or set it on a schedule."));

  const panel = h("div", { class: "panel" });
  main.appendChild(panel);
  panel.appendChild(h("div", { class: "muted" }, "Loading..."));

  try {
    const { jobs } = await api("/jobs");
    panel.innerHTML = "";
    if (!jobs.length) {
      panel.appendChild(h("div", { class: "empty" }, "No jobs found. Add a jobs/*.yaml file, or draft one below."));
    } else {
      const table = h("table", {}, [
        h("thead", {}, h("tr", {}, [h("th", {}, "Job"), h("th", {}, "Description"), h("th", {}, "Trigger"), h("th", {}, "Status"), h("th", {}, "")])),
      ]);
      const tbody = h("tbody");
      jobs.forEach((j) => {
        tbody.appendChild(h("tr", {}, [
          h("td", {}, h("a", { href: `#/jobs/${j.id}` }, j.name)),
          h("td", {}, j.description || ""),
          h("td", {}, badge(j.trigger_type, j.trigger_type)),
          h("td", {}, badge(j.status, j.status)),
          h("td", {}, h("button", { class: "btn", onclick: () => runJobPrompt(j) }, "Run")),
        ]));
      });
      table.appendChild(tbody);
      panel.appendChild(table);
    }
  } catch (err) {
    panel.innerHTML = "";
    panel.appendChild(errorBox(err));
  }

  renderJobDraftForm(main);
}

async function runJobPrompt(job) {
  const task = prompt(`Task for "${job.name}":`, job.description || "");
  if (task == null) return;
  try {
    const result = await api(`/jobs/${job.id}/run`, { method: "POST", body: JSON.stringify({ task }) });
    window.location.hash = `#/jobs/run/${result.run_id}`;
  } catch (err) {
    alert("Failed to start run: " + err.message);
  }
}

function renderJobDraftForm(main) {
  main.appendChild(h("h2", {}, "Draft a new Job from a description"));
  const panel = h("div", { class: "panel" });
  const idInput = h("input", { type: "text", placeholder: "job_id (lowercase_snake_case)" });
  const descInput = h("textarea", { rows: 3, placeholder: "e.g. Run the QA team on a regression pass: manager scopes it, lead plans it, automation engineer and test executor carry it out." });
  const output = h("div");
  panel.appendChild(h("label", {}, "Job id"));
  panel.appendChild(idInput);
  panel.appendChild(h("label", {}, "Description"));
  panel.appendChild(descInput);
  panel.appendChild(h("div", { class: "row", style: "margin-top:10px" }, [
    h("button", { class: "btn", onclick: async () => {
      output.innerHTML = "Generating...";
      try {
        const draft = await api("/jobs/draft", { method: "POST", body: JSON.stringify({ id: idInput.value.trim(), description: descInput.value }) });
        renderDraftResult(output, draft, "yaml", async (text) => {
          await api(`/jobs/${idInput.value.trim()}`, { method: "PUT", body: JSON.stringify({ yaml: text }) });
          router();
        });
      } catch (err) { output.innerHTML = ""; output.appendChild(errorBox(err)); }
    } }, "Generate draft"),
  ]));
  panel.appendChild(output);
  main.appendChild(panel);
}

function renderDraftResult(container, draft, kind, onSave) {
  container.innerHTML = "";
  if (!draft.valid) {
    container.appendChild(errorBox(new Error("Draft did not validate: " + draft.error)));
  }
  const textarea = h("textarea", { rows: 16 }, draft[kind === "yaml" ? "yaml" : "markdown"]);
  container.appendChild(textarea);
  container.appendChild(h("div", { class: "row", style: "margin-top:8px" }, [
    h("button", { class: "btn", onclick: () => onSave(textarea.value) }, "Save"),
  ]));
}

// ------------------------------------------------- Job workflow editor
//
// Lets an admin add/remove/reorder a Job's agent chain and edit its Tasks
// (including a Task's `depends_on`, which is what lets two Tasks fan out
// in parallel -- see core/supervisor.py's DAG dispatch) without hand-
// writing YAML. Saves by serializing back to YAML client-side and calling
// the same PUT /jobs/{id} the raw-YAML draft flow uses -- the backend
// (core/job_loader.save_job_yaml) is the single source of validation
// either way, so a bad edit here is rejected exactly like a bad hand
// edit would be.

function yamlStr(s) {
  return JSON.stringify(s == null ? "" : String(s)); // valid YAML double-quoted scalar
}

function serializeJobYaml(job) {
  const lines = [];
  lines.push(`id: ${job.id}`);
  lines.push(`name: ${yamlStr(job.name)}`);
  lines.push(`description: ${yamlStr(job.description || "")}`);
  lines.push(`trigger_type: ${job.trigger_type || "manual"}`);
  if (job.webhook_url) lines.push(`webhook_url: ${yamlStr(job.webhook_url)}`);
  lines.push("");
  lines.push("agents:");
  (job.agents || []).forEach((a) => lines.push(`  - ${a}`));
  lines.push("");
  lines.push("tasks:");
  (job.tasks || []).forEach((t) => {
    lines.push(`  - id: ${t.id}`);
    lines.push(`    agent: ${t.agent}`);
    lines.push(`    name: ${yamlStr(t.name)}`);
    lines.push(`    instructions: ${yamlStr(t.instructions || "")}`);
    if (t.depends_on && t.depends_on.length) {
      lines.push(`    depends_on: [${t.depends_on.join(", ")}]`);
    }
  });
  return lines.join("\n") + "\n";
}

async function renderJobEditor(job) {
  // work on a mutable local copy; nothing is saved until "Save changes"
  const draft = JSON.parse(JSON.stringify(job));
  const wrap = h("div");
  wrap.appendChild(h("h2", {}, "Edit workflow"));
  const panel = h("div", { class: "panel" });
  const status = h("div");
  wrap.appendChild(panel);
  wrap.appendChild(status);

  let allAgentNames = [];
  try { allAgentNames = (await api("/agents")).agents.map((a) => a.name); } catch (e) { /* best effort */ }

  function redraw() {
    panel.innerHTML = "";

    panel.appendChild(h("label", {}, "Agent chain (order matters -- an agent with no Task of its own passes the previous step's output straight through)"));
    const chain = h("div", { class: "row" });
    draft.agents.forEach((agentName, i) => {
      chain.appendChild(h("div", { class: "wf-node pending", style: "min-width:auto;padding:6px 10px" }, [
        h("div", { class: "wf-name" }, agentName),
        h("div", { class: "row", style: "margin-top:6px;gap:4px" }, [
          h("button", { class: "btn secondary", disabled: i === 0, onclick: () => { [draft.agents[i - 1], draft.agents[i]] = [draft.agents[i], draft.agents[i - 1]]; redraw(); } }, "↑"),
          h("button", { class: "btn secondary", disabled: i === draft.agents.length - 1, onclick: () => { [draft.agents[i + 1], draft.agents[i]] = [draft.agents[i], draft.agents[i + 1]]; redraw(); } }, "↓"),
          h("button", { class: "btn danger", onclick: () => {
            const removedTasks = draft.tasks.filter((t) => t.agent === agentName);
            if (removedTasks.length && !confirm(`Also remove ${removedTasks.length} task(s) assigned to ${agentName}?`)) return;
            draft.agents.splice(i, 1);
            draft.tasks = draft.tasks.filter((t) => t.agent !== agentName);
            redraw();
          } }, "✕"),
        ]),
      ]));
    });
    panel.appendChild(chain);

    const addableAgents = allAgentNames.filter((a) => !draft.agents.includes(a));
    if (addableAgents.length) {
      const addSelect = h("select", {}, addableAgents.map((a) => h("option", { value: a }, a)));
      panel.appendChild(h("div", { class: "row", style: "margin-top:8px" }, [
        addSelect,
        h("button", { class: "btn secondary", onclick: () => { draft.agents.push(addSelect.value); redraw(); } }, "Add agent to chain"),
      ]));
    }

    panel.appendChild(h("h2", {}, "Tasks"));
    panel.appendChild(h("div", { class: "muted" }, "Two tasks that both depend only on the same upstream task run in parallel."));
    draft.tasks.forEach((t, i) => {
      const agentSelect = h("select", {}, draft.agents.map((a) => h("option", { value: a, selected: a === t.agent ? "selected" : null }, a)));
      const nameInput = h("input", { type: "text", value: t.name });
      const idInput = h("input", { type: "text", value: t.id });
      const instrInput = h("textarea", { rows: 2 }, t.instructions || "");
      const otherTaskIds = draft.tasks.map((x) => x.id).filter((tid) => tid !== t.id);
      const dependsSelect = h("select", { multiple: "multiple", size: String(Math.min(4, Math.max(2, otherTaskIds.length))) },
        otherTaskIds.map((tid) => h("option", { value: tid, selected: (t.depends_on || []).includes(tid) ? "selected" : null }, tid)));

      agentSelect.addEventListener("change", () => { t.agent = agentSelect.value; });
      nameInput.addEventListener("change", () => { t.name = nameInput.value; });
      idInput.addEventListener("change", () => { t.id = idInput.value.trim(); });
      instrInput.addEventListener("change", () => { t.instructions = instrInput.value; });
      dependsSelect.addEventListener("change", () => {
        t.depends_on = Array.from(dependsSelect.selectedOptions).map((o) => o.value);
      });

      panel.appendChild(h("div", { class: "panel", style: "background:var(--panel-2)" }, [
        h("div", { class: "row" }, [
          h("div", {}, [h("label", {}, "Task id"), idInput]),
          h("div", {}, [h("label", {}, "Agent"), agentSelect]),
          h("div", {}, [h("label", {}, "Depends on"), dependsSelect]),
          h("button", { class: "btn danger", style: "align-self:flex-end", onclick: () => { draft.tasks.splice(i, 1); redraw(); } }, "Remove task"),
        ]),
        h("label", {}, "Name"), nameInput,
        h("label", {}, "Instructions"), instrInput,
      ]));
    });

    panel.appendChild(h("button", { class: "btn secondary", onclick: () => {
      draft.tasks.push({ id: `task_${draft.tasks.length + 1}`, agent: draft.agents[0] || "", name: "New task", instructions: "", depends_on: [] });
      redraw();
    } }, "Add task"));

    panel.appendChild(h("div", { class: "row", style: "margin-top:14px" }, [
      h("button", { class: "btn", onclick: save }, "Save changes"),
      h("details", {}, [
        h("summary", { style: "cursor:pointer;color:var(--text-dim)" }, "Advanced: edit raw YAML"),
        (() => {
          const ta = h("textarea", { rows: 16 }, serializeJobYaml(draft));
          const saveYamlBtn = h("button", { class: "btn", style: "margin-top:8px", onclick: async () => {
            status.innerHTML = "";
            try {
              await api(`/jobs/${job.id}`, { method: "PUT", body: JSON.stringify({ yaml: ta.value }) });
              status.appendChild(h("div", { class: "muted" }, "Saved."));
              router();
            } catch (err) { status.innerHTML = ""; status.appendChild(errorBox(err)); }
          } }, "Save raw YAML");
          return h("div", {}, [ta, saveYamlBtn]);
        })(),
      ]),
    ]));
  }

  async function save() {
    status.innerHTML = "";
    if (!draft.agents.length) { status.appendChild(errorBox(new Error("a Job needs at least one agent"))); return; }
    try {
      await api(`/jobs/${job.id}`, { method: "PUT", body: JSON.stringify({ yaml: serializeJobYaml(draft) }) });
      status.appendChild(h("div", { class: "muted" }, "Saved."));
      router();
    } catch (err) {
      status.innerHTML = "";
      status.appendChild(errorBox(err));
    }
  }

  redraw();
  return wrap;
}

async function renderJobDetail(main, jobId) {
  main.appendChild(h("a", { href: "#/jobs", class: "muted" }, "← Jobs"));
  main.appendChild(h("h1", {}, jobId));
  const panel = h("div", { class: "panel" }, h("div", { class: "muted" }, "Loading..."));
  main.appendChild(panel);

  try {
    const [job, graph, runsResp] = await Promise.all([
      api(`/jobs/${jobId}`),
      api(`/jobs/${jobId}/graph`),
      api(`/jobs/${jobId}/runs`),
    ]);
    panel.innerHTML = "";
    panel.appendChild(h("div", { class: "subtitle" }, job.description || ""));
    panel.appendChild(renderWorkflow(graph, null, null));

    panel.appendChild(h("h2", {}, "Tasks"));
    const taskTable = h("table", {}, [h("thead", {}, h("tr", {}, [h("th", {}, "Task"), h("th", {}, "Agent"), h("th", {}, "Depends on")]))]);
    const tbody = h("tbody");
    (job.tasks || []).forEach((t) => {
      tbody.appendChild(h("tr", {}, [h("td", {}, t.name), h("td", {}, t.agent), h("td", {}, (t.depends_on || []).join(", ") || "-")]));
    });
    taskTable.appendChild(tbody);
    panel.appendChild(taskTable);

    panel.appendChild(h("div", { style: "margin-top:14px" }, h("button", { class: "btn", onclick: () => runJobPrompt(job) }, "Run this Job")));

    if (isAdmin()) {
      main.appendChild(await renderJobEditor(job));
    }

    main.appendChild(h("h2", {}, "Run history"));
    const runPanel = h("div", { class: "panel" });
    if (!runsResp.runs.length) {
      runPanel.appendChild(h("div", { class: "empty" }, "No runs yet."));
    } else {
      const rt = h("table", {}, [h("thead", {}, h("tr", {}, [h("th", {}, "Started"), h("th", {}, "Status"), h("th", {}, "Current agent"), h("th", {}, "Triggered by"), h("th", {}, "")]))]);
      const rb = h("tbody");
      runsResp.runs.forEach((r) => {
        rb.appendChild(h("tr", {}, [
          h("td", {}, fmtTime(r.started_at)),
          h("td", {}, [
            badge(r.status, r.status),
            r.stale ? h("span", { class: "badge stale", style: "margin-left:6px" }, "stuck?") : null,
          ]),
          h("td", {}, r.current_agent || "-"),
          h("td", {}, r.triggered_by),
          h("td", {}, h("a", { href: `#/jobs/run/${r.id}` }, "View")),
        ]));
      });
      rt.appendChild(rb);
      runPanel.appendChild(rt);
    }
    main.appendChild(runPanel);
  } catch (err) {
    panel.innerHTML = "";
    panel.appendChild(errorBox(err));
  }
}

function renderWorkflow(graph, currentAgent, runStatus) {
  const wrap = h("div", { class: "workflow" });
  const currentPos = currentAgent ? (graph.nodes.find((n) => n.id === currentAgent) || {}).position : null;
  graph.nodes.forEach((node, i) => {
    let cls = "pending";
    let state = "pending";
    if (currentPos != null) {
      if (node.position < currentPos) { cls = "done"; state = "done"; }
      else if (node.position === currentPos) {
        if (runStatus === "error") { cls = "error"; state = "failed"; }
        else if (runStatus === "awaiting_human") { cls = "active"; state = "awaiting approval"; }
        else if (runStatus === "done") { cls = "done"; state = "done"; }
        else { cls = "active"; state = "running"; }
      }
    } else if (runStatus === "done") { cls = "done"; state = "done"; }
    wrap.appendChild(h("div", { class: `wf-node ${cls}` }, [
      h("div", { class: "wf-name" }, node.id),
      h("div", { class: "wf-state" }, state),
    ]));
    if (i < graph.nodes.length - 1) wrap.appendChild(h("div", { class: "wf-arrow" }, "→"));
  });
  return wrap;
}

async function renderRunDetail(main, runId) {
  main.appendChild(h("h1", {}, "Run " + runId.slice(0, 8)));
  const panel = h("div", { class: "panel" }, h("div", { class: "muted" }, "Loading..."));
  main.appendChild(panel);

  async function load() {
    const run = await api(`/runs/${runId}`);
    const graph = await api(`/jobs/${run.job_id}/graph`);
    panel.innerHTML = "";
    panel.appendChild(h("div", {}, [
      h("span", { class: "muted" }, "Job: "), h("a", { href: `#/jobs/${run.job_id}` }, run.job_id),
      h("span", { class: "muted" }, "  ·  Status: "), badge(run.status, run.status),
      run.stale ? h("span", { class: "badge stale", style: "margin-left:6px" }, "stuck?") : null,
    ]));
    panel.appendChild(h("div", { class: "muted", style: "margin-top:6px" }, `Task: ${run.task}`));
    panel.appendChild(renderWorkflow(graph, run.current_agent, run.status));

    if (run.status === "error") {
      panel.appendChild(h("div", { class: "error-box" }, `Failed at ${run.current_agent}: ${run.error}`));
    }
    if (run.status === "done") {
      panel.appendChild(h("h2", {}, "Final answer"));
      panel.appendChild(h("div", { class: "panel" }, run.final_answer || "(empty)"));
    }
    if (run.status === "running" && run.stale) {
      panel.appendChild(h("div", { class: "warn-box" }, [
        h("div", {}, `This run hasn't reported any progress in a while and may be stuck at ${run.current_agent || "its first step"}.`),
        h("div", { class: "row", style: "margin-top:10px" }, [
          h("button", { class: "btn danger", onclick: () => markFailed(runId) }, "Mark as failed"),
        ]),
      ]));
    }
    if (run.status === "awaiting_human") {
      panel.appendChild(h("div", { class: "row", style: "margin-top:12px" }, [
        h("button", { class: "btn", onclick: () => resumeRun(runId, "approve") }, "Approve"),
        h("button", { class: "btn danger", onclick: () => {
          const reason = prompt("Reason for denying:", "too risky") || "denied";
          resumeRun(runId, `deny: ${reason}`);
        } }, "Deny"),
      ]));
    }

    panel.appendChild(h("div", { style: "margin-top:16px" }, h("a", { href: `#/logs?run=${runId}` }, "View this run's logs →")));
  }

  async function resumeRun(id, decision) {
    try {
      await api(`/runs/${id}/resume`, { method: "POST", body: JSON.stringify({ decision }) });
      load();
    } catch (err) { alert("Resume failed: " + err.message); }
  }

  async function markFailed(id) {
    if (!confirm("Mark this run as failed? This can't be undone.")) return;
    try {
      await api(`/runs/${id}/mark_failed`, { method: "POST" });
      load();
    } catch (err) { alert("Failed to mark run as failed: " + err.message); }
  }

  try { await load(); } catch (err) { panel.innerHTML = ""; panel.appendChild(errorBox(err)); }
}

// ------------------------------------------------------------- Agents

async function renderAgentsList(main) {
  main.appendChild(h("h1", {}, "Agents"));
  main.appendChild(h("div", { class: "subtitle" }, "Roles are defined as Markdown files (agents/*.md), not hardcoded prompts."));

  const panel = h("div", { class: "panel" }, h("div", { class: "muted" }, "Loading..."));
  main.appendChild(panel);

  try {
    const { agents } = await api("/agents");
    panel.innerHTML = "";
    const table = h("table", {}, [h("thead", {}, h("tr", {}, [h("th", {}, "Name"), h("th", {}, "Reports to"), h("th", {}, "Hands off to"), h("th", {}, "Tools"), h("th", {}, "")]))]);
    const tbody = h("tbody");
    agents.forEach((a) => {
      if (a.error) {
        tbody.appendChild(h("tr", {}, [h("td", {}, a.name), h("td", { colspan: "4" }, badge("error", "error") )]));
        return;
      }
      tbody.appendChild(h("tr", {}, [
        h("td", {}, h("strong", {}, a.display_name || a.name)),
        h("td", {}, a.reports_to || "-"),
        h("td", {}, a.hands_off_to || "-"),
        h("td", {}, (a.tools || []).map((t) => `<code class="inline">${t}</code>`).join(" ")),
        h("td", {}, h("button", { class: "btn secondary", onclick: () => showAgentDetail(a.name) }, "View")),
      ]));
    });
    table.appendChild(tbody);
    panel.appendChild(table);
  } catch (err) {
    panel.innerHTML = "";
    panel.appendChild(errorBox(err));
  }

  const detail = h("div", { id: "agent-detail" });
  main.appendChild(detail);

  async function showAgentDetail(name) {
    detail.innerHTML = "";
    detail.appendChild(h("h2", {}, name));
    const p = h("div", { class: "panel" }, h("div", { class: "muted" }, "Loading..."));
    detail.appendChild(p);
    try {
      const a = await api(`/agents/${name}`);
      p.innerHTML = "";
      p.appendChild(h("pre", { style: "white-space:pre-wrap;font-size:12.5px" }, a.instructions));
    } catch (err) { p.innerHTML = ""; p.appendChild(errorBox(err)); }
  }

  main.appendChild(h("h2", {}, "Draft a new agent from a description"));
  const draftPanel = h("div", { class: "panel" });
  const nameInput = h("input", { type: "text", placeholder: "agent_name (lowercase_snake_case)" });
  const descInput = h("textarea", { rows: 3, placeholder: "e.g. Reviews the QA Lead's test plan for coverage gaps before automation starts." });
  const output = h("div");
  draftPanel.appendChild(h("label", {}, "Agent name"));
  draftPanel.appendChild(nameInput);
  draftPanel.appendChild(h("label", {}, "Description"));
  draftPanel.appendChild(descInput);
  draftPanel.appendChild(h("div", { class: "row", style: "margin-top:10px" }, [
    h("button", { class: "btn", onclick: async () => {
      output.innerHTML = "Generating...";
      try {
        const draft = await api("/agents/draft", { method: "POST", body: JSON.stringify({ name: nameInput.value.trim(), description: descInput.value }) });
        renderDraftResult(output, draft, "markdown", async (text) => {
          await api(`/agents/${nameInput.value.trim()}`, { method: "PUT", body: JSON.stringify({ markdown: text }) });
          router();
        });
      } catch (err) { output.innerHTML = ""; output.appendChild(errorBox(err)); }
    } }, "Generate draft"),
  ]));
  draftPanel.appendChild(output);
  main.appendChild(draftPanel);
}

// --------------------------------------------------------- Log Explorer

async function renderLogExplorer(main) {
  main.appendChild(h("h1", {}, "Log Explorer"));
  main.appendChild(h("div", { class: "subtitle" }, "Every node transition across every agent, correlated by run."));

  const params = new URLSearchParams(window.location.hash.split("?")[1] || "");
  const panel = h("div", { class: "panel" });

  const runIdInput = h("input", { type: "text", placeholder: "run_id", value: params.get("run") || "" });
  const levelSelect = h("select", {}, [
    h("option", { value: "" }, "All levels"),
    h("option", { value: "DEBUG" }, "DEBUG"),
    h("option", { value: "INFO" }, "INFO"),
    h("option", { value: "WARN" }, "WARN"),
    h("option", { value: "ERROR" }, "ERROR"),
  ]);
  const agentInput = h("input", { type: "text", placeholder: "filter by agent name" });
  const searchInput = h("input", { type: "text", placeholder: "search node/payload text" });
  const loadBtn = h("button", { class: "btn" }, "Load");

  panel.appendChild(h("div", { class: "row" }, [
    h("div", {}, [h("label", {}, "Run ID"), runIdInput]),
    h("div", {}, [h("label", {}, "Level"), levelSelect]),
    h("div", {}, [h("label", {}, "Agent"), agentInput]),
    h("div", {}, [h("label", {}, "Search"), searchInput]),
    h("div", {}, [h("label", {}, " "), loadBtn]),
  ]));

  main.appendChild(panel);
  const correctionNote = h("div");
  main.appendChild(correctionNote);
  const resultsWrap = h("div");
  main.appendChild(resultsWrap);

  // A run_id is a plain uuid4. The internal LangGraph thread_id
  // (f"{run_id}-{agent_name}", core/supervisor.py) looks enough like one --
  // and shows up right next to run_id in the console trace logs -- that
  // it's an easy paste mistake. Trim a suffix like "-qa_manager" off
  // client-side so the Run ID field always ends up holding a real run_id
  // (the backend also tolerates this -- see core.job_store.resolve_run_id
  // -- this is just so the correction is visible immediately, not after a
  // round trip).
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

  async function load() {
    let runId = runIdInput.value.trim();
    if (!runId) {
      resultsWrap.innerHTML = "";
      resultsWrap.appendChild(h("div", { class: "empty" }, "Enter a run_id (from a Run's detail page) to see its logs."));
      return;
    }
    const match = runId.match(UUID_RE);
    correctionNote.innerHTML = "";
    if (match && match[0] !== runId) {
      const corrected = runId;
      runId = match[0];
      runIdInput.value = runId;
      correctionNote.appendChild(h("div", { class: "muted" }, `"${corrected}" looks like an internal thread id, not a run id -- using "${runId}" instead.`));
    }
    resultsWrap.innerHTML = "";
    resultsWrap.appendChild(h("div", { class: "muted" }, "Loading..."));
    try {
      const qs = new URLSearchParams();
      if (levelSelect.value) qs.set("level", levelSelect.value);
      if (agentInput.value.trim()) qs.set("agent", agentInput.value.trim());
      qs.set("limit", "500");
      const { events } = await api(`/runs/${runId}/events?${qs.toString()}`);
      const search = searchInput.value.trim().toLowerCase();
      const filtered = search
        ? events.filter((e) => (e.node + " " + (e.payload || "")).toLowerCase().includes(search))
        : events;

      resultsWrap.innerHTML = "";
      if (!filtered.length) {
        resultsWrap.appendChild(h("div", { class: "empty" }, "No matching events."));
        return;
      }
      const table = h("table", {}, [
        h("thead", {}, h("tr", {}, [h("th", {}, "Time"), h("th", {}, "Level"), h("th", {}, "Module"), h("th", {}, "Event"), h("th", {}, "Detail")])),
      ]);
      const tbody = h("tbody");
      filtered.forEach((e) => {
        const module = [e.job_id, e.agent_name, e.node].filter(Boolean).join(" / ");
        tbody.appendChild(h("tr", {}, [
          h("td", { style: "white-space:nowrap" }, new Date(e.ts * 1000).toLocaleTimeString()),
          h("td", {}, badge(e.level, `level-${e.level}`)),
          h("td", {}, module),
          h("td", {}, e.event_type),
          h("td", {}, [
            h("details", {}, [
              h("summary", { style: "cursor:pointer;color:var(--text-dim)" }, "payload"),
              h("pre", { style: "white-space:pre-wrap;font-size:11.5px;margin:6px 0 0" }, e.payload || ""),
            ]),
          ]),
        ]));
      });
      table.appendChild(tbody);
      resultsWrap.appendChild(table);
    } catch (err) {
      resultsWrap.innerHTML = "";
      resultsWrap.appendChild(errorBox(err));
    }
  }

  loadBtn.addEventListener("click", load);
  if (runIdInput.value) load();
}

// ----------------------------------------------------------- Scheduler

async function renderScheduler(main) {
  main.appendChild(h("h1", {}, "Scheduler"));
  main.appendChild(h("div", { class: "subtitle" }, "Run a Job on a recurring cron schedule."));

  const panel = h("div", { class: "panel" }, h("div", { class: "muted" }, "Loading..."));
  main.appendChild(panel);

  async function load() {
    const [{ schedules }, { jobs }] = await Promise.all([api("/schedules"), api("/jobs")]);
    panel.innerHTML = "";
    if (!schedules.length) {
      panel.appendChild(h("div", { class: "empty" }, "No schedules yet."));
    } else {
      const table = h("table", {}, [h("thead", {}, h("tr", {}, [h("th", {}, "Job"), h("th", {}, "Cron"), h("th", {}, "Enabled"), h("th", {}, "Last run"), h("th", {}, "")]))]);
      const tbody = h("tbody");
      schedules.forEach((s) => {
        tbody.appendChild(h("tr", {}, [
          h("td", {}, h("a", { href: `#/jobs/${s.job_id}` }, s.job_id)),
          h("td", {}, h("code", { class: "inline" }, s.cron_expr)),
          h("td", {}, badge(s.enabled ? "enabled" : "disabled", s.enabled ? "done" : "error")),
          h("td", {}, fmtTime(s.last_run_at)),
          h("td", {}, [
            h("button", { class: "btn secondary", onclick: () => toggle(s) }, s.enabled ? "Disable" : "Enable"),
            h("button", { class: "btn secondary", onclick: () => runNow(s) }, "Run now"),
            h("button", { class: "btn danger", onclick: () => remove(s) }, "Delete"),
          ]),
        ]));
      });
      table.appendChild(tbody);
      panel.appendChild(table);
    }

    const jobSelect = h("select", {}, jobs.map((j) => h("option", { value: j.id }, j.name)));
    const cronInput = h("input", { type: "text", placeholder: "0 7 * * *" });
    const form = h("div", { class: "panel" }, [
      h("h2", { style: "margin-top:0" }, "New schedule"),
      h("div", { class: "row" }, [
        h("div", {}, [h("label", {}, "Job"), jobSelect]),
        h("div", {}, [h("label", {}, "Cron (5-field)"), cronInput]),
        h("div", {}, [h("label", {}, " "), h("button", { class: "btn", onclick: create }, "Create")]),
      ]),
      h("div", { class: "muted", style: "margin-top:6px" }, "Standard 5-field cron, e.g. \"0 7 * * *\" = every day at 07:00."),
    ]);
    main.appendChild(form);

    async function create() {
      try {
        await api("/schedules", { method: "POST", body: JSON.stringify({ job_id: jobSelect.value, cron: cronInput.value.trim() }) });
        router();
      } catch (err) { alert("Failed: " + err.message); }
    }
    async function toggle(s) {
      try { await api(`/schedules/${s.id}`, { method: "PUT", body: JSON.stringify({ enabled: !s.enabled }) }); router(); }
      catch (err) { alert("Failed: " + err.message); }
    }
    async function remove(s) {
      if (!confirm("Delete this schedule?")) return;
      try { await api(`/schedules/${s.id}`, { method: "DELETE" }); router(); }
      catch (err) { alert("Failed: " + err.message); }
    }
    async function runNow(s) {
      try { const r = await api(`/schedules/${s.id}/run-now`, { method: "POST" }); window.location.hash = `#/jobs/run/${r.run_id}`; }
      catch (err) { alert("Failed: " + err.message); }
    }
  }

  try { await load(); } catch (err) { panel.innerHTML = ""; panel.appendChild(errorBox(err)); }
}

// --------------------------------------------------------------- boot

async function checkApiHealth() {
  const el = document.getElementById("api-status");
  try {
    await api("/health");
    el.textContent = "connected";
    el.className = "api-status ok";
  } catch (err) {
    el.textContent = "unreachable: " + err.message;
    el.className = "api-status bad";
  }
}

document.getElementById("api-base-input").value = apiBase();
document.getElementById("api-base-save").addEventListener("click", async () => {
  setApiBase(document.getElementById("api-base-input").value.trim() || DEFAULT_API_BASE);
  await checkApiHealth();
  boot();
});

async function boot() {
  await checkApiHealth();
  if (!getToken()) {
    renderAuthGate();
    return;
  }
  try {
    await loadCurrentUser();
    renderApp();
  } catch (err) {
    // api() already clears the token and calls renderAuthGate() on a 401;
    // any other failure here (e.g. the API being unreachable) just leaves
    // the login screen up rather than throwing an unhandled rejection.
    renderAuthGate();
  }
}

boot();
