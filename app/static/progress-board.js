(() => {
  const board = document.getElementById('progressboard');
  if (!board) return;

  const meetingId = board.dataset.mid;
  const flow = board.querySelector('[data-flow]');
  const peopleNode = board.querySelector('[data-people]');
  const reportNode = board.querySelector('[data-report-node]');
  const svg = board.querySelector('[data-connectors]');
  const connection = board.querySelector('[data-connection]');
  const statusLabels = {
    queued: 'В очереди', pending: 'Ожидает', waiting: 'Ждёт отправки',
    processing: 'Обрабатывается', sent: 'Доставлено', responded: 'Ответил',
    attention: 'Нужно внимание', no_recipient: 'Нет получателя', disabled: 'Рассылка выключена'
  };
  const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
  const mobileLayout = window.matchMedia('(max-width: 650px)');
  let version = null;
  let inFlight = false;
  let timer = null;
  let offline = false;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function stateLabel(state, label) {
    return label || statusLabels[state] || 'Состояние неизвестно';
  }

  function taskWord(count) {
    const lastTwo = count % 100;
    if (lastTwo >= 11 && lastTwo <= 14) return 'поручений';
    if (count % 10 === 1) return 'поручение';
    if (count % 10 >= 2 && count % 10 <= 4) return 'поручения';
    return 'поручений';
  }

  function safeHistoryUrl(value) {
    try {
      const url = new URL(value, window.location.origin);
      if (url.origin === window.location.origin &&
          (url.pathname.startsWith('/meetings/') || url.pathname === '/employees')) {
        return url.pathname + url.search + url.hash;
      }
    } catch (error) { }
    return '/meetings/' + encodeURIComponent(meetingId);
  }

  function personCard(person) {
    const state = statusLabels[person.state] ? person.state : 'pending';
    const card = element('article', 'progress-board-person');
    card.dataset.personId = String(person.id);

    const heading = element('div', 'progress-board-person-heading');
    const name = element('h3', 'progress-board-person-name', person.name || 'Без имени');
    const count = Number.isFinite(Number(person.task_count)) ? Number(person.task_count) : 0;
    const tasks = element('span', 'progress-board-task-count', `${count} ${taskWord(count)}`);
    heading.append(name, tasks);

    const status = element('span', `progress-board-state state-${state}`, stateLabel(person.state, person.label));
    const detail = element('p', 'progress-board-person-detail', person.detail || '');
    card.append(heading, status, detail);

    if (person.latest_message) {
      const message = element('p', 'progress-board-latest');
      message.append(element('span', 'progress-board-latest-label', 'Последний ответ'), document.createTextNode(person.latest_message));
      card.append(message);
    }

    const history = element('a', 'progress-board-history', 'История переписки →');
    history.href = safeHistoryUrl(person.history_url);
    card.append(history);
    return card;
  }

  function renderUnassigned(items) {
    const region = board.querySelector('[data-unassigned]');
    region.replaceChildren();
    if (!Array.isArray(items) || !items.length) {
      region.hidden = true;
      return;
    }
    region.hidden = false;
    region.append(element('strong', '', 'Без исполнителя'));
    const list = element('ul');
    items.forEach(item => list.append(element('li', '', item.title || 'Поручение без названия')));
    region.append(list);
  }

  function redrawConnectors() {
    svg.setAttribute('width', '0');
    svg.setAttribute('height', '0');
    const flowRect = flow.getBoundingClientRect();
    const width = Math.max(flow.scrollWidth, flowRect.width);
    const height = Math.max(flow.scrollHeight, flowRect.height);
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('width', String(width));
    svg.setAttribute('height', String(height));
    svg.replaceChildren();

    const reportRect = reportNode.getBoundingClientRect();
    const startX = mobileLayout.matches
      ? reportRect.left + reportRect.width / 2 - flowRect.left
      : reportRect.right - flowRect.left;
    const startY = mobileLayout.matches
      ? reportRect.bottom - flowRect.top
      : reportRect.top + reportRect.height / 2 - flowRect.top;

    Array.from(peopleNode.children).forEach((card, index) => {
      if (!card.dataset.personId) return;
      const rect = card.getBoundingClientRect();
      const endX = mobileLayout.matches
        ? rect.left + rect.width / 2 - flowRect.left
        : rect.left - flowRect.left;
      const endY = mobileLayout.matches
        ? rect.top - flowRect.top
        : rect.top + rect.height / 2 - flowRect.top;
      const pathData = mobileLayout.matches
        ? `M ${startX} ${startY} C ${startX} ${startY + 30}, ${endX} ${endY - 30}, ${endX} ${endY}`
        : `M ${startX} ${startY} C ${startX + 46} ${startY}, ${endX - 46} ${endY}, ${endX} ${endY}`;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', pathData);
      path.setAttribute('class', 'progress-board-connector');
      svg.append(path);

      if (card.dataset.active === 'true' && !offline && !motionPreference.matches) {
        const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        dot.setAttribute('r', '3.5');
        dot.setAttribute('class', 'progress-board-motion-dot');
        const motion = document.createElementNS('http://www.w3.org/2000/svg', 'animateMotion');
        motion.setAttribute('path', pathData);
        motion.setAttribute('dur', '3s');
        motion.setAttribute('repeatCount', 'indefinite');
        dot.append(motion);
        svg.append(dot);
      }
    });
  }

  function render(data) {
    const activeCard = document.activeElement?.closest('[data-person-id]');
    const focusedPerson = activeCard?.dataset.personId;
    const people = Array.isArray(data.people) ? data.people : [];
    const meeting = data.meeting || {};
    const summary = data.summary || {};

    board.querySelector('[data-report-title]').textContent = meeting.title || 'Совещание';
    board.querySelector('[data-report-status]').textContent =
      `${meeting.approved ? 'Утверждён' : 'Не утверждён'} · ${meeting.label || meeting.status || 'Статус неизвестен'}`;
    ['recipients', 'delivered', 'responded', 'attention'].forEach(key => {
      board.querySelector(`[data-summary="${key}"]`).textContent = Number(summary[key]) || 0;
    });

    const cards = people.map(person => {
      const card = personCard(person);
      card.dataset.active = person.active ? 'true' : 'false';
      return card;
    });
    peopleNode.replaceChildren(...cards);
    board.querySelector('[data-empty]').hidden = people.length > 0;
    renderUnassigned(data.unassigned);
    requestAnimationFrame(() => {
      redrawConnectors();
      if (focusedPerson) {
        const target = Array.from(peopleNode.children).find(card => card.dataset.personId === focusedPerson);
        target?.querySelector('a')?.focus({ preventScroll: true });
      }
    });
    document.dispatchEvent(new CustomEvent('sysai:board-update', { detail: data }));
  }

  async function poll() {
    if (inFlight || document.hidden) return;
    inFlight = true;
    try {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 12000);
      let response;
      try {
        response = await fetch(`/meetings/${encodeURIComponent(meetingId)}/board`, {
          headers: { accept: 'application/json' }, cache: 'no-store', signal: controller.signal
        });
      } finally { window.clearTimeout(timeout); }
      if (!response.ok) throw new Error(`Board request failed: ${response.status}`);
      const data = await response.json();
      const reconnecting = offline;
      offline = false;
      if (data.version !== version) {
        render(data);
        version = data.version;
      }
      if (reconnecting) { connection.textContent = 'Соединение восстановлено · обновление каждые 2 сек'; redrawConnectors(); }
      else if (connection.textContent.startsWith('Подключаемся') || connection.textContent.startsWith('Проверяем')) {
        connection.textContent = 'Данные актуальны · обновление каждые 2 сек';
      }
    } catch (error) {
      if (!offline) {
        connection.textContent = version
          ? 'Связь потеряна · показаны последние данные'
          : 'Нет соединения · повторяем попытку';
      }
      offline = true;
      redrawConnectors();
    } finally {
      inFlight = false;
      if (!document.hidden) timer = window.setTimeout(poll, 2000);
    }
  }

  function resumePolling() {
    if (document.hidden) {
      if (!offline) connection.textContent = 'Автообновление при возвращении на вкладку';
      return;
    }
    window.clearTimeout(timer);
    if (!offline) connection.textContent = version ? 'Проверяем соединение…' : 'Подключаемся…';
    requestAnimationFrame(redrawConnectors);
    poll();
  }

  window.addEventListener('resize', redrawConnectors);
  motionPreference.addEventListener?.('change', redrawConnectors);
  mobileLayout.addEventListener?.('change', redrawConnectors);
  document.addEventListener('visibilitychange', resumePolling);
  poll();
})();
