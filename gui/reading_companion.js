// Manual reading companion. It never clicks reward cards or auto-advances pages.
(() => {
  'use strict';

  const state = { queue: [], index: 0, startedAt: 0, timerId: null, targetSeconds: 300, elapsedSeconds: 0 };
  const $ = (id) => document.getElementById(id);

  function render() {
    const list = $('reading_queue');
    if (!list) return;
    list.replaceChildren();
    state.queue.forEach((item, index) => {
      const row = document.createElement('li');
      row.className = index === state.index ? 'reading-current' : '';
      const label = document.createElement('span');
      label.textContent = (index + 1) + '. ' + item.title;
      row.appendChild(label);
      const link = document.createElement('button');
      link.type = 'button';
      link.className = 'reading-open-link';
      link.textContent = 'Open';
      link.disabled = !item.url;
      link.addEventListener('click', () => open_item(index));
      row.appendChild(link);
      list.appendChild(row);
    });
    $('reading_position').textContent = state.queue.length
      ? 'Article ' + Math.min(state.index + 1, state.queue.length) + ' of ' + state.queue.length
      : 'No articles queued';
    $('reading_prev').disabled = state.index <= 0;
    $('reading_next').disabled = state.index >= state.queue.length - 1;
    $('reading_open').disabled = !state.queue[state.index];
  }

  function parse_queue() {
    const rows = $('reading_links').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    const parsed = [];
    for (const row of rows) {
      const split = row.indexOf('|');
      const title = (split < 0 ? row : row.slice(0, split)).trim();
      const url = (split < 0 ? '' : row.slice(split + 1)).trim();
      if (!title) continue;
      if (url) {
        let parsedUrl;
        try { parsedUrl = new URL(url); } catch (_) { throw new Error('Invalid URL for: ' + title); }
        if (parsedUrl.protocol !== 'https:') throw new Error('Only HTTPS article links are accepted: ' + title);
      }
      parsed.push({ title, url });
    }
    if (!parsed.length) throw new Error('Add at least one article title.');
    state.queue = parsed;
    state.index = 0;
    render();
  }

  async function open_item(index) {
    const item = state.queue[index];
    if (!item || !item.url) return;
    if (!window.pywebview || !pywebview.api || typeof pywebview.api.open_link !== 'function') {
      $('reading_status').textContent = 'Open links manually: ' + item.url;
      return;
    }
    const opened = await pywebview.api.open_link(item.url);
    $('reading_status').textContent = opened ? 'Opened: ' + item.title : 'Could not open the link. Open it manually.';
  }

  function update_clock() {
    if (!state.startedAt) return;
    state.elapsedSeconds = Math.floor((Date.now() - state.startedAt) / 1000);
    const minutes = Math.floor(state.elapsedSeconds / 60);
    const seconds = state.elapsedSeconds % 60;
    $('reading_clock').textContent = String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');
    if (state.elapsedSeconds >= state.targetSeconds) {
      $('reading_status').textContent = 'Target time reached. Finish reading, then choose Next article yourself.';
    }
  }

  function start_timer() {
    if (state.timerId) clearInterval(state.timerId);
    state.targetSeconds = Math.max(30, Math.min(3600, Number($('reading_duration').value) || 300));
    state.startedAt = Date.now();
    state.elapsedSeconds = 0;
    $('reading_status').textContent = 'Timer running. Read the article yourself; navigation stays manual.';
    update_clock();
    state.timerId = setInterval(update_clock, 500);
    $('reading_start').disabled = true;
    $('reading_stop').disabled = false;
  }

  function stop_timer() {
    if (state.timerId) clearInterval(state.timerId);
    state.timerId = null;
    state.startedAt = 0;
    $('reading_start').disabled = false;
    $('reading_stop').disabled = true;
    $('reading_status').textContent = 'Timer stopped at ' + $('reading_clock').textContent + '.';
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('reading_load').addEventListener('click', () => {
      try { parse_queue(); $('reading_status').textContent = 'Queue loaded. Select Open to visit an article.'; }
      catch (error) { $('reading_status').textContent = error.message; }
    });
    $('reading_open').addEventListener('click', () => open_item(state.index));
    $('reading_prev').addEventListener('click', () => { state.index = Math.max(0, state.index - 1); render(); });
    $('reading_next').addEventListener('click', () => { state.index = Math.min(state.queue.length - 1, state.index + 1); render(); });
    $('reading_start').addEventListener('click', start_timer);
    $('reading_stop').addEventListener('click', stop_timer);
    render();
  });
})();
