// PSAラベルのバーコード／QRを読み取る。BarcodeDetector（対応ブラウザ）→ ZXing（非対応ブラウザ）の順。
(function () {
  let stream = null, timer = null, zxingReader = null;

  async function loadZxing() {
    if (window.ZXing) return window.ZXing;
    await new Promise((ok, ng) => {
      const s = document.createElement('script');
      s.src = '/static/vendor/zxing-library-0.21.3.min.js';
      s.onload = ok; s.onerror = ng; document.head.appendChild(s);
    });
    return window.ZXing;
  }

  async function start(video, onCode) {
    stop(video);
    if ('BarcodeDetector' in window) {
      try {
        const formats = await window.BarcodeDetector.getSupportedFormats();
        const want = ['code_128', 'code_39', 'qr_code', 'ean_13', 'itf', 'data_matrix'].filter(f => formats.includes(f));
        const det = new window.BarcodeDetector({ formats: want });
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
        video.srcObject = stream; await video.play();
        timer = setInterval(async () => {
          try {
            const codes = await det.detect(video);
            if (codes.length) { const v = codes[0].rawValue; stop(video); onCode(v); }
          } catch (e) { /* フレーム未準備 */ }
        }, 250);
        return;
      } catch (e) { console.warn('BarcodeDetector failed, fallback to ZXing', e); stop(video); }
    }
    const ZX = await loadZxing();
    zxingReader = new ZX.BrowserMultiFormatReader();
    zxingReader.decodeFromConstraints({ video: { facingMode: 'environment' } }, video, (res, err) => {
      if (res) { const v = res.getText(); stop(video); onCode(v); }
    });
  }

  function stop(video) {
    if (timer) { clearInterval(timer); timer = null; }
    if (zxingReader) { try { zxingReader.reset(); } catch (e) {} zxingReader = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
    if (video) video.srcObject = null;
  }

  window.CertScanner = { start, stop };
})();
