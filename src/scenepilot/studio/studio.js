(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const state = {
    mode: "script",
    jobId: null,
    job: null,
    scenes: [],
    selectedSceneId: null,
    selectedRating: null,
    pollingJobId: null,
    availableLinks: {},
  };

  const stageLabels = [
    ["queued", "Queued"],
    ["planning", "Scene planning"],
    ["retrieval", "Footage & rights"],
    ["render", "Render & validate"],
  ];
  const ratioNames = { "16:9": "Landscape", "9:16": "Portrait", "1:1": "Square" };
  const inputTabs = [...document.querySelectorAll(".mode-tab")];

  function toast(message) {
    const node = byId("toast");
    node.textContent = message;
    node.classList.add("visible");
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => node.classList.remove("visible"), 3200);
  }

  function setAlert(message = "") {
    const node = byId("form-alert");
    node.hidden = !message;
    node.textContent = message;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data?.error;
      throw new Error(detail?.message || `Request failed (${response.status}).`);
    }
    return data;
  }

  function setMode(mode) {
    state.mode = mode;
    for (const tab of inputTabs) {
      const active = tab.dataset.mode === mode;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      byId(`mode-${tab.dataset.mode}`).hidden = !active;
    }
    const required = {
      script: "script-input",
      scenes: "scenes-input",
      audio: "audio-file",
      url: "audio-url",
    };
    for (const id of Object.values(required)) byId(id).required = false;
    byId(required[mode]).required = true;
    setAlert("");
  }

  function checkedRatios() {
    return [...document.querySelectorAll('input[name="ratio"]:checked')].map(
      (input) => input.value,
    );
  }

  function makeIdempotencyKey() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return `ff-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function requestOptions() {
    return {
      voice: byId("voice-select").value,
      aspect_ratios: checkedRatios(),
      style: byId("style-input").value.trim(),
      avoid: byId("avoid-input").value.split(",").map((item) => item.trim()).filter(Boolean),
      captions: { burn_in: byId("captions-toggle").checked, srt: true },
    };
  }

  function buildSubmission() {
    const options = requestOptions();
    if (!options.aspect_ratios.length) throw new Error("Choose at least one output format.");
    if (state.mode === "script") {
      const script = byId("script-input").value.trim();
      if (!script) throw new Error("Add a script before submitting.");
      return {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...options, script }),
      };
    }
    if (state.mode === "scenes") {
      let scenes;
      try {
        scenes = JSON.parse(byId("scenes-input").value);
      } catch {
        throw new Error("Scene list must be valid JSON.");
      }
      if (!Array.isArray(scenes) || !scenes.length) {
        throw new Error("Provide a non-empty JSON array of scenes.");
      }
      if (scenes.some((scene) => !scene || typeof scene.text !== "string" || !scene.text.trim())) {
        throw new Error("Every scene must contain non-empty text.");
      }
      return {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...options, scenes }),
      };
    }
    if (state.mode === "url") {
      const audioUrl = byId("audio-url").value.trim();
      if (!audioUrl) throw new Error("Enter an audio URL.");
      return {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...options, audio_url: audioUrl }),
      };
    }
    const file = byId("audio-file").files[0];
    if (!file) throw new Error("Choose an audio file.");
    const form = new FormData();
    form.append("file", file);
    form.append("voice", options.voice);
    form.append("aspect_ratios", JSON.stringify(options.aspect_ratios));
    form.append("style", options.style);
    form.append("avoid", JSON.stringify(options.avoid));
    form.append("captions", JSON.stringify(options.captions));
    return { body: form };
  }

  async function submitJob(event) {
    event.preventDefault();
    setAlert("");
    const submitButton = byId("create-job");
    submitButton.disabled = true;
    submitButton.querySelector("span").textContent = "Creating plan…";
    try {
      const submission = buildSubmission();
      const job = await api("/api/v1/broll/jobs", {
        method: "POST",
        headers: { ...submission.headers, "Idempotency-Key": makeIdempotencyKey() },
        body: submission.body,
      });
      state.jobId = job.job_id;
      state.job = job;
      state.scenes = [];
      state.selectedSceneId = null;
      state.availableLinks = {};
      history.replaceState(null, "", `/?job_id=${encodeURIComponent(job.job_id)}`);
      byId("project-title").textContent = projectName();
      byId("refresh-job").disabled = false;
      byId("evidence-empty").hidden = true;
      byId("evidence-content").hidden = false;
      byId("evidence-job-id").textContent = job.job_id;
      clearSceneViews();
      updateJob(job);
      pollJob(job.job_id);
    } catch (error) {
      setAlert(error.message || "The request could not be submitted.");
    } finally {
      submitButton.disabled = false;
      submitButton.querySelector("span").textContent = "Create scene plan";
    }
  }

  function projectName() {
    if (state.mode === "script") {
      const words = byId("script-input").value.trim().split(/\s+/).slice(0, 5).join(" ");
      return words ? `${words}${byId("script-input").value.trim().split(/\s+/).length > 5 ? "…" : ""}` : "Untitled production";
    }
    if (state.mode === "scenes") return "Structured scene production";
    if (state.mode === "audio") return byId("audio-file").files[0]?.name || "Audio production";
    return "Audio URL production";
  }

  async function pollJob(jobId) {
    if (state.pollingJobId === jobId) return;
    state.pollingJobId = jobId;
    while (state.pollingJobId === jobId && state.jobId === jobId) {
      try {
        const job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
        if (state.pollingJobId !== jobId || state.jobId !== jobId) return;
        updateJob(job);
        if (job.status === "succeeded" || job.status === "failed") {
          state.pollingJobId = null;
          return;
        }
      } catch (error) {
        if (state.pollingJobId !== jobId || state.jobId !== jobId) return;
        byId("progress-label").textContent = `Status refresh failed: ${error.message}`;
        state.pollingJobId = null;
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1300));
    }
  }

  function updateJob(job) {
    state.job = job;
    state.jobId = job.job_id || state.jobId;
    const status = job.status || "queued";
    const progress = Number.isFinite(job.progress) ? Math.max(0, Math.min(100, job.progress)) : 0;
    const badge = byId("job-badge");
    badge.className = `status-badge status-${status}`;
    byId("job-status-text").textContent = status[0].toUpperCase() + status.slice(1);
    byId("progress-number").textContent = `${progress}%`;
    byId("progress-fill").style.width = `${progress}%`;
    document.querySelector(".progress-track").setAttribute("aria-valuenow", String(progress));
    const stageText = {
      queued: "Waiting for a worker to claim this job",
      running: "Worker is processing this job",
      succeeded: job.result_links && Object.keys(job.result_links).some((key) => key.includes(":"))
        ? "Validated output artifacts are ready"
        : "Job finished; inspect warnings and results",
      failed: job.error?.message || "The job failed; inspect the error and warnings below",
    };
    byId("progress-label").textContent = stageText[status] || "Job status unavailable";
    const videoReady = Object.keys(job.result_links || {}).some((key) => ["16:9", "9:16", "1:1"].includes(key));
    const hasPlan = Boolean(job.scene_plan);
    const retrievalAttempted = hasPlan && (job.scene_plan.scenes || []).some((scene) => scene.visual_retrieval);
    const confirmed = [status === "queued", hasPlan, retrievalAttempted, videoReady];
    for (const [index, [key]] of stageLabels.entries()) {
      const element = document.querySelector(`[data-stage="${key}"]`);
      element.className = "pipeline-step";
      if (confirmed[index]) {
        element.classList.add("complete");
      } else if (status === "queued" && index === 0) {
        element.classList.add("active");
      } else if (status === "running" && index === 1 && !hasPlan) {
        element.classList.add("active");
      } else if (status === "failed" && index === 0 && !hasPlan) {
        element.classList.add("failed");
      }
    }
    renderJobEvidence(job);
    if (job.scene_plan) renderScenePlan(job.scene_plan, job);
    if (job.result_links) renderArtifacts(job.result_links);
    if (status === "failed") {
      const warning = job.error?.message;
      if (warning && !(job.warnings || []).includes(warning)) {
        renderWarnings([warning, ...(job.warnings || [])]);
      }
    }
  }

  function renderJobEvidence(job) {
    byId("evidence-job-id").textContent = job.job_id || state.jobId || "";
    renderWarnings(job.warnings || []);
    byId("scene-json").textContent = job.scene_plan ? JSON.stringify(job.scene_plan, null, 2) : "No scene plan was returned.";
    byId("evidence-content").hidden = false;
    byId("evidence-empty").hidden = true;
  }

  function renderWarnings(warnings) {
    const list = byId("warning-list");
    list.replaceChildren();
    for (const warning of warnings) {
      const item = document.createElement("div");
      item.className = "warning-item";
      item.textContent = warning;
      list.append(item);
    }
  }

  function renderArtifacts(links) {
    state.availableLinks = links || {};
    const list = byId("artifact-list");
    list.replaceChildren();
    const labels = { "16:9": "MP4 · 16:9", "9:16": "MP4 · 9:16", "1:1": "MP4 · 1:1", captions: "Captions · SRT", timeline: "Timeline · JSON", manifest: "Manifest · JSON" };
    for (const [key, url] of Object.entries(state.availableLinks)) {
      if (typeof url !== "string" || !url.startsWith("/api/v1/jobs/")) continue;
      const link = document.createElement("a");
      link.className = "artifact-link";
      link.href = url;
      link.textContent = labels[key] || key;
      link.setAttribute("download", "");
      if (key === "16:9" || key === "9:16" || key === "1:1") link.addEventListener("click", (event) => {
        event.preventDefault();
        loadPreview(key, url);
      });
      list.append(link);
    }
    const preferred = byId("preview-format").value;
    if (state.availableLinks[preferred]) loadPreview(preferred, state.availableLinks[preferred]);
    const hasLandscape = Boolean(state.availableLinks["16:9"]);
    const hasPortrait = Boolean(state.availableLinks["9:16"]);
    byId("comparison-row").hidden = !(hasLandscape && hasPortrait);
    byId("compare-button").disabled = !(hasLandscape && hasPortrait);
    if (hasLandscape && hasPortrait) byId("comparison-message").textContent = "Both generated files are available for side-by-side review.";
  }

  function loadPreview(ratio, url) {
    const player = byId("video-player");
    const frame = byId("preview-frame");
    frame.classList.toggle("portrait", ratio === "9:16");
    frame.classList.toggle("square", ratio === "1:1");
    player.src = url;
    player.hidden = false;
    byId("preview-empty").hidden = true;
    byId("video-overlay-label").hidden = false;
    byId("video-format-label").textContent = `${ratio} · ${ratioNames[ratio]}`;
    byId("preview-meta-text").textContent = `Validated MP4 · ${ratio} · ${url.split("/").pop()}`;
    byId("preview-duration").textContent = state.job?.narration_duration_seconds
      ? `${Number(state.job.narration_duration_seconds).toFixed(2)} sec`
      : "";
  }

  function clearSceneViews() {
    state.scenes = [];
    byId("storyboard").replaceChildren();
    byId("storyboard").hidden = true;
    byId("storyboard-empty").hidden = false;
    byId("timeline-track").replaceChildren();
    byId("timeline-track").hidden = true;
    byId("timeline-ruler").hidden = true;
    byId("caption-track").hidden = true;
    byId("timeline-empty").hidden = false;
    byId("scene-count").textContent = "0";
    byId("inspector-content").hidden = true;
    byId("inspector-empty").hidden = false;
    byId("inspector-title").textContent = "Select a scene";
    byId("preview-frame").classList.remove("portrait", "square");
    byId("video-player").pause();
    byId("video-player").removeAttribute("src");
    byId("video-player").load();
    byId("video-player").hidden = true;
    byId("preview-empty").hidden = false;
    byId("video-overlay-label").hidden = true;
    byId("preview-meta-text").textContent = "No media artifact attached";
    byId("preview-duration").textContent = "";
    byId("comparison-row").hidden = true;
    byId("comparison-players").hidden = true;
  }

  function renderScenePlan(plan, job) {
    const scenes = Array.isArray(plan.scenes) ? plan.scenes : [];
    state.scenes = scenes;
    byId("scene-count").textContent = String(scenes.length);
    if (!scenes.length) {
      clearSceneViews();
      return;
    }
    byId("storyboard-empty").hidden = true;
    byId("storyboard").hidden = false;
    byId("timeline-empty").hidden = true;
    byId("timeline-track").hidden = false;
    byId("timeline-ruler").hidden = false;
    byId("caption-track").hidden = false;
    byId("timeline-scale").textContent = `${Number(plan.duration_seconds || 0).toFixed(2)} sec total narration`;
    const maxDuration = Number(plan.duration_seconds) || Math.max(...scenes.map((scene) => Number(scene.end_seconds) || 0), 1);
    byId("timeline-ruler").replaceChildren();
    for (const mark of [0, 0.25, 0.5, 0.75, 1]) {
      const tick = document.createElement("span");
      tick.textContent = `${(maxDuration * mark).toFixed(mark === 0 ? 0 : 1)}s`;
      byId("timeline-ruler").append(tick);
    }
    const track = byId("timeline-track");
    const captionItems = byId("caption-items");
    const board = byId("storyboard");
    track.replaceChildren();
    captionItems.replaceChildren();
    board.replaceChildren();
    scenes.forEach((scene, index) => {
      const duration = Math.max(0.01, Number(scene.duration_seconds) || Number(scene.end_seconds) - Number(scene.start_seconds) || 0.01);
      const share = Math.max(duration / maxDuration * 100, 2);
      const segment = document.createElement("button");
      segment.type = "button";
      segment.className = "timeline-segment";
      segment.style.setProperty("--segment-share", String(share));
      segment.setAttribute("aria-label", `Scene ${index + 1}, ${Number(scene.start_seconds).toFixed(2)} to ${Number(scene.end_seconds).toFixed(2)} seconds`);
      const segmentId = document.createElement("strong");
      segmentId.textContent = `S${String(index + 1).padStart(2, "0")}`;
      const segmentTime = document.createElement("small");
      segmentTime.textContent = `${duration.toFixed(1)}s`;
      segment.append(segmentId, segmentTime);
      segment.addEventListener("click", () => selectScene(scene.scene_id));
      track.append(segment);

      const caption = document.createElement("span");
      caption.className = "caption-item";
      caption.style.setProperty("--segment-share", String(share));
      caption.title = scene.narration_text || "";
      caption.textContent = scene.narration_text || "No caption text";
      captionItems.append(caption);

      const card = document.createElement("button");
      card.type = "button";
      card.className = "story-card";
      card.setAttribute("role", "listitem");
      card.setAttribute("aria-label", `Inspect scene ${index + 1}`);
      card.dataset.sceneId = scene.scene_id || "";
      const top = document.createElement("div");
      top.className = "story-card-top";
      const indexLabel = document.createElement("span");
      indexLabel.textContent = `SCENE ${String(index + 1).padStart(2, "0")}`;
      const startEnd = document.createElement("span");
      startEnd.textContent = `${Number(scene.start_seconds).toFixed(1)}–${Number(scene.end_seconds).toFixed(1)}s`;
      const concept = document.createElement("h3");
      concept.textContent = scene.intent_graph?.central_concept || scene.narration_text || "Scene";
      const description = document.createElement("p");
      description.textContent = scene.intent_graph?.intent || "No intent details were returned.";
      const retrieval = activeRetrieval(scene);
      const match = document.createElement("div");
      match.className = "story-rights";
      match.textContent = retrieval?.rights_passport
        ? `${retrieval.rights_passport.provider} · retrieval score ${formatScore(retrieval.overall_score)}`
        : retrieval?.status || "Footage not selected";
      top.append(indexLabel, startEnd);
      card.append(top, concept, description, match);
      card.addEventListener("click", () => selectScene(scene.scene_id));
      board.append(card);
    });
    if (!state.selectedSceneId || !scenes.some((scene) => scene.scene_id === state.selectedSceneId)) {
      selectScene(scenes[0].scene_id);
    } else {
      selectScene(state.selectedSceneId);
    }
    void job;
  }

  function activeRetrieval(scene) {
    const choices = scene.visual_retrieval?.by_aspect_ratio || {};
    return choices[byId("preview-format").value] || Object.values(choices).find((choice) => choice?.rights_passport) || Object.values(choices)[0] || null;
  }

  function formatScore(score) {
    return typeof score === "number" && Number.isFinite(score) ? `${(score * 100).toFixed(0)}%` : "not scored";
  }

  function selectScene(sceneId) {
    const scene = state.scenes.find((item) => item.scene_id === sceneId);
    if (!scene) return;
    state.selectedSceneId = sceneId;
    state.selectedRating = null;
    byId("rating-rationale").value = "";
    byId("submit-rating").disabled = true;
    byId("rating-feedback").textContent = "";
    document.querySelectorAll(".story-card").forEach((card) => card.classList.toggle("selected", card.dataset.sceneId === sceneId));
    const timelineSegments = [...document.querySelectorAll(".timeline-segment")];
    const index = state.scenes.indexOf(scene);
    timelineSegments.forEach((segment, segmentIndex) => segment.classList.toggle("selected", segmentIndex === index));
    byId("inspector-empty").hidden = true;
    byId("inspector-content").hidden = false;
    byId("inspector-title").textContent = `Scene ${String(index + 1).padStart(2, "0")}`;
    byId("scene-narration").textContent = scene.narration_text || "Narration text unavailable.";
    byId("scene-time").textContent = `${Number(scene.start_seconds).toFixed(2)}s → ${Number(scene.end_seconds).toFixed(2)}s · ${(Number(scene.end_seconds) - Number(scene.start_seconds)).toFixed(2)}s`;
    const intent = scene.intent_graph || {};
    byId("scene-concept").textContent = intent.central_concept || "Meaning not provided";
    byId("scene-intent").textContent = intent.intent || intent.interpretation || "No intent explanation was returned.";
    const entities = [...(intent.entities || []), ...(intent.actions || [])];
    const entityList = byId("scene-entities");
    entityList.replaceChildren();
    for (const entity of entities.slice(0, 10)) {
      const tag = document.createElement("span");
      tag.textContent = entity;
      entityList.append(tag);
    }
    const retrieval = activeRetrieval(scene);
    const passport = retrieval?.rights_passport;
    const score = retrieval?.overall_score;
    byId("scene-score").textContent = formatScore(score);
    byId("score-bar-fill").style.width = typeof score === "number" ? `${Math.max(0, Math.min(1, score)) * 100}%` : "0";
    byId("scene-rationale").textContent = retrieval?.selection_rationale?.join("; ") || "No selected-asset rationale was returned.";
    const independent = retrieval?.independent_relevance_evaluation;
    byId("independent-score").textContent = independent?.status === "evaluated"
      ? `${formatScore(independent.score)} · ${independent.model || independent.provider}`
      : independent?.status === "unavailable" ? "Unavailable" : "Not configured";
    byId("independent-rationale").textContent = independent?.rationale || "No independent frame-based evaluation was returned.";
    const sourceLink = byId("asset-source-link");
    const sourceUrl = passport?.original_asset_url;
    sourceLink.hidden = !(typeof sourceUrl === "string" && sourceUrl.startsWith("https://"));
    if (!sourceLink.hidden) sourceLink.href = sourceUrl;
    renderRights(passport, retrieval);
    loadSceneRatings();
  }

  function renderRights(passport, retrieval) {
    const status = byId("rights-status");
    const details = byId("rights-details");
    details.replaceChildren();
    status.className = "rights-indicator";
    if (!passport) {
      status.textContent = retrieval?.status === "no_eligible_asset" ? "No eligible asset" : "Not available";
      status.classList.add("rejected");
      return;
    }
    const verified = passport.commercial_use_verification_status === "verified" && passport.watermark_verification === "clear";
    status.textContent = verified ? "Verified · watermark clear" : "Unverified / blocked";
    status.classList.add(verified ? "verified" : "rejected");
    const rows = [
      ["Provider", passport.provider],
      ["Asset", passport.asset_id],
      ["Licence", passport.licence_name],
      ["Commercial", passport.commercial_use_verification_status],
      ["Watermark", passport.watermark_verification],
      ["AI generated", passport.ai_generated === true ? "Yes" : passport.ai_generated === false ? "No" : "Unknown"],
      ["Retrieved", passport.retrieved_at],
    ];
    for (const [label, value] of rows) {
      const term = document.createElement("dt");
      term.textContent = label;
      const definition = document.createElement("dd");
      definition.textContent = value == null ? "Not provided" : String(value);
      details.append(term, definition);
    }
    if (passport.licence_evidence_url) {
      const term = document.createElement("dt");
      term.textContent = "Evidence";
      const definition = document.createElement("dd");
      const link = document.createElement("a");
      link.href = passport.licence_evidence_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = "Licence terms ↗";
      definition.append(link);
      details.append(term, definition);
    }
    if (passport.source_terms_reference) {
      const term = document.createElement("dt");
      term.textContent = "Source terms";
      const definition = document.createElement("dd");
      const link = document.createElement("a");
      link.href = passport.source_terms_reference;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = "View ↗";
      definition.append(link);
      details.append(term, definition);
    }
    byId("rights-attribution").textContent = passport.attribution_required
      ? `Attribution required: ${passport.attribution_text || "text not supplied"}`
      : "Attribution not reported as required by provider.";
  }

  async function loadSceneRatings() {
    if (!state.jobId) return;
    const container = byId("saved-ratings");
    try {
      const result = await api(`/api/v1/jobs/${encodeURIComponent(state.jobId)}/ratings`);
      const matching = (result.ratings || []).filter((rating) => rating.scene_id === state.selectedSceneId);
      container.replaceChildren();
      if (!matching.length) {
        const empty = document.createElement("span");
        empty.textContent = "No human ratings saved for this scene.";
        container.append(empty);
      } else {
        for (const rating of matching) {
          const entry = document.createElement("div");
          entry.className = "rating-entry";
          entry.textContent = `${rating.score}/5${rating.rationale ? ` · ${rating.rationale}` : ""}`;
          container.append(entry);
        }
      }
    } catch (error) {
      container.textContent = `Ratings could not be loaded: ${error.message}`;
    }
  }

  async function saveRating() {
    if (!state.jobId || !state.selectedSceneId || !state.selectedRating) return;
    const button = byId("submit-rating");
    button.disabled = true;
    byId("rating-feedback").textContent = "Saving…";
    try {
      const result = await api(`/api/v1/jobs/${encodeURIComponent(state.jobId)}/ratings`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": makeIdempotencyKey() },
        body: JSON.stringify({
          scene_id: state.selectedSceneId,
          score: state.selectedRating,
          rationale: byId("rating-rationale").value.trim() || null,
        }),
      });
      byId("rating-feedback").textContent = result.idempotent_replay ? "Existing rating returned." : "Human rating saved.";
      await loadSceneRatings();
    } catch (error) {
      byId("rating-feedback").textContent = error.message;
      button.disabled = false;
    }
  }

  async function refreshCurrentJob() {
    if (!state.jobId) return;
    byId("refresh-job").disabled = true;
    try {
      updateJob(await api(`/api/v1/jobs/${encodeURIComponent(state.jobId)}`));
      if (state.job.status === "queued" || state.job.status === "running") pollJob(state.jobId);
    } catch (error) {
      toast(`Refresh failed: ${error.message}`);
    } finally {
      byId("refresh-job").disabled = false;
    }
  }

  async function resumeJob(jobId) {
    if (!jobId) return;
    try {
      const job = await api(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
      state.jobId = job.job_id;
      byId("project-title").textContent = "Resumed production";
      byId("refresh-job").disabled = false;
      updateJob(job);
      if (job.status === "queued" || job.status === "running") pollJob(job.job_id);
    } catch (error) {
      toast(`Could not load job: ${error.message}`);
    }
  }

  async function checkService() {
    try {
      const result = await api("/health");
      byId("service-state").textContent = result.status === "ok" ? "API online" : "API status unknown";
    } catch {
      byId("service-state").textContent = "API unavailable";
    }
  }

  function setup() {
    inputTabs.forEach((tab) => tab.addEventListener("click", () => setMode(tab.dataset.mode)));
    byId("job-form").addEventListener("submit", submitJob);
    byId("script-input").addEventListener("input", () => {
      byId("script-count").textContent = `${byId("script-input").value.length.toLocaleString()} / 100,000`;
    });
    byId("audio-file").addEventListener("change", () => {
      byId("file-name").textContent = byId("audio-file").files[0]?.name || "Choose an audio file";
    });
    byId("dropzone").addEventListener("dragover", (event) => {
      event.preventDefault();
      byId("dropzone").classList.add("dragging");
    });
    byId("dropzone").addEventListener("dragleave", () => byId("dropzone").classList.remove("dragging"));
    byId("dropzone").addEventListener("drop", (event) => {
      event.preventDefault();
      byId("dropzone").classList.remove("dragging");
      const file = event.dataTransfer.files[0];
      if (!file) return;
      const transfer = new DataTransfer();
      transfer.items.add(file);
      byId("audio-file").files = transfer.files;
      byId("file-name").textContent = file.name;
      setMode("audio");
    });
    document.querySelectorAll(".rating-control button").forEach((button) => {
      button.addEventListener("click", () => {
        state.selectedRating = Number(button.dataset.rating);
        document.querySelectorAll(".rating-control button").forEach((choice) => choice.classList.toggle("selected", choice === button));
        byId("submit-rating").disabled = !state.jobId || !state.selectedSceneId;
      });
    });
    byId("submit-rating").addEventListener("click", saveRating);
    byId("refresh-job").addEventListener("click", refreshCurrentJob);
    byId("preview-format").addEventListener("change", () => {
      const ratio = byId("preview-format").value;
      if (state.availableLinks[ratio]) loadPreview(ratio, state.availableLinks[ratio]);
      if (state.scenes.length && state.selectedSceneId) selectScene(state.selectedSceneId);
    });
    byId("compare-button").addEventListener("click", () => {
      const container = byId("comparison-players");
      container.replaceChildren();
      for (const ratio of ["16:9", "9:16"]) {
        const figure = document.createElement("figure");
        const caption = document.createElement("figcaption");
        caption.textContent = `${ratio} · ${ratioNames[ratio]}`;
        const player = document.createElement("video");
        player.controls = true;
        player.playsInline = true;
        player.preload = "metadata";
        player.src = state.availableLinks[ratio];
        player.setAttribute("aria-label", `${ratio} rendered output for reframe comparison`);
        figure.append(caption, player);
        container.append(figure);
      }
      container.hidden = false;
    });
    byId("copy-job-id").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(state.jobId || "");
        toast("Job ID copied.");
      } catch {
        toast("Clipboard permission is unavailable.");
      }
    });
    setMode("script");
    void checkService();
    const jobId = new URLSearchParams(location.search).get("job_id");
    if (jobId) void resumeJob(jobId);
  }

  setup();
})();
