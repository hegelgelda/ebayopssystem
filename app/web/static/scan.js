// PSAラベルのバーコード／QR、SKUラベルのQRを読み取る。
// ブラウザ標準の BarcodeDetector（Android の Chrome など）があればそれを使い、無ければ（iPhone の Safari など）
// zxing-cpp の WebAssembly 版（barcode-detector ponyfill、/static/vendor に同梱）を同じ API で使う。
// 細い1次元バーコードは解像度が低いと読めないので、高解像度・連続オートフォーカスでカメラを開く。
(function () {
  let stream = null, timer = null, hintTimer = null;

  const VIDEO = { facingMode: 'environment', width: { ideal: 1920 }, height: { ideal: 1080 } };
  const FORMATS = ['code_128', 'code_39', 'qr_code', 'itf', 'ean_13', 'data_matrix'];
  const PONYFILL = '/static/vendor/barcode-detector-3.2.3-ponyfill.js';
  const WASM = '/static/vendor/zxing_reader-3.1.6.wasm';

  function loadScript(src) {
    return new Promise((ok, ng) => {
      const s = document.createElement('script');
      s.src = src; s.onload = ok; s.onerror = () => ng(new Error('読み込みに失敗しました: ' + src));
      document.head.appendChild(s);
    });
  }

  let detectorClass = null;
  async function getDetectorClass() {
    if (detectorClass) return detectorClass;
    if ('BarcodeDetector' in window) {
      try {
        const supported = await window.BarcodeDetector.getSupportedFormats();
        if (FORMATS.some(f => supported.includes(f))) return (detectorClass = window.BarcodeDetector);
      } catch (e) { /* 標準のものが使えなければ下へ */ }
    }
    if (!window.BarcodeDetectionAPI) await loadScript(PONYFILL);
    const api = window.BarcodeDetectionAPI;
    // .wasm は CDN ではなく自分のサーバーから配信する
    api.prepareZXingModule({
      overrides: { locateFile: (path, prefix) => (path.endsWith('.wasm') ? WASM : prefix + path) },
      fireImmediately: true,
    });
    return (detectorClass = api.BarcodeDetector);
  }

  // 読み取り枠（狙う位置の目安）とコツの表示
  const GUIDE = { w: 0.8, h: 0.45 };
  function wrapOf(video) {
    let wrap = video.parentElement;
    if (!(wrap && wrap.classList.contains('scan-wrap'))) {
      wrap = document.createElement('div');
      wrap.className = 'scan-wrap';
      wrap.style.cssText = 'position:relative;overflow:hidden;border-radius:10px';
      video.parentNode.insertBefore(wrap, video);
      wrap.appendChild(video);
    }
    return wrap;
  }
  function showGuide(video, on) {
    const wrap = video.parentElement && video.parentElement.classList.contains('scan-wrap') ? video.parentElement
      : (on ? wrapOf(video) : null);
    if (!wrap) return;
    let g = wrap.querySelector('.scan-guide');
    if (!g) {
      g = document.createElement('div');
      g.className = 'scan-guide';
      g.style.cssText = `position:absolute;left:${(1 - GUIDE.w) * 50}%;right:${(1 - GUIDE.w) * 50}%;` +
        `top:${(1 - GUIDE.h) * 50}%;bottom:${(1 - GUIDE.h) * 50}%;border:3px solid rgba(80,200,120,.9);` +
        'border-radius:8px;box-shadow:0 0 0 9999px rgba(0,0,0,.25);pointer-events:none';
      wrap.appendChild(g);
    }
    g.hidden = !on;
  }
  function showHint(video, text) {
    const anchor = video.parentElement && video.parentElement.classList.contains('scan-wrap') ? video.parentElement : video;
    let el = anchor.nextElementSibling;
    if (!el || !el.classList || !el.classList.contains('scan-hint')) {
      if (!text) return;
      el = document.createElement('div');
      el.className = 'scan-hint small muted';
      el.style.margin = '6px 0';
      anchor.insertAdjacentElement('afterend', el);
    }
    el.hidden = !text;
    el.textContent = text || '';
  }

  async function focusContinuously(s) {
    const track = s && s.getVideoTracks()[0];
    if (!track || !track.applyConstraints) return;
    try { await track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] }); } catch (e) { /* 非対応の端末 */ }
  }

  async function start(video, onCode) {
    stop(video);
    const Detector = await getDetectorClass();
    const det = new Detector({ formats: FORMATS });
    stream = await navigator.mediaDevices.getUserMedia({ video: VIDEO, audio: false });
    await focusContinuously(stream);
    video.srcObject = stream;
    await video.play();
    showGuide(video, true);
    showHint(video, 'バーコードを横向きにして、緑の枠の中に大きく映してください（10〜15cmくらい）。明るい場所で、反射が入らない角度に。');
    hintTimer = setTimeout(() => showHint(video,
      '読み取れない場合：ラベル裏の QR コードを試すか、バーコードの下に印字された番号を手入力してください。'), 10000);
    let busy = false;
    timer = setInterval(async () => {
      if (busy || !video.videoWidth) return;
      busy = true;
      try {
        const codes = await det.detect(video);
        if (codes.length && timer) { const v = codes[0].rawValue; stop(video); onCode(v); }
      } catch (e) { /* フレーム未準備など */ } finally { busy = false; }
    }, 200);
  }

  function stop(video) {
    if (timer) { clearInterval(timer); timer = null; }
    if (hintTimer) { clearTimeout(hintTimer); hintTimer = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    if (video) { video.srcObject = null; showGuide(video, false); showHint(video, ''); }
  }

  window.CertScanner = { start, stop };
})();
