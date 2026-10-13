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
let speaker = "fern";  // who the current spoken reply belongs to (for captions)
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
  element("#mute").hidden = !live;
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

function addTranscriptLine(who, text) {
  if (!text) return;
  const line = create("li");
  line.append(create("small", "", who), text);
  element("#transcript-lines").append(line);
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
      if (event.transcript?.trim()) {
        addYouCard(event.transcript);
        addTranscriptLine(`you → ${talkingTo}`, event.transcript);
      }
      break;
    case "response.created":
      speaker = talkingTo;
      showCaption("");
      setActivity("speaking", "Speaking…");
      break;
    case "response.output_audio_transcript.delta":
      showCaption(element("#caption").textContent + event.delta);
      break;
    case "response.output_audio_transcript.done":
      showCaption(event.transcript);
      addTranscriptLine(speaker, event.transcript);
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
      if (message.startsWith("data: ")) onFernEvent(JSON.parse(message.slice(6)));
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
  element("#mode").hidden = !claude;
}

// ── Cards ("moments") ───────────────────────────────────────────────────────────────────────────────
function addCard(kind, label) {
  element("#welcome")?.remove();
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

// ── Settings: the device token (the browser keeps it after the first paste) ─────────────────────────
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
    return;
  }
  status.textContent = "Token saved, checking…";
  const response = await api("/v1/profile").catch(() => null);
  if (response?.ok) status.textContent = `Token saved ✓ · device: ${(await response.json()).device}`;
  else if (response?.status === 401) status.textContent = "The saved token was rejected. Paste a new one.";
  else status.textContent = "Token saved ✓ (couldn't check: fern is unreachable)";
}

element("#token-form").onsubmit = async (submitEvent) => {
  submitEvent.preventDefault();
  deviceToken = element("#token").value.trim();
  localStorage.setItem("fern-token", deviceToken);
  element("#token").value = "";
  await refreshTokenStatus();
  setState("idle", deviceToken ? "Tap to talk" : "Add your device token ⚙");
};
element("#forget-token").onclick = () => {
  localStorage.removeItem("fern-token");
  deviceToken = "";
  refreshTokenStatus();
  setState("idle", "Add your device token ⚙");
};
element("#settings").onclick = () => {
  if (element("#token-form").hidden) showTokenForm();
  else element("#token-form").hidden = true;
};
element("#show-transcript").onclick = () => { element("#transcript").hidden = !element("#transcript").hidden; };
element("#close-transcript").onclick = () => { element("#transcript").hidden = true; };
element("#lightbox").onclick = () => { element("#lightbox").hidden = true; };
element("#orb").onclick = () => (peerConnection ? stop() : start());
element("#end").onclick = () => stop();
element("#mute").onclick = () => {
  const track = microphone?.getTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  element("#mute").textContent = track.enabled ? "🎙" : "🔇";
  element("#mute").setAttribute("aria-label", track.enabled ? "Mute" : "Unmute");
  element("#status").textContent = track.enabled ? "Listening…" : "Muted";
};
setState("idle", deviceToken ? "Tap to talk" : "Add your device token ⚙");
