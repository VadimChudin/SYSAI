// SYSAI UI: source switch, upload with progress, microphone toggle, live status.
(function () {
  const form = document.getElementById('upload-form');
  if (form) {
    const input = document.getElementById('file'), drop = document.getElementById('drop'), text = document.getElementById('drop-text');
    // file / microphone switch
    document.querySelectorAll('#src-switch input').forEach(r => r.addEventListener('change', () => {
      document.querySelectorAll('.pane').forEach(p => p.classList.toggle('on', p.dataset.pane === r.value));
      input.required = r.value === 'file';
      history.replaceState(null, '', '?source=' + r.value);
    }));
    const show = () => { if (input.files[0]) text.innerHTML = '<b>' + input.files[0].name + '</b><br><small class="muted">' + (input.files[0].size / 1048576).toFixed(1) + ' МБ</small>'; };
    input.addEventListener('change', show);
    ['dragover', 'dragenter'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(e => drop.addEventListener(e, () => drop.classList.remove('over')));
    drop.addEventListener('drop', ev => { ev.preventDefault(); input.files = ev.dataTransfer.files; show(); });
    form.addEventListener('submit', ev => {
      const target = (ev.submitter && ev.submitter.getAttribute('formaction')) || form.getAttribute('action');
      if (target !== '/upload') return; // demo / microphone simulation: normal submit
      ev.preventDefault();
      const bar = document.getElementById('up-progress'), btn = document.getElementById('upload-btn');
      bar.classList.remove('hidden'); btn.disabled = true; btn.textContent = 'Загрузка…';
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/upload');
      xhr.upload.onprogress = e => { if (e.lengthComputable) bar.firstElementChild.style.width = (100 * e.loaded / e.total) + '%'; };
      xhr.onload = () => { if (xhr.status < 400) location = xhr.responseURL; else { document.open(); document.write(xhr.responseText); document.close(); } };
      xhr.onerror = () => { alert('Ошибка загрузки'); btn.disabled = false; btn.textContent = 'Загрузить и обработать'; };
      xhr.send(new FormData(form));
    });
  }
  const mic = document.getElementById('mic-toggle');
  if (mic) mic.addEventListener('change', async () => {
    const r = await fetch('/settings/mic', { method: 'POST', headers: { 'content-type': 'application/json', accept: 'application/json' }, body: JSON.stringify({ on: mic.checked }) });
    if (r.ok) location.reload(); else { mic.checked = !mic.checked; alert('Не удалось сохранить'); }
  });
  const pill = document.getElementById('status-pill');
  const dialogues = document.getElementById('employee-dialogues');
  const board = document.getElementById('progressboard');
  let editing = false;
  document.querySelectorAll('form[action$="/save"] input, form[action$="/save"] textarea, form[action$="/save"] select').forEach(el => el.addEventListener('input', () => { editing = true; }));
  if (pill && !board && (dialogues || ['queued', 'transcribing', 'analyzing', 'sending', 'delivery_queued', 'delivery_retry'].includes(pill.dataset.status))) {
    const tick = async () => {
      try {
        const r = await fetch('/meetings/' + pill.dataset.id + '/status', { headers: { accept: 'application/json' } });
        const j = await r.json();
        if (!editing && (j.status !== pill.dataset.status || (dialogues && JSON.stringify(j.dialogue_version) !== JSON.stringify(JSON.parse(dialogues.dataset.version))))) return location.reload();
        document.getElementById('progress').textContent = j.progress;
      } catch (e) { }
      setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  }
  let boardVersion, refreshing = false;
  document.addEventListener('sysai:board-update', async event => {
    if (!pill) return;
    const meeting = event.detail.meeting;
    pill.textContent = meeting.label;
    const colors = { error: 'red', delivery_failed: 'red', done: 'green', ready: 'green', awaiting_approval: 'amber', delivery_retry: 'amber', rejected: 'gray' };
    pill.className = 'pill ' + (colors[meeting.status] || 'blue');
    pill.dataset.status = meeting.status;
    document.getElementById('progress').textContent = meeting.progress;
    if (boardVersion !== event.detail.version && !refreshing) {
      refreshing = true;
      try {
        const response = await fetch(location.pathname, { headers: { accept: 'text/html' } });
        if (response.ok) {
          const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
          ['employee-dialogues', 'delivery-region', 'versions-region', 'meeting-live-status', ...(!editing ? ['report-editor'] : [])].forEach(id => {
            const current = document.getElementById(id), updated = doc.getElementById(id);
            if (current && updated && !current.contains(document.activeElement)) {
              const open = [...current.querySelectorAll('details[open]')].map(el => el.id);
              current.replaceWith(updated);
              if (id === 'report-editor') updated.querySelectorAll('input,textarea,select').forEach(el => el.addEventListener('input', () => { editing = true; }));
              open.forEach(detail => { const el = document.getElementById(detail); if (el) el.open = true; });
            }
          });
          openDialogue();
        }
      } catch (_) { }
      finally { refreshing = false; }
    }
    boardVersion = event.detail.version;
  });
  function openDialogue() {
    if (!location.hash.startsWith('#dialogue-')) return;
    const target = document.getElementById(location.hash.slice(1));
    if (target?.tagName === 'DETAILS') target.open = true;
  }
  window.addEventListener('hashchange', openDialogue);
  openDialogue();
})();
