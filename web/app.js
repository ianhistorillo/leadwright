(() => {
  const form = document.getElementById("run-form");
  const fileInput = document.getElementById("csv-file");
  const fileName = document.getElementById("file-name");
  const dropzone = document.getElementById("dropzone");
  const submitBtn = document.getElementById("submit-btn");
  const statusPanel = document.getElementById("status-panel");
  const statusPill = document.getElementById("status-pill");
  const statusText = document.getElementById("status-text");
  const meterFill = document.getElementById("meter-fill");
  const stats = document.getElementById("stats");
  const results = document.getElementById("results");
  const resultsBody = document.getElementById("results-body");
  const exportLinks = document.getElementById("export-links");
  const tabs = document.querySelectorAll(".tab");
  const pipelineMode = document.getElementById("pipeline_mode");
  const modeHint = document.getElementById("mode-hint");
  const resumeBtn = document.getElementById("resume-btn");

  const JOB_KEY = "leadwright_last_job_id";

  let currentTab = "guessed";
  let latestView = null;
  let pollTimer = null;
  let streamAbort = null;

  const apiKey = localStorage.getItem("leadwright_api_key") || "";

  const MODE_HINTS = {
    search_only_guess:
      "Search-only: Apollo finds people (0 credits), then guesses emails. Companies run one-by-one. Higher max pages = more people, slower runs. ZeroBounce is off.",
    search_and_enrich:
      "Apollo emails mode: search + enrichment (~1 credit per email found). Uses Apollo verified status only — ZeroBounce is off.",
  };

  function headers() {
    const h = {};
    if (apiKey) h["X-API-Key"] = apiKey;
    return h;
  }

  function saveJobId(jobId) {
    localStorage.setItem(JOB_KEY, jobId);
    syncResumeButton();
  }

  function lastJobId() {
    return localStorage.getItem(JOB_KEY) || "";
  }

  function syncResumeButton() {
    if (!resumeBtn) return;
    resumeBtn.hidden = !lastJobId();
  }

  function syncModeUi() {
    if (modeHint) {
      modeHint.textContent =
        MODE_HINTS[pipelineMode.value] || MODE_HINTS.search_only_guess;
    }
  }
  pipelineMode.addEventListener("change", syncModeUi);
  syncModeUi();
  syncResumeButton();

  async function refreshActiveTitles() {
    const label = document.getElementById("active-titles-label");
    if (!label) return;
    try {
      const res = await fetch("/ui/config/target-job-titles", { headers: headers() });
      if (!res.ok) throw new Error("failed");
      const data = await res.json();
      const titles = Array.isArray(data.target_job_titles) ? data.target_job_titles : [];
      label.textContent = titles.length ? titles.join(", ") : "(none — check .env)";
    } catch {
      label.textContent = "(could not load — hard-refresh or restart server)";
    }
  }
  refreshActiveTitles();

  fileInput.addEventListener("change", () => {
    fileName.textContent = fileInput.files?.[0]?.name || "No file selected";
  });

  ["dragenter", "dragover"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    });
  });
  dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    fileName.textContent = file.name;
  });

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      currentTab = tab.dataset.tab;
      tabs.forEach((t) => t.classList.toggle("active", t === tab));
      if (latestView) renderPeople(latestView);
    });
  });

  if (resumeBtn) {
    resumeBtn.addEventListener("click", () => {
      const id = lastJobId();
      if (!id) return;
      statusPanel.hidden = false;
      setStatus("running", `Resuming job ${id}…`);
      startLiveUpdates(id, { resumed: true, continueJob: true });
    });
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!fileInput.files?.length) return;

    submitBtn.disabled = true;
    statusPanel.hidden = false;
    results.hidden = true;
    setStatus("running", "Uploading CSV and starting discovery…");
    meterFill.style.width = "5%";

    const mode = pipelineMode.value;
    const body = new FormData();
    body.append("file", fileInput.files[0]);
    body.append("job_title_profile", "custom");
    body.append("pipeline_mode", mode);
    body.append("max_pages", document.getElementById("max_pages").value);
    body.append(
      "skip_existing_people",
      document.getElementById("skip_existing_people")?.checked ? "true" : "false"
    );
    body.append("run_external_verification", "false");
    body.append("job_name", `ui-${new Date().toISOString().slice(0, 19)}`);

    try {
      const res = await fetch("/ui/quick-run", {
        method: "POST",
        headers: headers(),
        body,
      });
      const data = await res.json();
      if (!res.ok) {
        const detail = data.detail;
        const message =
          typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
              : "Quick run failed";
        throw new Error(message);
      }

      const importSummary = data.import_summary;
      const modeLabel =
        data.pipeline_mode === "search_and_enrich"
          ? "search + Apollo emails"
          : "search-only + email guess";
      const newOnlyLabel = data.skip_existing_people ? ", new people only" : "";
      saveJobId(data.job_id);
      const titles = Array.isArray(data.job_titles) ? data.job_titles : [];
      setStatus(
        "running",
        `Imported ${importSummary.valid_companies} new companies` +
          (importSummary.existing_domains_skipped
            ? ` (${importSummary.existing_domains_skipped} already known)`
            : "") +
          `. Job ${data.job_id} running (${modeLabel}${newOnlyLabel})` +
          (titles.length ? `. Titles: ${titles.join(", ")}` : "") +
          `…`
      );
      startLiveUpdates(data.job_id);
    } catch (err) {
      setStatus("failed", err.message || String(err));
      submitBtn.disabled = false;
    }
  });

  function stopLiveUpdates() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (streamAbort) {
      streamAbort.abort();
      streamAbort = null;
    }
  }

  function applyView(view) {
    latestView = view;
    renderProgress(view);
    renderPeople(view);
    results.hidden = false;

    const status = view.progress?.job?.status;
    if (["completed", "failed", "cancelled"].includes(status)) {
      stopLiveUpdates();
      submitBtn.disabled = false;
      if (status === "completed") {
        setStatus("completed", "Run finished. Review results below.");
      } else {
        setStatus(status, `Run ended with status: ${status}`);
      }
      return true;
    }
    return false;
  }

  function startLiveUpdates(jobId, { resumed = false, continueJob = false } = {}) {
    stopLiveUpdates();
    submitBtn.disabled = true;
    statusPanel.hidden = false;
    if (resumed) {
      setStatus(
        "running",
        continueJob
          ? `Resuming job ${jobId} and restarting worker if needed…`
          : `Watching job ${jobId}…`
      );
    }

    streamAbort = new AbortController();
    const signal = streamAbort.signal;

    (async () => {
      try {
        if (continueJob) {
          const cont = await fetch(`/ui/jobs/${jobId}/continue`, {
            method: "POST",
            headers: headers(),
            signal,
          });
          const contBody = await cont.json().catch(() => ({}));
          if (!cont.ok) {
            throw new Error(contBody.detail || `Could not resume job (${cont.status})`);
          }
          if (contBody.worker_started) {
            setStatus("running", `Job ${jobId} worker restarted — processing continues…`);
          } else if (["completed", "failed", "cancelled"].includes(contBody.status)) {
            setStatus(contBody.status, `Job already ${contBody.status}. Loading results…`);
          }
        }

        const res = await fetch(`/ui/jobs/${jobId}/stream`, {
          headers: { ...headers(), Accept: "text/event-stream" },
          signal,
        });
        if (!res.ok) {
          const errBody = await res.json().catch(() => ({}));
          throw new Error(errBody.detail || `Stream failed (${res.status})`);
        }
        if (!res.body) throw new Error("No stream body");

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let eventName = "message";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let sep;
          while ((sep = buffer.indexOf("\n\n")) !== -1) {
            const chunk = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            let dataLines = [];
            eventName = "message";
            for (const rawLine of chunk.split("\n")) {
              const line = rawLine.replace(/\r$/, "");
              if (!line || line.startsWith(":")) continue;
              if (line.startsWith("event:")) {
                eventName = line.slice(6).trim();
              } else if (line.startsWith("data:")) {
                dataLines.push(line.slice(5).trimStart());
              }
            }
            if (!dataLines.length) continue;
            const data = dataLines.join("\n");
            if (eventName === "error") {
              const parsed = JSON.parse(data);
              throw new Error(parsed.detail || "Stream error");
            }
            if (eventName === "view" || eventName === "done" || eventName === "message") {
              const view = JSON.parse(data);
              if (applyView(view) || eventName === "done") return;
            }
          }
        }

        // Stream ended without terminal status — fall back to polling.
        startPollingFallback(jobId);
      } catch (err) {
        if (signal.aborted) return;
        // SSE unavailable — keep UI alive with fast polling.
        console.warn("Live stream unavailable, falling back to polling:", err);
        startPollingFallback(jobId);
      }
    })();
  }

  function startPollingFallback(jobId) {
    if (pollTimer) clearInterval(pollTimer);
    submitBtn.disabled = true;
    statusPanel.hidden = false;

    const tick = async () => {
      try {
        const res = await fetch(`/ui/jobs/${jobId}/view`, { headers: headers() });
        const view = await res.json();
        if (!res.ok) throw new Error(view.detail || "Failed to load job view");
        applyView(view);
      } catch (err) {
        setStatus("failed", err.message || String(err));
        stopLiveUpdates();
        submitBtn.disabled = false;
      }
    };
    tick();
    pollTimer = setInterval(tick, 800);
  }

  function setStatus(kind, text) {
    statusPill.textContent = kind;
    statusPill.className = `pill ${kind}`;
    statusText.textContent = text;
  }

  function activeStageLabel(p) {
    if ((p.searching || 0) > 0) return `searching ${p.searching} company(ies)`;
    if ((p.enriching || 0) > 0) return `enriching ${p.enriching} company(ies)`;
    if ((p.verifying || 0) > 0) return `verifying ${p.verifying} company(ies)`;
    if ((p.pending || 0) > 0) return `${p.pending} waiting`;
    return "working";
  }

  function renderProgress(view) {
    const p = view.progress || {};
    const job = p.job || {};
    const pct = Number(p.percent_complete || 0);
    const running = job.status === "running";
    meterFill.style.width = `${Math.max(pct, running ? 5 : 0)}%`;
    if (running) {
      setStatus(
        "running",
        `Job ${job.id || ""} · ${activeStageLabel(p)} · ${pct}% · people found: ${view.people_found || 0}`
      );
    }
    stats.innerHTML = [
      ["Processed", `${job.processed_companies || 0}/${job.total_companies || 0}`],
      ["Searching", p.searching || 0],
      ["Enriching", p.enriching || 0],
      ["Completed", p.completed || 0],
      ["Failed", p.failed || 0],
      ["People found", view.people_found || 0],
      ["Guessed", (view.guessed_people || []).length],
      ["Accepted", (view.accepted_people || []).length],
    ]
      .map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`)
      .join("");

    if (view.export) {
      const id = job.id;
      exportLinks.innerHTML = `
        <a data-export href="/jobs/${id}/export/accepted_contacts.csv">Download accepted/guessed CSV</a>
        <a data-export href="/jobs/${id}/export/rejected_contacts.csv">Download rejected CSV</a>
        <a data-export href="/jobs/${id}/export/processing_errors.csv">Download errors CSV</a>
      `;
      exportLinks.querySelectorAll("a[data-export]").forEach((anchor) => {
        anchor.addEventListener("click", async (event) => {
          if (!apiKey) return;
          event.preventDefault();
          const res = await fetch(anchor.getAttribute("href"), { headers: headers() });
          if (!res.ok) {
            setStatus("failed", "Could not download export file");
            return;
          }
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const tmp = document.createElement("a");
          tmp.href = url;
          tmp.download = anchor.getAttribute("href").split("/").pop();
          tmp.click();
          URL.revokeObjectURL(url);
        });
      });
    } else {
      exportLinks.innerHTML = "";
    }
  }

  function rowsForTab(view) {
    if (currentTab === "guessed") return view.guessed_people || [];
    if (currentTab === "accepted") return view.accepted_people || [];
    return view.rejected_people || [];
  }

  function renderPeople(view) {
    const rows = rowsForTab(view);
    if (!rows.length) {
      resultsBody.innerHTML = `<tr><td class="empty" colspan="7">No ${currentTab} contacts yet.</td></tr>`;
      return;
    }
    resultsBody.innerHTML = rows
      .map((p) => {
        const company = p.company_domain || p.company_id || "";
        return `<tr>
          <td>${escapeHtml(company)}</td>
          <td>${escapeHtml(p.full_name || "")}</td>
          <td>${escapeHtml(p.job_title || "")}</td>
          <td>${escapeHtml(p.email || "")}</td>
          <td>${escapeHtml(p.email_guess_candidates || "")}</td>
          <td>${escapeHtml(p.apollo_email_status || "")}</td>
          <td>${escapeHtml(p.final_status || "")}</td>
        </tr>`;
      })
      .join("");
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  async function resumeOnLoad() {
    let jobId = lastJobId();
    let jobStatus = "";
    try {
      if (!jobId) {
        const res = await fetch("/ui/jobs/latest", { headers: headers() });
        if (res.ok) {
          const data = await res.json();
          jobId = data.job_id;
          jobStatus = data.status || "";
          if (jobId) saveJobId(jobId);
        }
      } else {
        const res = await fetch(`/jobs/${jobId}`, { headers: headers() });
        if (res.ok) {
          const data = await res.json();
          jobStatus = data.status || "";
        }
      }
    } catch {
      // ignore — no prior job
    }
    if (!jobId) return;

    // Only auto-attach unfinished jobs. Completed/failed/cancelled jobs kept
    // showing old people (often from different TARGET_JOB_TITLES) and looked
    // like the .env change "didn't apply".
    const terminal = ["completed", "failed", "cancelled"];
    if (jobStatus && terminal.includes(jobStatus)) {
      return;
    }
    startLiveUpdates(jobId, {
      resumed: true,
      continueJob: !jobStatus || !terminal.includes(jobStatus),
    });
  }

  resumeOnLoad();

  const backfillForm = document.getElementById("backfill-form");
  const backfillFile = document.getElementById("backfill-file");
  const backfillDropzone = document.getElementById("backfill-dropzone");
  const backfillFileName = document.getElementById("backfill-file-name");
  const backfillBtn = document.getElementById("backfill-btn");
  const backfillStatus = document.getElementById("backfill-status");
  const backfillProgress = document.getElementById("backfill-progress");
  const backfillMeterFill = document.getElementById("backfill-meter-fill");
  const backfillProgressText = document.getElementById("backfill-progress-text");
  const backfillStats = document.getElementById("backfill-stats");

  function setBackfillFile(file) {
    if (!file || !backfillFile) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    backfillFile.files = dt.files;
    if (backfillFileName) backfillFileName.textContent = file.name;
    if (backfillStatus) {
      backfillStatus.textContent = `Ready: ${file.name}. Click “Fill company names”.`;
    }
  }

  function renderBackfillProgress(data) {
    if (backfillProgress) backfillProgress.hidden = false;
    const pct = Number(data.percent || 0);
    if (backfillMeterFill) {
      backfillMeterFill.style.width = `${Math.max(pct, data.phase === "starting" ? 2 : 0)}%`;
    }
    const domain = data.current_domain ? ` · ${data.current_domain}` : "";
    const domainDone = data.domains_searched || 0;
    const domainIdx =
      data.domain_index != null ? data.domain_index : domainDone;
    const domainTotal = data.domains_total || 0;
    if (backfillProgressText) {
      backfillProgressText.textContent =
        `${data.phase || "working"}${domain} · ${pct}% · ` +
        `domain ${domainIdx}/${domainTotal}`;
    }
    if (backfillStats) {
      backfillStats.innerHTML = [
        ["Domains", `${domainDone}/${domainTotal}`],
        ["Filled", data.filled || 0],
        ["Already exact", data.already_exact || 0],
        ["Unresolved", data.unresolved || 0],
        ["Apollo calls", data.apollo_calls || 0],
        ["Rows", data.total_rows || 0],
      ]
        .map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`)
        .join("");
    }
  }

  function downloadCsvBase64(filename, b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    const blob = new Blob([bytes], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "accepted_contacts_apollo_company_names.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  if (backfillFile && backfillFileName) {
    backfillFile.addEventListener("change", () => {
      const file = backfillFile.files?.[0];
      backfillFileName.textContent = file?.name || "No file selected";
      if (file && backfillStatus) {
        backfillStatus.textContent = `Ready: ${file.name}. Click “Fill company names”.`;
      }
    });
  }

  if (backfillDropzone) {
    ["dragenter", "dragover"].forEach((evt) => {
      backfillDropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        backfillDropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((evt) => {
      backfillDropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        backfillDropzone.classList.remove("dragover");
      });
    });
    backfillDropzone.addEventListener("drop", (e) => {
      const file = e.dataTransfer?.files?.[0];
      if (!file) return;
      setBackfillFile(file);
    });
  }

  if (backfillForm) {
    backfillForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!backfillFile?.files?.length) {
        if (backfillStatus) {
          backfillStatus.textContent = "Please choose an accepted_contacts.csv file first.";
        }
        return;
      }

      backfillBtn.disabled = true;
      if (backfillProgress) backfillProgress.hidden = false;
      backfillStatus.textContent =
        "Streaming progress… keep this tab open until download starts.";
      renderBackfillProgress({
        phase: "starting",
        percent: 1,
        domains_searched: 0,
        domains_total: 0,
      });

      const body = new FormData();
      body.append("file", backfillFile.files[0]);
      body.append(
        "max_pages_per_domain",
        document.getElementById("backfill_max_pages").value || "5"
      );

      try {
        const res = await fetch("/ui/backfill-company-names", {
          method: "POST",
          headers: { ...headers(), Accept: "text/event-stream" },
          body,
        });
        if (!res.ok) {
          if (res.status === 404) {
            throw new Error(
              "Backfill API not found on this server. Open http://127.0.0.1:8002/app (not 8001), hard-refresh, and try again."
            );
          }
          const errBody = await res.json().catch(() => ({}));
          const detail = errBody.detail;
          throw new Error(
            typeof detail === "string"
              ? detail
              : `Backfill failed (${res.status})`
          );
        }
        if (!res.body) throw new Error("No progress stream from server");

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let eventName = "message";
        let finished = false;

        while (!finished) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let sep;
          while ((sep = buffer.indexOf("\n\n")) !== -1) {
            const chunk = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const dataLines = [];
            eventName = "message";
            for (const rawLine of chunk.split("\n")) {
              const line = rawLine.replace(/\r$/, "");
              if (!line || line.startsWith(":")) continue;
              if (line.startsWith("event:")) eventName = line.slice(6).trim();
              else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
            }
            if (!dataLines.length) continue;
            const payload = JSON.parse(dataLines.join("\n"));

            if (eventName === "error") {
              throw new Error(payload.detail || "Backfill failed");
            }
            if (eventName === "progress" || eventName === "message") {
              renderBackfillProgress(payload);
              if (payload.current_domain) {
                backfillStatus.textContent = `Searching ${payload.current_domain}…`;
              }
            }
            if (eventName === "done") {
              renderBackfillProgress(payload);
              if (payload.csv_base64) {
                downloadCsvBase64(payload.filename, payload.csv_base64);
              }
              backfillStatus.textContent =
                `Done. Filled ${payload.filled}, already exact ${payload.already_exact}, ` +
                `unresolved ${payload.unresolved} ` +
                `(${payload.domains_searched}/${payload.domains_total} domains, ` +
                `${payload.apollo_calls} Apollo Search calls). Download started.`;
              finished = true;
              break;
            }
          }
        }
        if (!finished) {
          throw new Error("Progress stream ended before completion");
        }
      } catch (err) {
        backfillStatus.textContent = err.message || String(err);
      } finally {
        backfillBtn.disabled = false;
      }
    });
  }

  // --- Contact enrich (names + emails, uses credits) ---
  const enrichForm = document.getElementById("enrich-form");
  const enrichFile = document.getElementById("enrich-file");
  const enrichDropzone = document.getElementById("enrich-dropzone");
  const enrichFileName = document.getElementById("enrich-file-name");
  const enrichBtn = document.getElementById("enrich-btn");
  const enrichStatus = document.getElementById("enrich-status");
  const enrichProgress = document.getElementById("enrich-progress");
  const enrichMeterFill = document.getElementById("enrich-meter-fill");
  const enrichProgressText = document.getElementById("enrich-progress-text");
  const enrichStats = document.getElementById("enrich-stats");

  const enrichResumeBtn = document.getElementById("enrich-resume-btn");
  const enrichDownloadBtn = document.getElementById("enrich-download-btn");
  const enrichResumeHint = document.getElementById("enrich-resume-hint");
  const ENRICH_RUN_KEY = "employeeDiscovery.enrichRunId";
  let resumableEnrichRun = null;

  function setEnrichFile(file) {
    if (!file || !enrichFile) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    enrichFile.files = dt.files;
    if (enrichFileName) enrichFileName.textContent = file.name;
    if (enrichStatus) {
      enrichStatus.textContent = `Ready: ${file.name}. Click “Fill names + emails”.`;
    }
  }

  function renderEnrichProgress(data) {
    if (enrichProgress) enrichProgress.hidden = false;
    if (data.run_id) {
      try {
        localStorage.setItem(ENRICH_RUN_KEY, data.run_id);
      } catch (_) {
        /* ignore */
      }
    }
    const pct = Number(data.percent || 0);
    if (enrichMeterFill) {
      enrichMeterFill.style.width = `${Math.max(pct, data.phase === "starting" ? 2 : 0)}%`;
    }
    const label = data.current_label ? ` · ${data.current_label}` : "";
    if (enrichProgressText) {
      const stopGoal = data.stop_after_valid_emails || 0;
      const validBit =
        stopGoal > 0
          ? ` · valid ${data.valid_emails_found || 0}/${stopGoal}`
          : ` · valid ${data.valid_emails_found || 0}`;
      enrichProgressText.textContent =
        `${data.phase || "working"}${label} · ${pct}%` +
        ` · people ${data.people_done || 0}/${data.enrich_targets || 0}` +
        validBit +
        (data.stopped_early ? " · stopped early" : "");
    }
    if (enrichStats) {
      enrichStats.innerHTML = [
        ["To attempt", data.enrich_targets || 0],
        ["Done", data.people_done || 0],
        ["Valid (verified+name)", data.valid_emails_found || 0],
        ["Credits (API)", data.credits_consumed || 0],
        ["Names updated", data.names_updated || 0],
        ["Emails updated", data.emails_updated || 0],
        ["Skipped (no Apollo email)", data.skipped_no_apollo_email || 0],
        ["Skipped (already done)", data.skipped_already_processed || 0],
        ["Skipped (no CSV email)", data.skipped_no_email || 0],
        ["Skipped (after stop)", data.skipped_after_stop || 0],
        ["Unresolved", data.unresolved || 0],
      ]
        .map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`)
        .join("");
    }
  }

  async function refreshEnrichResume() {
    if (!enrichResumeBtn) return;
    try {
      const res = await fetch("/ui/backfill-contact-enrich/latest", {
        headers: headers(),
      });
      const data = await res.json().catch(() => ({}));
      resumableEnrichRun = data.run || null;
      const saved = localStorage.getItem(ENRICH_RUN_KEY);
      if (!resumableEnrichRun && saved) {
        resumableEnrichRun = { run_id: saved, status: "unknown" };
      }
      if (resumableEnrichRun?.run_id) {
        enrichResumeBtn.hidden = false;
        if (enrichDownloadBtn) enrichDownloadBtn.hidden = false;
        if (enrichResumeHint) {
          enrichResumeHint.hidden = false;
          enrichResumeHint.textContent =
            `Saved run: ${resumableEnrichRun.run_id} ` +
            `(valid ${resumableEnrichRun.valid_emails_found || 0}` +
            `${resumableEnrichRun.stop_after_valid_emails ? `/${resumableEnrichRun.stop_after_valid_emails}` : ""}, ` +
            `status ${resumableEnrichRun.status || "interrupted"}). ` +
            `You can download partial results now, or resume later when you have credits.`;
        }
      } else {
        enrichResumeBtn.hidden = true;
        if (enrichDownloadBtn) enrichDownloadBtn.hidden = true;
        if (enrichResumeHint) enrichResumeHint.hidden = true;
      }
    } catch (_) {
      /* ignore */
    }
  }

  async function runContactEnrich({ resume = false } = {}) {
    const maxPeople = Number(document.getElementById("enrich_max_people")?.value || 3500);
    const stopAfter = Number(document.getElementById("enrich_stop_after")?.value || 0);

    if (!resume && !enrichFile?.files?.length) {
      if (enrichStatus) {
        enrichStatus.textContent = "Please choose an accepted_contacts.csv file first.";
      }
      return;
    }
    if (resume && !resumableEnrichRun?.run_id) {
      if (enrichStatus) enrichStatus.textContent = "No resumable enrich run found.";
      return;
    }

    const confirmed = window.confirm(
      resume
        ? `Resume enrich run ${resumableEnrichRun.run_id}?\n\n` +
            `Already-processed people are skipped so you do not re-spend credits.\n` +
            `Stop after valid emails: ${stopAfter || "keep previous / none"}`
        : `This uses Apollo enrichment credits (~1 credit per person when data is found).\n\n` +
            `Max people to attempt: ${maxPeople}\n` +
            `Stop after valid emails: ${stopAfter || "no early stop"}\n\n` +
            `Progress is checkpointed every batch so you can resume if it stops.`
    );
    if (!confirmed) return;

    enrichBtn.disabled = true;
    if (enrichResumeBtn) enrichResumeBtn.disabled = true;
    if (enrichProgress) enrichProgress.hidden = false;
    enrichStatus.textContent = resume
      ? `Resuming ${resumableEnrichRun.run_id}… already-done people are skipped.`
      : "Enriching… keep this tab open until download starts.";
    renderEnrichProgress({
      phase: resume ? "resuming" : "starting",
      percent: 1,
      people_done: 0,
      enrich_targets: 0,
    });

    const body = new FormData();
    if (resume) {
      body.append("resume_run_id", resumableEnrichRun.run_id);
    } else {
      body.append("file", enrichFile.files[0]);
    }
    body.append("max_people", String(maxPeople));
    body.append("stop_after_valid_emails", String(stopAfter));
    body.append(
      "max_pages_per_domain",
      document.getElementById("enrich_max_pages")?.value || "5"
    );
    body.append(
      "only_incomplete",
      document.getElementById("enrich_only_incomplete")?.checked ? "true" : "false"
    );
    body.append(
      "only_enrich_if_has_email",
      document.getElementById("enrich_only_has_email")?.checked ? "true" : "false"
    );

    try {
      const res = await fetch("/ui/backfill-contact-enrich", {
        method: "POST",
        headers: { ...headers(), Accept: "text/event-stream" },
        body,
      });
      if (!res.ok) {
        if (res.status === 404) {
          throw new Error(
            "Enrich API not found on this server. Open http://127.0.0.1:8002/app, hard-refresh, and try again."
          );
        }
        const errBody = await res.json().catch(() => ({}));
        const detail = errBody.detail;
        throw new Error(
          typeof detail === "string" ? detail : `Enrich failed (${res.status})`
        );
      }
      if (!res.body) throw new Error("No progress stream from server");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let eventName = "message";
      let finished = false;

      while (!finished) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let sep;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const chunk = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const dataLines = [];
          eventName = "message";
          for (const rawLine of chunk.split("\n")) {
            const line = rawLine.replace(/\r$/, "");
            if (!line || line.startsWith(":")) continue;
            if (line.startsWith("event:")) eventName = line.slice(6).trim();
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
          }
          if (!dataLines.length) continue;
          const payload = JSON.parse(dataLines.join("\n"));

          if (eventName === "error") {
            throw new Error(payload.detail || "Enrich failed");
          }
          if (eventName === "progress" || eventName === "message") {
            renderEnrichProgress(payload);
            if (payload.current_label) {
              enrichStatus.textContent = `${payload.phase || "working"}: ${payload.current_label}`;
            }
          }
          if (eventName === "done") {
            renderEnrichProgress(payload);
            if (payload.csv_base64) {
              downloadCsvBase64(payload.filename, payload.csv_base64);
            }
            enrichStatus.textContent =
              `Done${payload.stopped_early ? " (stopped early)" : ""}. ` +
              `Valid emails ${payload.valid_emails_found || 0}` +
              `${payload.stop_after_valid_emails ? `/${payload.stop_after_valid_emails}` : ""}. ` +
              `Names updated ${payload.names_updated}, emails updated ${payload.emails_updated}, ` +
              `skipped already-done ${payload.skipped_already_processed || 0}, ` +
              `from DB ${payload.filled_from_db || 0}. ` +
              `API credits reported: ${payload.credits_consumed || 0}. Download started.`;
            finished = true;
            break;
          }
        }
      }
      if (!finished) {
        throw new Error("Progress stream ended before completion — use Resume if a checkpoint was saved.");
      }
    } catch (err) {
      enrichStatus.textContent = err.message || String(err);
      await refreshEnrichResume();
    } finally {
      enrichBtn.disabled = false;
      if (enrichResumeBtn) enrichResumeBtn.disabled = false;
      await refreshEnrichResume();
    }
  }

  if (enrichFile && enrichFileName) {
    enrichFile.addEventListener("change", () => {
      const file = enrichFile.files?.[0];
      enrichFileName.textContent = file?.name || "No file selected";
      if (file && enrichStatus) {
        enrichStatus.textContent = `Ready: ${file.name}. Click “Fill names + emails”.`;
      }
    });
  }

  if (enrichDropzone) {
    ["dragenter", "dragover"].forEach((evt) => {
      enrichDropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        enrichDropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((evt) => {
      enrichDropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        enrichDropzone.classList.remove("dragover");
      });
    });
    enrichDropzone.addEventListener("drop", (e) => {
      const file = e.dataTransfer?.files?.[0];
      if (!file) return;
      setEnrichFile(file);
    });
  }

  if (enrichForm) {
    enrichForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await runContactEnrich({ resume: false });
    });
  }
  if (enrichResumeBtn) {
    enrichResumeBtn.addEventListener("click", async () => {
      await runContactEnrich({ resume: true });
    });
  }
  if (enrichDownloadBtn) {
    enrichDownloadBtn.addEventListener("click", () => {
      const runId = resumableEnrichRun?.run_id || localStorage.getItem(ENRICH_RUN_KEY);
      if (!runId) {
        if (enrichStatus) enrichStatus.textContent = "No saved enrich run to download.";
        return;
      }
      const a = document.createElement("a");
      a.href = `/ui/backfill-contact-enrich/${encodeURIComponent(runId)}/download`;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      if (enrichStatus) {
        enrichStatus.textContent =
          `Downloading partial checkpoint for ${runId}. Unenriched rows stay as they were.`;
      }
    });
  }
  refreshEnrichResume();
})();
