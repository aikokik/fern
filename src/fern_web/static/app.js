// fern voice client: microphone + speaker over WebRTC, captions, approval cards. No tool logic lives here.
const element = (selector) => document.querySelector(selector);
let deviceToken = localStorage.getItem("fern-token") || "";
let peerConnection = null;
let microphone = null;
let sessionId = null;

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
  caption.textContent = text;
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
  else if (event.type === "error") addCard("Something went wrong", `${event.message} · check fern logs for ref ${event.reference}`);
  else if (event.type === "command" && event.command.type === "alarm.set") showAlarmCard(event.command);
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
