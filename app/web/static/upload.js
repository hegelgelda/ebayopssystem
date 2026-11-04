// 写真アップロード。未送信はIndexedDBに保持し、回線復帰時に再送する（F6-14）。
// サーバーは client_upload_id で重複を無視するので、何度再送しても1枚として扱われる。
(function () {
  const DB = 'ops-uploads', STORE = 'pending';
  function open() {
    return new Promise((ok, ng) => {
      const r = indexedDB.open(DB, 1);
      r.onupgradeneeded = () => r.result.createObjectStore(STORE, { keyPath: 'id' });
      r.onsuccess = () => ok(r.result); r.onerror = () => ng(r.error);
    });
  }
  async function tx(mode, fn) {
    const db = await open();
    return new Promise((ok, ng) => {
      const t = db.transaction(STORE, mode); const s = t.objectStore(STORE);
      const res = fn(s); t.oncomplete = () => ok(res && res.result); t.onerror = () => ng(t.error);
    });
  }
  const put = (rec) => tx('readwrite', s => s.put(rec));
  const del = (id) => tx('readwrite', s => s.delete(id));
  const all = () => tx('readonly', s => s.getAll());

  const listeners = [];
  function emit(ev) { listeners.forEach(f => { try { f(ev); } catch (e) {} }); }

  async function send(rec) {
    const fd = new FormData();
    fd.append('kind', rec.kind);
    fd.append('client_upload_id', rec.id);
    if (rec.replace_photo_id) fd.append('replace_photo_id', rec.replace_photo_id);
    fd.append('file', rec.blob, rec.name || 'photo.jpg');
    const r = await fetch(`/api/items/${rec.item_id}/photos`, { method: 'POST', body: fd, credentials: 'same-origin' });
    if (r.status >= 400 && r.status < 500) {
      const js = await r.json().catch(() => ({}));
      await del(rec.id);  // 入力エラーは再送しても直らない
      emit({ type: 'error', rec, message: js.error || ('HTTP ' + r.status) });
      return;
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const js = await r.json();
    await del(rec.id);
    emit({ type: 'done', rec, result: js });
  }

  let flushing = false;
  async function flush() {
    if (flushing) return; flushing = true;
    try {
      const recs = (await all()) || [];
      for (const rec of recs) {
        try { await send(rec); } catch (e) { emit({ type: 'retry', rec, message: String(e) }); break; }
      }
      emit({ type: 'queue', count: ((await all()) || []).length });
    } finally { flushing = false; }
  }

  async function enqueue(itemId, kind, file, replacePhotoId) {
    const id = (crypto.randomUUID ? crypto.randomUUID() : (Date.now() + '-' + Math.random())).toString();
    const rec = { id, item_id: itemId, kind, blob: file, name: file.name, replace_photo_id: replacePhotoId || '', created: Date.now() };
    try { await put(rec); } catch (e) { /* IndexedDB 不可の環境は直接送る */ return send(rec); }
    emit({ type: 'queued', rec });
    flush();
  }

  window.addEventListener('online', flush);
  setInterval(flush, 15000);
  window.PhotoUploader = { enqueue, flush, on: (f) => listeners.push(f), pending: all };
  flush();
})();
