// fern's living canvas: voice over WebRTC, an orb that reacts to real sound, and cards fern or Claude put on screen.
// No tool logic here: the server decides what to show; this page only renders known card types, always with
// textContent, so nothing a model writes ever becomes HTML.
const element = (selector) => document.querySelector(selector);
const create = (tag, className = "", text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

let deviceToken = localStorage.getItem("fern-token") || "";
let peerConnection = null;
let microphone = null;
let sessionId = null;
let talkingTo = "fern";  // who the voice relays: fern, or Claude
let claudeCard = null;  // the card Claude's current turn streams into
let audio = null;  // { context, analysers, frame } while a call is live
let orbLevel = 0;
const MAX_CLAUDE_STEPS = 30;
const PHOTO_HOST = "https://images.pexels.com/";
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const api = (path, body) => fetch(path, {
  method: body === undefined ? "GET" : "POST",
  headers: { Authorization: `Bearer ${deviceToken}`, "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

// ── State, captions and transcript ──────────────────────────────────────────────────────────────────
function setState(state, statusText) {
  document.body.dataset.state = state;
  element("#status").textContent = statusText;
  const live = state === "live";
  element("#end").hidden = !live;
  element("#orb").setAttribute("aria-label", live ? "Stop talking" : "Start talking");
}

function setActivity(activity, statusText) {
  document.body.dataset.activity = activity;
  if (statusText && sessionId) element("#status").textContent = statusText;
}

function showCaption(text) {
  element("#caption").textContent = text;
}

// ── Voice call ──────────────────────────────────────────────────────────────────────────────────────
async function start() {
  if (!deviceToken) return showTokenForm();
  setState("connecting", "Connecting…");
  try {
    microphone = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    startLevels();
    listenTo(microphone);
    peerConnection = new RTCPeerConnection();
    peerConnection.ontrack = (trackEvent) => {
      element("#remote").srcObject = trackEvent.streams[0];
      listenTo(trackEvent.streams[0]);
    };
    peerConnection.addTrack(microphone.getTracks()[0]);
    const captionsChannel = peerConnection.createDataChannel("oai-events");  // read-only captions
    captionsChannel.onmessage = (message) => onRealtimeEvent(JSON.parse(message.data));
    peerConnection.onconnectionstatechange = () => {
      if (["failed", "disconnected"].includes(peerConnection?.connectionState)) stop("Connection lost. Tap to restart.");
    };
    await peerConnection.setLocalDescription(await peerConnection.createOffer());

    const response = await api("/realtime/call", { sdp: peerConnection.localDescription.sdp });
    if (response.status === 401) { stop("Token rejected"); return showTokenForm(); }
    if (!response.ok) return stop((await response.json().catch(() => ({}))).detail || "Couldn't start");
    const call = await response.json();
    sessionId = call.session_id;
    await peerConnection.setRemoteDescription({ type: "answer", sdp: call.sdp });
    setState("live", "Listening… say “stop listening” when done");
    followFernEvents(sessionId);
  } catch (error) {
    stop(error.name === "NotAllowedError" ? "Microphone blocked" : "Couldn't start");
  }
}

function stop(statusText = "Tap to talk") {
  if (sessionId) api("/realtime/end", { session_id: sessionId }).catch(() => {});
  sessionId = null;
  microphone?.getTracks().forEach((track) => track.stop());
  peerConnection?.close();
  peerConnection = null;
  microphone = null;
  stopLevels();
  showMode({ mode: "fern" });
  showCaption("");
  setActivity("");
  setState("idle", statusText);
}

// The orb follows the louder of your microphone and fern's voice (Web Audio analysers, nothing is recorded).
function startLevels() {
  audio = { context: new AudioContext(), analysers: [], frame: 0 };
  if (!reducedMotion) animateOrb();
}

function listenTo(stream) {
  if (!audio) return;
  const analyser = audio.context.createAnalyser();
  analyser.fftSize = 512;
  audio.context.createMediaStreamSource(stream).connect(analyser);
  audio.analysers.push(analyser);
}

function animateOrb() {
  let loudest = 0;
  for (const analyser of audio.analysers) {
    const samples = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(samples);
    let energy = 0;
    for (const sample of samples) energy += ((sample - 128) / 128) ** 2;
    loudest = Math.max(loudest, Math.sqrt(energy / samples.length));
  }
  orbLevel = orbLevel * 0.8 + Math.min(1, loudest * 4) * 0.2;
  element("#orb").style.setProperty("--level", orbLevel.toFixed(3));
  audio.frame = requestAnimationFrame(animateOrb);
}

function stopLevels() {
  if (!audio) return;
  cancelAnimationFrame(audio.frame);
  audio.context.close().catch(() => {});
  audio = null;
  orbLevel = 0;
  element("#orb").style.setProperty("--level", "0");
}

function onRealtimeEvent(event) {
  switch (event.type) {
    case "input_audio_buffer.speech_started":
      setActivity("listening", "Listening…");
      break;
    case "conversation.item.input_audio_transcription.completed":
      if (event.transcript?.trim()) addYouCard(event.transcript);
      break;
    case "response.created":
      showCaption("");
      setActivity("speaking", "Speaking…");
      break;
    case "response.output_audio_transcript.delta":
      showCaption(element("#caption").textContent + event.delta);
      break;
    case "response.output_audio_transcript.done":
      showCaption(event.transcript);
      break;
    case "response.done":
      if (document.body.dataset.activity === "speaking") setActivity("listening", "Listening…");
      break;
  }
}

async function followFernEvents(id) {
  const response = await fetch(`/realtime/events?session_id=${encodeURIComponent(id)}`, {
    headers: { Authorization: `Bearer ${deviceToken}` },
  });
  await readServerEvents(response, onFernEvent);
}

// Server-sent events over fetch (EventSource can't send the device token header).
async function readServerEvents(response, handle) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    let boundary;
    while ((boundary = buffered.indexOf("\n\n")) >= 0) {
      const message = buffered.slice(0, boundary);
      buffered = buffered.slice(boundary + 2);
      if (message.startsWith("data: ")) handle(JSON.parse(message.slice(6)));
    }
  }
}

const fernEventHandlers = {
  end: () => { if (sessionId) stop(); },
  error: (event) => addErrorCard(event),
  pending: (event) => showApprovalCard(event),
  resolved: (event) => resolveApprovalCards(event.approved),
  command: (event) => showCommand(event.command),
  fern_reply: (event) => showAnswer(event.markdown),
  mode: (event) => showMode(event),
  working: (event) => showWorking(event.by),
  claude_turn: (event) => startClaudeTurn(event.text),
  claude_progress: (event) => addClaudeStep(event.text),
  claude_text: (event) => addClaudeText(event.markdown),
  claude_permission: (event) => showClaudePermission(event),
  claude_permission_resolved: (event) => resolveClaudePermission(event.decision),
  claude_question: (event) => showClaudeQuestion(event),
  claude_question_answered: () => claudeCard?.prompt.querySelectorAll("button").forEach((button) => { button.disabled = true; }),
  claude_link: (event) => addClaudeLink(event),
};

function onFernEvent(event) {
  fernEventHandlers[event.type]?.(event);
}

function showWorking(handler) {
  if (handler === "fern") setActivity("thinking", "fern is thinking…");
  else if (handler === "claude") setActivity("thinking", "Claude is working…");
  else {
    setActivity("listening", "Listening…");
    if (claudeCard) claudeCard.liveStep.textContent = "";
  }
}

function showMode(event) {
  const claude = event.mode === "claude";
  talkingTo = claude ? "Claude" : "fern";
  document.body.dataset.who = claude ? "claude" : "fern";
  element("#who").textContent = talkingTo;
}

// ── Cards ("moments") ───────────────────────────────────────────────────────────────────────────────
function addCard(kind, label) {
  const card = create("article", `card ${kind}`);
  if (label) card.append(create("p", "card-label", label));
  element("#moments").append(card);
  scrollToEnd();
  return card;
}

function scrollToEnd() {
  requestAnimationFrame(() => { element("#moments").scrollTop = element("#moments").scrollHeight; });
}

function addYouCard(text) {
  addCard("you").append(text);
}

function addErrorCard(event) {
  const card = addCard("error", "Something went wrong");
  card.append(create("p", "", `${event.message} · fern logs reference ${event.reference}`));
}

// An answer: its first paragraph as the headline, the rest behind "More".
function showAnswer(markdown, who = "fern") {
  const card = addCard(who === "Claude" ? "claude" : "fern", who);
  const [first, ...rest] = markdown.trim().split(/\n\s*\n/);
  const headline = create("div", "headline");
  renderMarkdown(headline, first);
  card.append(headline);
  if (rest.length) {
    const more = create("div");
    more.hidden = true;
    renderMarkdown(more, rest.join("\n\n"));
    const toggle = create("button", "more-toggle", "More");
    toggle.onclick = () => {
      more.hidden = !more.hidden;
      toggle.textContent = more.hidden ? "More" : "Less";
      scrollToEnd();
    };
    card.append(more, toggle);
  }
}

function showCommand(command) {
  if (command.type === "alarm.set") showAlarmCard(command);
  else if (command.type === "show.images") showPhotos(command);
  else if (command.type === "show.card") showFernCard(command.card);
}

function showFernCard(card) {
  if (card.type === "recipe") showRecipe(card);
  else if (card.type === "checklist") addCard("fern", card.title).append(tickList(card.items));
  else if (card.type === "choices") showChoices(card);
  else if (card.type === "table") showTable(card);
}

function safeLink(url, text, className = "") {
  if (!url?.startsWith("https://")) return null;
  const link = create("a", className, text);
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function showPhotos(command) {
  const card = addCard("fern", command.query);
  const strip = create("div", "photos");
  for (const photo of command.images) {
    if (!photo.src.startsWith(PHOTO_HOST)) continue;
    const figure = create("figure");
    const image = create("img");
    image.src = photo.src;
    image.alt = photo.alt;
    image.loading = "lazy";
    image.onclick = () => openLightbox(photo.src, photo.alt);
    const caption = create("figcaption", "", "Photo: ");
    caption.append(safeLink(photo.link, photo.credit || "Pexels") ?? photo.credit, " · Pexels");
    figure.append(image, caption);
    strip.append(figure);
  }
  card.append(strip);
}

function openLightbox(src, alt) {
  element("#lightbox-image").src = src;
  element("#lightbox-image").alt = alt;
  element("#lightbox").hidden = false;
}

// A recipe: the servings stepper rescales every amount; steps tick off as you cook.
function showRecipe(recipe) {
  const card = addCard("fern", "Recipe");
  card.append(create("h3", "", recipe.title));
  const baseServings = Math.max(1, recipe.servings || 1);
  let servings = baseServings;
  const count = create("output");
  const ingredients = create("ul", "ingredients");
  const render = () => {
    count.textContent = `${servings} serving${servings === 1 ? "" : "s"}`;
    ingredients.replaceChildren(...recipe.ingredients.map((ingredient) => {
      const item = create("li");
      if (ingredient.amount != null) {
        const amount = formatAmount((ingredient.amount * servings) / baseServings);
        item.append(create("b", "", ingredient.unit ? `${amount} ${ingredient.unit}` : amount), " ");
      }
      item.append(ingredient.name);
      return item;
    }));
  };
  const fewer = create("button", "ghost", "−");
  const more = create("button", "ghost", "+");
  fewer.onclick = () => { if (servings > 1) { servings -= 1; render(); } };
  more.onclick = () => { servings += 1; render(); };
  const stepper = create("div", "servings");
  stepper.append(fewer, count, more);
  render();
  card.append(stepper, ingredients, tickList(recipe.steps));
}

function formatAmount(amount) {
  const fractions = { 0.25: "¼", 0.5: "½", 0.75: "¾" };
  const whole = Math.floor(amount);
  const quarter = Math.round((amount - whole) * 4) / 4;
  if (amount < 10 && fractions[quarter] && Math.abs(amount - whole - quarter) < 0.05) return `${whole || ""}${fractions[quarter]}`;
  return String(amount >= 10 ? Math.round(amount) : Math.round(amount * 10) / 10);
}

function tickList(items) {
  const list = create("ol", "ticks");
  for (const text of items) {
    const item = create("li", "", text);
    item.onclick = () => item.classList.toggle("done");
    list.append(item);
  }
  return list;
}

// Tappable choices; `choose` sends the answer and returns the fetch response.
function choiceButtons(options, descriptions, choose) {
  const box = create("div", "choices");
  options.forEach((label, index) => {
    const button = create("button", "choice");
    button.append(create("span", "", label));
    if (descriptions?.[index]) button.append(create("small", "", descriptions[index]));
    button.onclick = async () => {
      for (const choice of box.children) choice.disabled = true;
      button.classList.add("chosen");
      const response = await choose(label, index).catch(() => null);
      if (!response?.ok) {
        for (const choice of box.children) choice.disabled = false;
        button.classList.remove("chosen");
        box.after(create("p", "muted", "Couldn't send. Say it instead."));
      }
    };
    box.append(button);
  });
  return box;
}

function showChoices(card) {
  const box = addCard("fern", "Choose");
  box.append(create("p", "headline", card.question));
  box.append(choiceButtons(card.options, [], (label) => api("/realtime/say", { session_id: sessionId, text: label })));
}

function showTable(table) {
  const card = addCard("fern", "Comparison");
  card.append(create("h3", "", table.title));
  const grid = create("table");
  const head = create("tr");
  for (const column of table.columns) head.append(create("th", "", column));
  grid.append(head);
  for (const row of table.rows) {
    const line = create("tr");
    for (const cell of row) line.append(create("td", "", cell));
    grid.append(line);
  }
  const scroll = create("div", "table-scroll");
  scroll.append(grid);
  card.append(scroll);
}

function showAlarmCard(command) {
  const card = addCard("fern", "Alarm");
  card.append(create("h3", "", `${command.time}${command.label ? " · " + command.label : ""}`));
  const link = create("a", "button", "Add to Clock");
  link.href = `shortcuts://run-shortcut?name=${encodeURIComponent("Fern Alarm")}&input=text&text=${encodeURIComponent(command.time)}`;
  card.append(link);
}

function showApprovalCard(pending) {
  const card = addCard("fern approval", "Approve?");
  const detail = create("p", "", `${pending.tool} ${pending.args}`);
  const buttons = create("div", "row approval-buttons");
  for (const [label, approve] of [["Approve", true], ["Reject", false]]) {
    const button = create("button", approve ? "" : "ghost", label);
    button.onclick = async () => {
      buttons.remove();
      const response = await api("/realtime/approve", { session_id: sessionId, pending_id: pending.id, approve });
      detail.textContent += response.ok ? (approve ? " ✓ approved" : " ✗ rejected") : " (expired)";
    };
    buttons.append(button);
  }
  card.append(detail, buttons);
}

function resolveApprovalCards(approved) {
  for (const buttons of document.querySelectorAll(".approval-buttons")) {
    buttons.closest("article").querySelector("p:not(.card-label)").textContent += approved ? " ✓ approved by voice" : " ✗ rejected by voice";
    buttons.remove();
  }
}

// ── Claude: one card per turn, like the VS Code chat: your words, live steps, its text, any prompt ──────
function startClaudeTurn(text) {
  const card = addCard("claude", "Claude");
  card.append(create("p", "muted", text));
  const liveStep = create("p", "live-step");
  const steps = create("details", "steps");
  const stepsSummary = create("summary", "", "Steps");
  const stepList = create("ol");
  steps.append(stepsSummary, stepList);
  steps.hidden = true;
  const content = create("div");
  const prompt = create("div", "prompt");
  const links = create("div", "links");
  card.append(liveStep, steps, content, prompt, links);
  claudeCard = { card, liveStep, steps, stepsSummary, stepList, content, prompt, links };
}

function currentClaudeCard() {
  if (!claudeCard) startClaudeTurn("");
  return claudeCard;
}

function addClaudeStep(text) {
  const turn = currentClaudeCard();
  turn.liveStep.textContent = `▸ ${text}`;
  turn.stepList.append(create("li", "", text));
  while (turn.stepList.children.length > MAX_CLAUDE_STEPS) turn.stepList.firstElementChild.remove();
  turn.steps.hidden = false;
  turn.stepsSummary.textContent = `${turn.stepList.children.length} steps`;
  scrollToEnd();
}

function addClaudeText(markdown) {
  const block = create("div");
  renderMarkdown(block, markdown);
  currentClaudeCard().content.append(block);
  scrollToEnd();
}

function showClaudePermission(request) {
  const prompt = currentClaudeCard().prompt;
  prompt.replaceChildren(create("p", "headline", `Allow ${request.detail}?`));
  const buttons = create("div", "row");
  for (const [label, decision] of [["Approve", "allow"], ["Always", "always"], ["Reject", "deny"]]) {
    const button = create("button", decision === "allow" ? "" : "ghost", label);
    button.onclick = async () => {
      buttons.querySelectorAll("button").forEach((choice) => { choice.disabled = true; });
      const response = await api("/realtime/claude/permission", { session_id: sessionId, permission_id: request.id, decision });
      if (!response.ok) prompt.append(create("p", "muted", "Couldn't send. Answer by voice."));
    };
    buttons.append(button);
  }
  prompt.append(buttons);
  scrollToEnd();
}

function resolveClaudePermission(decision) {
  const prompt = claudeCard?.prompt;
  if (!prompt) return;
  prompt.querySelector(".row")?.remove();
  prompt.querySelector("p").textContent += { allow: " ✓ approved", always: " ✓ always allowed", deny: " ✗ rejected" }[decision];
}

function showClaudeQuestion(question) {
  const prompt = currentClaudeCard().prompt;
  prompt.replaceChildren(create("p", "headline", question.text));
  prompt.append(choiceButtons(question.options, question.descriptions, (_label, index) =>
    api("/realtime/claude", { session_id: sessionId, text: `option ${index + 1}` })));
  scrollToEnd();
}

function addClaudeLink(event) {
  const link = safeLink(event.url, event.label, "button");
  if (link) currentClaudeCard().links.append(link);
}

// Small markdown subset (headings, lists, code blocks, **bold**, `code`). Built with textContent only: model text
// never becomes HTML.
function renderMarkdown(container, markdown) {
  container.replaceChildren();
  let list = null;
  let codeBlock = null;
  for (const line of markdown.split("\n")) {
    if (line.trimStart().startsWith("```")) {
      codeBlock = codeBlock ? null : container.appendChild(document.createElement("pre"));
      list = null;
      continue;
    }
    if (codeBlock) { codeBlock.textContent += `${line}\n`; continue; }
    const heading = line.match(/^#{1,4}\s+(.*)/);
    const listItem = line.match(/^\s*([-*]|\d+\.)\s+(.*)/);
    if (listItem) {
      const listType = listItem[1].endsWith(".") ? "OL" : "UL";
      if (list?.tagName !== listType) list = container.appendChild(document.createElement(listType));
      appendInline(list.appendChild(document.createElement("li")), listItem[2]);
      continue;
    }
    list = null;
    if (heading) appendInline(container.appendChild(document.createElement("h3")), heading[1]);
    else if (line.trim()) appendInline(container.appendChild(document.createElement("p")), line);
  }
}

function appendInline(parent, text) {
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      parent.appendChild(document.createElement("strong")).textContent = part.slice(2, -2);
    } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      parent.appendChild(document.createElement("code")).textContent = part.slice(1, -1);
    } else if (part) {
      parent.append(part);
    }
  }
}

// ── Claude activity: what Claude Code reported for each run, laid out like the VS Code plugin's chat ──────────
// Every value comes from fern-bridge (Agent SDK messages and Claude Code's hooks); this only lays it out.
const TOOL_DETAIL = {
  Bash: "command", Read: "file_path", Edit: "file_path", MultiEdit: "file_path", Write: "file_path",
  NotebookEdit: "notebook_path", Grep: "pattern", Glob: "pattern", WebSearch: "query", WebFetch: "url",
  Agent: "description", Task: "description", Skill: "skill",
};
const DOT = { done: "success", error: "failure", denied: "failure", waiting_for_you: "warning", running: "progress" };
const expandedKeys = new Set();  // IN/OUT rows and "Thinking" blocks the user opened, kept across live updates
let activityStream = null;  // AbortController while the panel is open

async function openActivity() {
  element("#activity").hidden = false;
  activityStream?.abort();
  activityStream = new AbortController();
  try {
    const response = await fetch("/claude/runs/stream", {
      headers: { Authorization: `Bearer ${deviceToken}` }, signal: activityStream.signal,
    });
    if (!response.ok) {
      const reasons = { 401: "A device token is needed.", 403: "This device isn't allowed to use Claude Code." };
      return showActivityNote(reasons[response.status] || `Couldn't load Claude's runs (${response.status}).`);
    }
    await readServerEvents(response, onRunEvent);
  } catch (error) {
    if (error.name !== "AbortError") showActivityNote("Lost the connection to the run log.");
  }
}

function closeActivity() {
  activityStream?.abort();
  element("#activity").hidden = true;
}

function showActivityNote(text) {
  element("#activity-source").replaceChildren(create("p", "muted", text));
}

function onRunEvent(event) {
  if (event.type === "snapshot") {
    showRunSource(event);
    element("#runs").replaceChildren(...[...event.runs].reverse().map(renderRun));  // oldest first, like a chat
    scrollActivity(true);
  } else if (event.type === "run") {
    upsertRun(event.run);
  } else if (event.type === "unavailable") {
    showActivityNote(`fern-bridge isn't reachable (${event.message}). Is it running on the Mac?`);
  }
}

function showRunSource(report) {
  const about = create("details", "about-log");
  about.append(create("summary", "", "About this log"));
  about.append(keyValue("Runs on", report.runs_on), keyValue("Run log", report.run_log_file));
  const limits = create("ul");
  for (const limit of report.limits) limits.append(create("li", "", limit));
  about.append(limits);
  element("#activity-source").replaceChildren(about);
}

function keyValue(label, value) {
  const line = create("p", "key-value");
  line.append(create("span", "", `${label}: `), create("code", "", value));
  return line;
}

function scrollActivity(force) {
  const panel = element("#activity");
  const nearEnd = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 120;
  if (force || nearEnd) requestAnimationFrame(() => { panel.scrollTop = panel.scrollHeight; });
}

function upsertRun(run) {
  const panel = element("#activity");
  const wasNearEnd = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 120;
  const existing = element(`#runs [data-run-id="${CSS.escape(run.id)}"]`);
  const fresh = renderRun(run);
  if (existing) existing.replaceWith(fresh);
  else element("#runs").append(fresh);
  scrollActivity(wasNearEnd);
}

function renderRun(run) {
  const item = create("li", "run");
  item.dataset.runId = run.id;
  const when = new Date(run.started * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  item.append(create("p", "run-stamp", when), create("div", "user-message", run.input));
  // Runs logged before the timeline existed only kept their tool calls.
  const timeline = run.timeline ?? (run.actions || []).map((action) => ({ kind: "tool", parent: null, ...action }));
  const steps = create("div", "timeline");
  renderSteps(steps, timeline, null, run);
  const shownText = timeline.some((entry) => entry.kind === "text" && entry.text === run.output);
  if (run.output && !shownText) steps.append(step("", markdownBlock(run.output)));
  const ending = {
    running: ["progress", create("span", "working", "Working…")],
    waiting_for_you: ["warning", create("span", "", "Waiting for your approval in fern")],
    stopping: ["progress", create("span", "working", "Stopping…")],
    stopped: ["failure", create("span", "secondary", "Interrupted")],
  }[run.status];
  if (ending) steps.append(step(...ending));
  for (const error of run.errors || []) steps.append(step("failure", create("span", "error-text", error)));
  if (run.permission_denials?.length) {
    steps.append(step("failure", create("span", "error-text", `${run.permission_denials.length} permission denials`)));
  }
  item.append(steps);
  const reported = [
    run.model,
    run.num_turns != null && `${run.num_turns} turns`,
    run.duration_ms != null && `${(run.duration_ms / 1000).toFixed(1)} s`,
    run.cost_usd != null && `$${run.cost_usd.toFixed(4)} API-equivalent`,
    run.permission_mode && `permissions: ${run.permission_mode}`,
  ].filter(Boolean);
  if (reported.length) item.append(create("p", "run-meta", reported.join(" · ")));
  if (run.transcript_path) {
    const transcript = create("p", "run-meta");
    transcript.append("Transcript (kept by Claude Code): ", create("code", "", run.transcript_path));
    item.append(transcript);
  }
  return item;
}

function step(dot, ...children) {
  const row = create("div", dot ? `step dot-${dot}` : "step");
  row.append(...children);
  return row;
}

function markdownBlock(markdown) {
  const block = create("div", "assistant-text");
  renderMarkdown(block, markdown);
  return block;
}

function renderSteps(container, timeline, parent, run) {
  timeline.forEach((entry, index) => {
    if ((entry.parent ?? null) !== parent) return;
    if (entry.kind === "text") container.append(step("", markdownBlock(entry.text)));
    else if (entry.kind === "thinking") container.append(step("", thinkingBlock(entry.text, `${run.id}:thinking:${index}`)));
    else container.append(step(DOT[entry.status] || "", renderTool(entry, timeline, run)));
  });
}

function thinkingBlock(text, key) {
  const details = create("details", "thinking");
  details.open = expandedKeys.has(key);
  details.ontoggle = () => (details.open ? expandedKeys.add(key) : expandedKeys.delete(key));
  details.append(create("summary", "", "Thinking"), create("div", "thinking-content", text));
  return details;
}

function toolInput(tool) {
  if (typeof tool.input !== "string") return tool.input || {};
  try { return JSON.parse(tool.input); } catch { return { input: tool.input }; }
}

function relativePath(path, run) {
  return run.cwd && typeof path === "string" ? path.replace(`${run.cwd}/`, "") : path;
}

function renderTool(tool, timeline, run) {
  const input = toolInput(tool);
  const box = create("div", "tool");
  const head = create("div", "tool-head");
  head.append(create("span", "tool-name", tool.tool === "TodoWrite" ? "Update Todos" : tool.tool));
  if (tool.tool === "Bash") {  // like the plugin: the description up top, the command in IN
    if (input.description) head.append(create("span", "tool-detail plain", input.description));
  } else if (input[TOOL_DETAIL[tool.tool]]) {
    head.append(create("span", "tool-detail", relativePath(String(input[TOOL_DETAIL[tool.tool]]), run)));
  }
  box.append(head);

  const body = create("div", "tool-body");
  const key = `${run.id}:${tool.id}`;
  if (["Edit", "MultiEdit"].includes(tool.tool)) {
    for (const edit of input.edits || [input]) body.append(ioRow("IN", diffLines(edit.old_string, edit.new_string), `${key}:in`, true));
  } else if (tool.tool === "TodoWrite") {
    body.append(todoRow(input.todos || []));
  } else if (tool.tool === "Write") {
    body.append(ioRow("IN", input.content ?? "", `${key}:in`));
  } else if (tool.tool === "Bash") {
    body.append(ioRow("IN", input.command ?? "", `${key}:in`));
  } else if (!["Read", "Glob"].includes(tool.tool) || Object.keys(input).length > 1) {
    body.append(ioRow("IN", JSON.stringify(input, null, 2), `${key}:in`));
  }
  const routine = tool.tool === "TodoWrite" && tool.status !== "error";  // its output only says "updated"
  if (tool.output && !routine) body.append(ioRow(tool.status === "error" ? "ERROR" : "OUT", tool.output, `${key}:out`, false, tool.status === "error"));
  if (body.children.length) box.append(body);

  if (timeline.some((entry) => entry.parent === tool.id)) {  // a subagent's own steps, under its Agent call
    const nested = create("div", "timeline nested");
    renderSteps(nested, timeline, tool.id, run);
    box.append(nested);
  }
  const notes = { denied: "Denied", stopped: "Interrupted" };  // waiting is shown once, at the end of the run
  if (notes[tool.status]) box.append(create("p", "tool-note", notes[tool.status]));
  return box;
}

// One IN/OUT row: clipped to a few lines with a fade, click to expand (like the plugin).
function ioRow(label, content, key, isNode = false, isError = false) {
  const row = create("div", "io-row");
  const body = isNode ? content : create("pre", "", content);
  body.classList.add("io-content");
  if (isError) body.classList.add("error-text");
  if (!expandedKeys.has(key)) body.classList.add("clipped");
  row.onclick = () => {
    body.classList.toggle("clipped");
    body.classList.contains("clipped") ? expandedKeys.delete(key) : expandedKeys.add(key);
  };
  row.append(create("span", "io-label", label), body);
  return row;
}

function diffLines(before = "", after = "") {
  const diff = create("div", "diff");
  for (const line of String(before).split("\n")) if (before) diff.append(create("div", "diff-del", `- ${line}`));
  for (const line of String(after).split("\n")) if (after) diff.append(create("div", "diff-add", `+ ${line}`));
  return diff;
}

function todoRow(todos) {
  const list = create("ul", "todo-list");
  for (const todo of todos) {
    const item = create("li", `todo ${todo.status}`);
    item.append(create("span", "todo-mark", { completed: "☑", in_progress: "◐" }[todo.status] || "☐"));
    item.append(create("span", "", todo.status === "in_progress" && todo.activeForm ? todo.activeForm : todo.content));
    list.append(item);
  }
  const row = create("div", "io-row todo-row");
  row.append(list);
  return row;
}

// ── Device token: asked for only when this browser has none or fern rejected it (kept after the first paste) ──
function showTokenForm() {
  element("#token-form").hidden = false;
  refreshTokenStatus();
  element("#token").focus();
}

async function refreshTokenStatus() {
  const status = element("#token-status");
  element("#forget-token").hidden = !deviceToken;
  if (!deviceToken) {
    status.textContent = "No token saved in this browser.";
    return false;
  }
  status.textContent = "Token saved, checking…";
  const response = await api("/v1/profile").catch(() => null);
  if (response?.ok) status.textContent = `Token saved ✓ · device: ${(await response.json()).device}`;
  else if (response?.status === 401) status.textContent = "The saved token was rejected. Paste a new one.";
  else status.textContent = "Token saved ✓ (couldn't check: fern is unreachable)";
  return response?.status !== 401;
}

element("#token-form").onsubmit = async (submitEvent) => {
  submitEvent.preventDefault();
  deviceToken = element("#token").value.trim();
  localStorage.setItem("fern-token", deviceToken);
  element("#token").value = "";
  if (await refreshTokenStatus()) element("#token-form").hidden = true;
};
element("#forget-token").onclick = () => {
  localStorage.removeItem("fern-token");
  deviceToken = "";
  refreshTokenStatus();
};
element("#who").onclick = () => (element("#activity").hidden ? openActivity() : closeActivity());
element("#close-activity").onclick = () => closeActivity();
element("#lightbox").onclick = () => { element("#lightbox").hidden = true; };
element("#orb").onclick = () => (peerConnection ? stop() : start());
element("#end").onclick = () => stop();
setState("idle", "Tap to talk");
