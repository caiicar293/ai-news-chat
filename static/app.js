/* News Chat front end - plain JavaScript, no framework, no build step. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    app: $("app"),
    sidebar: $("sidebar"),
    sidebarClose: $("sidebar-close"),
    menuBtn: $("menu-btn"),
    scrim: $("scrim"),
    chatList: $("chat-list"),
    newChatBtn: $("new-chat-btn"),
    chatTitle: $("chat-title"),
    chatSubtitle: $("chat-subtitle"),
    modelSelect: $("model-select"),
    modelCustom: $("model-custom"),
    articlesBtn: $("articles-btn"),
    articlesLabel: $("articles-btn-label"),
    articleCount: $("article-count"),
    banner: $("banner"),
    messages: $("messages"),
    composer: $("composer"),
    input: $("input"),
    sendBtn: $("send-btn"),
    statusDot: $("status-dot"),
    statusText: $("status-text"),
    reader: $("reader"),
    readerTitle: $("reader-title"),
    readerBack: $("reader-back"),
    readerClose: $("reader-close"),
    readerBody: $("reader-body"),
    newsDialog: $("news-dialog"),
    newsForm: $("news-form"),
    newsCancel: $("news-cancel"),
    newsSubmit: $("news-submit"),
    newsError: $("news-error"),
    newsTopic: $("news-topic"),
    newsPageSize: $("news-page-size"),
  };

  const CUSTOM_MODEL = "__custom__";

  const state = {
    chats: [],
    chat: null, // {id, title, query, messages, articles, model, ...}
    view: [], // messages drawn in the pane (may include client-side errors)
    models: [],
    defaultModel: "",
    sending: false,
    cooldownUntil: 0,
    cooldownTimer: null,
    readerOpen: false,
    currentArticle: null,
    cooldownWatchers: [],
  };

  const SUGGESTIONS = [
    "What are the biggest stories right now?",
    "Summarise the top 3 articles in bullet points.",
    "Which story matters most and why?",
  ];

  /* ------------------------------------------------------------------ */
  /* helpers                                                             */
  /* ------------------------------------------------------------------ */

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
    } catch (networkError) {
      const error = new Error(
        "Could not reach the server. Is the Flask app still running?"
      );
      error.status = 0;
      throw error;
    }

    let data = null;
    try {
      data = await response.json();
    } catch (parseError) {
      data = null;
    }

    if (!response.ok) {
      const error = new Error(
        (data && data.error) || `Request failed (HTTP ${response.status}).`
      );
      error.status = response.status;
      error.code = (data && data.code) || "http_" + response.status;
      error.detail = (data && data.detail) || "";
      error.retryAfter =
        (data && data.retry_after) ||
        Number(response.headers.get("Retry-After")) ||
        0;
      throw error;
    }

    return data || {};
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(
      /[&<>"']/g,
      (ch) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        }[ch])
    );
  }

  function inline(text) {
    return text
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(
        /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
      );
  }

  /* Tiny markdown renderer: the text is escaped first, then only the tags we
     generate ourselves are inserted, so answers can never inject HTML. */
  function renderMarkdown(text) {
    const parts = escapeHtml(text).split(/```/);
    let html = "";

    parts.forEach((chunk, index) => {
      if (index % 2 === 1) {
        const body = chunk.replace(/^[A-Za-z0-9_+-]*\r?\n/, "").replace(/\r?\n$/, "");
        html += `<pre><code>${body}</code></pre>`;
        return;
      }

      chunk.split(/\r?\n\s*\r?\n/).forEach((block) => {
        const trimmed = block.trim();
        if (!trimmed) return;

        const heading = trimmed.match(/^(#{1,3})\s+(.*)$/);
        if (heading) {
          const level = Math.min(heading[1].length + 1, 4);
          html += `<h${level}>${inline(heading[2])}</h${level}>`;
          return;
        }

        const lines = trimmed.split(/\r?\n/);
        if (lines.every((line) => /^\s*[-*+]\s+/.test(line))) {
          const items = lines
            .map((line) => `<li>${inline(line.replace(/^\s*[-*+]\s+/, ""))}</li>`)
            .join("");
          html += `<ul>${items}</ul>`;
          return;
        }

        if (lines.every((line) => /^\s*\d+[.)]\s+/.test(line))) {
          const items = lines
            .map((line) => `<li>${inline(line.replace(/^\s*\d+[.)]\s+/, ""))}</li>`)
            .join("");
          html += `<ol>${items}</ol>`;
          return;
        }

        const paragraph = lines.map((line) => inline(line)).join("<br>");
        html += `<p>${paragraph}</p>`;
      });
    });

    return html || "<p></p>";
  }

  function formatTime(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function toolLabel(call) {
    const detail = call.detail || "";
    if (call.tool === "list_articles_tool") {
      const count = /count=(\d+)/.exec(detail);
      return count ? `checked ${count[1]} headlines` : "checked the headlines";
    }
    if (call.tool === "fetch_article_full_text_tool") {
      const index = /index=(\d+)/.exec(detail);
      const which = index ? `article ${Number(index[1]) + 1}` : "an article";
      return detail.includes("newsapi_fallback")
        ? `read ${which} (snippet only)`
        : `read ${which}`;
    }
    return String(call.tool || "tool").replace(/_/g, " ");
  }

  function formatDate(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    return date.toLocaleString([], {
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function scrollToBottom() {
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function showBanner(message) {
    els.banner.textContent = message;
    els.banner.classList.toggle("hidden", !message);
  }

  /* ------------------------------------------------------------------ */
  /* boot                                                                */
  /* ------------------------------------------------------------------ */

  async function init() {
    wireEvents();
    await Promise.all([loadModels(), loadHealth()]);
    await loadChats();
  }

  async function loadHealth() {
    try {
      const health = await api("/api/health");
      if (health.ok) {
        els.statusDot.className = "status-dot is-ok";
        els.statusText.textContent = "Ready";
        showBanner("");
      } else {
        els.statusDot.className = "status-dot is-bad";
        els.statusText.textContent = "Setup needed";
        const python = health.python || "python";
        showBanner(
          "The backend is missing its Python packages, so chats cannot start.\n" +
            `Interpreter: ${python}\n` +
            `Install them with: "${python}" -m pip install -r requirements.txt\n\n` +
            (health.error || "")
        );
      }
    } catch (error) {
      els.statusDot.className = "status-dot is-bad";
      els.statusText.textContent = "Offline";
      showBanner(error.message);
    }
  }

  async function loadModels() {
    try {
      const data = await api("/api/models");
      state.models = data.models || [];
      state.defaultModel = data.default || "";

      els.modelSelect.innerHTML = "";
      state.models.forEach((model) => {
        const option = document.createElement("option");
        option.value = model.id;
        option.textContent = model.label;
        els.modelSelect.appendChild(option);
      });

      const custom = document.createElement("option");
      custom.value = CUSTOM_MODEL;
      custom.textContent = "Custom model id...";
      els.modelSelect.appendChild(custom);

      els.modelSelect.value = state.defaultModel;
      els.modelCustom.value = "";
      els.modelCustom.classList.add("hidden");
    } catch (error) {
      els.modelSelect.innerHTML = `<option value="${escapeHtml(
        state.defaultModel || "gemini-3.5-flash-lite"
      )}">${escapeHtml(state.defaultModel || "gemini-3.5-flash-lite")}</option>`;
    }
  }

  /* ------------------------------------------------------------------ */
  /* sidebar + chat list                                                 */
  /* ------------------------------------------------------------------ */

  async function loadChats() {
    try {
      const data = await api("/api/chats");
      state.chats = data.chats || [];
      renderSidebar();
      if (!state.chat && state.chats.length) {
        await openChat(state.chats[0].id);
      } else {
        renderHeader();
        renderMessages();
        renderComposer();
      }
    } catch (error) {
      els.chatList.innerHTML = `<p class="chat-list-empty">${escapeHtml(
        error.message
      )}</p>`;
    }
  }

  function renderSidebar() {
    els.chatList.innerHTML = "";

    if (!state.chats.length) {
      const empty = document.createElement("p");
      empty.className = "chat-list-empty";
      empty.textContent = "No chats yet. Start one to see it here.";
      els.chatList.appendChild(empty);
      return;
    }

    state.chats.forEach((chat) => {
      const item = document.createElement("div");
      item.className = "chat-item" + (state.chat && state.chat.id === chat.id ? " is-active" : "");
      item.dataset.id = chat.id;
      item.tabIndex = 0;
      item.setAttribute("role", "button");

      const title = document.createElement("span");
      title.className = "chat-item-title";
      title.textContent = chat.title || chat.query;

      const meta = document.createElement("span");
      meta.className = "chat-item-meta";
      const parts = [chat.query || "no news yet"];
      if (chat.message_count) {
        parts.push(
          `${chat.message_count} ${chat.message_count === 1 ? "message" : "messages"}`
        );
      }
      meta.textContent = parts.join(" \u00b7 ");

      const del = document.createElement("button");
      del.className = "chat-item-delete";
      del.type = "button";
      del.dataset.delete = chat.id;
      del.title = "Delete chat";
      del.setAttribute("aria-label", `Delete chat ${chat.title || chat.query}`);
      del.innerHTML = "&times;";

      item.append(title, meta, del);
      els.chatList.appendChild(item);
    });
  }

  async function openChat(id) {
    try {
      const data = await api(`/api/chats/${id}`);
      applyChat(data);
      closeSidebar();
    } catch (error) {
      showBanner(error.message);
      if (error.status === 404) {
        state.chats = state.chats.filter((chat) => chat.id !== id);
        renderSidebar();
      }
    }
  }

  function applyChat(detail) {
    state.chat = detail;
    state.view = (detail.messages || []).slice();
    state.currentArticle = null;

    if (detail.model) {
      const known = state.models.some((model) => model.id === detail.model);
      els.modelSelect.value = known ? detail.model : CUSTOM_MODEL;
      els.modelCustom.classList.toggle("hidden", known);
      if (!known) els.modelCustom.value = detail.model;
    }

    renderHeader();
    renderMessages();
    renderComposer();
    renderSidebar();
    closeReader();
  }

  function renderHeader() {
    if (!state.chat) {
      els.chatTitle.textContent = "News Chat";
      els.chatSubtitle.textContent = "Ask anything - or fetch today's headlines";
      els.articlesBtn.disabled = false;
      els.articlesLabel.textContent = "Get news";
      els.articleCount.textContent = "0";
      els.articleCount.classList.add("hidden");
      return;
    }

    const count = state.chat.article_count || 0;
    els.chatTitle.textContent = state.chat.title || state.chat.query || "New chat";
    els.chatSubtitle.textContent = count
      ? `Topic: ${state.chat.query} \u00b7 ${count} headlines loaded`
      : "No headlines loaded \u00b7 Get news adds them to this chat";
    els.articlesBtn.disabled = false;
    els.articlesLabel.textContent = count ? "Read articles" : "Get news";
    els.articleCount.textContent = String(count);
    els.articleCount.classList.toggle("hidden", count === 0);
    els.articlesBtn.title = count
      ? "Browse the fetched headlines and read one yourself"
      : "Fetch today's headlines for a topic";
  }

  async function deleteChat(id) {
    const chat = state.chats.find((item) => item.id === id);
    const label = chat ? chat.title || chat.query : "this chat";
    if (!window.confirm(`Delete "${label}"? This cannot be undone.`)) return;

    try {
      await api(`/api/chats/${id}`, { method: "DELETE" });
    } catch (error) {
      if (error.status !== 404) {
        showBanner(error.message);
        return;
      }
    }

    state.chats = state.chats.filter((item) => item.id !== id);
    if (state.chat && state.chat.id === id) {
      state.chat = null;
      state.view = [];
      closeReader();
      if (state.chats.length) {
        await openChat(state.chats[0].id);
      } else {
        renderHeader();
        renderMessages();
        renderComposer();
      }
    }
    renderSidebar();
  }

  /* ------------------------------------------------------------------ */
  /* new chat                                                            */
  /* ------------------------------------------------------------------ */

  /* A chat is just a conversation: no topic, no NewsAPI call. */
  async function createChat() {
    try {
      applyChat(await api("/api/chats", { method: "POST", body: "{}" }));
      showBanner("");
      await loadChats();
      closeSidebar();
      els.input.focus();
    } catch (error) {
      showBanner(error.message);
    }
  }

  function openNewsDialog(topic = "") {
    els.newsError.classList.add("hidden");
    els.newsError.textContent = "";
    els.newsSubmit.disabled = false;
    els.newsSubmit.textContent = "Fetch headlines";
    els.newsTopic.value = topic || (state.chat && state.chat.query) || "";
    els.newsDialog.showModal();
    els.newsTopic.focus();
    els.newsTopic.select();
  }

  /* Fetches headlines into the current chat, creating one if needed. */
  async function fetchNews(topic, pageSize) {
    els.newsSubmit.disabled = true;
    els.newsSubmit.textContent = "Fetching...";
    els.newsError.classList.add("hidden");

    try {
      if (!state.chat) {
        applyChat(await api("/api/chats", { method: "POST", body: "{}" }));
      }
      const data = await api(`/api/chats/${state.chat.id}/news`, {
        method: "POST",
        body: JSON.stringify({ query: topic, page_size: pageSize }),
      });
      els.newsDialog.close();
      applyChat(data.chat);
      openReader();
      showBanner("");
      await loadChats();
    } catch (error) {
      els.newsError.textContent = error.message;
      els.newsError.classList.remove("hidden");
    } finally {
      els.newsSubmit.disabled = false;
      els.newsSubmit.textContent = "Fetch headlines";
    }
  }

  /* ------------------------------------------------------------------ */
  /* messages                                                            */
  /* ------------------------------------------------------------------ */

  function renderMessages() {
    state.cooldownWatchers = [];
    els.messages.innerHTML = "";

    if (!state.chat) {
      renderWelcome({
        title: "Ask anything",
        body: "Chat with Gemini about whatever you like. News is one button away: press Get news to load today's headlines for a topic, then read any of them yourself.",
        action: "Get today's news",
        onAction: () => openNewsDialog(),
      });
      return;
    }

    if (!state.view.length) {
      if (state.chat.article_count > 0) {
        renderWelcome({
          title: `${state.chat.article_count} ${state.chat.query} headlines loaded`,
          body: "Ask anything about them - the assistant calls its article tools to look them up when a question needs the details.",
          browse: `Browse ${state.chat.article_count} headlines`,
        });
      } else {
        renderWelcome({
          title: "Ready when you are",
          body: "This chat is just conversation - no headlines are loaded yet. Press Get news to add today's news for a topic, or just start talking.",
          action: "Get today's news",
          onAction: () => openNewsDialog(),
        });
      }
      return;
    }

    state.view.forEach((message) => {
      els.messages.appendChild(buildMessage(message));
    });
    scrollToBottom();
  }

  function renderWelcome({ title, body, action, browse, onAction }) {
    const wrap = document.createElement("div");
    wrap.className = "welcome";
    wrap.innerHTML = `
      <h2>${escapeHtml(title)}</h2>
      <p>${escapeHtml(body)}</p>
      <div class="suggestions"></div>
    `;

    const box = wrap.querySelector(".suggestions");

    if (browse) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "suggestion suggestion-strong";
      chip.textContent = browse;
      chip.addEventListener("click", openReader);
      box.appendChild(chip);
    }

    if (action) {
      const start = document.createElement("button");
      start.type = "button";
      start.className = "suggestion";
      start.textContent = action;
      start.addEventListener("click", () => (onAction ? onAction() : openNewsDialog()));
      box.appendChild(start);
      els.messages.appendChild(wrap);
      return;
    }

    SUGGESTIONS.forEach((text) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "suggestion";
      chip.textContent = text;
      chip.addEventListener("click", () => {
        els.input.value = text;
        autoSize();
        els.input.focus();
      });
      box.appendChild(chip);
    });

    els.messages.appendChild(wrap);
  }

  function buildMessage(message) {
    const wrap = document.createElement("div");

    if (message.role === "error") {
      wrap.className = "msg msg-error";
      wrap.innerHTML = `
        <div class="msg-body">${escapeHtml(message.text)}</div>
        ${
          message.detail
            ? `<div class="msg-detail">${escapeHtml(message.detail)}</div>`
            : ""
        }
        <div class="msg-error-actions">
          <button type="button" class="retry-btn" data-retry>Try again</button>
          <span class="retry-note" data-note></span>
        </div>
      `;

      const action = wrap.querySelector("[data-retry]");
      const note = wrap.querySelector("[data-note]");
      const failedQuestion = message.question;

      const refreshNote = () => {
        const seconds = Math.ceil((state.cooldownUntil - Date.now()) / 1000);
        if (seconds > 0) {
          note.textContent = `Rate limited - wait ${seconds}s`;
          action.disabled = true;
        } else {
          note.textContent = "";
          action.disabled = state.sending || !failedQuestion;
        }
      };

      action.addEventListener("click", () => {
        state.view = state.view.filter((item) => item !== message);
        // Drop the user bubble of the failed attempt so the retry does not
        // appear twice in the transcript.
        const lastUser = [...state.view].reverse().find((item) => item.role === "user");
        if (lastUser && lastUser.text === failedQuestion) {
          state.view = state.view.filter((item) => item !== lastUser);
        }
        renderMessages();
        send(failedQuestion);
      });

      if (message.cooldown) {
        state.cooldownWatchers.push(refreshNote);
        refreshNote();
      }

      return wrap;
    }

    const isUser = message.role === "user";
    wrap.className = `msg msg-${isUser ? "user" : "model"}`;

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.innerHTML = `
      <span class="avatar">${isUser ? "YOU" : "AI"}</span>
      ${
        !isUser && message.model
          ? `<span class="msg-model-tag">${escapeHtml(message.model)}</span>`
          : ""
      }
      <span class="msg-time">${escapeHtml(formatTime(message.at))}</span>
    `;

    const body = document.createElement("div");
    body.className = "msg-body";

    if (message.pending) {
      body.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
    } else if (isUser) {
      body.textContent = message.text;
    } else {
      body.innerHTML = renderMarkdown(message.text || "");
    }

    wrap.append(meta, body);

    if (!isUser && message.tools && message.tools.length) {
      const tools = document.createElement("div");
      tools.className = "msg-tools";
      message.tools.forEach((call) => {
        const chip = document.createElement("span");
        chip.className = "tool-chip";
        chip.textContent = toolLabel(call);
        tools.appendChild(chip);
      });
      wrap.appendChild(tools);
    }

    return wrap;
  }

  /* ------------------------------------------------------------------ */
  /* sending                                                             */
  /* ------------------------------------------------------------------ */

  function selectedModel() {
    if (els.modelSelect.value === CUSTOM_MODEL) {
      return els.modelCustom.value.trim() || state.defaultModel;
    }
    return els.modelSelect.value || state.defaultModel;
  }

  function renderComposer() {
    const seconds = Math.ceil((state.cooldownUntil - Date.now()) / 1000);
    const cooling = seconds > 0;
    const hasNews = Boolean(state.chat && state.chat.article_count);

    els.input.disabled = state.sending;
    els.input.placeholder = hasNews
      ? "Ask about today's headlines..."
      : "Ask anything...";

    els.sendBtn.disabled = state.sending || cooling;
    els.sendBtn.textContent = state.sending
      ? "Sending..."
      : cooling
      ? `Wait ${seconds}s`
      : "Send";
  }

  function startCooldown(seconds) {
    const wait = Math.max(1, Number(seconds) || 5);
    state.cooldownUntil = Date.now() + wait * 1000;
    renderComposer();

    if (state.cooldownTimer) clearInterval(state.cooldownTimer);
    state.cooldownTimer = setInterval(() => {
      renderComposer();
      state.cooldownWatchers.forEach((fn) => fn());
      if (Date.now() >= state.cooldownUntil) {
        clearInterval(state.cooldownTimer);
        state.cooldownTimer = null;
        state.cooldownWatchers.forEach((fn) => fn());
      }
    }, 250);
  }

  async function send(rawQuestion) {
    const question = String(rawQuestion ?? els.input.value).trim();
    if (!question || state.sending) return;
    if (Date.now() < state.cooldownUntil) return;

    // Plain chatting is the default: the first message creates the chat.
    if (!state.chat) {
      try {
        applyChat(await api("/api/chats", { method: "POST", body: "{}" }));
        await loadChats();
      } catch (error) {
        showBanner(error.message);
        return;
      }
    }
    if (!state.chat) return;

    const chatId = state.chat.id;
    state.sending = true;

    els.input.value = "";
    autoSize();
    state.view.push({ role: "user", text: question, at: new Date().toISOString() });
    state.view.push({ role: "model", text: "", pending: true });
    renderMessages();
    renderComposer();

    try {
      const data = await api(`/api/chats/${chatId}/messages`, {
        method: "POST",
        body: JSON.stringify({ question, model: selectedModel() }),
      });

      if (state.chat && state.chat.id === chatId) {
        state.view = (data.messages || []).slice();
        state.chat.messages = state.view.slice();
        if (data.chat) {
          state.chat.title = data.chat.title;
          state.chat.updated_at = data.chat.updated_at;
          state.chat.model = data.chat.model;
        }
        renderHeader();
      }
      await loadChats();
    } catch (error) {
      state.view.pop(); // drop the pending bubble
      state.view.push({
        role: "error",
        text: error.message,
        detail: error.status === 429 ? "" : error.detail,
        question,
        cooldown: error.status === 429,
      });
      if (error.status === 429) startCooldown(error.retryAfter || 20);
    } finally {
      state.sending = false;
      renderMessages();
      renderComposer();
    }
  }

  /* ------------------------------------------------------------------ */
  /* article reader                                                      */
  /* ------------------------------------------------------------------ */

  function openReader() {
    if (!state.chat) return;
    state.readerOpen = true;
    state.currentArticle = null;
    els.app.classList.add("reader-open");
    els.reader.hidden = false;
    requestAnimationFrame(() => els.reader.classList.add("is-open"));
    renderArticleList();
  }

  function closeReader() {
    els.app.classList.remove("reader-open");
    if (els.reader.hidden) return;
    state.readerOpen = false;
    els.reader.classList.remove("is-open");
    window.setTimeout(() => {
      if (!state.readerOpen) els.reader.hidden = true;
    }, 200);
  }

  function renderArticleList() {
    const topic = state.chat && state.chat.query;
    els.readerTitle.textContent = topic ? `Articles \u00b7 ${topic}` : "Get news";
    els.readerBack.classList.add("hidden");
    els.readerBody.innerHTML = "";

    const articles = (state.chat && state.chat.articles) || [];
    if (!articles.length) {
      els.readerBody.innerHTML =
        '<p class="reader-empty">No headlines are loaded in this chat yet.<br>Fetch a topic and they show up here.</p>' +
        '<div class="reader-cta"><button type="button" class="primary-btn" id="reader-get-news">Get news</button></div>';
      const getNews = $("reader-get-news");
      if (getNews) {
        getNews.addEventListener("click", () => {
          closeReader();
          openNewsDialog(state.chat ? state.chat.query : "");
        });
      }
      return;
    }

    const hint = document.createElement("p");
    hint.className = "reader-hint";
    hint.textContent =
      "Click a headline to read the full text here - the same text the assistant gets from its article tools.";
    els.readerBody.appendChild(hint);

    articles.forEach((article) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "article-item";
      button.innerHTML = `
        <h3>${escapeHtml(article.title || "Untitled")}</h3>
        <div class="article-meta">
          <span>${escapeHtml(article.source || "Unknown source")}</span>
          <span>${escapeHtml(formatDate(article.publishedAt))}</span>
          ${
            article.has_full_text
              ? '<span class="badge">already read</span>'
              : ""
          }
        </div>
        ${
          article.description
            ? `<p class="article-desc">${escapeHtml(article.description)}</p>`
            : ""
        }
      `;
      button.addEventListener("click", () => showArticle(article.index));
      els.readerBody.appendChild(button);
    });
  }

  async function showArticle(index) {
    const chatId = state.chat && state.chat.id;
    if (!chatId) return;

    state.currentArticle = index;
    els.readerBack.classList.remove("hidden");
    els.readerTitle.textContent = "Reading";
    els.readerBody.innerHTML = `
      <div class="skeleton-line" style="height: 20px; width: 80%"></div>
      <div class="skeleton-line"></div>
      <div class="skeleton-line"></div>
      <div class="skeleton-line" style="width: 65%"></div>
      <div class="skeleton-line" style="margin-top: 22px"></div>
      <div class="skeleton-line" style="width: 90%"></div>
    `;

    try {
      const article = await api(`/api/chats/${chatId}/articles/${index}`);
      renderArticle(article);
    } catch (error) {
      els.readerBody.innerHTML = `
        <p class="reader-note">${escapeHtml(error.message)}</p>
        <button type="button" class="ghost-btn" id="reader-retry">Try again</button>
      `;
      const retry = $("reader-retry");
      if (retry) retry.addEventListener("click", () => showArticle(index));
    }
  }

  function renderArticle(article) {
    els.readerTitle.textContent = article.source || "Article";

    const paragraphs = String(article.text || "")
      .split(/\n{2,}/)
      .map((block) => `<p>${escapeHtml(block.trim())}</p>`)
      .join("");

    const snippetOnly = article.text_source === "newsapi_fallback";

    els.readerBody.innerHTML = `
      <article class="reader-article">
        <h2>${escapeHtml(article.title || "Untitled")}</h2>
        <div class="article-meta">
          <span>${escapeHtml(article.source || "Unknown source")}</span>
          <span>${escapeHtml(formatDate(article.publishedAt))}</span>
          ${
            article.author
              ? `<span>${escapeHtml(article.author)}</span>`
              : ""
          }
          <span class="badge${snippetOnly ? " is-warn" : ""}">${
      snippetOnly ? "snippet only" : "full text"
    }</span>
        </div>
        ${
          article.url
            ? `<a class="reader-original" href="${escapeHtml(
                article.url
              )}" target="_blank" rel="noopener noreferrer">Open original \u2197</a>`
            : ""
        }
        ${
          snippetOnly
            ? '<p class="reader-note">The full article could not be extracted (paywall, bot block or timeout) - this is the NewsAPI summary.</p>'
            : ""
        }
        <div class="reader-text">${paragraphs || "<p>(No text returned.)</p>"}</div>
      </article>
    `;

    els.readerBody.scrollTop = 0;
  }

  /* ------------------------------------------------------------------ */
  /* events                                                              */
  /* ------------------------------------------------------------------ */

  function autoSize() {
    els.input.style.height = "auto";
    els.input.style.height = `${Math.min(els.input.scrollHeight, 200)}px`;
  }

  function openSidebar() {
    els.app.classList.add("sidebar-open");
    els.scrim.hidden = false;
  }

  function closeSidebar() {
    els.app.classList.remove("sidebar-open");
    els.scrim.hidden = true;
  }

  function wireEvents() {
    els.newChatBtn.addEventListener("click", createChat);

    els.chatList.addEventListener("click", (event) => {
      const del = event.target.closest("[data-delete]");
      if (del) {
        event.stopPropagation();
        deleteChat(del.dataset.delete);
        return;
      }
      const item = event.target.closest(".chat-item");
      if (item && item.dataset.id !== (state.chat && state.chat.id)) {
        openChat(item.dataset.id);
      }
    });

    els.chatList.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const item = event.target.closest(".chat-item");
      if (!item) return;
      event.preventDefault();
      openChat(item.dataset.id);
    });

    els.newsForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const topic = els.newsTopic.value.trim();
      if (!topic) {
        els.newsTopic.focus();
        return;
      }
      const pageSize = Math.max(
        1,
        Math.min(100, Number(els.newsPageSize.value) || 20)
      );
      fetchNews(topic, pageSize);
    });

    els.newsCancel.addEventListener("click", () => els.newsDialog.close());

    els.composer.addEventListener("submit", (event) => {
      event.preventDefault();
      send();
    });

    els.input.addEventListener("input", autoSize);

    els.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        send();
      }
    });

    els.modelSelect.addEventListener("change", () => {
      const custom = els.modelSelect.value === CUSTOM_MODEL;
      els.modelCustom.classList.toggle("hidden", !custom);
      if (custom) {
        els.modelCustom.focus();
      } else {
        persistModel(els.modelSelect.value);
      }
    });

    els.modelCustom.addEventListener("change", () => {
      if (els.modelCustom.value.trim()) persistModel(els.modelCustom.value.trim());
    });

    els.articlesBtn.addEventListener("click", () => {
      if (!state.chat || !(state.chat.article_count > 0)) {
        openNewsDialog();
        return;
      }
      openReader();
    });
    els.readerClose.addEventListener("click", closeReader);
    els.readerBack.addEventListener("click", () => {
      state.currentArticle = null;
      renderArticleList();
    });

    els.menuBtn.addEventListener("click", openSidebar);
    els.sidebarClose.addEventListener("click", closeSidebar);
    els.scrim.addEventListener("click", closeSidebar);

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.readerOpen && !els.newsDialog.open) {
        closeReader();
      }
    });
  }

  async function persistModel(model) {
    if (!state.chat || !model) return;
    try {
      await api(`/api/chats/${state.chat.id}`, {
        method: "PATCH",
        body: JSON.stringify({ model }),
      });
      state.chat.model = model;
    } catch (error) {
      showBanner(`Could not save the model choice: ${error.message}`);
    }
  }

  init();
})();
