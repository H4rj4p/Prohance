(() => {
  const messagesEl = document.getElementById("messages");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("message");
  const sendBtn = document.getElementById("send");
  const statusEl = document.getElementById("status");

  function addBubble(role, html) {
    const el = document.createElement("div");
    el.className = `bubble ${role}`;
    el.innerHTML = html;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function escapeHtml(text) {
    return String(text)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function renderTable(columns, rows) {
    if (!columns?.length) return "";
    const head = columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
    const body = rows
      .map(
        (row) =>
          `<tr>${row
            .map((cell) => `<td>${escapeHtml(cell ?? "")}</td>`)
            .join("")}</tr>`
      )
      .join("");
    return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  async function refreshHealth() {
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      const db = data.database || {};
      if (db.ok) {
        statusEl.textContent = `Connected to ${db.database || "SQL Server"}. OpenAI: ${
          data.openai_configured ? data.model : "not configured"
        }.`;
        statusEl.className = "status ok";
      } else {
        statusEl.textContent = `Database offline: ${db.error || "unknown error"}`;
        statusEl.className = "status bad";
      }
    } catch (err) {
      statusEl.textContent = `Health check failed: ${err.message}`;
      statusEl.className = "status bad";
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) return;

    addBubble("user", escapeHtml(message));
    input.value = "";
    sendBtn.disabled = true;
    const thinking = addBubble("assistant", "Thinking…");

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || `Request failed (${res.status})`);
      }

      thinking.innerHTML = `
        <div>${escapeHtml(data.answer || "")}</div>
        <div class="meta">
          <div>SQL</div>
          <pre>${escapeHtml(data.sql || "")}</pre>
          ${renderTable(data.columns, data.rows)}
          ${
            data.truncated
              ? "<p>Showing the first rows only (result truncated).</p>"
              : ""
          }
        </div>
      `;
    } catch (err) {
      thinking.classList.add("error");
      thinking.textContent = err.message || "Something went wrong.";
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  refreshHealth();
})();
