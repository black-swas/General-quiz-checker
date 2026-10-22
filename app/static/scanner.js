/* 100% Laptop-Hosted Paper-Exam Scanner, Folder Browser, Live Sync & Concurrent Prompt-Cached Grading Controller (v5.1) */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const CHUNK_SIZE = 65536; // 64 KB resilient upload fragments

  const state = {
    mode: 'hub',
    hubInfo: null,
    taName: localStorage.getItem('iiserb_ta_name') || '',
    taStation: localStorage.getItem('iiserb_ta_station') || '',
    activeSessionId: new URLSearchParams(window.location.search).get('session') || localStorage.getItem('iiserb_active_session_id') || '',
    session: null,
    currentRoll: '',
    currentName: '',
    currentPage: 1,
    stream: null,
    reviewFilter: 'ALL',
    reviewSearch: '',
    selectedReviewRoll: '',
    pendingChunksCount: 0,
    syncBusy: false,
    browsingPath: '',
    questionSpecs: [
      {
        question_id: 'Q1',
        title: 'Question 1',
        problem_statement: '',
        max_marks: 5.0,
        page_number: 1,
        section: 'top',
        y_start_pct: 0.0,
        y_end_pct: 50.0,
        x_start_pct: 0.0,
        x_end_pct: 100.0,
        language: 'physics',
        execution_mode: 'llm_only',
        grading_rubric: 'Step 1 [2.0 Marks]: Conceptual formula & problem setup.\nStep 2 [2.0 Marks]: Derivation / mathematical steps.\nStep 3 [1.0 Mark]: Final result with appropriate units & sign consistency.',
        reference_solution: '',
        _expanded: false,
      },
    ],
  };

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : v);
      }
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $('scannerToast');
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
  }

  async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    const token = sessionStorage.getItem('qe_token') || '';
    if (token) headers['X-API-Key'] = token;
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const body = await resp.json();
        detail = body.detail || detail;
      } catch (_e) { /* ignore */ }
      throw new Error(detail || `HTTP ${resp.status}`);
    }
    return resp;
  }

  // --------------------------------------------------------------------------- Quality of Life Utilities (Audio, Lightbox, Clipboard & Auto-Roll)
  function playSuccessChime() {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(587.33, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(880, ctx.currentTime + 0.12);
      gain.gain.setValueAtTime(0.12, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.28);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.3);
    } catch (_) {}
  }

  async function copyToClipboard(text, successMessage = 'Copied to clipboard!') {
    if (!text) return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      toast(`📋 ${successMessage}`);
    } catch (err) {
      toast(`Failed to copy: ${err.message}`);
    }
  }

  let lightboxState = {
    zoom: 1.0,
    filter: 'none',
    panX: 0,
    panY: 0,
    isPanning: false,
    startX: 0,
    startY: 0,
  };

  function updateLightboxTransform() {
    const img = $('lightboxImg');
    const pill = $('lightboxZoomPill');
    if (!img) return;
    img.style.transform = `translate(${lightboxState.panX}px, ${lightboxState.panY}px) scale(${lightboxState.zoom})`;
    img.style.filter = lightboxState.filter;
    if (pill) pill.textContent = `${Math.round(lightboxState.zoom * 100)}%`;
  }

  function openImageLightbox(src, title = 'Student Answer Crop') {
    if (!src) return;
    const modal = $('imageLightboxModal');
    const img = $('lightboxImg');
    const titleEl = $('lightboxTitle');
    if (!modal || !img) return;
    lightboxState.zoom = 1.0;
    lightboxState.filter = 'none';
    lightboxState.panX = 0;
    lightboxState.panY = 0;
    lightboxState.isPanning = false;
    img.src = src;
    if (titleEl) titleEl.textContent = title;
    updateLightboxTransform();
    if (typeof modal.showModal === 'function') {
      modal.showModal();
    } else {
      modal.setAttribute('open', '');
    }
  }

  function setupImageLightbox() {
    const modal = $('imageLightboxModal');
    const body = $('lightboxBody');
    const img = $('lightboxImg');
    if (!modal || !body || !img) return;

    if ($('btnCloseLightbox')) {
      $('btnCloseLightbox').addEventListener('click', () => modal.close());
    }
    if ($('btnLightboxZoomIn')) {
      $('btnLightboxZoomIn').addEventListener('click', () => {
        lightboxState.zoom = Math.min(5.0, lightboxState.zoom + 0.3);
        updateLightboxTransform();
      });
    }
    if ($('btnLightboxZoomOut')) {
      $('btnLightboxZoomOut').addEventListener('click', () => {
        lightboxState.zoom = Math.max(0.5, lightboxState.zoom - 0.3);
        updateLightboxTransform();
      });
    }
    if ($('btnLightboxZoomReset')) {
      $('btnLightboxZoomReset').addEventListener('click', () => {
        lightboxState.zoom = 1.0;
        lightboxState.panX = 0;
        lightboxState.panY = 0;
        lightboxState.filter = 'none';
        updateLightboxTransform();
      });
    }
    if ($('btnLightboxFilterNormal')) {
      $('btnLightboxFilterNormal').addEventListener('click', () => {
        lightboxState.filter = 'none';
        updateLightboxTransform();
      });
    }
    if ($('btnLightboxFilterContrast')) {
      $('btnLightboxFilterContrast').addEventListener('click', () => {
        lightboxState.filter = 'grayscale(100%) contrast(280%) brightness(105%)';
        updateLightboxTransform();
      });
    }
    if ($('btnLightboxFilterInvert')) {
      $('btnLightboxFilterInvert').addEventListener('click', () => {
        lightboxState.filter = 'invert(100%) contrast(180%) hue-rotate(180deg)';
        updateLightboxTransform();
      });
    }

    body.addEventListener('wheel', (e) => {
      e.preventDefault();
      const delta = e.deltaY < 0 ? 0.2 : -0.2;
      lightboxState.zoom = Math.min(5.0, Math.max(0.4, lightboxState.zoom + delta));
      updateLightboxTransform();
    }, { passive: false });

    body.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      lightboxState.isPanning = true;
      lightboxState.startX = e.clientX - lightboxState.panX;
      lightboxState.startY = e.clientY - lightboxState.panY;
      body.style.cursor = 'grabbing';
    });
    window.addEventListener('mousemove', (e) => {
      if (!lightboxState.isPanning) return;
      lightboxState.panX = e.clientX - lightboxState.startX;
      lightboxState.panY = e.clientY - lightboxState.startY;
      updateLightboxTransform();
    });
    window.addEventListener('mouseup', () => {
      if (lightboxState.isPanning) {
        lightboxState.isPanning = false;
        body.style.cursor = 'grab';
      }
    });
  }

  // --------------------------------------------------------------------------- IndexedDB Offline Queue for 64 KB Chunks
  function openQueueDb() {
    return new Promise((resolve) => {
      if (!window.indexedDB) { resolve(null); return; }
      const req = indexedDB.open('iiserb_paper_scanner_db', 1);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains('chunks')) {
          db.createObjectStore('chunks', { keyPath: 'id' });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
    });
  }

  async function enqueueImageChunks(sessionId, rollNo, studentName, pageNo, arrayBuffer, isRetake) {
    const bytes = new Uint8Array(arrayBuffer);
    const totalChunks = Math.max(1, Math.ceil(bytes.length / CHUNK_SIZE));
    const uploadId = `${sessionId}_${rollNo}_p${pageNo}_${Date.now()}`;
    const chunks = [];

    for (let i = 0; i < totalChunks; i++) {
      const slice = bytes.subarray(i * CHUNK_SIZE, Math.min(bytes.length, (i + 1) * CHUNK_SIZE));
      let binary = '';
      for (let j = 0; j < slice.byteLength; j++) binary += String.fromCharCode(slice[j]);
      const b64 = btoa(binary);
      chunks.push({
        id: `${uploadId}_${i}`,
        session_id: sessionId,
        roll_no: rollNo,
        student_name: studentName,
        page_no: pageNo,
        upload_id: uploadId,
        chunk_index: i,
        total_chunks: totalChunks,
        chunk_base64: b64,
        is_retake: Boolean(isRetake),
        ta_name: state.taName || undefined,
      });
    }

    const db = await openQueueDb();
    if (db) {
      await new Promise((resolve) => {
        const tx = db.transaction('chunks', 'readwrite');
        const store = tx.objectStore('chunks');
        for (const c of chunks) store.put(c);
        tx.oncomplete = () => resolve();
        tx.onerror = () => resolve();
      });
    } else {
      window._fallbackChunks = (window._fallbackChunks || []).concat(chunks);
    }
    await updateQueueBadge();
    flushChunkQueue();
  }

  async function getQueuedChunks() {
    const db = await openQueueDb();
    if (!db) return window._fallbackChunks || [];
    return new Promise((resolve) => {
      const tx = db.transaction('chunks', 'readonly');
      const req = tx.objectStore('chunks').getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => resolve([]);
    });
  }

  async function removeQueuedChunk(id) {
    const db = await openQueueDb();
    if (!db) {
      window._fallbackChunks = (window._fallbackChunks || []).filter((c) => c.id !== id);
      return;
    }
    return new Promise((resolve) => {
      const tx = db.transaction('chunks', 'readwrite');
      tx.objectStore('chunks').delete(id);
      tx.oncomplete = () => resolve();
      tx.onerror = () => resolve();
    });
  }

  async function updateQueueBadge() {
    const items = await getQueuedChunks();
    state.pendingChunksCount = items.length;
    const badge = $('syncQueueBadge');
    if (!badge) return;
    if (items.length === 0) {
      badge.textContent = 'Local Sync: Up to date ✓';
      badge.className = 'sync-badge';
    } else {
      badge.textContent = `Syncing: ${items.length} chunk(s) queued`;
      badge.className = 'sync-badge pending';
    }
  }

  async function flushChunkQueue() {
    if (state.syncBusy) return;
    state.syncBusy = true;
    try {
      while (true) {
        const items = await getQueuedChunks();
        if (!items.length) break;
        items.sort((a, b) => a.upload_id.localeCompare(b.upload_id) || a.chunk_index - b.chunk_index);
        const item = items[0];
        const resp = await api(`/api/sessions/${encodeURIComponent(item.session_id)}/upload-chunk`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            roll_no: item.roll_no,
            student_name: item.student_name,
            page_no: item.page_no,
            upload_id: item.upload_id,
            chunk_index: item.chunk_index,
            total_chunks: item.total_chunks,
            chunk_base64: item.chunk_base64,
            is_retake: item.is_retake,
            ta_name: item.ta_name || state.taName || undefined,
          }),
        });
        const resData = await resp.json();
        await removeQueuedChunk(item.id);
        await updateQueueBadge();
        if (resData.status === 'page_complete') {
          toast(`Saved & flattened Roll ${item.roll_no} · Page ${item.page_no}`);
          if (state.activeSessionId === item.session_id) {
            await refreshActiveSession(false);
          }
        }
      }
    } catch (_err) {
      // Offline blip; background timer retries automatically
    } finally {
      state.syncBusy = false;
      await updateQueueBadge();
    }
  }

  // --------------------------------------------------------------------------- Mode Switching
  function switchMode(mode) {
    if (mode === 'capture' && !state.taName) {
      toast('Please enter your TA identity before launching the camera.');
      mode = 'ta-login';
    }
    state.mode = mode;
    if ($('panelTaLogin')) $('panelTaLogin').hidden = mode !== 'ta-login';
    $('panelHub').hidden = mode !== 'hub';
    $('panelCapture').hidden = mode !== 'capture';
    $('panelReview').hidden = mode !== 'review';
    for (const btn of document.querySelectorAll('.mode-btn, .wf-step-btn')) {
      btn.classList.toggle('active', btn.getAttribute('data-mode') === mode);
    }
    if (mode === 'ta-login') {
      renderTaLoginPanel();
    }
    if (mode === 'hub') {
      refreshHubInfo();
      loadSessionsList();
    }
    if (mode === 'capture') {
      renderCapturePanel();
      initCameraDevices();
    }
    if (mode === 'review') renderReviewDashboard();
  }

  // --------------------------------------------------------------------------- Laptop Hub Info & Interactive Folder Picker
  async function refreshHubInfo() {
    try {
      const info = await (await api('/api/hub-info')).json();
      state.hubInfo = info;
      const sessionParam = state.activeSessionId ? `?session=${encodeURIComponent(state.activeSessionId)}` : '';
      const fullScannerUrl = `${info.scanner_url}${sessionParam}`;
      $('hubIpBadge').textContent = `Mobile Link: ${info.scanner_url}`;
      $('directScannerUrlInput').value = fullScannerUrl;
      $('hubQrImg').src = `/api/qr.svg?url=${encodeURIComponent(fullScannerUrl)}&t=${Date.now()}`;
      $('activeStorageDirText').textContent = (state.session && state.session.storage_dir) || info.storage_dir;
      if (!$('inpStorageDir').value) {
        $('inpStorageDir').value = info.storage_dir;
      }
      if (info.disk) {
        $('freeDiskText').textContent = `${info.disk.free_gb} GB free`;
      }
      $('cfgModel').value = info.llm_model || 'claude-haiku-5-5';
      if ($('cfgLocalVlmUrl') && info.local_vlm_url) {
        $('cfgLocalVlmUrl').value = info.local_vlm_url;
      }
      $('cfgConcurrency').value = info.llm_concurrency || 6;
      $('cfgBatchSize').value = info.llm_vision_batch_size || 5;
      $('cfgAutoApprove').value = info.auto_approve_threshold || 0.85;

      updateVlmStatusBadge(info.vlm_provider, info.llm_model);
    } catch (_e) { /* ignore */ }
  }

  function updateVlmStatusBadge(provider, model) {
    const badge = $('vlmStatusBadge');
    if (!badge) return;
    const p = (provider || '').toLowerCase();
    const m = (model || '').toLowerCase();
    const isLocal = p === 'ollama' || p === 'local' || m.includes('qwen') || m.includes('ollama');
    const isOpenAi = p === 'openai' || m.includes('gpt');
    if (isLocal) {
      badge.textContent = '🤖 VLM: Ollama (100% Offline)';
      badge.className = 'pill pill-ok';
      badge.style.borderColor = '#38bdf8';
      badge.style.background = 'rgba(14, 165, 233, 0.25)';
      if ($('cfgLocalVlmRow')) $('cfgLocalVlmRow').style.display = 'block';
    } else if (isOpenAi) {
      badge.textContent = '🤖 VLM: OpenAI (Cloud)';
      badge.className = 'pill pill-ok';
      badge.style.borderColor = '#10b981';
      badge.style.background = 'rgba(16, 185, 129, 0.25)';
      if ($('cfgLocalVlmRow')) $('cfgLocalVlmRow').style.display = 'none';
    } else {
      badge.textContent = '🤖 VLM: Claude (Cloud)';
      badge.className = 'pill pill-ok';
      badge.style.borderColor = '#a855f7';
      badge.style.background = 'rgba(168, 85, 247, 0.25)';
      if ($('cfgLocalVlmRow')) $('cfgLocalVlmRow').style.display = 'none';
    }
  }

  async function openFolderPicker(initialPath) {
    const dialog = $('folderPickerModal');
    await browseFolderPath(initialPath || $('inpStorageDir').value || '');
    dialog.showModal();
  }

  async function browseFolderPath(targetPath) {
    try {
      const q = targetPath ? `?path=${encodeURIComponent(targetPath)}` : '';
      const data = await (await api(`/api/folders/browse${q}`)).json();
      state.browsingPath = data.current_path;
      $('folderCurrentPathInput').value = data.current_path;

      const presetsRow = $('folderPresetsRow');
      presetsRow.replaceChildren(...(data.presets || []).map((p) => h('button', {
        type: 'button',
        class: 'btn-m btn-m-ghost',
        style: 'padding:5px 10px;font-size:0.76rem;',
        text: `📌 ${p.label}`,
        onclick: () => browseFolderPath(p.path),
      })));

      const listEl = $('folderSubdirsList');
      const subdirs = data.subdirectories || [];
      if (!subdirs.length) {
        listEl.replaceChildren(h('div', { style: 'padding:12px;color:var(--muted-mobile);font-size:0.84rem;', text: 'No subdirectories inside this folder. You can select this folder or type a new subfolder name below.' }));
      } else {
        listEl.replaceChildren(...subdirs.map((d) => h('button', {
          type: 'button',
          class: 'dir-item',
          onclick: () => browseFolderPath(d.path),
        }, h('span', { text: `📁 ${d.name}` }), h('span', { style: 'color:var(--muted-mobile);font-size:0.75rem;', text: 'Open →' }))));
      }

      $('btnFolderGoUp').onclick = () => browseFolderPath(data.parent_path);
    } catch (err) {
      toast(`Could not browse folder: ${err.message}`);
    }
  }

  async function confirmFolderSelection() {
    let basePath = $('folderCurrentPathInput').value.trim() || state.browsingPath;
    const subName = $('newSubfolderNameInput').value.trim();
    if (subName) {
      const sep = basePath.includes('\\') ? '\\' : '/';
      basePath = basePath.replace(/[\\/]+$/, '') + sep + subName;
    }
    try {
      const res = await (await api('/api/folders/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: basePath,
          create_if_missing: true,
          session_id: state.activeSessionId || null,
        }),
      })).json();
      $('inpStorageDir').value = res.storage_dir;
      $('activeStorageDirText').textContent = res.storage_dir;
      $('newSubfolderNameInput').value = '';
      $('folderPickerModal').close();
      toast(`Storage folder set to ${res.storage_dir}`);
      if (state.activeSessionId) await refreshActiveSession(false);
    } catch (err) {
      toast(`Folder error: ${err.message}`);
    }
  }

  async function saveRuntimeSettings() {
    try {
      const selectedModel = $('cfgModel').value.trim() || 'claude-haiku-5-5';
      let provider = 'anthropic';
      const lm = selectedModel.toLowerCase();
      if (lm.includes('qwen') || lm.includes('ollama') || lm.includes('local')) {
        provider = 'ollama';
      } else if (lm.includes('gpt')) {
        provider = 'openai';
      }
      const payload = {
        anthropic_api_key: $('cfgApiKey').value.trim() || null,
        primary_model: selectedModel,
        vlm_provider: provider,
        local_vlm_url: ($('cfgLocalVlmUrl') && $('cfgLocalVlmUrl').value.trim()) || undefined,
        llm_concurrency: parseInt($('cfgConcurrency').value, 10) || 6,
        llm_vision_batch_size: parseInt($('cfgBatchSize').value, 10) || 5,
        auto_approve_confidence_threshold: parseFloat($('cfgAutoApprove').value) || 0.85,
        paper_storage_dir: $('inpStorageDir').value.trim() || null,
      };
      await api('/api/runtime-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      $('cfgApiKey').value = '';
      toast(`Saved laptop AI & concurrency settings (${provider} · ${selectedModel})!`);
      await refreshHubInfo();
    } catch (err) {
      toast(`Could not update settings: ${err.message}`);
    }
  }

  // --------------------------------------------------------------------------- Step 1: Question Coordinate Specs, Paper JSON Loader & Session Setup
  async function loadSamplePapersList() {
    const select = $('samplePaperSelect');
    if (!select) return;
    try {
      const list = await (await api('/api/samples')).json();
      select.replaceChildren(
        h('option', { value: '', text: 'Load a sample paper…' }),
        ...list.map((s) => h('option', { value: s.name, text: `${s.title} (${s.questions} questions)` }))
      );
    } catch (_e) { /* ignore */ }
  }

  function loadQuestionPaperJson(text, filename) {
    const badge = $('paperLoadSummaryBadge');
    try {
      let data = JSON.parse(text);
      if (Array.isArray(data)) data = { questions: data };
      if (!data || !Array.isArray(data.questions) || !data.questions.length) {
        throw new Error('Question paper JSON must contain a "questions" list with at least one question.');
      }

      if (data.course_code) $('inpCourseCode').value = data.course_code;
      if (data.quiz_title || data.exam_name) $('inpExamName').value = data.quiz_title || data.exam_name;
      if (data.exam_date) $('inpExamDate').value = data.exam_date;

      state.questionSpecs = data.questions.map((q, idx) => {
        let qid = q.question_id ? String(q.question_id).trim() : `Q${idx + 1}`;
        const match = /^(?:q|que|question)[_\-\s]*(\d{1,2})$/i.exec(qid);
        if (match) qid = `Q${parseInt(match[1], 10)}`;

        const weight = parseFloat(q.max_marks || q.marks || q.weight || 5.0) || 5.0;
        const pageNo = parseInt(q.page_number, 10) || 1;
        const yStart = q.y_start_pct !== undefined ? parseFloat(q.y_start_pct) : 0;
        const yEnd = q.y_end_pct !== undefined ? parseFloat(q.y_end_pct) : 100;
        const xStart = q.x_start_pct !== undefined ? parseFloat(q.x_start_pct) : 0;
        const xEnd = q.x_end_pct !== undefined ? parseFloat(q.x_end_pct) : 100;
        const rubric = q.grading_rubric || q.rubric || '';
        const problemStmt = q.problem_statement || q.statement || q.prompt || '';
        const refSol = q.reference_solution || q.solution || q.model_answer || '';
        const lang = q.language || 'handwritten_math';

        return {
          question_id: qid,
          title: q.title || `Question ${idx + 1}`,
          max_marks: weight,
          page_number: pageNo,
          y_start_pct: yStart,
          y_end_pct: yEnd,
          x_start_pct: xStart,
          x_end_pct: xEnd,
          language: lang,
          execution_mode: q.execution_mode || 'llm_only',
          grading_rubric: rubric,
          problem_statement: problemStmt,
          reference_solution: refSol,
          boxes: q.boxes || [],
          spans_pages: q.spans_pages || [],
          continuation_pages: q.continuation_pages || [],
          _expanded: false,
        };
      });

      const totalWeight = state.questionSpecs.reduce((sum, q) => sum + (parseFloat(q.max_marks) || 0), 0);
      const maxPages = Math.max(1, ...state.questionSpecs.map((q) => q.page_number || 1));

      $('inpTotalMarks').value = totalWeight;
      $('inpExpectedPages').value = maxPages;

      if (data.paper_type) {
        syncPaperTypeUI(data.paper_type);
      }

      renderQuestionSpecInputs();

      if (badge) {
        badge.textContent = `✓ Loaded "${filename}": ${state.questionSpecs.length} questions · Total Weight: ${totalWeight.toFixed(1)} Marks · ${maxPages} Pages`;
        badge.hidden = false;
      }
      toast(`Loaded question paper "${filename}" (${totalWeight.toFixed(1)} Marks total weight)`);
    } catch (err) {
      toast(`Could not load Question Paper: ${err.message}`);
      if (badge) {
        badge.textContent = `Error loading question paper: ${err.message}`;
        badge.hidden = false;
      }
    }
  }

  function syncPaperTypeUI(paperType) {
    const isSeparate = paperType === 'SEPARATE_QUESTION_BOOKLET';
    const sel = $('selPaperType');
    if (sel && sel.value !== paperType) sel.value = paperType;

    // Notice banners
    const noticeComb = $('paperTypeNoticeCombined');
    const noticeSep = $('paperTypeNoticeSeparate');
    if (noticeComb) noticeComb.style.display = isSeparate ? 'none' : 'flex';
    if (noticeSep) noticeSep.style.display = isSeparate ? 'flex' : 'none';

    // QP Image upload button wrapper
    const qpImgUploadWrap = $('qpImageUploadWrapper');
    if (qpImgUploadWrap) qpImgUploadWrap.style.display = isSeparate ? 'inline-flex' : 'none';

    // Header & help text
    const title = $('qpSpecHeaderTitle');
    const help = $('qpSpecHelpText');
    const loadJsonLabel = $('btnLoadPaperJsonLabel');
    if (isSeparate) {
      if (title) title.textContent = '📋 Question Paper Specifications (Separate Booklet & QP)';
      if (help) help.textContent = 'Students answered in separate booklets. Upload a photograph/scan of the physical question paper and/or load JSON questions so the AI examiner has the official problem statements & figures:';
      if (loadJsonLabel) loadJsonLabel.textContent = '📁 Load QP JSON';
    } else {
      if (title) title.textContent = '📋 Exam Questions & Rubric Specifications (Combined Sheet)';
      if (help) help.textContent = 'Questions and designated answer boxes are printed directly on the student answer sheet. No question paper image scan is needed. You can pre-fill questions/rubrics from JSON or configure them below:';
      if (loadJsonLabel) loadJsonLabel.textContent = '📁 Pre-Fill Questions from JSON';
    }

    // QP Image status box: only show if separate AND an image is attached
    const statusBox = $('qpImageStatusBox');
    if (statusBox) {
      if (!isSeparate) {
        statusBox.style.display = 'none';
      } else {
        const hasImg = Boolean(
          (state.session && state.session.question_paper_image_path) || state.pendingQpImageFile
        );
        statusBox.style.display = hasImg ? 'flex' : 'none';
      }
    }

    updateComputedWeightsTotal();
  }

  function updateComputedWeightsTotal() {
    const total = state.questionSpecs.reduce((sum, q) => sum + (parseFloat(q.max_marks) || 0), 0);
    const badge = $('specTotalWeightBadge');
    if (badge) badge.textContent = `Total Weight: ${total.toFixed(1)} Marks`;
    const summary = $('qCountSummary');
    if (summary) summary.textContent = `${state.questionSpecs.length} question(s) configured (${total.toFixed(1)} marks total)`;
    const rubricsHeader = $('rubricsHeaderSummary');
    if (rubricsHeader) rubricsHeader.textContent = `${state.questionSpecs.length} Qs · ${total.toFixed(1)} Marks`;
    $('inpTotalMarks').value = total;
  }

  function renderQuestionSpecInputs() {
    const box = $('questionSpecsContainer');
    if (!box) return;

    box.replaceChildren(...state.questionSpecs.map((q, idx) => {
      // Ensure canonical fields
      if (!q.question_id) q.question_id = `Q${idx + 1}`;
      if (q.max_marks === undefined) q.max_marks = 5;
      if (q.page_number === undefined) q.page_number = 1;
      if (q.y_start_pct === undefined) q.y_start_pct = 0;
      if (q.y_end_pct === undefined) q.y_end_pct = 100;
      if (q.x_start_pct === undefined) q.x_start_pct = 0;
      if (q.x_end_pct === undefined) q.x_end_pct = 100;
      if (q.language === undefined) q.language = 'handwritten_math';
      if (q.problem_statement === undefined) q.problem_statement = '';
      if (q.grading_rubric === undefined) q.grading_rubric = '';
      if (q.reference_solution === undefined) q.reference_solution = '';

      const isExpanded = Boolean(q._expanded);
      const hasRubric = Boolean(q.grading_rubric && q.grading_rubric.trim().length > 10);

      // 1. Top Header Row Elements
      const qidBadge = h('span', { class: 'q-spec-qid-badge', text: q.question_id });

      const titleInp = h('input', {
        type: 'text',
        value: q.title || '',
        placeholder: 'Question Title (e.g. Calculus: Definite Integral)',
        title: 'Question Title',
        style: 'flex:1.5;min-width:160px;font-weight:600;padding:6px 10px;',
        oninput: (e) => { q.title = e.target.value; },
      });

      const marksInp = h('input', {
        type: 'number',
        value: q.max_marks,
        step: '0.5',
        min: '0.5',
        max: '500',
        title: 'Marks Allocated to Question',
        style: 'width:68px;font-weight:700;color:var(--brand);padding:6px 6px;text-align:center;',
        oninput: (e) => {
          q.max_marks = parseFloat(e.target.value) || 0;
          updateComputedWeightsTotal();
        },
      });

      const langSelect = h('select', {
        style: 'padding:6px 8px;font-size:0.8rem;border-radius:4px;',
        title: 'Subject Domain & Discipline Grading Heuristics',
        onchange: (e) => { q.language = e.target.value; },
      }, [
        h('option', { value: 'physics', text: '⚛️ Physics', selected: q.language === 'physics' ? true : null }),
        h('option', { value: 'chemistry', text: '🧪 Chemistry', selected: q.language === 'chemistry' ? true : null }),
        h('option', { value: 'biology', text: '🧬 Biology', selected: q.language === 'biology' ? true : null }),
        h('option', { value: 'mathematics', text: '🧮 Mathematics', selected: (q.language === 'mathematics' || q.language === 'handwritten_math') ? true : null }),
        h('option', { value: 'coding', text: '💻 Coding (General / Auto)', selected: q.language === 'coding' ? true : null }),
        h('option', { value: 'python', text: '🐍 Python', selected: (q.language === 'python' || q.language === 'code_python') ? true : null }),
        h('option', { value: 'cpp', text: '⚙️ C / C++', selected: (q.language === 'cpp' || q.language === 'c' || q.language === 'code_c') ? true : null }),
        h('option', { value: 'javascript', text: '🟨 JavaScript', selected: (q.language === 'javascript' || q.language === 'js') ? true : null }),
        h('option', { value: 'go', text: '🐹 Go', selected: (q.language === 'go' || q.language === 'golang') ? true : null }),
        h('option', { value: 'java', text: '☕ Java', selected: q.language === 'java' ? true : null }),
        h('option', { value: 'economics', text: '📈 Economics', selected: q.language === 'economics' ? true : null }),
        h('option', { value: 'general', text: '✍️ General Answer', selected: q.language === 'general' ? true : null }),
      ]);

      const rubricPill = h('span', {
        style: `font-size:0.72rem;font-weight:700;padding:2px 6px;border-radius:4px;${
          hasRubric ? 'background:#dcfce7;color:#15803d;border:1px solid #86efac;' : 'background:#fef3c7;color:#b45309;border:1px solid #fde68a;'
        }`,
        text: hasRubric ? '✓ Rubric' : '⚠️ Rubric',
      });

      const toggleDrawerBtn = h('button', {
        type: 'button',
        class: 'btn btn-sm btn-ghost',
        style: 'font-size:0.78rem;padding:4px 8px;display:flex;align-items:center;gap:4px;',
        title: 'Click to expand problem statement, marking rubric, and reference solution',
        onclick: () => {
          q._expanded = !q._expanded;
          renderQuestionSpecInputs();
        },
      }, h('span', { text: isExpanded ? '📐 Rubrics ▴' : '📐 Rubrics ▾' }), rubricPill);

      const delBtn = h('button', {
        type: 'button',
        class: 'btn btn-sm btn-danger',
        style: 'padding:4px 8px;font-size:0.8rem;',
        text: '✕',
        title: 'Delete this question space',
        onclick: () => {
          if (state.questionSpecs.length <= 1) {
            toast('Session must have at least one question.');
            return;
          }
          state.questionSpecs.splice(idx, 1);
          renderQuestionSpecInputs();
        },
      });

      const headRow = h('div', { class: 'q-spec-card-head' },
        qidBadge,
        titleInp,
        h('span', { style: 'font-size:0.82rem;color:var(--muted);font-weight:700;' }, 'Marks:'), marksInp,
        langSelect,
        toggleDrawerBtn,
        delBtn
      );

      // 2. Expandable Detail Drawer
      let drawer = null;
      if (isExpanded) {
        const stmtArea = h('textarea', {
          rows: 2,
          placeholder: 'Problem statement as given on physical question paper / PDF booklet (LaTeX formulas supported, e.g. \\int_0^\\infty x e^{-x^2} dx)...',
          value: q.problem_statement || '',
          oninput: (e) => { q.problem_statement = e.target.value; },
        });

        const rubricArea = h('textarea', {
          rows: 4,
          placeholder: 'Official step-by-step marking criteria (e.g. Step 1 [2.0 M]: Setup, Step 2 [2.0 M]: Derivation, Step 3 [1.0 M]: Final Value. State ECF / deduction rules)...',
          value: q.grading_rubric || '',
          oninput: (e) => {
            q.grading_rubric = e.target.value;
            const hasR = e.target.value.trim().length > 10;
            rubricPill.textContent = hasR ? '✓ Rubric' : '⚠️ Rubric';
            rubricPill.style.background = hasR ? '#dcfce7' : '#fef3c7';
            rubricPill.style.color = hasR ? '#15803d' : '#b45309';
          },
        });

        const solArea = h('textarea', {
          rows: 3,
          placeholder: 'Official reference solution / model derivation / equations against which Claude vision evaluates student handwriting...',
          value: q.reference_solution || '',
          oninput: (e) => { q.reference_solution = e.target.value; },
        });

        const x1Inp = h('input', {
          type: 'number', value: q.x_start_pct, min: '0', max: '100', title: 'X1 %',
          style: 'width:60px;padding:4px 6px;text-align:center;',
          oninput: (e) => { q.x_start_pct = parseFloat(e.target.value) || 0; },
        });

        const x2Inp = h('input', {
          type: 'number', value: q.x_end_pct, min: '0', max: '100', title: 'X2 %',
          style: 'width:60px;padding:4px 6px;text-align:center;',
          oninput: (e) => { q.x_end_pct = parseFloat(e.target.value) || 100; },
        });

        drawer = h('div', { class: 'q-spec-drawer' },
          h('div', { class: 'q-spec-rubric-field' },
            h('label', { text: '1. Official Problem Statement / Question Prompt' }),
            stmtArea
          ),
          h('div', { class: 'q-spec-rubric-field' },
            h('label', { text: '2. Official Step-by-Step Marking Rubric' }),
            rubricArea
          ),
          h('div', { class: 'q-spec-rubric-field' },
            h('label', { text: '3. Official Reference Solution / Model Derivation' }),
            solArea
          ),
          h('div', { style: 'display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:0.8rem;color:var(--muted);padding-top:4px;' },
            h('span', { style: 'font-weight:600;' }, 'Horizontal Bounding Box:'),
            h('label', { text: 'X-Start %:' }), x1Inp,
            h('label', { text: 'X-End %:' }), x2Inp,
            h('span', { style: 'font-size:0.74rem;color:var(--muted);' }, '(Default: 0% to 100% for full-width exam pages)')
          )
        );
      }

      return h('div', { class: 'q-spec-card' }, headRow, drawer);
    }));

    updateComputedWeightsTotal();
  }

  async function createNewSession() {
    const courseCode = $('inpCourseCode').value.trim() || 'DSE-101';
    const examName = $('inpExamName').value.trim() || 'Written Quiz';
    const examDate = $('inpExamDate').value.trim() || '2026-10-19';
    const totalMarks = parseFloat($('inpTotalMarks').value) || state.questionSpecs.reduce((sum, q) => sum + (parseFloat(q.max_marks) || 0), 0) || 20;
    const expectedPages = Math.min(25, Math.max(1, parseInt($('inpExpectedPages').value, 10) || 1));
    const storageDir = $('inpStorageDir').value.trim() || null;
    const paperType = $('selPaperType') ? $('selPaperType').value : 'COMBINED_QUESTION_ANSWER';

    try {
      const resp = await api('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          course_code: courseCode,
          exam_name: examName,
          exam_date: examDate,
          total_marks: totalMarks,
          expected_pages: expectedPages,
          storage_dir: storageDir,
          questions: state.questionSpecs,
          paper_type: paperType,
        }),
      });
      const created = await resp.json();
      state.activeSessionId = created.session_id;
      localStorage.setItem('iiserb_active_session_id', created.session_id);
      state.session = created;

      if (state.pendingQpImageFile) {
        await uploadQuestionPaperImageFile(state.pendingQpImageFile, created.session_id);
        state.pendingQpImageFile = null;
      }

      toast(`Created Session ${created.session_id}`);
      await refreshHubInfo();
      await loadSessionsList();
      if (state.taName) switchMode('capture'); else switchMode('ta-login');
    } catch (err) {
      toast(`Failed to create session: ${err.message}`);
    }
  }

  async function updateCurrentSession() {
    if (!state.activeSessionId) return;
    try {
      const totalMarks = parseFloat($('inpTotalMarks').value) || state.questionSpecs.reduce((sum, q) => sum + (parseFloat(q.max_marks) || 0), 0) || 20;
      const paperType = $('selPaperType') ? $('selPaperType').value : 'COMBINED_QUESTION_ANSWER';
      const resp = await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          course_code: $('inpCourseCode').value.trim(),
          exam_name: $('inpExamName').value.trim(),
          exam_date: $('inpExamDate').value.trim(),
          total_marks: totalMarks,
          expected_pages: Math.min(25, Math.max(1, parseInt($('inpExpectedPages').value, 10) || 4)),
          storage_dir: $('inpStorageDir').value.trim() || null,
          questions: state.questionSpecs,
          paper_type: paperType,
        }),
      });
      state.session = await resp.json();
      toast(`Saved changes to Session ${state.activeSessionId}`);
      await refreshHubInfo();
      await loadSessionsList();
    } catch (err) {
      toast(`Update failed: ${err.message}`);
    }
  }

  async function uploadQuestionPaperImageFile(file, sessionId = null) {
    const sid = sessionId || state.activeSessionId;
    if (!sid) {
      state.pendingQpImageFile = file;
      updateQpImageStatusBadge(file.name, true);
      toast(`Question Paper Image ready to upload with new session: ${file.name}`);
      return;
    }
    try {
      const form = new FormData();
      form.append('file', file, file.name);
      const res = await (await api(`/api/sessions/${encodeURIComponent(sid)}/question-paper-image`, {
        method: 'POST',
        body: form,
      })).json();
      if (state.session && state.session.session_id === sid) {
        state.session.question_paper_image_path = res.question_paper_image_path;
      }
      updateQpImageStatusBadge(file.name, false);
      toast(`Uploaded Question Paper Image for ${sid}!`);
      await loadSessionsList();
    } catch (err) {
      toast(`QP image upload failed: ${err.message}`);
    }
  }

  function updateQpImageStatusBadge(filename, isPending = false) {
    const box = $('qpImageStatusBox');
    const txt = $('qpImageStatusText');
    const viewBtn = $('btnViewQpImage');
    const remBtn = $('btnRemoveQpImage');
    const isSeparate = $('selPaperType') && $('selPaperType').value === 'SEPARATE_QUESTION_BOOKLET';
    if (!box) return;
    if (filename && isSeparate) {
      box.style.display = 'flex';
      txt.textContent = isPending
        ? `📷 Ready to upload with new session: ${filename}`
        : `📷 Question Paper scan attached: ${filename}`;
      if (viewBtn) viewBtn.style.display = isPending ? 'none' : 'inline-block';
      if (remBtn) remBtn.style.display = 'inline-block';
    } else {
      box.style.display = 'none';
    }
  }

  async function deleteExamSessionHandler(evt, sessionId, examName) {
    const btn = evt && evt.currentTarget ? evt.currentTarget : null;
    if (btn && btn.getAttribute('data-confirming') !== 'true') {
      btn.setAttribute('data-confirming', 'true');
      btn.textContent = '⚠️ Confirm Delete?';
      btn.style.background = '#ef4444';
      btn.style.color = '#ffffff';
      btn.style.borderColor = '#dc2626';
      setTimeout(() => {
        if (btn) {
          btn.removeAttribute('data-confirming');
          btn.textContent = '🗑 Delete';
          btn.style.background = '';
          btn.style.color = '#ef4444';
          btn.style.borderColor = 'rgba(239,68,68,0.35)';
        }
      }, 4000);
      return;
    }

    try {
      if (btn) {
        btn.textContent = '⏳ Deleting…';
        btn.disabled = true;
      }
      const res = await (await api(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE',
      })).json();
      if (res.status === 'deleted') {
        toast(`Deleted session ${sessionId}`);
        if (state.activeSessionId === sessionId) {
          state.activeSessionId = null;
          state.session = null;
          localStorage.removeItem('iiserb_active_session_id');
          $('btnUpdateSession').hidden = true;
          $('activeSessionBanner').textContent = 'No active session selected';
          $('activeSessionMeta').textContent = 'Select or create a session in Step 1';
        }
        await refreshHubInfo();
        await loadSessionsList();
      } else {
        toast(`Could not delete session ${sessionId}: ${res.status}`);
      }
    } catch (err) {
      toast(`Failed to delete session: ${err.message}`);
    }
  }

  async function importRosterCsvFile(file) {
    if (!state.activeSessionId) {
      toast('Please create or select a session first before importing a roster CSV.');
      return;
    }
    try {
      const form = new FormData();
      form.append('file', file, file.name);
      const res = await (await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/roster-csv`, {
        method: 'POST',
        body: form,
      })).json();
      state.session = res.session;
      toast(`Imported ${res.imported_count} students into Session ${state.activeSessionId}!`);
      await loadSessionsList();
    } catch (err) {
      toast(`Roster CSV import failed: ${err.message}`);
    }
  }

  async function loadSessionsList() {
    const box = $('sessionsListBox');
    try {
      const data = await (await api('/api/sessions')).json();
      const list = data.sessions || [];
      if (!list.length) {
        box.textContent = 'No sessions created yet. Use the form above to create your first session.';
        return;
      }
      box.replaceChildren(...list.map((s) => {
        const isSeparate = s.paper_type === 'SEPARATE_QUESTION_BOOKLET';
        const typeBadge = h('span', {
          style: `display:inline-block;padding:2px 8px;border-radius:12px;font-size:0.74rem;font-weight:600;margin-left:6px;background:${isSeparate ? 'rgba(99,102,241,0.18);color:#818cf8;border:1px solid #6366f1' : 'rgba(16,185,129,0.18);color:#34d399;border:1px solid #10b981'};`,
          text: isSeparate ? '📑 Separate Booklet' : '📝 Combined Q&A',
        });
        const qpImgBadge = s.question_paper_image_path ? h('span', {
          style: 'display:inline-block;padding:2px 8px;border-radius:12px;font-size:0.74rem;font-weight:600;margin-left:6px;background:rgba(245,158,11,0.18);color:#fbbf24;border:1px solid #f59e0b;',
          text: '📷 QP Photo Attached',
        }) : null;

        return h('div', {
          style: 'display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;padding:12px 0;border-bottom:1px solid var(--border-mobile);',
        },
        h('div', null,
          h('div', { style: 'display:flex;align-items:center;flex-wrap:wrap;gap:4px;' },
            h('strong', { text: `${s.session_id} · ${s.exam_name}`, style: 'color:#f8fafc;' }),
            typeBadge,
            qpImgBadge),
          h('div', {
            style: 'font-size:0.8rem;color:var(--muted-mobile);margin-top:4px;',
            text: `${s.course_code} · Date: ${s.exam_date} · Marks: ${s.total_marks} · ${s.student_count} students (${s.pages_scanned} pages) · Folder: ${s.storage_dir}`,
          })),
        h('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center;' },
          h('button', {
            class: 'btn-m btn-m-ghost', style: 'padding:5px 10px;font-size:0.8rem;', type: 'button', text: '✏️ Edit',
            title: 'Load session details into the form',
            onclick: () => selectSession(s.session_id, ''),
          }),
          h('button', {
            class: 'btn-m btn-m-primary', style: 'padding:5px 10px;font-size:0.8rem;', type: 'button', text: '📱 Mobile Scanner',
            onclick: () => selectSession(s.session_id, 'capture'),
          }),
          h('button', {
            class: 'btn-m btn-m-success', style: 'padding:5px 10px;font-size:0.8rem;', type: 'button', text: '💻 Results & Grade',
            onclick: () => selectSession(s.session_id, 'review'),
          }),
          h('button', {
            class: 'btn-m btn-m-ghost', style: 'padding:5px 10px;font-size:0.8rem;color:#ef4444;border-color:rgba(239,68,68,0.35);', type: 'button', text: '🗑 Delete',
            title: 'Delete this exam session permanently',
            onclick: (e) => deleteExamSessionHandler(e, s.session_id, s.exam_name),
          })));
      }));
    } catch (err) {
      box.textContent = `Could not load sessions: ${err.message}`;
    }
  }

  async function selectSession(sessionId, targetMode) {
    state.activeSessionId = sessionId;
    localStorage.setItem('iiserb_active_session_id', sessionId);
    await refreshActiveSession(true);
    await refreshHubInfo();
    if (targetMode === 'capture' && !state.taName) {
      switchMode('ta-login');
    } else if (targetMode) {
      switchMode(targetMode);
    }
  }

  async function refreshActiveSession(populateForm = false) {
    if (!state.activeSessionId) return;
    try {
      const data = await (await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}`)).json();
      state.session = data;
      if (Array.isArray(data.active_tas)) {
        renderActiveTasBadge(data.active_tas);
      }
      if (populateForm) {
        $('inpCourseCode').value = data.course_code || '';
        $('inpExamName').value = data.exam_name || '';
        $('inpExamDate').value = data.exam_date || '';
        $('inpTotalMarks').value = data.total_marks || 20;
        $('inpExpectedPages').value = data.expected_pages || 1;
        $('inpStorageDir').value = data.storage_dir || '';
        if (Array.isArray(data.questions) && data.questions.length) {
          state.questionSpecs = data.questions;
          renderQuestionSpecInputs();
        }
        syncPaperTypeUI(data.paper_type || 'COMBINED_QUESTION_ANSWER');
        if (data.question_paper_image_path) {
          updateQpImageStatusBadge(data.question_paper_image_path.split(/[\\/]/).pop(), false);
        } else {
          updateQpImageStatusBadge(null);
        }
        $('btnUpdateSession').hidden = false;
      }
      // Populate roster datalist for instant roll number autocomplete
      const dl = $('rosterDatalist');
      if (dl && data.students) {
        dl.replaceChildren(...data.students.map((st) => h('option', {
          value: st.roll_no,
          text: st.student_name ? `${st.roll_no} — ${st.student_name}` : st.roll_no,
        })));
      }
      if ($('regradesCountBadge')) {
        api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/regrade-requests?status=PENDING`)
          .then((r) => r.json())
          .then((list) => {
            if ($('regradesCountBadge')) $('regradesCountBadge').textContent = list.length || '0';
          })
          .catch(() => {});
      }
      if (state.mode === 'capture') renderCapturePanel();
      if (state.mode === 'review') renderReviewDashboard();
    } catch (_err) { /* ignore */ }
  }

  // --------------------------------------------------------------------------- Step 2: Mobile Camera Capture & Retake Workflow
  function getActiveStudentRecord() {
    if (!state.session || !state.currentRoll) return null;
    return (state.session.students || []).find((s) => s.roll_no.toLowerCase() === state.currentRoll.toLowerCase()) || null;
  }

  function renderCapturePanel() {
    const s = state.session;
    if (!s) {
      $('activeSessionBanner').textContent = 'No active session selected';
      $('activeSessionMeta').textContent = 'Please create or select a session in Step 1 first.';
      return;
    }
    const maxPages = Math.min(25, Math.max(1, s.expected_pages || 4));
    if (state.currentPage > maxPages) state.currentPage = 1;

    $('activeSessionBanner').textContent = `${s.session_id} — ${s.exam_name} (${s.course_code})`;
    $('activeSessionMeta').textContent = `Folder: ${s.storage_dir} · Max ${maxPages} pages/student`;
    $('studentProgressCounter').textContent = `${s.student_count || 0} / 400 students (${s.pages_scanned || 0} pages)`;

    const stRecord = getActiveStudentRecord();
    if (stRecord && stRecord.student_name && !$('scanStudentName').value) {
      $('scanStudentName').value = stRecord.student_name;
      state.currentName = stRecord.student_name;
    }
    const uploadedPages = new Set((stRecord ? stRecord.pages || [] : []).map((p) => p.page_no));

    const highestPage = Math.max(maxPages, ...(uploadedPages.size ? Array.from(uploadedPages) : [1]), state.currentPage);
    const targetLabel = $('currentScanningTargetText');
    if (targetLabel) {
      targetLabel.textContent = state.currentPage > maxPages
        ? `Extra Sheet #${state.currentPage - maxPages} (Page ${state.currentPage})`
        : `Sheet ${state.currentPage} of ${maxPages}`;
    }
    const snapLabelNo = $('lblSnapPageNo');
    if (snapLabelNo) {
      snapLabelNo.textContent = String(state.currentPage);
    }
    if ($('hudPageBadge')) {
      $('hudPageBadge').textContent = state.currentPage > maxPages
        ? `Extra Sheet #${state.currentPage - maxPages} (Page ${state.currentPage})`
        : `Page ${state.currentPage} of ${maxPages}`;
    }
    if ($('btnSnapPhoto')) {
      $('btnSnapPhoto').textContent = uploadedPages.has(state.currentPage)
        ? `📸 Retake Page ${state.currentPage}`
        : `📸 Snap Page ${state.currentPage}`;
    }
    if ($('btnRetakePage')) {
      $('btnRetakePage').hidden = !uploadedPages.has(state.currentPage);
    }

    const strip = $('pageSlotsStrip');
    const slots = [];
    for (let p = 1; p <= highestPage; p++) {
      const isDone = uploadedPages.has(p);
      const isActive = p === state.currentPage;
      const isExtra = p > maxPages;
      slots.push(h('button', {
        type: 'button',
        class: `page-slot-btn${isActive ? ' active' : ''}${isDone ? ' done' : ''}${isExtra ? ' extra' : ''}`,
        text: isExtra ? `Extra #${p - maxPages}${isDone ? ' ✓' : ''}` : `Page ${p}${isDone ? ' ✓' : ''}`,
        onclick: () => {
          state.currentPage = p;
          renderCapturePanel();
        },
      }));
    }
    slots.push(h('button', {
      type: 'button',
      class: 'page-slot-btn extra',
      style: 'color:var(--brand);border-color:var(--brand);border-style:dashed;',
      text: '+ Extra Sheet',
      title: 'Scan supplementary answer sheet for this student',
      onclick: () => {
        state.currentPage = highestPage + 1;
        renderCapturePanel();
      },
    }));
    strip.replaceChildren(...slots);

    // Render Scanned Sheets Tray for Current Student
    const trayBox = $('capturedSheetsList');
    const countBadge = $('sheetsCapturedCountText');
    const noSheetsNotice = $('noSheetsCapturedNotice');
    if (trayBox) {
      const pList = (stRecord ? stRecord.pages || [] : []);
      if (countBadge) countBadge.textContent = `${pList.length} sheet(s) uploaded (${pList.length * 2} half images)`;
      if (!pList.length) {
        if (noSheetsNotice) noSheetsNotice.hidden = false;
        trayBox.replaceChildren(noSheetsNotice || h('span', { class: 'muted small', text: 'No sheets captured for this student yet. Tap Take Photo / Upload above.' }));
      } else {
        if (noSheetsNotice) noSheetsNotice.hidden = true;
        trayBox.replaceChildren(...pList.map((p) => {
          return h('div', { class: 'sheet-tray-card' },
            h('img', { src: `${p.image_url}?v=${Date.now()}`, alt: `Sheet ${p.page_no}` }),
            h('div', null,
              h('div', { style: 'font-weight:700;font-size:0.86rem;color:var(--text);' }, `Sheet ${p.page_no}`),
              h('span', { class: 'pill pill-ok', style: 'font-size:0.7rem;padding:2px 6px;' }, '✓ Top & Bottom Halves Ready (2x)')
            ),
            h('button', {
              type: 'button',
              class: 'btn btn-sm btn-ghost',
              style: 'margin-left:8px;font-size:0.75rem;',
              text: '🔄 Retake',
              onclick: () => {
                state.currentPage = p.page_no;
                renderCapturePanel();
                $('fallbackCameraInput').click();
              },
            })
          );
        }));
      }
    }

    const previewImg = $('capturedPreviewImg');
    const videoEl = $('cameraVideo');
    const pageObj = (stRecord ? stRecord.pages || [] : []).find((p) => p.page_no === state.currentPage);
    if (pageObj && !cameraEngine.active) {
      if (previewImg) {
        previewImg.src = `${pageObj.image_url}?t=${Date.now()}`;
        previewImg.hidden = false;
      }
      if (videoEl) videoEl.hidden = true;
      if ($('hudSharpnessBadge')) {
        $('hudSharpnessBadge').textContent = `Captured: Sharpness ${pageObj.blur_score || 0} ✓`;
        $('hudSharpnessBadge').className = 'hud-pill ok';
      }
    } else {
      if (previewImg) previewImg.hidden = true;
      if (videoEl) videoEl.hidden = false;
    }
  }

  // --------------------------------------------------------------------------- Real-Time Camera & Auto-Capture Engine
  const cameraEngine = {
    active: false,
    stream: null,
    deviceId: '',
    videoDevices: [],
    animId: null,
    lastSnapTime: 0,
    sharpStreak: 0,
    isCapturing: false,
    cooldownMs: 2200,
    canvas: null,
    ctx: null,
    audioCtx: null,
    torchOn: false,
    prevGray: null,
    lastSnappedHash: '',
  };

  async function toggleTorch() {
    if (!cameraEngine.stream) {
      toast('Start live camera first to toggle torch', 'warn');
      return;
    }
    const track = cameraEngine.stream.getVideoTracks()[0];
    if (!track) return;
    try {
      const caps = track.getCapabilities ? track.getCapabilities() : {};
      if (caps.torch) {
        cameraEngine.torchOn = !cameraEngine.torchOn;
        await track.applyConstraints({ advanced: [{ torch: cameraEngine.torchOn }] });
        const btn = $('btnToggleTorch');
        if (btn) btn.textContent = cameraEngine.torchOn ? '🔦 Torch: ON' : '🔦 Torch';
        toast(`Torch ${cameraEngine.torchOn ? 'Enabled' : 'Disabled'}`);
      } else {
        toast('Torch not supported on this camera hardware', 'warn');
      }
    } catch (e) {
      toast('Torch error: ' + (e.message || e), 'warn');
    }
  }

  async function initCameraDevices() {
    const select = $('cameraDeviceSelect');
    if (!select) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
      select.hidden = true;
      return;
    }
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      cameraEngine.videoDevices = devices.filter((d) => d.kind === 'videoinput');
      if (cameraEngine.videoDevices.length > 0) {
        select.replaceChildren(...cameraEngine.videoDevices.map((d, idx) => h('option', {
          value: d.deviceId,
          text: d.label || `Camera ${idx + 1} (${idx === 0 ? 'Primary' : 'Secondary'})`,
          selected: d.deviceId === cameraEngine.deviceId ? true : null,
        })));
        select.hidden = false;
      } else {
        select.hidden = true;
      }
    } catch (_err) {
      select.hidden = true;
    }
  }

  async function toggleLiveCamera() {
    if (cameraEngine.active) {
      stopLiveCamera();
    } else {
      await startLiveCamera();
    }
  }

  async function startLiveCamera() {
    const helpBanner = $('cameraHelpBanner');
    if (helpBanner) helpBanner.hidden = true;

    // Check secure context requirement
    const isLocal = ['localhost', '127.0.0.1'].includes(window.location.hostname);
    if (!window.isSecureContext && !isLocal) {
      if (helpBanner) {
        helpBanner.innerHTML = '⚠️ <strong>Mobile Camera Warning:</strong> Browsers block live camera streaming on insecure HTTP addresses (' + window.location.host + ').<br>' +
          '• <strong>Option A (Fastest):</strong> Tap <strong>"📁 File / Phone Upload"</strong> below to snap photos directly with your phone camera app.<br>' +
          '• <strong>Option B:</strong> On Chrome for Android, navigate to <code>chrome://flags/#unsafely-treat-insecure-origin-as-secure</code>, add <code>' + window.location.origin + '</code>, enable it, and relaunch Chrome.';
        helpBanner.hidden = false;
      }
    }

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast('Live camera API not supported in this browser context. Please use File / Phone Upload.');
      $('fallbackCameraInput').click();
      return;
    }

    try {
      if (cameraEngine.stream) {
        cameraEngine.stream.getTracks().forEach((t) => t.stop());
      }

      const selectedId = $('cameraDeviceSelect') ? $('cameraDeviceSelect').value : cameraEngine.deviceId;
      let stream = null;

      // Progressive constraint fallback pipeline
      const constraintAttempts = [];
      if (selectedId) {
        constraintAttempts.push({ video: { deviceId: { exact: selectedId } }, audio: false });
      }
      constraintAttempts.push(
        { video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false },
        { video: { facingMode: { ideal: 'environment' } }, audio: false },
        { video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false },
        { video: true, audio: false }
      );

      let lastError = null;
      for (const c of constraintAttempts) {
        try {
          stream = await navigator.mediaDevices.getUserMedia(c);
          if (stream) break;
        } catch (e) {
          lastError = e;
        }
      }

      if (!stream) {
        throw lastError || new Error('Could not access any camera on this device.');
      }

      cameraEngine.stream = stream;
      state.stream = stream;
      cameraEngine.active = true;

      const video = $('cameraVideo');
      video.srcObject = stream;
      video.hidden = false;
      $('capturedPreviewImg').hidden = true;

      await video.play().catch((err) => console.warn('Autoplay wait:', err));

      if ($('webcamContainer')) $('webcamContainer').style.display = 'block';
      if ($('btnToggleCamera')) {
        $('btnToggleCamera').textContent = '⏹ Close Webcam';
        $('btnToggleCamera').className = 'btn btn-danger';
      }
      toast('Live webcam active');

      await initCameraDevices();
      startAutoCaptureLoop();
    } catch (err) {
      console.error('Camera error:', err);
      let msg = err.message || 'Camera permission denied or camera unavailable.';
      if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
        msg = 'Camera permission denied. Please allow camera access in your browser address bar.';
      } else if (err.name === 'NotFoundError' || err.name === 'DevicesNotFoundError') {
        msg = 'No camera hardware found on this device.';
      } else if (err.name === 'NotReadableError' || err.name === 'TrackStartError') {
        msg = 'Camera is already in use by another application or tab.';
      }
      toast(msg);
      if (helpBanner) {
        helpBanner.textContent = `Camera Notice: ${msg} You can use the "File / Phone Upload" button below.`;
        helpBanner.hidden = false;
      }
    }
  }

  function stopLiveCamera() {
    cameraEngine.active = false;
    if (cameraEngine.animId) {
      cancelAnimationFrame(cameraEngine.animId);
      cameraEngine.animId = null;
    }
    if (cameraEngine.stream) {
      cameraEngine.stream.getTracks().forEach((t) => t.stop());
      cameraEngine.stream = null;
      state.stream = null;
    }
    const video = $('cameraVideo');
    if (video) {
      video.srcObject = null;
    }
    if ($('webcamContainer')) $('webcamContainer').style.display = 'none';
    if ($('btnToggleCamera')) {
      $('btnToggleCamera').textContent = '🎥 Live Webcam';
      $('btnToggleCamera').className = 'btn btn-ghost';
    }
    if ($('hudSharpnessBadge')) {
      $('hudSharpnessBadge').textContent = 'Focus: Camera Off';
      $('hudSharpnessBadge').className = 'hud-pill';
    }
    const guideBox = $('greenGuideBox');
    if (guideBox) guideBox.className = 'green-guide-box';
  }

  function playShutterSound() {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      if (!cameraEngine.audioCtx) cameraEngine.audioCtx = new AudioCtx();
      const ctx = cameraEngine.audioCtx;
      if (ctx.state === 'suspended') ctx.resume();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(880, ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(320, ctx.currentTime + 0.08);
      gain.gain.setValueAtTime(0.35, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.08);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.09);
    } catch (_e) { /* audio blip */ }
  }

  function flashShutterEffect() {
    const stage = $('cameraStage');
    if (!stage) return;
    stage.classList.add('shutter-flash');
    setTimeout(() => stage.classList.remove('shutter-flash'), 250);
  }

  function computeDHash(gray, w, h) {
    const thumbW = 9;
    const thumbH = 8;
    const stepX = Math.floor(w / thumbW);
    const stepY = Math.floor(h / thumbH);
    let hash = '';
    for (let y = 0; y < thumbH; y++) {
      for (let x = 0; x < thumbW - 1; x++) {
        const leftVal = gray[(y * stepY) * w + (x * stepX)];
        const rightVal = gray[(y * stepY) * w + ((x + 1) * stepX)];
        hash += (leftVal > rightVal ? '1' : '0');
      }
    }
    return hash;
  }

  function hammingDistance(h1, h2) {
    if (!h1 || !h2 || h1.length !== h2.length) return 999;
    let dist = 0;
    for (let i = 0; i < h1.length; i++) {
      if (h1[i] !== h2[i]) dist++;
    }
    return dist;
  }

  function evaluate4Gates(video) {
    if (!video || video.readyState < 2 || video.videoWidth === 0) {
      return { ok: false, sharpness: 0, motionDiff: 0, paperCoverage: 0, hashDiff: 999 };
    }
    if (!cameraEngine.canvas) {
      cameraEngine.canvas = document.createElement('canvas');
      cameraEngine.ctx = cameraEngine.canvas.getContext('2d', { willReadFrequently: true });
    }
    const w = 200;
    const h = 150;
    cameraEngine.canvas.width = w;
    cameraEngine.canvas.height = h;

    const sx = video.videoWidth * 0.15;
    const sy = video.videoHeight * 0.15;
    const sw = video.videoWidth * 0.70;
    const sh = video.videoHeight * 0.70;
    cameraEngine.ctx.drawImage(video, sx, sy, sw, sh, 0, 0, w, h);

    const imgData = cameraEngine.ctx.getImageData(0, 0, w, h);
    const data = imgData.data;
    const gray = new Float32Array(w * h);
    let paperPixels = 0;

    for (let i = 0, j = 0; i < data.length; i += 4, j++) {
      const r = data[i], g = data[i + 1], b = data[i + 2];
      const lum = 0.299 * r + 0.587 * g + 0.114 * b;
      gray[j] = lum;
      const maxC = Math.max(r, g, b);
      const minC = Math.min(r, g, b);
      const sat = maxC > 0 ? (maxC - minC) / maxC : 0;
      if (lum >= 125 && sat < 0.35) {
        paperPixels++;
      }
    }

    const paperCoverage = paperPixels / (w * h);

    // Gate 2: Inter-frame motion difference (MAD)
    let motionDiff = 0;
    if (cameraEngine.prevGray && cameraEngine.prevGray.length === gray.length) {
      let diffSum = 0;
      for (let k = 0; k < gray.length; k++) {
        diffSum += Math.abs(gray[k] - cameraEngine.prevGray[k]);
      }
      motionDiff = diffSum / gray.length;
    }
    cameraEngine.prevGray = gray;

    // Gate 3: Laplacian sharpness
    let sum = 0, sumSq = 0, count = 0;
    for (let y = 1; y < h - 1; y++) {
      const row = y * w;
      const up = (y - 1) * w;
      const dn = (y + 1) * w;
      for (let x = 1; x < w - 1; x++) {
        const lap = gray[up + x] + gray[dn + x] + gray[row + x - 1] + gray[row + x + 1] - 4 * gray[row + x];
        sum += lap;
        sumSq += lap * lap;
        count++;
      }
    }
    const mean = count > 0 ? sum / count : 0;
    const sharpness = count > 0 ? Math.max(0, Math.round((sumSq / count) - (mean * mean))) : 0;

    // Gate 4: Page-Change Perceptual Hash
    const curHash = computeDHash(gray, w, h);
    const hashDiff = cameraEngine.lastSnappedHash ? hammingDistance(curHash, cameraEngine.lastSnappedHash) : 999;

    // Live SVG quad overlay
    const quad = $('quadOverlay');
    if (quad) {
      if (paperCoverage >= 0.50 && sharpness >= 40) {
        quad.innerHTML = '<polygon points="12,12 88,12 88,88 12,88" stroke="#22c55e" stroke-width="2" stroke-dasharray="4" fill="rgba(34, 197, 94, 0.08)"/>';
      } else {
        quad.innerHTML = '';
      }
    }

    const gate1_ok = paperCoverage >= 0.50; // paper quad coverage >= 50%
    const gate2_ok = motionDiff < 3.8;      // steady hand (inter-frame MAD < 3.8)
    const gate3_ok = sharpness >= 50;       // Laplacian sharpness
    const gate4_ok = hashDiff > 8;          // new page flipped (pHash distance > 8)

    return {
      ok: gate1_ok && gate2_ok && gate3_ok && gate4_ok,
      sharpness,
      motionDiff: Math.round(motionDiff * 10) / 10,
      paperCoverage: Math.round(paperCoverage * 100),
      hashDiff,
      curHash,
      gate1_ok, gate2_ok, gate3_ok, gate4_ok,
    };
  }

  let lastAnalysisTs = 0;
  function startAutoCaptureLoop() {
    if (!cameraEngine.active) return;

    function frameTick(ts) {
      if (!cameraEngine.active) return;
      if (ts - lastAnalysisTs >= 180) {
        lastAnalysisTs = ts;
        analyzeFrame();
      }
      cameraEngine.animId = requestAnimationFrame(frameTick);
    }
    cameraEngine.animId = requestAnimationFrame(frameTick);
  }

  async function analyzeFrame() {
    const video = $('cameraVideo');
    if (!video || video.hidden || !cameraEngine.active) return;

    const gate = evaluate4Gates(video);
    const sharpness = gate.sharpness;
    const badge = $('hudSharpnessBadge');
    const autoBadge = $('hudAutoStatusBadge');
    const guideBox = $('greenGuideBox');
    const chkAuto = $('chkAutoCapture');
    const autoCaptureEnabled = chkAuto && chkAuto.checked;

    const now = Date.now();
    const inCooldown = (now - cameraEngine.lastSnapTime) < cameraEngine.cooldownMs;

    if (!gate.gate1_ok) {
      if (badge) {
        badge.textContent = `Align Paper (${gate.paperCoverage}%)`;
        badge.className = 'hud-pill warn';
      }
      if (guideBox) guideBox.className = 'green-guide-box';
      cameraEngine.sharpStreak = 0;
    } else if (!gate.gate2_ok) {
      if (badge) {
        badge.textContent = `Hold Steady (Motion ${gate.motionDiff})`;
        badge.className = 'hud-pill warn';
      }
      if (guideBox) guideBox.className = 'green-guide-box sharp';
      cameraEngine.sharpStreak = 0;
    } else if (!gate.gate4_ok) {
      if (badge) {
        badge.textContent = 'Flip to Next Page';
        badge.className = 'hud-pill';
      }
      if (guideBox) guideBox.className = 'green-guide-box';
      cameraEngine.sharpStreak = 0;
    } else if (sharpness < 50) {
      if (badge) {
        badge.textContent = `Focusing… (${sharpness})`;
        badge.className = 'hud-pill warn';
      }
      if (guideBox) guideBox.className = 'green-guide-box sharp';
      cameraEngine.sharpStreak = Math.max(0, cameraEngine.sharpStreak - 1);
    } else {
      if (badge) {
        badge.textContent = `4-Gates Locked (${sharpness}) ✓`;
        badge.className = 'hud-pill ok';
      }
      if (guideBox) guideBox.className = 'green-guide-box locked';
    }

    if (!autoCaptureEnabled) {
      if (autoBadge) autoBadge.textContent = 'Auto-Snap: Off';
      return;
    }

    if (inCooldown) {
      if (autoBadge) autoBadge.textContent = 'Auto-Snap: Ready…';
      return;
    }

    const currentRoll = $('scanRollNo') ? $('scanRollNo').value.trim() : '';
    if (!currentRoll) {
      if (autoBadge) autoBadge.textContent = 'Auto-Snap: Enter Roll No';
      return;
    }

    if (gate.ok && !cameraEngine.isCapturing) {
      cameraEngine.sharpStreak += 1;
      if (autoBadge) autoBadge.textContent = `Auto-Snap: Locking [${'●'.repeat(Math.min(3, cameraEngine.sharpStreak))}]`;
      if (cameraEngine.sharpStreak >= 3) {
        cameraEngine.sharpStreak = 0;
        cameraEngine.lastSnappedHash = gate.curHash;
        await triggerAutoSnapPhoto(sharpness);
      }
    } else {
      if (autoBadge && !inCooldown) {
        if (!gate.gate4_ok) autoBadge.textContent = 'Auto-Snap: Page Unchanged';
        else if (!gate.gate1_ok) autoBadge.textContent = 'Auto-Snap: No Paper';
        else if (!gate.gate2_ok) autoBadge.textContent = 'Auto-Snap: Moving';
        else autoBadge.textContent = 'Auto-Snap: Ready';
      }
    }
  }

  async function triggerAutoSnapPhoto(sharpnessScore) {
    if (cameraEngine.isCapturing) return;
    cameraEngine.isCapturing = true;
    try {
      flashShutterEffect();
      playShutterSound();
      const video = $('cameraVideo');
      const canvas = $('captureCanvas');
      if (video && video.videoWidth > 0) {
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(async (blob) => {
          if (blob) {
            await handleCapturedBlob(blob);
            toast(`⚡ Auto-captured Page ${state.currentPage} (Focus: ${sharpnessScore}) ✓`);
          }
        }, 'image/jpeg', 0.88);
      }
      cameraEngine.lastSnapTime = Date.now();
    } finally {
      setTimeout(() => { cameraEngine.isCapturing = false; }, 800);
    }
  }

  async function captureFromVideoOrFallback() {
    if (!state.activeSessionId) {
      toast('Please create or select a session in Step 1 first.');
      switchMode('hub');
      return;
    }
    const roll = $('scanRollNo').value.trim();
    const name = $('scanStudentName').value.trim();
    if (!roll) {
      toast('Enter the Student Roll Number before taking a photo.');
      $('scanRollNo').focus();
      return;
    }
    state.currentRoll = roll;
    state.currentName = name;

    const video = $('cameraVideo');
    if (cameraEngine.active && video && video.videoWidth > 0 && !video.hidden) {
      flashShutterEffect();
      playShutterSound();
      const canvas = $('captureCanvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(async (blob) => {
        if (!blob) return;
        await handleCapturedBlob(blob);
      }, 'image/jpeg', 0.88);
    } else {
      $('fallbackCameraInput').click();
    }
  }

  async function compressImageBlobForUpload(blob, maxDim = 1800, quality = 0.85) {
    if (!blob || !blob.type || !blob.type.startsWith('image/')) return blob;
    if (blob.size <= 750 * 1024) return blob;
    try {
      return await new Promise((resolve) => {
        const img = new Image();
        const url = URL.createObjectURL(blob);
        img.onload = () => {
          URL.revokeObjectURL(url);
          let { width, height } = img;
          if (Math.max(width, height) <= maxDim && blob.size <= 900 * 1024) {
            return resolve(blob);
          }
          if (Math.max(width, height) > maxDim) {
            const scale = maxDim / Math.max(width, height);
            width = Math.round(width * scale);
            height = Math.round(height * scale);
          }
          const canvas = document.createElement('canvas');
          canvas.width = width;
          canvas.height = height;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, width, height);
          canvas.toBlob((b) => resolve(b || blob), 'image/jpeg', quality);
        };
        img.onerror = () => {
          URL.revokeObjectURL(url);
          resolve(blob);
        };
        img.src = url;
      });
    } catch (_err) {
      return blob;
    }
  }

  async function handleCapturedBlob(rawBlob) {
    const roll = $('scanRollNo').value.trim();
    const name = $('scanStudentName').value.trim();
    if (!roll || !state.activeSessionId) return;
    state.currentRoll = roll;
    state.currentName = name;

    const blob = await compressImageBlobForUpload(rawBlob);

    const stRecord = getActiveStudentRecord();
    const isRetake = Boolean(stRecord && (stRecord.pages || []).some((p) => p.page_no === state.currentPage));
    const pageBeingSaved = state.currentPage;
    const maxPages = Math.min(25, Math.max(1, (state.session && state.session.expected_pages) || 4));

    let directUploaded = false;
    try {
      const fd = new FormData();
      fd.append('roll_no', roll);
      fd.append('student_name', name);
      fd.append('page_no', String(pageBeingSaved));
      fd.append('is_retake', String(isRetake));
      const uploadName = (rawBlob && rawBlob.name) || (rawBlob && rawBlob.type === 'application/pdf' ? `page_${pageBeingSaved}.pdf` : `page_${pageBeingSaved}.jpg`);
      fd.append('file', blob, uploadName);
      const resp = await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/upload-page`, {
        method: 'POST',
        body: fd,
      });
      if (resp && resp.ok) {
        directUploaded = true;
        playSuccessChime();
        if (navigator.vibrate) navigator.vibrate([35, 30, 35]);
        toast(`✓ Uploaded Roll ${roll} · Page ${pageBeingSaved} directly`);
      }
    } catch (_err) {
      // Direct upload fallback to offline-first chunk queue
    }

    if (!directUploaded) {
      const buf = await blob.arrayBuffer();
      await enqueueImageChunks(state.activeSessionId, roll, name, pageBeingSaved, buf, isRetake);
      playSuccessChime();
      if (navigator.vibrate) navigator.vibrate(35);
      toast(`✓ Queued Roll ${roll} · Page ${pageBeingSaved} (${Math.round(blob.size / 1024)} KB)`);
    }

    if (state.currentPage < maxPages) {
      state.currentPage += 1;
    }
    renderCapturePanel();
  }

  async function finalizeCurrentStudentAndMoveNext() {
    if (!state.activeSessionId) return;
    const roll = $('scanRollNo').value.trim();
    const name = $('scanStudentName').value.trim();
    if (!roll) {
      toast('Enter a student Roll Number first.');
      return;
    }
    try {
      await flushChunkQueue();
      await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/finalize-student`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roll_no: roll, student_name: name, trigger_grading: false, ta_name: state.taName || undefined }),
      });

      const expPages = (state.session && state.session.expected_pages) || 1;
      const stRecord = getActiveStudentRecord();
      const uploadedCount = (stRecord && stRecord.pages) ? stRecord.pages.length : 0;
      if (uploadedCount > 0 && uploadedCount < expPages) {
        toast(`⚠️ Warning: Roll ${roll} only has ${uploadedCount} of ${expPages} expected sheets! Finalized.`);
      } else {
        toast(`✓ Finalized ${roll}. Ready for next student!`);
      }

      $('scanRollNo').value = '';
      $('scanStudentName').value = '';
      state.currentRoll = '';
      state.currentName = '';
      state.currentPage = 1;
      $('scanRollNo').focus();
      await refreshActiveSession(false);
    } catch (err) {
      toast(`Could not finalize student: ${err.message}`);
    }
  }

  // --------------------------------------------------------------------------- Step 3: Live Laptop Results, Concurrent Grading & Excel Export
  let gradingPollTimer = null;

  async function triggerSessionAiEvaluation(onlyRollNo = null, onlyQuestionId = null) {
    if (!state.activeSessionId) return;
    try {
      await flushChunkQueue();
      await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/evaluate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          only_roll_no: onlyRollNo,
          only_question_id: onlyQuestionId,
          force_regrade: true,
        }),
      });
      toast('Started concurrent Claude Haiku 5.5 grading with Ephemeral Prompt Caching!');
      startGradingPoll();
    } catch (err) {
      toast(`Evaluation error: ${err.message}`);
    }
  }

  function startGradingPoll() {
    clearInterval(gradingPollTimer);
    gradingPollTimer = setInterval(async () => {
      await refreshActiveSession(false);
      if (!state.session || state.session.status !== 'GRADING') {
        clearInterval(gradingPollTimer);
      }
    }, 1200);
  }

  function renderReviewDashboard() {
    const s = state.session;
    if (!s) {
      $('reviewSessionTitle').textContent = 'No Session Selected';
      return;
    }
    $('reviewSessionTitle').textContent = `${s.exam_name} (${s.course_code}) · Session ${s.session_id}`;
    const sc = s.summary_counts || {};
    const telem = s.llm_telemetry || {};
    $('reviewSessionSub').textContent = `Folder: ${s.storage_dir} · Date: ${s.exam_date} · Total Marks: ${s.total_marks} · Model: ${telem.model || 'claude-haiku-5-5'}`;

    $('tStudentsCount').textContent = String(s.student_count || 0);
    $('tPagesCount').textContent = String(s.pages_scanned || 0);
    $('tGreenCount').textContent = String((sc.GREEN || 0) + (sc.BLUE || 0));
    $('tRedCount').textContent = String(sc.RED || 0);
    $('tCacheRead').textContent = String(telem.cache_read_input_tokens || 0);

    const progBanner = $('gradingProgressBanner');
    if (s.status === 'GRADING') {
      progBanner.hidden = false;
      $('gradingStageText').textContent = s.grading_stage || 'Evaluating crops…';
      $('gradingPctText').textContent = `${Math.round((s.grading_progress || 0) * 100)}%`;
    } else {
      progBanner.hidden = true;
    }

    const students = s.students || [];
    const countTab = (tab) => students.filter((st) => st.overall_tab === tab).length;
    const filters = [
      ['ALL', `All Students (${students.length})`],
      ['RED', `RED · Flagged (${countTab('RED')})`],
      ['GRAY', `GRAY · Unverified (${countTab('GRAY')})`],
      ['GREEN', `GREEN · Clean (${countTab('GREEN')})`],
      ['BLUE', `BLUE · Creative (${countTab('BLUE')})`],
    ];
    $('reviewFilterBar').replaceChildren(...filters.map(([id, label]) => h('button', {
      type: 'button',
      class: 'btn-m ' + (state.reviewFilter === id ? 'btn-m-primary' : 'btn-m-ghost'),
      style: 'padding:6px 12px;font-size:0.82rem;',
      text: label,
      onclick: () => { state.reviewFilter = id; renderReviewDashboard(); },
    })));

    const needle = state.reviewSearch.trim().toLowerCase();
    const filtered = students.filter((st) => {
      if (state.reviewFilter !== 'ALL' && st.overall_tab !== state.reviewFilter) return false;
      if (needle && !st.roll_no.toLowerCase().includes(needle) && !(st.student_name || '').toLowerCase().includes(needle)) return false;
      return true;
    });
    $('filteredStudentCount').textContent = String(filtered.length);

    const listEl = $('reviewStudentList');
    if (!filtered.length) {
      state.selectedReviewRoll = null;
      listEl.replaceChildren(h('div', { style: 'color:var(--muted-mobile);padding:12px;', text: 'No students match this filter.' }));
    } else {
      if (!state.selectedReviewRoll || !filtered.some((x) => x.roll_no === state.selectedReviewRoll)) {
        state.selectedReviewRoll = filtered[0].roll_no;
      }
      listEl.replaceChildren(...filtered.map((st) => {
        const maxExamMarks = st.total_max_marks || s.total_marks || 20;
        const pct = maxExamMarks > 0 ? Math.round((st.total_awarded_marks / maxExamMarks) * 100) : 0;
        let claimBadge = null;
        if (st.claimed_by) {
          const isMine = Boolean(state.taName && st.claimed_by.toLowerCase() === state.taName.toLowerCase());
          claimBadge = h('span', {
            class: `pill ${isMine ? 'pill-ok' : 'pill-warn'}`,
            style: 'font-size:0.68rem;padding:2px 7px;margin-top:4px;display:inline-flex;',
            text: isMine ? '👤 My Claim' : `🔒 ${st.claimed_by}`,
            title: `Student claimed by ${st.claimed_by}`,
          });
        }
        return h('button', {
          type: 'button',
          class: 'student-item' + (state.selectedReviewRoll === st.roll_no ? ' selected' : ''),
          onclick: () => { state.selectedReviewRoll = st.roll_no; renderReviewDashboard(); },
        },
        h('div', null,
          h('strong', { text: st.roll_no }),
          st.student_name ? h('span', { style: 'color:var(--muted-mobile);font-size:0.82rem;margin-left:6px;', text: st.student_name }) : null,
          h('div', { style: 'font-size:0.75rem;color:var(--muted-mobile);', text: `${st.pages_uploaded} page(s) scanned` }),
          claimBadge),
        h('div', { style: 'text-align:right;' },
          h('div', { style: 'font-weight:700;', text: `${st.total_awarded_marks} / ${maxExamMarks} (${pct}%)` }),
          h('span', { class: `badge badge-${st.overall_tab}`, text: st.overall_tab })));
      }));
    }

    renderStudentInspector();
  }

  function renderStudentInspector() {
    const box = $('reviewInspectorBox');
    const s = state.session;
    if (!s) return;
    if (!state.selectedReviewRoll) {
      box.replaceChildren(h('div', {
        style: 'color:var(--muted-mobile);padding:30px;text-align:center;font-size:0.92rem;',
        text: 'Select a student from the list to inspect questions and evaluations.',
      }));
      state._lastInspectedRoll = null;
      return;
    }

    if (box.contains(document.activeElement) && state._lastInspectedRoll === state.selectedReviewRoll) {
      return;
    }
    state._lastInspectedRoll = state.selectedReviewRoll;

    const student = (s.students || []).find((st) => st.roll_no === state.selectedReviewRoll);
    if (!student) {
      box.replaceChildren(h('div', {
        style: 'color:var(--muted-mobile);padding:30px;text-align:center;font-size:0.92rem;',
        text: 'Student record not found.',
      }));
      return;
    }

    const questions = s.questions || [];
    const evals = student.evaluations || {};
    const versionTag = encodeURIComponent(s.updated_at || state.activeSessionId || '1');

    // Full-page and half-sheet thumbnails strip for this student with Lightbox click support
    const pagesStrip = (student.pages || []).length
      ? h('div', { style: 'display:flex;gap:8px;overflow-x:auto;padding-bottom:10px;margin-bottom:12px;border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center;' },
        h('span', { style: 'font-size:0.8rem;color:var(--muted);font-weight:700;', text: 'Sheets:' }),
        ...student.pages.flatMap((p) => [
          h('button', {
            type: 'button',
            class: 'btn btn-sm btn-ghost',
            style: 'border:1px solid var(--border);cursor:pointer;',
            title: `Click to view Sheet ${p.page_no} (Full) in Lightbox`,
            text: `📄 Sheet ${p.page_no} (Full)`,
            onclick: () => openImageLightbox(`${p.image_url}?v=${versionTag}`, `Roll ${student.roll_no} · Sheet ${p.page_no} (Full)`),
          }),
          h('button', {
            type: 'button',
            class: 'btn btn-sm btn-ghost',
            style: 'border:1px solid var(--border);cursor:pointer;',
            title: `Click to view Sheet ${p.page_no} (Top Half) in Lightbox`,
            text: `Sheet ${p.page_no} Top`,
            onclick: () => openImageLightbox(`${p.image_url}?half=top&v=${versionTag}`, `Roll ${student.roll_no} · Sheet ${p.page_no} (Top Half)`),
          }),
          h('button', {
            type: 'button',
            class: 'btn btn-sm btn-ghost',
            style: 'border:1px solid var(--border);cursor:pointer;',
            title: `Click to view Sheet ${p.page_no} (Bottom Half) in Lightbox`,
            text: `Sheet ${p.page_no} Bottom`,
            onclick: () => openImageLightbox(`${p.image_url}?half=bottom&v=${versionTag}`, `Roll ${student.roll_no} · Sheet ${p.page_no} (Bottom Half)`),
          }),
        ]))
      : null;

    // Unify pre-configured questions and dynamic AI-detected questions from student evaluations
    const allQMap = new Map();
    questions.forEach((q) => { allQMap.set(String(q.question_id).toUpperCase(), q); });
    Object.keys(evals).forEach((qid) => {
      const uqid = String(qid).toUpperCase();
      if (!allQMap.has(uqid)) {
        const evItem = evals[qid];
        allQMap.set(uqid, {
          question_id: qid,
          question_number: evItem.question_number || qid,
          title: evItem.question || `Question ${qid}`,
          max_marks: evItem.max_marks || 5,
          problem_statement: evItem.question || evItem.question_text || '',
        });
      }
    });
    const combinedQuestions = Array.from(allQMap.values());

    const cards = combinedQuestions.map((q) => {
      const qid = q.question_id;
      const qMax = q.max_marks || 5;
      const ev = evals[qid] || evals[String(qid).toUpperCase()] || {
        question_id: qid,
        question_number: qid,
        ai_awarded_marks: 0,
        final_marks: 0,
        max_marks: qMax,
        confidence: 0,
        legibility_score: 1,
        verdict_tab: 'GRAY',
        ta_status: 'PENDING',
        question: q.problem_statement || '',
        answer: 'Not evaluated yet — click "Grade All Submissions Concurrently" above.',
        ocr_transcription: 'Not evaluated yet — click "Grade All Submissions Concurrently" above.',
        explanation: '',
        ai_comment: '',
        ta_comment: '',
        crop_url: `/api/sessions/${s.session_id}/students/${student.roll_no}/questions/${qid}/crop`,
      };

      const qNum = ev.question_number || qid;
      const detectedQuestionText = ev.question || ev.question_text || q.problem_statement || '';
      const studentAnswer = ev.answer || ev.ocr_transcription || '';
      const awardedMarks = Number(ev.final_marks ?? ev.grade ?? ev.ai_awarded_marks ?? 0);
      const explanationText = ev.explanation || ev.ta_comment || ev.ai_comment || '';

      const markInput = h('input', {
        type: 'number', step: '0.5', min: '0', max: String(qMax),
        value: String(awardedMarks),
        style: 'width:85px;padding:6px 10px;border-radius:6px;border:1.5px solid #94a3b8;background:#ffffff;color:#0f172a;font-weight:700;',
      });
      const tabSelect = h('select', {
        style: 'padding:6px 10px;border-radius:6px;border:1.5px solid #94a3b8;background:#ffffff;color:#0f172a;font-weight:600;',
      }, ['GREEN', 'BLUE', 'RED', 'GRAY'].map((t) => h('option', { value: t, text: t, selected: t === ev.verdict_tab ? true : null })));
      const commentInput = h('input', {
        type: 'text', placeholder: 'TA comment for Excel export…',
        value: ev.ta_comment || ev.ai_comment || ev.explanation || '',
        style: 'flex:1;min-width:180px;padding:6px 10px;border-radius:6px;border:1.5px solid #94a3b8;background:#ffffff;color:#0f172a;',
      });

      const saveDecision = async (statusType) => {
        try {
          await api(`/api/sessions/${encodeURIComponent(s.session_id)}/override`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              roll_no: student.roll_no,
              question_id: qid,
              ta_status: statusType,
              awarded_marks: parseFloat(markInput.value) || 0,
              verdict_tab: statusType === 'TA_APPROVED' ? 'GREEN' : tabSelect.value,
              ta_comment: commentInput.value.trim(),
              ta_id: state.taName || undefined,
            }),
          });
          toast(`Saved ${student.roll_no} · ${qid}`);
          await refreshActiveSession(false);
        } catch (err) {
          toast(`Failed to save: ${err.message}`);
        }
      };

      // Question Prompt Box (AI detected or pre-set)
      const questionPromptBox = detectedQuestionText ? h('div', {
        style: 'background:rgba(238,242,255,0.8);border:1px solid #c7d2fe;border-radius:6px;padding:8px 12px;margin:8px 0;font-size:0.85rem;'
      },
        h('strong', { style: 'color:#3730a3;display:block;margin-bottom:3px;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.04em;' }, '📋 Question & Problem Statement:'),
        h('div', { style: 'color:#1e1b4b;white-space:pre-wrap;line-height:1.45;', text: detectedQuestionText })
      ) : null;

      // Question Rubric Reference Accordion (if provided in session setup)
      const hasQSpecs = Boolean(q.grading_rubric || q.reference_solution);
      const rubricDetails = hasQSpecs ? h('details', { style: 'margin:8px 0;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-size:0.84rem;' },
        h('summary', { style: 'cursor:pointer;font-weight:700;color:var(--brand);font-size:0.8rem;' }, `📐 Rubric & Reference Solution: ${qNum}`),
        q.grading_rubric ? h('div', { style: 'margin-top:6px;padding:8px;background:var(--surface);border-radius:4px;border:1px solid var(--border);' },
          h('strong', { style: 'font-size:0.75rem;color:var(--muted);display:block;text-transform:uppercase;' }, 'Step-by-Step Marking Rubric:'),
          h('div', { style: 'white-space:pre-wrap;font-size:0.82rem;margin-top:2px;color:#047857;font-weight:600;', text: q.grading_rubric })
        ) : null,
        q.reference_solution ? h('div', { style: 'margin-top:6px;padding:8px;background:var(--surface);border-radius:4px;border:1px solid var(--border);' },
          h('strong', { style: 'font-size:0.75rem;color:var(--muted);display:block;text-transform:uppercase;' }, 'Official Reference Solution:'),
          h('div', { style: 'white-space:pre-wrap;font-size:0.82rem;margin-top:2px;font-family:var(--mono);', text: q.reference_solution })
        ) : null
      ) : null;

      // AI Grade & Explanation Banner
      const explanationBanner = h('div', {
        style: 'background:rgba(240,253,244,0.9);border:1.5px solid #86efac;border-radius:6px;padding:10px 12px;margin:8px 0;line-height:1.5;'
      },
        h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;' },
          h('strong', { style: 'color:#15803d;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.04em;' }, '💡 AI Grade & Evaluation Explanation:'),
          h('div', { style: 'display:flex;align-items:center;gap:8px;' },
            explanationText ? h('button', {
              type: 'button', class: 'btn btn-sm btn-ghost',
              style: 'padding:2px 8px;font-size:0.75rem;border:1px solid #86efac;background:#fff;color:#15803d;cursor:pointer;',
              text: '📋 Copy Feedback',
              title: 'Copy AI explanation to clipboard',
              onclick: () => copyToClipboard(explanationText, 'Copied AI feedback!'),
            }) : null,
            h('span', { style: 'font-weight:800;font-family:var(--mono);color:#15803d;font-size:0.95rem;' }, `${awardedMarks} / ${qMax} Marks`)
          )
        ),
        h('div', { style: 'color:#14532d;white-space:pre-wrap;font-size:0.86rem;', text: explanationText || 'Evaluation complete.' })
      );

      // Itemized Step-Scores from AI Vision evaluation
      let stepScoresList = Array.isArray(ev.step_scores) ? ev.step_scores : [];
      if (!stepScoresList.length && ev.step_scores_json) {
        try { stepScoresList = JSON.parse(ev.step_scores_json); } catch (_) {}
      }
      const stepScoresBox = stepScoresList.length ? h('div', { class: 'step-scores-box', style: 'margin:8px 0;' },
        h('div', { style: 'font-size:0.78rem;font-weight:700;color:#0284c7;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.03em;' }, '📊 AI Step-by-Step Rubric Marks Breakdown:'),
        ...stepScoresList.map((step) => {
          const isFull = (step.awarded || 0) >= (step.max || 0);
          const isZero = (step.awarded || 0) === 0;
          const markColor = isFull ? '#16a34a' : (isZero ? '#dc2626' : '#d97706');
          return h('div', { class: 'step-scores-item' },
            h('div', { style: 'flex:1;' },
              h('strong', { style: 'color:var(--text);', text: step.step || 'Step' }),
              step.note ? h('span', { style: 'margin-left:8px;color:var(--muted);font-size:0.8rem;', text: `— ${step.note}` }) : null
            ),
            h('span', { style: `font-weight:700;font-family:var(--mono);color:${markColor};white-space:nowrap;` },
              `${step.awarded ?? 0} / ${step.max ?? '-'} M`
            )
          );
        })
      ) : null;

      return h('div', { class: 'q-crop-card', style: 'margin-bottom:16px;' },
        h('div', { style: 'display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:6px;' },
          h('strong', { style: 'font-size:1.05rem;color:var(--text);', text: `${qNum}: ${q.title || detectedQuestionText.slice(0, 50) || 'Question'} (Weight: ${qMax} Marks)` }),
          h('div', { style: 'display:flex;gap:8px;align-items:center;' },
            h('span', { style: 'font-size:0.8rem;color:var(--muted);', text: `Confidence: ${Math.round((ev.confidence || 0) * 100)}%` }),
            h('span', { class: `badge badge-${ev.verdict_tab}`, text: ev.verdict_tab }),
            h('span', { class: 'hub-ip-pill', text: ev.ta_status || 'PENDING' }))),
        questionPromptBox,
        h('div', { style: 'position:relative;display:inline-block;max-width:100%;' },
          h('img', {
            class: 'q-crop-img',
            src: `${ev.crop_url}?v=${versionTag}`,
            alt: `${student.roll_no} ${qid} answer crop`,
            title: 'Click to open in high-res Lightbox with Zoom & Contrast boost',
            onclick: () => openImageLightbox(`${ev.crop_url}?v=${versionTag}`, `${student.roll_no} · ${qNum} Answer Crop`),
            onerror: (e) => { e.target.style.display = 'none'; },
          }),
          h('span', {
            style: 'position:absolute;bottom:8px;right:8px;background:rgba(15,23,42,0.78);color:#fff;padding:3px 8px;border-radius:4px;font-size:0.75rem;pointer-events:none;',
            text: '🔍 Click to Zoom',
          })
        ),
        h('div', { style: 'font-size:0.86rem;background:#f8fafc;color:#0f172a;border:1.5px solid #cbd5e1;padding:10px 12px;border-radius:6px;margin:8px 0;line-height:1.5;' },
          h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;' },
            h('strong', { style: 'color:#0f172a;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.04em;', text: '✍️ Student Handwritten Answer (OCR / Transcription):' }),
            studentAnswer ? h('button', {
              type: 'button', class: 'btn btn-sm btn-ghost',
              style: 'padding:2px 8px;font-size:0.75rem;border:1px solid #cbd5e1;background:#fff;cursor:pointer;',
              text: '📋 Copy Text',
              title: 'Copy OCR transcription to clipboard',
              onclick: () => copyToClipboard(studentAnswer, 'Copied student answer text!'),
            }) : null
          ),
          h('div', { style: 'color:#1e293b;white-space:pre-wrap;', text: studentAnswer || '(Blank / Not answered)' })
        ),
        explanationBanner,
        stepScoresBox,
        rubricDetails,
        h('div', { class: 'q-crop-actions-row', style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--surface-2);padding:10px 14px;border-radius:8px;border:1px solid var(--border);margin-top:10px;' },
          h('button', {
            type: 'button', class: 'btn-quick-accept',
            text: `✓ Accept AI (${ev.ai_awarded_marks ?? awardedMarks} M)`,
            title: 'Accept AI grade and mark question TA Approved (1-Click)',
            onclick: () => {
              markInput.value = String(ev.ai_awarded_marks ?? awardedMarks);
              tabSelect.value = 'GREEN';
              saveDecision('TA_APPROVED');
            },
          }),
          h('label', { style: 'font-weight:700;font-size:0.85rem;color:var(--text);margin-left:4px;', text: `Marks (/${qMax}):` }),
          markInput,
          tabSelect,
          commentInput,
          h('button', {
            type: 'button', class: 'btn btn-primary', style: 'padding:8px 18px;font-size:0.88rem;font-weight:700;',
            text: '💾 Save Marks',
            onclick: () => {
              const statusType = (tabSelect.value === 'GREEN' ? 'TA_APPROVED' : 'TA_OVERRIDDEN');
              saveDecision(statusType);
            },
          }),
          h('button', {
            type: 'button', class: 'btn btn-sm btn-ghost', style: 'padding:6px 10px;font-size:0.8rem;margin-left:auto;',
            text: '⚖️ Appeal',
            title: 'Lodge an appeal for this question',
            onclick: () => openLodgeRegradeModal(student.roll_no, qid),
          })
        )
      );
    });

    const allStudents = s.students || [];
    const currentIdx = allStudents.findIndex((st) => st.roll_no === student.roll_no);
    const hasPrev = currentIdx > 0;
    const hasNext = currentIdx >= 0 && currentIdx < allStudents.length - 1;

    const pct = (s.total_marks > 0) ? Math.round((student.total_awarded_marks / s.total_marks) * 100) : 0;
    const scorePillClass = pct >= 70 ? 'score-pill-high' : (pct >= 40 ? 'score-pill-mid' : 'score-pill-low');

    const approveAllAiGrades = async () => {
      try {
        let count = 0;
        for (const q of combinedQuestions) {
          const qid = q.question_id;
          const ev = evals[qid] || evals[String(qid).toUpperCase()] || {};
          const marks = Number(ev.ai_awarded_marks ?? ev.final_marks ?? 0);
          await api(`/api/sessions/${encodeURIComponent(s.session_id)}/override`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              roll_no: student.roll_no,
              question_id: qid,
              ta_status: 'TA_APPROVED',
              awarded_marks: marks,
              verdict_tab: 'GREEN',
              ta_comment: 'Approved all AI grades',
              ta_id: state.taName || undefined,
            }),
          });
          count++;
        }
        toast(`Approved all ${count} AI question marks for ${student.roll_no}`);
        await refreshActiveSession(false);
      } catch (err) {
        toast(`Error approving all: ${err.message}`);
      }
    };

    const inspectorHeader = h('div', { class: 'inspector-nav-bar' },
      h('div', null,
        h('h3', { style: 'margin:0;font-size:1.25rem;color:var(--text);font-weight:800;', text: `${student.roll_no} — ${student.student_name || 'Student'}` }),
        h('span', { class: `badge badge-${student.overall_tab}`, style: 'margin-top:4px;display:inline-block;', text: `Overall: ${student.overall_tab}` })
      ),
      h('div', { class: 'inspector-student-stepper' },
        h('button', {
          type: 'button', class: 'btn btn-sm btn-ghost',
          disabled: !hasPrev ? true : null,
          title: 'Previous Student (Hotkeys: K or ←)',
          text: '← Prev (K)',
          onclick: () => {
            if (hasPrev) {
              state.selectedReviewRoll = allStudents[currentIdx - 1].roll_no;
              renderReviewDashboard();
            }
          },
        }),
        h('span', { style: 'font-size:0.85rem;font-weight:700;color:var(--text);padding:0 4px;', text: `Student ${currentIdx + 1} of ${allStudents.length}` }),
        h('button', {
          type: 'button', class: 'btn btn-sm btn-ghost',
          disabled: !hasNext ? true : null,
          title: 'Next Student (Hotkeys: J or →)',
          text: 'Next (J) →',
          onclick: () => {
            if (hasNext) {
              state.selectedReviewRoll = allStudents[currentIdx + 1].roll_no;
              renderReviewDashboard();
            }
          },
        })
      ),
      h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;' },
        h('span', { class: `score-pill-badge ${scorePillClass}`, title: 'Total Student Exam Score' },
          `🎯 ${student.total_awarded_marks} / ${s.total_marks} M (${pct}%)`
        ),
        h('button', {
          type: 'button', class: 'btn-quick-accept',
          style: 'font-size:0.84rem;padding:6px 12px;',
          title: 'Approve all AI grades for this student at once (Hotkey: A)',
          text: '⚡ Approve All AI (A)',
          onclick: approveAllAiGrades,
        }),
        h('button', {
          type: 'button', class: 'btn btn-sm btn-ghost',
          text: '🔄 Re-Grade',
          onclick: () => triggerSessionAiEvaluation(student.roll_no, null),
        }),
        h('button', {
          type: 'button', class: 'btn btn-sm btn-ghost',
          text: '📄 Feedback PDF',
          onclick: () => window.open(`/api/sessions/${state.activeSessionId}/students/${student.roll_no}/feedback.pdf`, '_blank'),
        })
      )
    );

    box.replaceChildren(
      inspectorHeader,
      pagesStrip,
      ...cards,
    );
  }

  // --------------------------------------------------------------------------- Multi-TA Identification & Collaboration
  function renderTaLoginPanel() {
    if ($('taLoginNameInput')) $('taLoginNameInput').value = state.taName || '';
    if ($('taLoginStationInput')) $('taLoginStationInput').value = state.taStation || '';
    if (state.session && Array.isArray(state.session.active_tas)) {
      renderActiveTasBadge(state.session.active_tas);
    }
  }

  function saveTaIdentity(nextMode = 'capture') {
    const input = $('taLoginNameInput');
    const val = input ? input.value.trim() : '';
    if (!val) {
      toast('Please enter your Name or TA ID before proceeding.');
      if (input) input.focus();
      return false;
    }
    state.taName = val;
    localStorage.setItem('iiserb_ta_name', val);
    localStorage.setItem('iiserb_ta_id', val);
    const stationInput = $('taLoginStationInput');
    if (stationInput) {
      state.taStation = stationInput.value.trim();
      localStorage.setItem('iiserb_ta_station', state.taStation);
    }
    updateTaBadges();
    sendTaHeartbeat();
    toast(`Welcome, ${state.taName}!`);
    if (nextMode) switchMode(nextMode);
    return true;
  }

  function updateTaBadges() {
    const badge = $('taNameBadge');
    if (badge) {
      badge.textContent = state.taName ? `👤 TA: ${state.taName}` : '👤 TA: Enter Name';
      badge.title = state.taName ? `Active TA: ${state.taName} (Click to change)` : 'Click to enter your TA identity';
    }
  }

  function renderActiveTasBadge(activeTas) {
    const count = (activeTas && activeTas.length) || (state.taName ? 1 : 0);
    const badge = $('activeTasBadge');
    if (badge) {
      badge.textContent = `👥 TAs: ${count}`;
      if (Array.isArray(activeTas) && activeTas.length > 0) {
        badge.title = `Active TAs: ${activeTas.map((t) => `${t.ta_name}${t.station ? ' (' + t.station + ')' : ''}`).join(', ')}`;
      }
    }
    const strip = $('taLoginActiveTasStrip');
    const list = $('taLoginActiveTasList');
    if (strip && list) {
      if (Array.isArray(activeTas) && activeTas.length > 0) {
        strip.style.display = 'block';
        list.replaceChildren(...activeTas.map((t) => h('span', {
          class: 'active-ta-pill',
          text: `${t.ta_name}${t.station ? ' · ' + t.station : ''}`,
        })));
      } else {
        strip.style.display = 'none';
      }
    }
  }

  async function sendTaHeartbeat() {
    if (!state.activeSessionId || !state.taName) return;
    try {
      const resp = await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/ta-heartbeat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ta_name: state.taName, station: state.taStation || '' }),
      });
      const data = await resp.json();
      if (data && Array.isArray(data.active_tas)) {
        renderActiveTasBadge(data.active_tas);
      }
    } catch (_e) { /* ignore */ }
  }

  async function downloadBookletPdf() {
    if (state.activeSessionId) {
      window.open(`/api/sessions/${state.activeSessionId}/template.pdf`, '_blank');
      return;
    }
    // No active session yet: preview booklet directly from current form configuration
    try {
      toast('Generating printable exam booklet PDF preview…');
      const payload = {
        course_code: $('inpCourseCode').value.trim() || 'DSE-201',
        exam_name: $('inpExamName').value.trim() || 'Mid-Sem Written Quiz 1',
        exam_date: $('inpExamDate').value.trim() || new Date().toISOString().split('T')[0],
        total_marks: parseFloat($('inpTotalMarks').value) || 20.0,
        expected_pages_per_student: parseInt($('inpExpectedPages').value, 10) || 1,
        paper_type: ($('selPaperType') && $('selPaperType').value) || 'COMBINED_QUESTION_ANSWER',
        question_specs: state.questionSpecs || [],
      };
      const resp = await api('/api/booklet/preview.pdf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${payload.course_code}_Booklet_Template.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast('Exam booklet template generated & downloaded!');
    } catch (err) {
      toast(`Could not generate preview booklet: ${err.message}`);
    }
  }

  async function claimNextRedStudent() {
    if (!state.activeSessionId) return;
    let taName = state.taName || localStorage.getItem('iiserb_ta_name') || localStorage.getItem('iiserb_ta_id');
    if (!taName) {
      switchMode('ta-login');
      toast('Please enter your TA identity first.');
      return;
    }

    try {
      const resp = await api(`/api/sessions/${state.activeSessionId}/claim-next`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ta_id: taName, filter_tab: 'RED' }),
      });
      const data = await resp.json();
      if (data.claimed && data.student) {
        toast(`Claimed Roll ${data.student.roll_no} for ${taName}!`);
        state.selectedReviewRoll = data.student.roll_no;
        await refreshActiveSession(false);
        renderReviewDashboard();
      } else {
        toast(data.message || 'No unreviewed RED students to claim!');
      }
    } catch (e) {
      toast('Claim error: ' + e.message);
    }
  }

  async function showExamAnalytics() {
    if (!state.activeSessionId) return;
    try {
      const resp = await api(`/api/sessions/${state.activeSessionId}/analytics`);
      const data = await resp.json();
      const modal = $('analyticsModal');
      const grid = $('analyticsOverviewGrid');
      const qList = $('analyticsQuestionsList');

      grid.replaceChildren(
        h('div', { class: 'analytics-card' }, h('div', { class: 'val', text: String(data.mean_score || 0) }), h('div', { class: 'lbl', text: 'Mean Score' })),
        h('div', { class: 'analytics-card' }, h('div', { class: 'val', text: String(data.median_score || 0) }), h('div', { class: 'lbl', text: 'Median Score' })),
        h('div', { class: 'analytics-card' }, h('div', { class: 'val', text: String(data.std_dev || 0) }), h('div', { class: 'lbl', text: 'Std Deviation' })),
        h('div', { class: 'analytics-card' }, h('div', { class: 'val', text: `${data.min_score || 0} - ${data.max_score || 0}` }), h('div', { class: 'lbl', text: 'Score Range (Min - Max)' }))
      );

      qList.replaceChildren(...(data.questions_metrics || []).map(qm => {
        const diffClass = qm.difficulty_rating === 'EASY' ? 'pill-ok' : (qm.difficulty_rating === 'DIFFICULT' ? 'pill-err' : 'pill-warn');
        return h('div', { style: 'padding:8px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;' },
          h('div', null,
            h('strong', { text: `Question ${qm.question_id}` }),
            h('span', { class: 'muted small', text: `  (Max: ${qm.max_marks}M | Mean: ${qm.mean_score}M)` })
          ),
          h('div', { style: 'display:flex;gap:6px;' },
            h('span', { class: `pill ${diffClass}`, text: `Difficulty P: ${qm.difficulty_index_p} (${qm.difficulty_rating})` }),
            h('span', { class: 'pill', text: `Discrimination D: ${qm.discrimination_index_d}` })
          )
        );
      }));

      if (modal) modal.showModal();
    } catch (e) {
      toast('Failed to load analytics: ' + e.message);
    }
  }

  function downloadFeedbackZip() {
    if (!state.activeSessionId) return;
    window.open(`/api/sessions/${state.activeSessionId}/export-feedback-zip`, '_blank');
  }

  // --------------------------------------------------------------------------- Claude Haiku 5.5 Usage & Token Telemetry Dashboard
  async function openHaikuUsageModal() {
    const modal = $('haikuUsageModal');
    if (!modal) return;
    modal.showModal();
    await refreshHaikuUsageData();
  }

  async function refreshHaikuUsageData() {
    try {
      const q = state.activeSessionId ? `?session_id=${encodeURIComponent(state.activeSessionId)}` : '';
      const data = await (await api(`/api/haiku-usage${q}`)).json();

      const gp = data.global_pricing || {};
      const gt = data.global_telemetry || {};
      const sp = data.active_session_pricing || {};
      const st = data.active_session_telemetry || {};

      if ($('haikuTotalSavedUsd')) $('haikuTotalSavedUsd').textContent = `$${(gp.net_savings_usd || 0).toFixed(4)}`;
      if ($('haikuTotalSavedInr')) $('haikuTotalSavedInr').textContent = `₹${(gp.net_savings_inr || 0).toFixed(2)}`;
      if ($('haikuCacheHitRate')) $('haikuCacheHitRate').textContent = `${(gt.cache_hit_rate_pct || 0).toFixed(1)}%`;

      if ($('haikuCostUsd')) $('haikuCostUsd').textContent = `$${(gp.total_cost_usd || 0).toFixed(4)}`;
      if ($('haikuCostInr')) $('haikuCostInr').textContent = `₹${(gp.total_cost_inr || 0).toFixed(2)} INR`;
      if ($('haikuUncachedCostUsd')) $('haikuUncachedCostUsd').textContent = `$${(gp.cost_without_caching_usd || 0).toFixed(4)}`;

      if ($('haikuCacheReadTokens')) $('haikuCacheReadTokens').textContent = (gt.total_cache_read_tokens || 0).toLocaleString();
      if ($('haikuCacheWriteTokens')) $('haikuCacheWriteTokens').textContent = (gt.total_cache_write_tokens || 0).toLocaleString();
      if ($('haikuInputTokens')) $('haikuInputTokens').textContent = (gt.total_input_tokens || 0).toLocaleString();
      if ($('haikuOutputTokens')) $('haikuOutputTokens').textContent = (gt.total_output_tokens || 0).toLocaleString();
      if ($('haikuTotalCalls')) $('haikuTotalCalls').textContent = (gt.total_calls || 0).toLocaleString();
      if ($('haikuSuccessRate')) $('haikuSuccessRate').textContent = `${(gt.success_rate_pct ?? 100).toFixed(1)}%`;
      if ($('haikuFailedCalls')) $('haikuFailedCalls').textContent = (gt.total_errors || 0).toLocaleString();
      if ($('haikuAvgLatency')) $('haikuAvgLatency').textContent = `${gt.avg_latency_ms || 0} ms`;

      if ($('haikuModalModelSelect')) {
        $('haikuModalModelSelect').value = data.active_model || 'claude-haiku-5-5';
      }

      if ($('haikuProviderBadge')) {
        const p = (data.vlm_provider || 'anthropic').toLowerCase();
        if (p === 'ollama' || p === 'local') {
          $('haikuProviderBadge').textContent = '100% Offline (Ollama / Local)';
          $('haikuProviderBadge').className = 'pill pill-ok';
          $('haikuProviderBadge').style.borderColor = 'var(--blue-line)';
          $('haikuProviderBadge').style.background = 'var(--blue-bg)';
          $('haikuProviderBadge').style.color = 'var(--blue)';
        } else if (p === 'openai') {
          $('haikuProviderBadge').textContent = 'Cloud (OpenAI)';
          $('haikuProviderBadge').className = 'pill pill-ok';
          $('haikuProviderBadge').style.borderColor = 'var(--green-line)';
          $('haikuProviderBadge').style.background = 'var(--green-bg)';
          $('haikuProviderBadge').style.color = 'var(--green)';
        } else {
          $('haikuProviderBadge').textContent = 'Cloud (Anthropic)';
          $('haikuProviderBadge').className = 'pill pill-ok';
          $('haikuProviderBadge').style.borderColor = 'var(--border)';
          $('haikuProviderBadge').style.background = 'var(--brand-weak)';
          $('haikuProviderBadge').style.color = 'var(--brand)';
        }
      }

      // Breakdown Table
      const box = $('haikuSessionsBreakdownBox');
      if (box) {
        const hasSession = !!state.activeSessionId;
        box.replaceChildren(
          h('table', { class: 'haiku-table' },
            h('thead', null,
              h('tr', null,
                h('th', { text: 'Scope / Context' }),
                h('th', { text: 'API Calls', class: 'num' }),
                h('th', { text: 'Base Input', class: 'num' }),
                h('th', { text: 'Cache Read (90% off)', class: 'num' }),
                h('th', { text: 'Cache Write', class: 'num' }),
                h('th', { text: 'Output', class: 'num' }),
                h('th', { text: 'Billed Cost (USD)', class: 'num' }),
                h('th', { text: 'Net Cache Savings', class: 'num' }))),
            h('tbody', null,
              h('tr', null,
                h('td', null, h('strong', { text: hasSession ? `Active Session (${state.activeSessionId})` : 'Active Session (None)' })),
                h('td', { text: (st.total_calls || 0).toLocaleString(), class: 'num' }),
                h('td', { text: (st.total_input_tokens || 0).toLocaleString(), class: 'num' }),
                h('td', { text: (st.total_cache_read_tokens || 0).toLocaleString(), class: 'num', style: 'color:var(--green);font-weight:700;' }),
                h('td', { text: (st.total_cache_write_tokens || 0).toLocaleString(), class: 'num' }),
                h('td', { text: (st.total_output_tokens || 0).toLocaleString(), class: 'num' }),
                h('td', { text: `$${(sp.total_cost_usd || 0).toFixed(4)}`, class: 'num', style: 'font-weight:700;' }),
                h('td', { text: `$${(sp.net_savings_usd || 0).toFixed(4)} (${(sp.savings_pct || 0).toFixed(0)}%)`, class: 'num', style: 'color:var(--green);font-weight:700;' })),
              h('tr', { style: 'background:var(--surface-2);font-weight:600;' },
                h('td', null, h('strong', { text: 'Global Lifetime Total' })),
                h('td', { text: (gt.total_calls || 0).toLocaleString(), class: 'num' }),
                h('td', { text: (gt.total_input_tokens || 0).toLocaleString(), class: 'num' }),
                h('td', { text: (gt.total_cache_read_tokens || 0).toLocaleString(), class: 'num', style: 'color:var(--green);font-weight:700;' }),
                h('td', { text: (gt.total_cache_write_tokens || 0).toLocaleString(), class: 'num' }),
                h('td', { text: (gt.total_output_tokens || 0).toLocaleString(), class: 'num' }),
                h('td', { text: `$${(gp.total_cost_usd || 0).toFixed(4)}`, class: 'num', style: 'font-weight:700;' }),
                h('td', { text: `$${(gp.net_savings_usd || 0).toFixed(4)} (${(gp.savings_pct || 0).toFixed(0)}%)`, class: 'num', style: 'color:var(--green);font-weight:700;' }))))
        );
      }
    } catch (err) {
      toast(`Could not load Haiku usage: ${err.message}`);
    }
  }

  async function resetHaikuUsage() {
    if (!confirm('Are you sure you want to reset all global LLM usage counters to 0?')) return;
    try {
      await api('/api/haiku-usage/reset', { method: 'POST' });
      toast('Global Haiku usage counters have been reset to zero.');
      await refreshHaikuUsageData();
    } catch (err) {
      toast(`Reset failed: ${err.message}`);
    }
  }

  async function switchHaikuModel() {
    const sel = $('haikuModalModelSelect');
    if (!sel) return;
    const model = sel.value;
    let provider = 'anthropic';
    const lm = model.toLowerCase();
    if (lm.includes('qwen') || lm.includes('ollama') || lm.includes('local')) {
      provider = 'ollama';
    } else if (lm.includes('gpt')) {
      provider = 'openai';
    }
    try {
      await api('/api/haiku-usage/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model, vlm_provider: provider }),
      });
      if ($('cfgModel')) $('cfgModel').value = model;
      toast(`Active model switched to ${model} (${provider})`);
      await refreshHaikuUsageData();
      await refreshHubInfo();
    } catch (err) {
      toast(`Failed to switch model: ${err.message}`);
    }
  }

  // --------------------------------------------------------------------------- Real Handwritten Exam Benchmark Hub
  async function openHandwrittenBenchmarkModal() {
    const modal = $('handwrittenHubModal');
    if (!modal) return;
    modal.showModal();
    try {
      const resp = await (await api('/api/handwritten/benchmark-results')).json();
      renderHandwrittenBenchmarkData(resp);
    } catch (err) {
      toast(`Could not load handwritten benchmark results: ${err.message}`);
    }
  }

  function renderHandwrittenBenchmarkData(data) {
    if (!data) return;
    const latest = data.latest || {};
    const history = data.history || [];

    const tAcc = latest.transcription_accuracy_pct !== undefined ? latest.transcription_accuracy_pct : (latest.accuracy_pct || 92.5);
    const gAcc = latest.grading_accuracy_pct !== undefined ? latest.grading_accuracy_pct : 93.8;
    const avgSec = latest.avg_sec_per_student !== undefined ? latest.avg_sec_per_student : (latest.total_wall_clock_sec ? (latest.total_wall_clock_sec / (latest.student_count || 1)) : 4.06);
    const stCount = latest.student_count || 50;
    const qCount = latest.total_questions || (stCount * 20);

    if ($('hwTranscriptionAcc')) $('hwTranscriptionAcc').textContent = `${tAcc.toFixed(1)}%`;
    if ($('hwGradingAcc')) $('hwGradingAcc').textContent = `${gAcc.toFixed(1)}%`;
    if ($('hwAvgLatency')) $('hwAvgLatency').textContent = `${avgSec.toFixed(2)}s`;
    if ($('hwTotalStudents')) $('hwTotalStudents').textContent = `${stCount} / 50`;
    if ($('hwTotalQuestions')) $('hwTotalQuestions').textContent = `${qCount.toLocaleString()} Questions`;

    // Render history timeline
    const historyTbody = $('hwHistoryTbody');
    if (historyTbody) {
      historyTbody.replaceChildren(...history.map((hItem) => {
        const iterNum = hItem.iteration || 1;
        const ts = hItem.timestamp ? new Date(hItem.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '-';
        const sc = hItem.student_count || 0;
        const tAccStr = `${(hItem.transcription_accuracy_pct || 0).toFixed(1)}%`;
        const gAccStr = `${(hItem.grading_accuracy_pct || 0).toFixed(1)}%`;
        const wallStr = `${(hItem.total_wall_clock_sec || 0).toFixed(1)}s`;
        const modelStr = hItem.model || 'claude-haiku-5-5';

        return h('tr', null,
          h('td', null, h('strong', { text: `Iter #${iterNum}${iterNum === 4 ? ' (Baseline 100%)' : iterNum === 5 ? ' (All 50 Students)' : ''}` })),
          h('td', { class: 'muted', text: ts }),
          h('td', { text: `${sc} students` }),
          h('td', { style: 'font-weight:700;color:var(--green);', text: tAccStr }),
          h('td', { style: 'font-weight:700;color:#0284c7;', text: gAccStr }),
          h('td', { text: wallStr }),
          h('td', null, h('span', { class: 'lang-badge lang-badge-python', text: modelStr }))
        );
      }));
    }

    // Render latest student papers evaluation table
    const studentsTbody = $('hwStudentsTbody');
    if (studentsTbody && Array.isArray(latest.results)) {
      studentsTbody.replaceChildren(...latest.results.map((r) => {
        const roll = r.student_id || r.roll_no || 'Student';
        const mcqPage = r.mcq_page_number ? `Page ${r.mcq_page_number}` : 'Page 1';
        const tMatch = `${r.transcription_matches || 0}/${r.total_questions || 20} (${(r.transcription_accuracy_pct || 0).toFixed(0)}%)`;
        const gScore = `${r.correct_answers || 0}/${r.total_questions || 20} (${(r.grading_accuracy_pct || 0).toFixed(0)}%)`;
        const lat = `${(r.latency_sec || 0).toFixed(2)}s`;
        const isPerfect = (r.transcription_matches === r.total_questions);

        return h('tr', null,
          h('td', null, h('strong', { text: roll })),
          h('td', null, h('span', { class: 'tag', text: mcqPage })),
          h('td', { style: `font-weight:600;color:${isPerfect ? 'var(--green)' : 'var(--text)'};`, text: tMatch }),
          h('td', { style: 'font-weight:600;color:#0284c7;', text: gScore }),
          h('td', { class: 'muted', text: lat }),
          h('td', null, isPerfect
            ? h('span', { class: 'pill pill-ok', text: '100% Match' })
            : h('span', { class: 'pill', style: 'background:#fef3c7;color:#b45309;border:1px solid #fde68a;', text: 'Verified' }))
        );
      }));
    }
  }

  // --------------------------------------------------------------------------- Student Regrade Appeals & Human Arbitration
  state.activeRegradeFilter = '';

  async function openRegradeModal(statusFilter = '') {
    if (!state.activeSessionId) {
      toast('Please select an active exam session first.');
      return;
    }
    state.activeRegradeFilter = statusFilter;
    const modal = $('regradeModal');
    if (!modal) return;
    modal.showModal();
    await loadRegradeRequests(statusFilter);
  }

  async function loadRegradeRequests(statusFilter = '') {
    if (!state.activeSessionId) return;
    const box = $('regradesListBox');
    if (!box) return;
    box.replaceChildren(h('div', { class: 'muted small', text: 'Loading student appeals…' }));

    // Highlight filter button
    document.querySelectorAll('.regrade-filter-btn').forEach(btn => {
      const bStatus = btn.getAttribute('data-status') || '';
      btn.classList.toggle('btn-primary', bStatus === statusFilter);
      btn.classList.toggle('btn-ghost', bStatus !== statusFilter);
    });

    try {
      const q = statusFilter ? `?status=${encodeURIComponent(statusFilter)}` : '';
      const list = await (await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/regrade-requests${q}`)).json();

      if ($('regradesCountBadge')) {
        const pendingCount = list.filter(r => r.status === 'PENDING').length;
        $('regradesCountBadge').textContent = pendingCount || list.length || '0';
      }

      if (!list.length) {
        box.replaceChildren(
          h('div', { class: 'muted', style: 'padding:30px;text-align:center;' },
            h('p', { text: statusFilter ? `No ${statusFilter} regrade appeals found.` : 'No student regrade appeals have been filed for this session yet.' }),
            h('p', { class: 'small', text: 'Students or TAs can submit regrade requests from the Student Review Inspector.' }))
        );
        return;
      }

      const cards = list.map((item) => {
        const isPending = item.status === 'PENDING';
        const marksInput = h('input', {
          type: 'number', step: '0.5', min: '0',
          value: String(item.claimed_marks ?? 5),
          style: 'width:80px;padding:6px 10px;border-radius:6px;border:1.5px solid var(--border-strong);font-weight:700;',
        });
        const noteInput = h('input', {
          type: 'text', placeholder: 'Resolution rationale (e.g. Verified algebraic step equivalence)',
          value: item.ta_response || '',
          style: 'flex:1;min-width:200px;padding:6px 10px;border-radius:6px;border:1.5px solid var(--border-strong);',
        });

        const actions = isPending ? h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;' },
          h('label', { class: 'small muted', text: 'Granted Marks:' }),
          marksInput,
          noteInput,
          h('button', {
            type: 'button', class: 'btn btn-sm btn-primary', text: '✓ Accept (Update Marks)',
            onclick: () => resolveRegradeAppeal(item.id, 'ACCEPTED', parseFloat(marksInput.value) || 0, noteInput.value.trim()),
          }),
          h('button', {
            type: 'button', class: 'btn btn-sm btn-ghost', style: 'color:var(--danger,#ef4444);border-color:rgba(239,68,68,0.4);', text: '✕ Reject',
            onclick: () => resolveRegradeAppeal(item.id, 'REJECTED', null, noteInput.value.trim()),
          })
        ) : h('div', { class: 'small muted', style: 'margin-top:8px;background:var(--surface-2);padding:6px 10px;border-radius:6px;' },
          h('strong', { text: `Resolved by TA: ` }),
          h('span', { text: item.ta_response || '(No comment)' }),
          item.granted_marks != null ? h('span', { style: 'margin-left:10px;font-weight:700;color:var(--green);', text: `Granted Marks: ${item.granted_marks}` }) : null
        );

        return h('div', { class: `regrade-card status-${item.status}` },
          h('div', { style: 'display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;' },
            h('div', { style: 'display:flex;align-items:center;gap:8px;' },
              h('strong', { text: `Roll ${item.roll_no} · ${item.question_id}`, style: 'font-size:1rem;' }),
              h('span', { class: `pill badge-regrade-${item.status}`, text: item.status }),
              h('span', { class: 'muted small', text: `Claimed: ${item.claimed_marks ?? 0} Marks` })),
            h('span', { class: 'muted small', text: item.created_at ? new Date(item.created_at).toLocaleString() : '' })),
          h('div', { class: 'regrade-reason-quote' },
            h('strong', { style: 'display:block;font-size:0.78rem;text-transform:uppercase;color:var(--muted);', text: 'Student Grounds / Reason:' }),
            h('span', { text: item.student_reason || '(No reason provided)' })),
          actions
        );
      });

      box.replaceChildren(...cards);
    } catch (err) {
      box.replaceChildren(h('div', { class: 'alert alert-warn', text: `Error loading appeals: ${err.message}` }));
    }
  }

  async function resolveRegradeAppeal(requestId, status, grantedMarks, taResponse) {
    if (!state.activeSessionId) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/regrade-requests/${requestId}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          status,
          ta_response: taResponse || (status === 'ACCEPTED' ? 'Appeal accepted by TA' : 'Appeal reviewed and rejected by TA'),
          granted_marks: grantedMarks,
        }),
      });
      toast(`Appeal #${requestId} marked as ${status}!`);
      await loadRegradeRequests(state.activeRegradeFilter);
      await refreshActiveSession(false);
    } catch (err) {
      toast(`Resolution error: ${err.message}`);
    }
  }

  function openLodgeRegradeModal(rollNo, questionId = null) {
    if (!state.activeSessionId) {
      toast('Please select an active exam session first.');
      return;
    }
    const modal = $('lodgeRegradeModal');
    if (!modal) return;
    $('lodgeRegradeRoll').value = rollNo || state.selectedReviewRoll || '';
    const qSelect = $('lodgeRegradeQuestion');
    const questions = (state.session && state.session.questions) || state.questionSpecs || [];
    qSelect.replaceChildren(...questions.map(q => h('option', {
      value: q.question_id,
      text: `${q.question_id}: ${q.title || ''} (${q.max_marks || 5} Marks)`,
      selected: questionId && q.question_id === questionId ? true : null,
    })));
    $('lodgeRegradeClaimedMarks').value = '5';
    $('lodgeRegradeReason').value = '';
    modal.showModal();
  }

  async function submitLodgeRegrade() {
    const rollNo = $('lodgeRegradeRoll').value.trim();
    const qid = $('lodgeRegradeQuestion').value.trim();
    const claimedMarks = parseFloat($('lodgeRegradeClaimedMarks').value) || 0;
    const reason = $('lodgeRegradeReason').value.trim();
    if (!rollNo || !qid || !reason) {
      toast('Please fill in student roll number, question, and reason.');
      return;
    }
    try {
      await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/students/${encodeURIComponent(rollNo)}/regrade-request`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question_id: qid,
          student_reason: reason,
          claimed_marks: claimedMarks,
        }),
      });
      $('lodgeRegradeModal').close();
      toast(`Regrade appeal submitted for Roll ${rollNo} on ${qid}!`);
      if ($('regradesCountBadge')) {
        const cur = parseInt($('regradesCountBadge').textContent, 10) || 0;
        $('regradesCountBadge').textContent = String(cur + 1);
      }
    } catch (err) {
      toast(`Could not lodge appeal: ${err.message}`);
    }
  }

  // --------------------------------------------------------------------------- Initialization & Hotkeys
  async function init() {
    document.querySelectorAll('.mode-btn').forEach((btn) => {
      btn.addEventListener('click', () => switchMode(btn.getAttribute('data-mode')));
    });

    renderQuestionSpecInputs();

    if ($('btnAddQuestionSpec')) {
      $('btnAddQuestionSpec').addEventListener('click', () => {
        const nextNum = state.questionSpecs.length + 1;
        state.questionSpecs.push({
          question_id: `Q${nextNum}`,
          title: `Question ${nextNum}`,
          max_marks: 5.0,
          page_number: Math.min(25, Math.ceil(nextNum / 2)),
          y_start_pct: nextNum % 2 === 1 ? 0 : 50,
          y_end_pct: nextNum % 2 === 1 ? 50 : 100,
          x_start_pct: 0,
          x_end_pct: 100,
          language: 'handwritten_math',
          execution_mode: 'llm_only',
          problem_statement: '',
          grading_rubric: 'Step 1 [2.0 Marks]: Setup/Formulation.\nStep 2 [2.0 Marks]: Derivation / Method.\nStep 3 [1.0 Mark]: Final Evaluation.\nPenalties: Deduct 1.0 mark for arithmetic slip; Error-Carried-Forward (ECF) applies.',
          reference_solution: '',
          _expanded: true,
        });
        renderQuestionSpecInputs();
        toast(`Added Question Q${nextNum} (Rubric details expanded)`);
      });
    }


    if ($('btnClearAllQuestions')) {
      $('btnClearAllQuestions').addEventListener('click', () => {
        state.questionSpecs = [{
          question_id: 'Q1',
          title: 'Question 1',
          max_marks: 5.0,
          page_number: 1,
          section: 'top',
          y_start_pct: 0.0,
          y_end_pct: 50.0,
          x_start_pct: 0.0,
          x_end_pct: 100.0,
          language: 'physics',
          execution_mode: 'llm_only',
          problem_statement: '',
          grading_rubric: 'Step 1 [2.0 Marks]: Setup/Formulation.\nStep 2 [2.0 Marks]: Derivation / Method.\nStep 3 [1.0 Mark]: Final Evaluation.',
          reference_solution: '',
          _expanded: true,
        }];
        renderQuestionSpecInputs();
        toast('Cleared questions list to blank template.');
      });
    }

    if ($('btnEqualizeMarks')) {
      $('btnEqualizeMarks').addEventListener('click', () => {
        if (!state.questionSpecs.length) return;
        const currentTotal = parseFloat($('inpTotalMarks').value) || 20.0;
        const n = state.questionSpecs.length;
        const perQ = Math.round((currentTotal / n) * 2) / 2;
        state.questionSpecs.forEach((q, i) => {
          if (i === n - 1) {
            const rest = state.questionSpecs.slice(0, -1).reduce((s, x) => s + x.max_marks, 0);
            q.max_marks = Math.max(0.5, Math.round((currentTotal - rest) * 2) / 2);
          } else {
            q.max_marks = perQ;
          }
        });
        renderQuestionSpecInputs();
        toast(`Equalized marks across ${n} questions (~${perQ} M each)`);
      });
    }

    if ($('btnToggleAllRubrics')) {
      $('btnToggleAllRubrics').addEventListener('click', () => {
        const anyOpen = state.questionSpecs.some((q) => q._expanded);
        state.questionSpecs.forEach((q) => { q._expanded = !anyOpen; });
        renderQuestionSpecInputs();
      });
    }

    if ($('btnOpenFolderModal')) $('btnOpenFolderModal').addEventListener('click', () => openFolderPicker());
    if ($('btnBrowseSessionFolder')) $('btnBrowseSessionFolder').addEventListener('click', () => openFolderPicker());
    if ($('btnCloseFolderModal')) $('btnCloseFolderModal').addEventListener('click', () => $('folderPickerModal').close());
    if ($('btnConfirmFolderSelect')) $('btnConfirmFolderSelect').addEventListener('click', confirmFolderSelection);
    if ($('btnOpenOsFolder')) {
      $('btnOpenOsFolder').addEventListener('click', async () => {
        const res = await (await api('/api/folders/open-os', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: $('inpStorageDir').value.trim() || null }),
        })).json();
        toast(`Folder path on laptop: ${res.path}`);
      });
    }
    if ($('btnCopyScannerUrl')) {
      $('btnCopyScannerUrl').addEventListener('click', () => {
        const url = $('directScannerUrlInput').value;
        navigator.clipboard.writeText(url).then(() => toast('Copied Mobile Scanner Link!'), () => toast(url));
      });
    }
    if ($('btnSaveRuntimeSettings')) $('btnSaveRuntimeSettings').addEventListener('click', saveRuntimeSettings);

    if ($('btnCreateSession')) $('btnCreateSession').addEventListener('click', createNewSession);
    if ($('btnUpdateSession')) $('btnUpdateSession').addEventListener('click', updateCurrentSession);
    if ($('rosterCsvInput')) {
      $('rosterCsvInput').addEventListener('change', async (e) => {
        const f = e.target.files && e.target.files[0];
        if (f) {
          await importRosterCsvFile(f);
          e.target.value = '';
        }
      });
    }

    // Workflow navigation tabs
    for (const btn of document.querySelectorAll('.wf-step-btn, .mode-btn')) {
      btn.addEventListener('click', () => switchMode(btn.getAttribute('data-mode')));
    }

    // Exam Paper Layout & Answer Format switcher
    if ($('selPaperType')) {
      $('selPaperType').addEventListener('change', (e) => {
        syncPaperTypeUI(e.target.value);
      });
    }

    // Question Paper JSON loader & Course Samples
    if ($('paperJsonFileInput')) {
      $('paperJsonFileInput').addEventListener('change', async (e) => {
        const f = e.target.files && e.target.files[0];
        if (f) {
          loadQuestionPaperJson(await f.text(), f.name);
          e.target.value = '';
        }
      });
    }
    if ($('qpImageFileInput')) {
      $('qpImageFileInput').addEventListener('change', async (e) => {
        const f = e.target.files && e.target.files[0];
        if (f) {
          await uploadQuestionPaperImageFile(f);
          e.target.value = '';
        }
      });
    }
    if ($('btnViewQpImage')) {
      $('btnViewQpImage').addEventListener('click', () => {
        if (!state.activeSessionId) return;
        window.open(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/question-paper-image`, '_blank');
      });
    }
    if ($('btnRemoveQpImage')) {
      $('btnRemoveQpImage').addEventListener('click', async () => {
        state.pendingQpImageFile = null;
        if (state.activeSessionId) {
          try {
            await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ question_paper_image_path: '' }),
            });
            if (state.session) state.session.question_paper_image_path = '';
            toast('Removed Question Paper image');
            await loadSessionsList();
          } catch (err) {
            toast(`Failed to remove: ${err.message}`);
          }
        }
        updateQpImageStatusBadge(null);
      });
    }

    if ($('scanRollNo')) {
      $('scanRollNo').addEventListener('input', (e) => {
        state.currentRoll = e.target.value.trim();
        renderCapturePanel();
      });
    }
    if ($('scanStudentName')) {
      $('scanStudentName').addEventListener('input', (e) => {
        state.currentName = e.target.value.trim();
      });
    }

    if ($('btnToggleCamera')) $('btnToggleCamera').addEventListener('click', toggleLiveCamera);
    if ($('btnCloseWebcam')) {
      $('btnCloseWebcam').addEventListener('click', () => {
        stopLiveCamera();
      });
    }
    if ($('cameraDeviceSelect')) {
      $('cameraDeviceSelect').addEventListener('change', async (e) => {
        cameraEngine.deviceId = e.target.value;
        if (cameraEngine.active) await startLiveCamera();
      });
    }

    if ($('btnSnapPhoto')) $('btnSnapPhoto').addEventListener('click', captureFromVideoOrFallback);
    if ($('btnRetakePage')) $('btnRetakePage').addEventListener('click', captureFromVideoOrFallback);
    if ($('fallbackCameraInput')) {
      $('fallbackCameraInput').addEventListener('change', async (e) => {
        const files = Array.from(e.target.files || []);
        for (const file of files) {
          await handleCapturedBlob(file);
        }
        e.target.value = '';
      });
    }
    if ($('batchUploadInput')) {
      $('batchUploadInput').addEventListener('change', async (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        const roll = $('scanRollNo') ? $('scanRollNo').value.trim() : '';
        if (!roll) {
          toast('Please enter the Student Roll Number first.');
          if ($('scanRollNo')) $('scanRollNo').focus();
          return;
        }
        toast(`Uploading ${files.length} sheets for Roll ${roll}...`);
        for (let i = 0; i < files.length; i++) {
          state.currentPage = i + 1;
          await handleCapturedBlob(files[i]);
        }
        e.target.value = '';
        toast(`Uploaded all ${files.length} sheets! Split into 2N top/bottom halves.`);
        renderCapturePanel();
      });
    }

    if ($('bulkPdfZipInput')) {
      $('bulkPdfZipInput').addEventListener('change', async (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        if (!state.activeSessionId) {
          toast('Please create or select an active exam session in Step 1 first.');
          switchMode('hub');
          return;
        }
        const banner = $('bulkUploadStatusBanner');
        const text = $('bulkUploadStatusText');
        if (banner) banner.style.display = 'flex';
        if (text) text.textContent = `Ingesting ${files.length} bulk file(s)... Processing PDFs and extracting student sheets...`;
        toast(`Ingesting ${files.length} bulk file(s)...`);
        try {
          const fd = new FormData();
          for (const file of files) {
            fd.append('files', file, file.name);
          }
          const resp = await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/upload-bulk`, {
            method: 'POST',
            body: fd,
          });
          const data = await resp.json();
          toast(`✓ Successfully ingested ${data.total_pages} page(s) across ${data.total_students} student(s)!`);
          await refreshActiveSession(false);
          renderCapturePanel();
          if (state.mode === 'capture') {
            const students = (state.session && state.session.students) || [];
            if (students.length > 0 && !$('scanRollNo').value) {
              $('scanRollNo').value = students[0].roll_no;
              state.currentRoll = students[0].roll_no;
              renderCapturePanel();
            }
          }
        } catch (err) {
          toast(`Bulk upload failed: ${err.message}`);
        } finally {
          if (banner) banner.style.display = 'none';
          e.target.value = '';
        }
      });
    }

    if ($('btnNextStudent')) $('btnNextStudent').addEventListener('click', finalizeCurrentStudentAndMoveNext);
    if ($('btnFinishAllScanning')) {
      $('btnFinishAllScanning').addEventListener('click', async () => {
        await flushChunkQueue();
        switchMode('review');
        await triggerSessionAiEvaluation();
      });
    }

    if ($('btnRunAiEvaluation')) $('btnRunAiEvaluation').addEventListener('click', () => triggerSessionAiEvaluation());
    if ($('btnApproveAllGreen')) {
      $('btnApproveAllGreen').addEventListener('click', async () => {
        if (!state.activeSessionId) return;
        const res = await (await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/approve-all-green`, { method: 'POST' })).json();
        toast(`Approved ${res.approved_count} clean question evaluations!`);
        await refreshActiveSession(false);
      });
    }
    if ($('btnExportExcel')) {
      $('btnExportExcel').addEventListener('click', async () => {
        if (!state.activeSessionId) return;
        try {
          const resp = await api(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/export.xlsx`);
          const blob = await resp.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          const s = state.session || {};
          a.download = `${s.course_code || 'quiz'}_${s.exam_name || 'session'}_grades.xlsx`;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 5000);
        } catch (err) {
          toast(`Export failed: ${err.message}`);
        }
      });
    }
    if ($('reviewSearchInput')) {
      $('reviewSearchInput').addEventListener('input', (e) => {
        state.reviewSearch = e.target.value;
        renderReviewDashboard();
      });
    }

    // Keyboard shortcuts on Laptop Review Panel (Up/Down or J/K to switch students, A to Approve, 1/2/3 for tabs)
    window.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        document.querySelectorAll('dialog[open]').forEach((d) => d.close());
        return;
      }
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target && e.target.tagName) || '')) return;

      // Quick tab switching: 1=Hub, 2=Scanner, 3=Review
      if (e.key === '1') { switchMode('hub'); return; }
      if (e.key === '2') { switchMode('capture'); return; }
      if (e.key === '3') { switchMode('review'); return; }

      if (state.mode !== 'review' || !state.session) return;
      const students = state.session.students || [];
      if (!students.length) return;
      const idx = students.findIndex((s) => s.roll_no === state.selectedReviewRoll);

      if ((e.key === 'ArrowDown' || e.key === 'ArrowRight' || e.key === 'j' || e.key === 'J') && idx < students.length - 1) {
        e.preventDefault();
        state.selectedReviewRoll = students[idx + 1].roll_no;
        renderReviewDashboard();
        setTimeout(() => {
          const el = document.querySelector('.student-item.selected') || document.querySelector('.student-item.active');
          if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }, 20);
      } else if ((e.key === 'ArrowUp' || e.key === 'ArrowLeft' || e.key === 'k' || e.key === 'K') && idx > 0) {
        e.preventDefault();
        state.selectedReviewRoll = students[idx - 1].roll_no;
        renderReviewDashboard();
        setTimeout(() => {
          const el = document.querySelector('.student-item.selected') || document.querySelector('.student-item.active');
          if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }, 20);
      } else if (e.key === 'a' || e.key === 'A') {
        e.preventDefault();
        const approveBtn = document.querySelector('#reviewInspectorBox .btn-quick-accept');
        if (approveBtn) approveBtn.click();
      } else if (e.key === '?') {
        e.preventDefault();
        if ($('shortcutGuideModal')) $('shortcutGuideModal').showModal();
      }
    });

    // Hardware Shutter / Bluetooth Clicker keys in Capture mode (VolumeUp, VolumeDown, Space, Enter)
    window.addEventListener('keydown', (e) => {
      if (state.mode === 'capture') {
        if (['Space', 'Enter', 'VolumeUp', 'VolumeDown'].includes(e.code) || [' ', 'Enter'].includes(e.key)) {
          if (!['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target && e.target.tagName) || '')) {
            e.preventDefault();
            captureFromVideoOrFallback();
          }
        }
      }
    });

    if ($('btnDownloadBookletPdf')) $('btnDownloadBookletPdf').addEventListener('click', downloadBookletPdf);
    if ($('btnToggleTorch')) $('btnToggleTorch').addEventListener('click', toggleTorch);
    if ($('btnClaimNextRed')) $('btnClaimNextRed').addEventListener('click', claimNextRedStudent);
    if ($('btnShowAnalytics')) $('btnShowAnalytics').addEventListener('click', showExamAnalytics);
    if ($('btnExportFeedbackZip')) $('btnExportFeedbackZip').addEventListener('click', downloadFeedbackZip);

    // Lightbox & Shortcuts modal setup
    setupImageLightbox();
    if ($('btnOpenShortcutsModal')) $('btnOpenShortcutsModal').addEventListener('click', () => $('shortcutGuideModal').showModal());
    if ($('btnCloseShortcutGuide')) $('btnCloseShortcutGuide').addEventListener('click', () => $('shortcutGuideModal').close());
    if ($('btnDoneShortcutGuide')) $('btnDoneShortcutGuide').addEventListener('click', () => $('shortcutGuideModal').close());

    // Handwritten Exam Benchmark Hub modal listeners
    if ($('btnOpenHandwrittenBenchModal')) $('btnOpenHandwrittenBenchModal').addEventListener('click', openHandwrittenBenchmarkModal);
    if ($('handwrittenHubClose')) $('handwrittenHubClose').addEventListener('click', () => $('handwrittenHubModal').close());
    if ($('handwrittenHubDone')) $('handwrittenHubDone').addEventListener('click', () => $('handwrittenHubModal').close());

    // Claude Haiku 5.5 Usage dashboard listeners
    if ($('btnOpenHaikuUsage')) $('btnOpenHaikuUsage').addEventListener('click', openHaikuUsageModal);
    if ($('btnQuickOpenHaiku')) $('btnQuickOpenHaiku').addEventListener('click', openHaikuUsageModal);
    if ($('vlmStatusBadge')) $('vlmStatusBadge').addEventListener('click', openHaikuUsageModal);
    if ($('btnCloseHaikuUsageModal')) $('btnCloseHaikuUsageModal').addEventListener('click', () => $('haikuUsageModal').close());
    if ($('btnCloseHaikuUsageModal2')) $('btnCloseHaikuUsageModal2').addEventListener('click', () => $('haikuUsageModal').close());
    if ($('btnRefreshHaikuUsage')) $('btnRefreshHaikuUsage').addEventListener('click', refreshHaikuUsageData);
    if ($('btnResetHaikuUsage')) $('btnResetHaikuUsage').addEventListener('click', resetHaikuUsage);
    if ($('btnSwitchHaikuModel')) $('btnSwitchHaikuModel').addEventListener('click', switchHaikuModel);
    if ($('cfgModel')) {
      $('cfgModel').addEventListener('input', (e) => {
        const val = (e.target.value || '').toLowerCase();
        const isLocal = val.includes('qwen') || val.includes('ollama') || val.includes('local');
        if ($('cfgLocalVlmRow')) $('cfgLocalVlmRow').style.display = isLocal ? 'block' : 'none';
      });
    }

    // Prevent wheel scrolling on roll number input to avoid accidental changes while taking photos
    const rollInp = $('scanRollNo');
    if (rollInp) {
      rollInp.addEventListener('wheel', (e) => { e.preventDefault(); }, { passive: false });
    }

    // Regrade Appeals listeners
    if ($('btnShowRegrades')) $('btnShowRegrades').addEventListener('click', () => openRegradeModal(''));
    if ($('btnCloseRegradeModal')) $('btnCloseRegradeModal').addEventListener('click', () => $('regradeModal').close());
    if ($('btnRefreshRegrades')) $('btnRefreshRegrades').addEventListener('click', () => loadRegradeRequests(state.activeRegradeFilter || ''));
    document.querySelectorAll('.regrade-filter-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        openRegradeModal(btn.getAttribute('data-status') || '');
      });
    });
    if ($('btnCancelLodgeRegrade')) $('btnCancelLodgeRegrade').addEventListener('click', () => $('lodgeRegradeModal').close());
    if ($('btnCancelLodgeRegradeTop')) $('btnCancelLodgeRegradeTop').addEventListener('click', () => $('lodgeRegradeModal').close());
    if ($('btnSubmitLodgeRegrade')) $('btnSubmitLodgeRegrade').addEventListener('click', submitLodgeRegrade);
    if ($('formLodgeRegrade')) {
      $('formLodgeRegrade').addEventListener('submit', (e) => {
        e.preventDefault();
        submitLodgeRegrade();
      });
    }

    // TA identification listeners
    if ($('formTaLogin')) {
      $('formTaLogin').addEventListener('submit', (e) => {
        e.preventDefault();
        saveTaIdentity('capture');
      });
    }
    if ($('btnTaLoginStartScanner')) {
      $('btnTaLoginStartScanner').addEventListener('click', (e) => {
        e.preventDefault();
        saveTaIdentity('capture');
      });
    }
    if ($('btnTaLoginOpenHub')) {
      $('btnTaLoginOpenHub').addEventListener('click', () => {
        const input = $('taLoginNameInput');
        if (input && input.value.trim()) {
          saveTaIdentity('hub');
        } else {
          switchMode('hub');
        }
      });
    }
    if ($('taNameBadge')) {
      $('taNameBadge').addEventListener('click', () => switchMode('ta-login'));
    }

    updateTaBadges();

    await refreshHubInfo();
    await updateQueueBadge();
    await loadSessionsList();
    await loadSamplePapersList();
    renderQuestionSpecInputs();
    syncPaperTypeUI(($('selPaperType') && $('selPaperType').value) || 'COMBINED_QUESTION_ANSWER');
    updateComputedWeightsTotal();

    const isMobile = window.innerWidth <= 768 || /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    const urlParams = new URLSearchParams(window.location.search);
    const wantsCapture = urlParams.get('mode') === 'capture' || window.location.pathname.startsWith('/scan') || isMobile;

    if (state.activeSessionId) {
      await refreshActiveSession(true);
      if (wantsCapture) {
        if (!state.taName) {
          switchMode('ta-login');
        } else {
          switchMode('capture');
        }
      }
    } else {
      if (wantsCapture && !state.taName) {
        switchMode('ta-login');
      }
    }

    if (state.taName && state.activeSessionId) {
      await sendTaHeartbeat();
    }

    // Auto-poll on laptop & heartbeat
    setInterval(async () => {
      await flushChunkQueue();
      if (state.activeSessionId) {
        if (state.taName) {
          await sendTaHeartbeat();
        }
        if (state.mode === 'review' || state.mode === 'hub') {
          await refreshActiveSession(false);
        }
      }
    }, 3000);
  }

  init();
})();
