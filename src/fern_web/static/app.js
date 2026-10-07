// fern voice client: microphone + speaker over WebRTC, captions, approval cards. No tool logic lives here.
const $ = (s) => document.querySelector(s);
let token = localStorage.getItem("fern-token") || "";
let pc = null, mic = null, sid = null;

const api = (path, body) => fetch(path, {
  method: body === undefined ? "GET" : "POST",
  headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

function setState(state, text) {
  document.body.dataset.state = state;
  $("#status").textContent = text;
  $("#mute").hidden = state !== "live";
}

function line(role, text) {
  if (!text) return;
  const p = document.createElement("p");
  p.className = role;
  p.textContent = text;
  $("#log").prepend(p);
}

async function start() {
  if (!token) return showTokenForm();
  setState("connecting", "Connecting…");
  try {
    mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    pc = new RTCPeerConnection();
    pc.ontrack = (e) => { $("#remote").srcObject = e.streams[0]; };
    pc.addTrack(mic.getTracks()[0]);
    const dc = pc.createDataChannel("oai-events");  // read-only captions
    dc.onmessage = (e) => onRealtime(JSON.parse(e.data));
    pc.onconnectionstatechange = () => {
      if (["failed", "disconnected"].includes(pc?.connectionState)) stop("Connection lost. Tap to restart.");
    };
    await pc.setLocalDescription(await pc.createOffer());
    const r = await api("/realtime/call", { sdp: pc.localDescription.sdp });
    if (r.status === 401) { stop("Token rejected"); return showTokenForm(); }
    if (!r.ok) return stop((await r.json().catch(() => ({}))).detail || "Couldn't start");
    const call = await r.json();
    sid = call.sid;
    await pc.setRemoteDescription({ type: "answer", sdp: call.sdp });
    setState("live", "Listening… say “stop listening” when done");
    followEvents(sid);
  } catch (err) {
    stop(err.name === "NotAllowedError" ? "Microphone blocked" : "Couldn't start");
  }
}

function stop(text = "Tap to talk") {
  if (sid) api("/realtime/end", { sid }).catch(() => {});
  sid = null;
  mic?.getTracks().forEach((t) => t.stop());
  pc?.close();
  pc = mic = null;
  setState("idle", text);
}

function onRealtime(ev) {
  if (ev.type === "conversation.item.input_audio_transcription.completed") line("you", ev.transcript);
  else if (ev.type === "response.output_audio_transcript.done") line("fern", ev.transcript);
  else if (ev.type === "input_audio_buffer.speech_started") setState("live", "Listening…");
  else if (ev.type === "response.created") setState("live", "Speaking…");
}

async function followEvents(id) {
  const r = await fetch(`/realtime/events?sid=${encodeURIComponent(id)}`, { headers: { Authorization: `Bearer ${token}` } });
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (chunk.startsWith("data: ")) onFern(JSON.parse(chunk.slice(6)));
    }
  }
}

function onFern(ev) {
  if (ev.type === "end") { if (sid) stop(); }
  else if (ev.type === "pending") approvalCard(ev);
  else if (ev.type === "command" && ev.command.type === "alarm.set") alarmCard(ev.command);
}

function card(title, detail) {
  const el = document.createElement("article");
  const h = document.createElement("h2");
  h.textContent = title;
  const d = document.createElement("p");
  d.textContent = detail;
  el.append(h, d);
  $("#cards").prepend(el);
  return el;
}

function approvalCard(p) {
  const el = card("Approve?", `${p.tool} ${p.args}`);
  const row = document.createElement("div");
  for (const [label, ok] of [["Approve", true], ["Reject", false]]) {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = ok ? "" : "ghost";
    b.onclick = async () => {
      row.remove();
      const r = await api("/realtime/approve", { sid, id: p.id, approve: ok });
      el.querySelector("p").textContent += r.ok ? (ok ? " ✓ approved" : " ✗ rejected") : " (expired)";
    };
    row.append(b);
  }
  el.append(row);
}

function alarmCard(c) {
  const el = card("Alarm", `${c.time}${c.label ? " · " + c.label : ""}`);
  const a = document.createElement("a");
  a.className = "button";
  a.textContent = "Add to Clock";
  a.href = `shortcuts://run-shortcut?name=${encodeURIComponent("Fern Alarm")}&input=text&text=${encodeURIComponent(c.time)}`;
  el.append(a);
}

function showTokenForm() {
  $("#token-form").hidden = false;
  $("#token").focus();
}

$("#token-form").onsubmit = (e) => {
  e.preventDefault();
  token = $("#token").value.trim();
  localStorage.setItem("fern-token", token);
  $("#token-form").hidden = true;
};
$("#settings").onclick = () => { $("#token-form").hidden = !$("#token-form").hidden; };
$("#mic").onclick = () => (pc ? stop() : start());
$("#mute").onclick = () => {
  const track = mic?.getTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  $("#mute").textContent = track.enabled ? "Mute" : "Unmute";
  setState("live", track.enabled ? "Listening…" : "Muted");
};
setState("idle", token ? "Tap to talk" : "Add your device token ⚙");
