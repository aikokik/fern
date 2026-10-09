// fern usage page: cost, calls, tokens and context size per session, split by part. Data from the brain's /v1/usage.
const element = (selector) => document.querySelector(selector);
const PART_LABELS = {
  voice: "Voice (Realtime audio)",
  transcription: "Transcription",
  brain: "Brain (Responses API)",
  subagent: "Subagents",
  tool: "Hosted tools",
};

const money = (dollars) => (dollars > 0 && dollars < 0.01 ? `$${dollars.toFixed(4)}` : `$${dollars.toFixed(2)}`);
const count = (number) =>
  number >= 1e6 ? `${(number / 1e6).toFixed(1)}M` : number >= 1e3 ? `${(number / 1e3).toFixed(1)}k` : `${number}`;
const when = (epochSeconds) =>
  epochSeconds ? new Date(epochSeconds * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "–";

function cell(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function summaryCard(title, totals) {
  const card = document.createElement("article");
  card.append(cell("h2", title), cell("p", money(totals.cost), "amount"), cell("p", `${count(totals.calls)} calls · ${count(totals.tokens)} tokens`, "muted"));
  return card;
}

function render(report) {
  const usedParts = report.parts.filter((part) => report.totals.parts[part].calls > 0);
  if (!usedParts.length) {
    element("#status").textContent = "No usage recorded yet.";
    return;
  }
  element("#status").hidden = true;

  const summary = element("#summary");
  summary.append(summaryCard("Total", report.totals));
  for (const part of usedParts) summary.append(summaryCard(PART_LABELS[part] || part, report.totals.parts[part]));

  const headers = ["Session", "Last used", ...usedParts.map((part) => PART_LABELS[part] || part), "Total", "Calls", "Tokens", "Max context"];
  element("#columns").append(...headers.map((header) => cell("th", header)));
  const body = element("#sessions tbody");
  for (const session of report.sessions) {
    const row = document.createElement("tr");
    row.append(cell("td", session.session), cell("td", when(session.last_used)));
    for (const part of usedParts) row.append(cell("td", session.parts[part] ? money(session.parts[part].cost) : "–", "number"));
    row.append(
      cell("td", money(session.cost), "number strong"),
      cell("td", count(session.calls), "number"),
      cell("td", count(session.tokens), "number"),
      cell("td", session.max_context ? count(session.max_context) : "–", "number"),
    );
    body.append(row);
  }
  element("#sessions").hidden = false;

  const notes = [`Prices as of ${report.prices_as_of} (OpenAI pricing); transcription audio is estimated.`];
  if (report.unpriced_models.length) notes.push(`No price for: ${report.unpriced_models.join(", ")}.`);
  notes.push("Max context = largest input of a single model call.");
  element("#footnote").textContent = notes.join(" ");
}

async function load() {
  const deviceToken = localStorage.getItem("fern-token");
  if (!deviceToken) {
    element("#status").textContent = "Add your device token on the voice page (⚙) first.";
    return;
  }
  const response = await fetch("/v1/usage", { headers: { Authorization: `Bearer ${deviceToken}` } });
  if (!response.ok) {
    element("#status").textContent = response.status === 401 ? "Device token rejected." : "Couldn't load usage.";
    return;
  }
  render(await response.json());
}

load();
