// TestDesk -- the "Self test guide" page (#/testdesk).
//
// A user describes a scenario in plain words, picks a target (a Job or an
// Agent) and a mode, and the Studio performs it:
//   Test -> runs in a throwaway sandbox (its own temporary databases, deleted
//           afterwards); only this page's short report is kept.
//   Prod -> runs for real; the Run is kept in the app database.
// Backend: TestDesk/routes.py. Uses h(), api(), badge(), errorBox(), fmtTime()
// from app.js (called at render time, so load order doesn't matter).

async function renderTestDesk(main) {
  main.appendChild(h("h1", {}, "TestDesk"));
  main.appendChild(h("div", { class: "subtitle" },
    "Self test guide: describe a scenario, choose where it runs, and the Studio performs it -- try anything in a sandbox before doing it live."));

  let targets;
  try { targets = await api("/testdesk/targets"); }
  catch (err) { main.appendChild(errorBox(err)); return; }

  const state = { editingId: null, mode: "test", armed: false, dirty: false, armTimer: null, pollTimer: null };

  // ------------------------------------------------------------ the form
  const nameIn = h("input", { type: "text", placeholder: "e.g. Checkout regression" });
  const taskIn = h("textarea", { rows: "4", placeholder: "Describe the scenario in your own words -- what should the Studio do, and what are you trying to find out?" });
  const typeSel = h("select", {}, [h("option", { value: "job" }, "Job"), h("option", { value: "agent" }, "Agent")]);
  const targetSel = h("select", {});
  const yamlIn = h("textarea", { rows: "8", placeholder: "id: my_draft_job\nname: ...\nagents: [...]\ntasks: [...]" });
  const expectIn = h("textarea", { rows: "3", placeholder: "One phrase per line that the final answer must mention (optional)" });
  const statusSel = h("select", {}, ["done", "error", "awaiting_human"].map((s) => h("option", { value: s }, s)));
  const cbBlock = h("input", { type: "checkbox" }); cbBlock.checked = true;
  const cbApprove = h("input", { type: "checkbox" }); cbApprove.checked = true;
  const cbKeep = h("input", { type: "checkbox" });
  const msg = h("div");

  const modeTest = h("div", { class: "mode-card", "data-mode": "test", onclick: () => setMode("test") }, [
    h("div", { class: "mode-title" }, "Test -- throwaway database"),
    h("div", { class: "muted" }, "Runs in a sandbox with its own temporary databases, logs and work folder. Nothing is saved to the app; the sandbox is deleted afterwards and only the report below is kept."),
  ]);
  const modeProd = h("div", { class: "mode-card", "data-mode": "prod", onclick: () => setMode("prod") }, [
    h("div", { class: "mode-title" }, "Prod -- keep in the app database"),
    h("div", { class: "muted" }, "Runs for real, exactly like starting the Job from the Jobs page. The Run, its logs and its result stay in the app database."),
  ]);
  const prodNote = h("div", { class: "muted", style: "margin-top:6px" });
  const testOptions = h("div", { class: "td-options" }, [
    h("label", { class: "check" }, [cbBlock, " Block outbound tools (HTTP writes, shell, Gmail, AWS)"]),
    h("label", { class: "check" }, [cbApprove, " Auto-approve human-approval gates so the scenario can finish"]),
    h("label", { class: "check" }, [cbKeep, " Keep the sandbox folder afterwards (for inspection)"]),
  ]);
  const runBtn = h("button", { class: "btn", onclick: onRun }, "Run in test sandbox");
  const saveBtn = h("button", { class: "btn secondary", onclick: onSave }, "Save scenario");
  const newBtn = h("button", { class: "btn secondary", onclick: () => resetForm() }, "New scenario");

  const form = h("div", { class: "panel" }, [
    h("h2", { style: "margin-top:0" }, "Scenario"),
    h("label", {}, "Name (optional)"), nameIn,
    h("label", {}, "What should the Studio test?"), taskIn,
    h("div", { class: "row" }, [
      h("div", {}, [h("label", {}, "Target"), typeSel]),
      h("div", {}, [h("label", {}, " "), targetSel]),
      h("div", {}, [h("label", {}, "Expected outcome"), statusSel]),
    ]),
    h("label", {}, "Final answer should mention"), expectIn,
    h("details", { class: "td-draft" }, [
      h("summary", {}, "Try a draft Job definition that isn't saved yet (test mode only)"),
      h("div", { class: "muted", style: "margin:6px 0" }, "Paste Job YAML here to test it before saving it. It only exists inside the sandbox."),
      yamlIn,
    ]),
    h("label", {}, "Where should it run?"),
    h("div", { class: "mode-cards" }, [modeTest, modeProd]),
    prodNote,
    testOptions,
    h("div", { class: "row", style: "margin-top:14px" }, [runBtn, saveBtn, newBtn]),
    msg,
  ]);
  main.appendChild(form);

  const savedPanel = h("div", { class: "panel" });
  const detailPanel = h("div", { class: "panel", style: "display:none" });
  const resultsPanel = h("div", { class: "panel" });
  main.appendChild(h("h2", {}, "Saved scenarios")); main.appendChild(savedPanel);
  main.appendChild(h("h2", {}, "Report")); main.appendChild(detailPanel);
  main.appendChild(h("h2", {}, "History")); main.appendChild(resultsPanel);

  function fillTargets() {
    const list = typeSel.value === "job" ? targets.jobs : targets.agents;
    const keep = targetSel.value;
    targetSel.innerHTML = "";
    list.forEach((t) => targetSel.appendChild(h("option", { value: t }, t)));
    if (list.includes(keep)) targetSel.value = keep;
  }

  function prodAllowed() { return typeSel.value === "job" && !yamlIn.value.trim(); }

  function syncForm() {
    const draft = !!yamlIn.value.trim();
    typeSel.disabled = draft; targetSel.disabled = draft;
    if (state.mode === "prod" && !prodAllowed()) state.mode = "test";
    modeTest.classList.toggle("selected", state.mode === "test");
    modeProd.classList.toggle("selected", state.mode === "prod");
    modeProd.classList.toggle("disabled", !prodAllowed());
    prodNote.textContent = prodAllowed() ? "" : "Prod runs a saved Job -- agents and draft Jobs can only be tested in the sandbox.";
    testOptions.style.display = state.mode === "test" ? "" : "none";
    runBtn.className = "btn" + (state.mode === "prod" ? " danger" : "");
    runBtn.textContent = state.mode === "prod"
      ? (state.armed ? "Click again to confirm -- run for real" : "Run for real (prod)")
      : "Run in test sandbox";
  }

  function disarm() { state.armed = false; clearTimeout(state.armTimer); syncForm(); }
  function setMode(mode) { if (mode === "prod" && !prodAllowed()) return; state.mode = mode; disarm(); }
  function touch() { state.dirty = true; disarm(); }
  [nameIn, taskIn, targetSel, yamlIn, expectIn, statusSel, cbBlock, cbApprove, cbKeep].forEach((el) => el.addEventListener("input", touch));
  typeSel.addEventListener("change", () => { fillTargets(); touch(); });
  yamlIn.addEventListener("input", syncForm);

  function readForm() {
    return {
      name: nameIn.value.trim(),
      target_type: typeSel.value,
      target_id: targetSel.value || "",
      task: taskIn.value.trim(),
      expectations: expectIn.value.split("\n").map((s) => s.trim()).filter(Boolean),
      expect_status: statusSel.value,
      job_yaml: yamlIn.value.trim(),
      options: { block_external: cbBlock.checked, auto_approve: cbApprove.checked, keep_sandbox: cbKeep.checked },
    };
  }

  function loadIntoForm(sc, mode) {
    state.editingId = sc.id || null; state.dirty = false;
    nameIn.value = sc.name || ""; taskIn.value = sc.task || "";
    typeSel.value = sc.target_type || "job"; fillTargets();
    if (sc.target_id) targetSel.value = sc.target_id;
    yamlIn.value = sc.job_yaml || ""; expectIn.value = (sc.expectations || []).join("\n");
    statusSel.value = sc.expect_status || "done";
    const o = sc.options || {};
    cbBlock.checked = o.block_external !== false; cbApprove.checked = o.auto_approve !== false; cbKeep.checked = !!o.keep_sandbox;
    msg.innerHTML = "";
    state.mode = mode || "test"; disarm();
    form.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function resetForm() { loadIntoForm({ target_type: "job" }, "test"); state.editingId = null; }

  // ------------------------------------------------------------- actions
  function say(node) { msg.innerHTML = ""; if (node) msg.appendChild(node); }

  async function onSave() {
    try {
      const body = JSON.stringify(readForm());
      const saved = state.editingId
        ? await api(`/testdesk/scenarios/${state.editingId}`, { method: "PUT", body })
        : await api("/testdesk/scenarios", { method: "POST", body });
      state.editingId = saved.id; state.dirty = false;
      say(h("div", { class: "muted" }, "Saved."));
      await loadSaved();
    } catch (err) { say(errorBox(err)); }
  }

  async function onRun() {
    if (state.mode === "prod" && !state.armed) {
      state.armed = true; syncForm();
      state.armTimer = setTimeout(disarm, 8000);
      return;
    }
    disarm();
    try {
      const useSaved = state.editingId && !state.dirty;
      const resp = await api("/testdesk/run", {
        method: "POST",
        body: JSON.stringify(useSaved ? { mode: state.mode, scenario_id: state.editingId } : { mode: state.mode, scenario: readForm() }),
      });
      say(null);
      await showResult(resp.result_id);
      await loadResults();
    } catch (err) { say(errorBox(err)); }
  }

  async function runSaved(sc, mode) {
    loadIntoForm(sc, mode);
    if (mode === "prod") { state.armed = true; syncForm(); state.armTimer = setTimeout(disarm, 8000); return; }
    await onRun();
  }

  // ------------------------------------------------------------ listings
  async function loadSaved() {
    const { scenarios } = await api("/testdesk/scenarios");
    savedPanel.innerHTML = "";
    if (!scenarios.length) { savedPanel.appendChild(h("div", { class: "empty" }, "No saved scenarios yet. Fill in the form above and press Save.")); return; }
    const tbody = h("tbody");
    scenarios.forEach((sc) => tbody.appendChild(h("tr", {}, [
      h("td", {}, sc.name),
      h("td", {}, [badge(sc.target_type, "manual"), " ", h("code", { class: "inline" }, sc.job_yaml ? `${sc.target_id} (draft)` : sc.target_id)]),
      h("td", { class: "muted" }, sc.task.length > 90 ? sc.task.slice(0, 90) + "…" : sc.task),
      h("td", {}, [
        h("button", { class: "btn secondary", onclick: () => loadIntoForm(sc, "test") }, "Load"),
        h("button", { class: "btn", onclick: () => runSaved(sc, "test") }, "Run test"),
        sc.target_type === "job" && !sc.job_yaml ? h("button", { class: "btn secondary", onclick: () => runSaved(sc, "prod") }, "Run prod…") : null,
        h("button", { class: "btn danger", onclick: () => removeSaved(sc) }, "Delete"),
      ]),
    ])));
    savedPanel.appendChild(h("table", {}, [
      h("thead", {}, h("tr", {}, ["Scenario", "Target", "Task", ""].map((t) => h("th", {}, t)))), tbody,
    ]));
  }

  async function removeSaved(sc) {
    try { await api(`/testdesk/scenarios/${sc.id}`, { method: "DELETE" }); if (state.editingId === sc.id) resetForm(); await loadSaved(); }
    catch (err) { say(errorBox(err)); }
  }

  async function loadResults() {
    const { results } = await api("/testdesk/results");
    resultsPanel.innerHTML = "";
    if (!results.length) { resultsPanel.appendChild(h("div", { class: "empty" }, "Nothing has been run yet.")); return false; }
    const tbody = h("tbody");
    results.forEach((r) => tbody.appendChild(h("tr", {}, [
      h("td", {}, fmtTime(r.started_at)),
      h("td", {}, r.scenario_name),
      h("td", {}, badge(r.mode, r.mode)),
      h("td", {}, r.state === "running" ? badge("running") : badge(r.verdict || "error", r.verdict)),
      h("td", {}, h("button", { class: "btn secondary", onclick: () => showResult(r.id) }, "View")),
    ])));
    resultsPanel.appendChild(h("table", {}, [
      h("thead", {}, h("tr", {}, ["Started", "Scenario", "Mode", "Verdict", ""].map((t) => h("th", {}, t)))), tbody,
    ]));
    return results.some((r) => r.state === "running");
  }

  // -------------------------------------------------------------- report
  async function showResult(id) {
    clearTimeout(state.pollTimer);
    let r;
    try { r = await api(`/testdesk/results/${id}`); }
    catch (err) { detailPanel.style.display = ""; detailPanel.innerHTML = ""; detailPanel.appendChild(errorBox(err)); return; }
    detailPanel.style.display = "";
    renderReport(r);
    if (r.state === "running") {
      state.pollTimer = setTimeout(async () => {
        if (!document.body.contains(detailPanel)) return;  // user navigated away
        await showResult(id);
        await loadResults();
      }, 2000);
    }
  }

  function renderReport(r) {
    const sc = r.scenario || {};
    detailPanel.innerHTML = "";
    const head = [
      r.state === "running" ? badge("running") : badge(r.verdict || "error", r.verdict),
      " ", badge(r.mode, r.mode), " ", h("strong", {}, r.scenario_name),
      h("span", { class: "muted" }, r.finished_at ? `  --  ${Math.max(0, Math.round(r.finished_at - r.started_at))}s` : ""),
    ];
    detailPanel.appendChild(h("div", {}, head));
    detailPanel.appendChild(h("div", { class: "muted", style: "margin:6px 0" }, sc.task || ""));

    if (r.state === "running") {
      detailPanel.appendChild(h("div", { class: "muted" }, r.mode === "test"
        ? "Running in a sandbox… this can take a few minutes if agents are calling the model."
        : "Running… the Run is already visible in the app."));
      return;
    }

    if (r.verdict === "pass") detailPanel.appendChild(h("div", { class: "ok-box" }, r.mode === "test" ? "Passed in the sandbox. Nothing was saved to the app database." : "Passed."));
    if (r.verdict === "awaiting_human") detailPanel.appendChild(h("div", { class: "warn-box" }, "Stopped at a human-approval gate. Approve it from the Run page to continue."));
    if (r.verdict === "error") detailPanel.appendChild(h("div", { class: "error-box" }, "TestDesk could not carry this scenario out: " + (r.error || "unknown error")));
    else if (r.verdict === "fail" && r.error) detailPanel.appendChild(errorBox(r.error));

    if ((r.checks || []).length) {
      detailPanel.appendChild(h("h2", {}, "Checks"));
      detailPanel.appendChild(h("ul", { class: "checks" }, r.checks.map((c) =>
        h("li", { class: c.ok ? "ok" : "bad" }, [h("span", { class: "mark" }, c.ok ? "✓" : "✗"), " ", c.name, h("span", { class: "muted" }, c.detail ? ` -- ${c.detail}` : "")]))));
    }
    if (r.final_answer) {
      detailPanel.appendChild(h("h2", {}, "Final answer"));
      detailPanel.appendChild(h("pre", { class: "answer" }, r.final_answer));
    }
    if ((r.approvals || []).length) detailPanel.appendChild(h("div", { class: "muted" }, "Auto-approved gates: " + r.approvals.join(", ")));
    if (r.sandbox_path) detailPanel.appendChild(h("div", { class: "muted" }, ["Sandbox kept at ", h("code", { class: "inline" }, r.sandbox_path)]));
    if (r.run_id) detailPanel.appendChild(h("div", { style: "margin-top:8px" }, h("a", { href: `#/jobs/run/${r.run_id}` }, "Open the Run in the app →")));

    if ((r.events || []).length) {
      const rows = r.events.map((e) => h("tr", {}, [
        h("td", { class: "muted" }, fmtTime(e.ts)), h("td", {}, badge(e.level || "INFO", `level-${e.level || "INFO"}`)),
        h("td", {}, e.agent_name || ""), h("td", {}, e.event_type || e.node || ""),
        h("td", { class: "muted" }, (e.payload || "").slice(0, 160)),
      ]));
      detailPanel.appendChild(h("details", { style: "margin-top:12px" }, [
        h("summary", {}, `Run events (${r.events.length})`),
        h("table", {}, [h("thead", {}, h("tr", {}, ["Time", "Level", "Agent", "Event", "Detail"].map((t) => h("th", {}, t)))), h("tbody", {}, rows)]),
      ]));
    }

    if (r.mode === "test" && r.verdict === "pass" && sc.target_type === "job" && !sc.job_yaml) {
      detailPanel.appendChild(h("div", { style: "margin-top:14px" }, [
        h("button", { class: "btn secondary", onclick: () => loadIntoForm(sc, "prod") }, "Looks good -- set up a real (prod) run"),
      ]));
    }
  }

  // ---------------------------------------------------------------- boot
  fillTargets(); syncForm();
  try {
    await loadSaved();
    const anyRunning = await loadResults();
    if (anyRunning) {
      const { results } = await api("/testdesk/results");
      const running = results.find((r) => r.state === "running");
      if (running) await showResult(running.id);
    }
  } catch (err) { main.appendChild(errorBox(err)); }
}
