(() => {
  const start = document.getElementById('recording-start');
  if (!start) return;
  const form = document.getElementById('upload-form');
  const stop = document.getElementById('recording-stop'), retry = document.getElementById('recording-retry');
  const cancel = document.getElementById('recording-cancel'), download = document.getElementById('recording-download');
  const state = document.getElementById('recording-state'), progress = document.getElementById('recording-progress');
  const maxBytes = Number(document.querySelector('script[src="/static/recording.js"]').dataset.maxBytes);
  let recorder, stream, id, mime, chunks = [], next = 0, bytes = 0, busy = false;
  let ended = false, cancelled = false, active = false, failed = false, startedAt, timer, url;

  function status(text, error = false) {
    state.textContent = text;
    state.className = 'pill ' + (error ? 'red' : active ? 'amber' : 'gray');
    document.getElementById('recording-icon').classList.toggle('live', active && !ended);
  }
  function lock(on) {
    form.querySelectorAll('input,select,textarea,button').forEach(el => {
      if (!el.id.startsWith('recording-')) el.disabled = on;
    });
    start.disabled = on;
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    try {
      const response = await fetch(path, { ...options, headers: { accept: 'application/json', ...(options.headers || {}) }, signal: controller.signal });
      if (!response.ok) {
        let message = `Ошибка передачи (${response.status})`;
        try { const body = await response.json(); message = body.detail || message; } catch (_) { }
        throw new Error(message);
      }
      return await response.json();
    } finally { clearTimeout(timeout); }
  }
  function release() {
    if (stream) stream.getTracks().forEach(track => track.stop());
    clearInterval(timer);
  }
  function localCopy() {
    if (!chunks.length) return;
    if (url) URL.revokeObjectURL(url);
    url = URL.createObjectURL(new Blob(chunks, { type: mime }));
    download.href = url;
    download.download = 'sysai-recording.' + (mime.includes('mp4') ? 'm4a' : mime.includes('ogg') ? 'ogg' : 'webm');
    download.hidden = false;
  }
  function error(exc) {
    failed = true;
    status('Передача остановлена', true);
    progress.textContent = `${exc.message || exc}. Запись сохранена в этой вкладке. Повторите передачу или скачайте её после остановки.`;
    retry.hidden = false;
    if (recorder && recorder.state !== 'inactive') recorder.stop();
  }
  async function pump() {
    if (busy || failed || cancelled) return;
    busy = true;
    try {
      while (next < chunks.length && !cancelled) {
        const response = await request(`/recordings/${id}/chunks/${next}`, { method: 'POST', body: chunks[next] });
        if (response.next_sequence !== next + 1) throw new Error('Нарушен порядок аудиочастей');
        next++;
        progress.textContent = `Передано ${next} частей · ${(bytes / 1048576).toFixed(1)} МБ · ${Math.floor((Date.now() - startedAt) / 1000)} сек`;
      }
      if (ended && !cancelled) {
        status('Обработка записи');
        const result = await request(`/recordings/${id}/finish`, { method: 'POST' });
        active = false;
        lock(false);
        location.assign(result.url);
      }
    } catch (exc) { if (!cancelled) error(exc); }
    finally { busy = false; }
  }
  start.addEventListener('click', async () => {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      status('Микрофон недоступен', true);
      progress.textContent = 'Откройте страницу по HTTPS в браузере с поддержкой записи звука.';
      return;
    }
    lock(true); status('Разрешите доступ к микрофону');
    try {
      chunks = []; next = 0; bytes = 0; ended = false; cancelled = false; failed = false;
      download.hidden = true; retry.hidden = true;
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
      const supported = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(type => MediaRecorder.isTypeSupported(type));
      if (!supported) throw new Error('Браузер не поддерживает подходящий формат. Попробуйте Chrome, Firefox или Safari.');
      recorder = new MediaRecorder(stream, { mimeType: supported, audioBitsPerSecond: 64000 });
      mime = recorder.mimeType;
      lock(false);
      const data = new FormData(form);
      data.set('title', form.elements.mic_title.value);
      data.set('meeting_date', form.elements.mic_date.value);
      data.set('mime_type', mime); data.delete('file');
      lock(true);
      const session = await request('/recordings', { method: 'POST', body: data });
      id = session.id; active = true; startedAt = Date.now();
      recorder.addEventListener('dataavailable', event => {
        if (!event.data.size) return;
        chunks.push(event.data); bytes += event.data.size;
        if (cancelled) return;
        if (bytes > maxBytes) { error(new Error('Достигнут предел размера записи')); return; }
        pump();
      });
      recorder.addEventListener('stop', () => {
        ended = true; stop.disabled = true; release();
        localCopy();
        if (!cancelled && !failed) { status('Передача последних частей'); pump(); }
      });
      recorder.addEventListener('error', () => error(new Error('Ошибка записи микрофона')));
      stream.getAudioTracks().forEach(track => track.addEventListener('ended', () => {
        if (recorder.state !== 'inactive') recorder.stop();
      }));
      recorder.start(3000); stop.disabled = false; cancel.disabled = false; status('Идёт запись');
      progress.textContent = 'Микрофон включён. Первые части отправятся через несколько секунд.';
      timer = setInterval(() => {
        if (!failed && !ended) progress.textContent = `Запись ${Math.floor((Date.now() - startedAt) / 1000)} сек · ${(bytes / 1048576).toFixed(1)} МБ · передано ${next} частей`;
      }, 1000);
    } catch (exc) { active = false; release(); lock(false); status('Запись не началась', true); progress.textContent = exc.name === 'NotAllowedError' ? 'Доступ к микрофону запрещён. Разрешите его в настройках браузера и повторите.' : exc.message; }
  });
  stop.addEventListener('click', () => { stop.disabled = true; if (recorder?.state !== 'inactive') recorder.stop(); });
  retry.addEventListener('click', () => { failed = false; retry.hidden = true; status('Повтор передачи'); pump(); });
  cancel.addEventListener('click', async () => {
    if (!confirm('Отменить запись? Скачайте локальную копию перед удалением, если она нужна.')) return;
    cancelled = true;
    if (recorder?.state !== 'inactive') {
      await new Promise(resolve => { recorder.addEventListener('stop', resolve, { once: true }); recorder.stop(); });
    }
    release(); localCopy();
    try {
      await request(`/recordings/${id}/cancel`, { method: 'POST' });
      active = false; lock(false); cancel.disabled = true; stop.disabled = true; retry.hidden = true;
      status('Запись отменена'); progress.textContent = 'Обработка и рассылка не запускались.';
    } catch (exc) { status('Не удалось отменить на сервере', true); progress.textContent = exc.message; }
  });
  window.addEventListener('beforeunload', event => { if (active) { event.preventDefault(); event.returnValue = ''; } });
})();
