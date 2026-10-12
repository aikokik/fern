// fern voice client: microphone + speaker over WebRTC, captions, approval cards, Claude panel. No tool logic here.
const element = (selector) => document.querySelector(selector);
let deviceToken = localStorage.getItem("fern-token") || "";
let peerConnection = null;
let microphone = null;
let sessionId = null;
let talkingTo = "fern";
const MAX_CLAUDE_STEPS = 20;  // who answers in the voice: fern, or Claude (relayed by the same voice)

const api = (path, body) => fetch(path, {
  method: body === undefined ? "GET" : "POST",
  headers: { Authorization: `Bearer ${deviceToken}`, "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

function setState(state, statusText) {
  document.body.dataset.state = state;
  element("#status").textContent = statusText;
  element("#mute").hidden = state !== "live";
}

function addCaption(speaker, text) {
  if (!text) return;
  const caption = document.createElement("p");
  caption.className = speaker;
  const label = document.createElement("small");
  label.textContent = speaker === "you" ? `you → ${talkingTo}` : talkingTo;
  caption.append(label, text);
  element("#log").prepend(caption);
}

async function start() {
  if (!deviceToken) return showTokenForm();
  setState("connecting", "Connecting…");
  try {
    microphone = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    peerConnection = new RTCPeerConnection();
    peerConnection.ontrack = (trackEvent) => { element("#remote").srcObject = trackEvent.streams[0]; };
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
  showMode({ mode: "fern" });
  setState("idle", statusText);
}

function onRealtimeEvent(event) {
  if (event.type === "conversation.item.input_audio_transcription.completed") addCaption("you", event.transcript);
  else if (event.type === "response.output_audio_transcript.done") addCaption("fern", event.transcript);
  else if (event.type === "input_audio_buffer.speech_started") setState("live", "Listening…");
  else if (event.type === "response.created") setState("live", "Speaking…");
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

function onFernEvent(event) {
  if (event.type === "end") { if (sessionId) stop(); }
  else if (event.type === "pending") showApprovalCard(event);
  else if (event.type === "resolved") resolveApprovalCards(event.approved);
  else if (event.type === "error") addCard("Something went wrong", `${event.message} · check fern logs for ref ${event.reference}`);
  else if (event.type === "command" && event.command.type === "alarm.set") showAlarmCard(event.command);
  else if (event.type === "mode") showMode(event);
  else if (event.type === "working") showWorking(event.by);
  else if (event.type === "claude_turn") startClaudeTurn(event.text);
  else if (event.type === "claude_progress") addClaudeStep(event.text);
  else if (event.type === "claude_text") addClaudeText(event.markdown);
  else if (event.type === "claude_permission") showClaudePermission(event);
  else if (event.type === "claude_permission_resolved") resolveClaudePermission(event.decision);
  else if (event.type === "claude_question") showClaudeQuestion(event);
  else if (event.type === "claude_question_answered") element("#claude-question").replaceChildren();
  else if (event.type === "claude_link") addClaudeLink(event);
}

function showWorking(handler) {
  if (!sessionId) return;
  const working = { fern: "fern's brain is working…", claude: "Claude is working…" }[handler];
  setState("live", working || "Listening…");
}

function showMode(event) {
  const talkingToClaude = event.mode === "claude";
  talkingTo = talkingToClaude ? "Claude" : "fern";
  element("#mode").hidden = !talkingToClaude;
  element("#mode").textContent = talkingToClaude ? "Claude" : "";
  if (talkingToClaude) element("#claude-panel").hidden = false;  // the conversation stays readable afterwards
}

// Like the VS Code chat: each turn shows your words, Claude's steps as they happen, its text, and any prompt.
function startClaudeTurn(text) {
  for (const part of ["#claude-steps", "#claude-plan", "#claude-permission", "#claude-question", "#claude-links"]) {
    element(part).replaceChildren();
  }
  const request = document.createElement("p");
  request.className = "claude-you";
  request.textContent = text;
  element("#claude-plan").append(request);
}

function addClaudeStep(text) {
  const steps = element("#claude-steps");
  steps.appendChild(document.createElement("li")).textContent = text;
  while (steps.children.length > MAX_CLAUDE_STEPS) steps.firstElementChild.remove();
}

function addClaudeText(markdown) {
  const block = document.createElement("div");
  renderMarkdown(block, markdown);
  element("#claude-plan").append(block);
}

function promptWithButtons(container, text, choices) {
  container.replaceChildren();
  const prompt = document.createElement("p");
  prompt.textContent = text;
  const buttons = document.createElement("div");
  buttons.className = "option-buttons";
  for (const { label, primary, onChoose } of choices) {
    const button = document.createElement("button");
    button.textContent = label;
    button.className = primary ? "" : "ghost";
    button.onclick = async () => {
      buttons.remove();
      if (!(await onChoose()).ok) prompt.textContent += " (couldn't send, answer by voice)";
    };
    buttons.append(button);
  }
  container.append(prompt, buttons);
}

function showClaudePermission(request) {
  const decide = (decision) => () =>
    api("/realtime/claude/permission", { session_id: sessionId, permission_id: request.id, decision });
  promptWithButtons(element("#claude-permission"), `Allow ${request.detail}?`, [
    { label: "Approve", primary: true, onChoose: decide("allow") },
    { label: "Always", onChoose: decide("always") },
    { label: "Reject", onChoose: decide("deny") },
  ]);
}

function resolveClaudePermission(decision) {
  const box = element("#claude-permission");
  box.querySelector(".option-buttons")?.remove();
  const prompt = box.querySelector("p");
  if (prompt) prompt.textContent += { allow: " ✓ approved", always: " ✓ always allowed", deny: " ✗ rejected" }[decision];
}

function showClaudeQuestion(question) {
  promptWithButtons(element("#claude-question"), question.text, question.options.map((label, index) => ({
    label: `${index + 1}. ${label}`,
    onChoose: () => api("/realtime/claude", { session_id: sessionId, text: `option ${index + 1}` }),
  })));
}

function addClaudeLink(event) {
  if (!event.url.startsWith("https://")) return;
  const link = document.createElement("a");
  link.className = "button";
  link.href = event.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = event.label;
  element("#claude-links").append(link);
}

// Small markdown subset (headings, lists, code blocks, **bold**, `code`). Built with textContent only: Claude's
// text never becomes HTML.
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

function addCard(title, detail) {
  const card = document.createElement("article");
  const heading = document.createElement("h2");
  heading.textContent = title;
  const description = document.createElement("p");
  description.textContent = detail;
  card.append(heading, description);
  element("#cards").prepend(card);
  return card;
}

function showApprovalCard(pending) {
  const card = addCard("Approve?", `${pending.tool} ${pending.args}`);
  const buttons = document.createElement("div");
  buttons.className = "approval-buttons";
  for (const [label, approve] of [["Approve", true], ["Reject", false]]) {
    const button = document.createElement("button");
    button.textContent = label;
    button.className = approve ? "" : "ghost";
    button.onclick = async () => {
      buttons.remove();
      const response = await api("/realtime/approve", { session_id: sessionId, pending_id: pending.id, approve });
      const outcome = response.ok ? (approve ? " ✓ approved" : " ✗ rejected") : " (expired)";
      card.querySelector("p").textContent += outcome;
    };
    buttons.append(button);
  }
  card.append(buttons);
}

function resolveApprovalCards(approved) {
  for (const buttons of document.querySelectorAll("#cards .approval-buttons")) {
    buttons.closest("article").querySelector("p").textContent += approved ? " ✓ approved by voice" : " ✗ rejected by voice";
    buttons.remove();
  }
}

function showAlarmCard(command) {
  const card = addCard("Alarm", `${command.time}${command.label ? " · " + command.label : ""}`);
  const link = document.createElement("a");
  link.className = "button";
  link.textContent = "Add to Clock";
  const shortcut = encodeURIComponent("Fern Alarm");
  link.href = `shortcuts://run-shortcut?name=${shortcut}&input=text&text=${encodeURIComponent(command.time)}`;
  card.append(link);
}

function showTokenForm() {
  element("#token-form").hidden = false;
  element("#token").focus();
}

element("#token-form").onsubmit = (submitEvent) => {
  submitEvent.preventDefault();
  deviceToken = element("#token").value.trim();
  localStorage.setItem("fern-token", deviceToken);
  element("#token-form").hidden = true;
};
element("#settings").onclick = () => { element("#token-form").hidden = !element("#token-form").hidden; };
element("#mic").onclick = () => (peerConnection ? stop() : start());
element("#mute").onclick = () => {
  const track = microphone?.getTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  element("#mute").textContent = track.enabled ? "Mute" : "Unmute";
  setState("live", track.enabled ? "Listening…" : "Muted");
};
setState("idle", deviceToken ? "Tap to talk" : "Add your device token ⚙");
