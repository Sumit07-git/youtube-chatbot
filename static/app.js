const $ = (sel) => document.querySelector(sel);

const els = {
  form: $("#form"),
  url: $("#url"),
  go: $("#go"),
  error: $("#error"),
  loading: $("#loading"),
  loadingText: $("#loading-text"),
  result: $("#result"),
  thumb: $("#thumb"),
  title: $("#video-title"),
  meta: $("#video-meta"),
  summary: $("#summary"),
  copy: $("#copy"),
  reset: $("#reset"),
  askForm: $("#ask-form"),
  question: $("#question"),
  askGo: $("#ask-go"),
  thread: $("#thread"),
};

let current = null; 
let loadingTimer = null;

function esc(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`(.+?)`/g, "<code>$1</code>");
}
function renderMarkdown(md) {
  const out = [];
  let inList = false;
  const closeList = () => {
    if (inList) { out.push("</ul>"); inList = false; }
  };

  for (const raw of md.split("\n")) {
    const line = raw.trim();
    if (!line) { closeList(); continue; }

    let m;
    if ((m = line.match(/^#{1,4}\s+(.*)$/))) {
      closeList();
      out.push(`<h3>${inline(m[1])}</h3>`);
    } else if ((m = line.match(/^(?:[-*•]|\d+\.)\s+(.*)$/))) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else {
      closeList();
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();
  return out.join("");
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch {  }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(
      typeof detail === "string" ? detail : "Check the link and try again."
    );
  }
  return data;
}

function formatDuration(totalSeconds) {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${s} sec`;
}

function showError(message) {
  els.error.textContent = message;
  els.error.hidden = false;
}

function setLoading(on) {
  clearTimeout(loadingTimer);
  els.loading.hidden = !on;
  els.go.disabled = on;
  els.url.disabled = on;
  if (on) {
    els.loadingText.textContent = "Fetching the transcript…";
    loadingTimer = setTimeout(() => {
      els.loadingText.textContent =
        "Writing the summary. Long videos can take a few minutes.";
    }, 3500);
  }
}


els.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = els.url.value.trim();
  if (!url) {
    showError("Paste a YouTube link to get started.");
    els.url.focus();
    return;
  }

 const length = document.querySelector('input[name="length"]:checked').value;

  els.error.hidden = true;
  els.result.hidden = true;
  setLoading(true);

  try {
    const data = await api("/api/summarize", { url, length });
    current = { video_id: data.video_id, markdown: data.summary };

    els.thumb.src = `https://i.ytimg.com/vi/${data.video_id}/hqdefault.jpg`;
    els.title.textContent = data.title;
    els.meta.textContent = [
      data.channel,
      formatDuration(data.duration_seconds),
      `${data.word_count.toLocaleString()} words of transcript`,
    ].filter(Boolean).join(", ");
    els.summary.innerHTML = renderMarkdown(data.summary);
    els.thread.innerHTML = "";
    els.copy.textContent = "Copy summary";

    els.result.hidden = false;
    els.result.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showError(err.message);
  } finally {
    setLoading(false);
  }
});

els.copy.addEventListener("click", async () => {
  if (!current) return;
  try {
    await navigator.clipboard.writeText(current.markdown);
    els.copy.textContent = "Copied";
  } catch {
    els.copy.textContent = "Copy failed";
  }
  setTimeout(() => { els.copy.textContent = "Copy summary"; }, 2000);
});

els.reset.addEventListener("click", () => {
  current = null;
  els.result.hidden = true;
  els.error.hidden = true;
  els.url.value = "";
  window.scrollTo({ top: 0, behavior: "smooth" });
  els.url.focus();
});


els.askForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = els.question.value.trim();
  if (!question || !current) return;

  const item = document.createElement("li");
  const q = document.createElement("p");
  q.className = "q";
  q.textContent = question;
  const a = document.createElement("div");
  a.className = "a pending";
  a.textContent = "Searching the transcript…";
  item.append(q, a);
  els.thread.prepend(item);

  els.question.value = "";
  els.askGo.disabled = true;

  try {
    const data = await api("/api/ask", { video_id: current.video_id, question });
    a.className = "a";
    a.innerHTML = renderMarkdown(data.answer);
  } catch (err) {
    a.className = "a failed";
    a.textContent = err.message;
  } finally {
    els.askGo.disabled = false;
    els.question.focus();
  }
});