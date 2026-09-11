// 用打印媒体模拟渲染 HTML 并截图，用于版式/字体校验。
// 用法: node shot.js <html> <png输出目录> [页码起始滚动像素...]
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9334;
const htmlPath = process.argv[2];
const outDir = process.argv[3];
const W = 794, H = 1123; // A4 @96dpi

const fileUrl = 'file:///' + path.resolve(htmlPath).replace(/\\/g, '/');
const userDataDir = path.join(require('os').tmpdir(), 'sunwu-shot-' + Date.now());
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitUp() {
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch(`http://127.0.0.1:${PORT}/json/version`)).ok) return true; }
    catch (_) {}
    await sleep(400);
  }
  throw new Error('devtools not up');
}
async function pageTarget() {
  for (let i = 0; i < 30; i++) {
    try {
      const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const p = l.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
      if (p) return p;
    } catch (_) {}
    await sleep(400);
  }
  throw new Error('no page target');
}
function cdp(ws) {
  let id = 0; const pending = new Map();
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
    }
  });
  return {
    send(method, params = {}) {
      const i = ++id;
      ws.send(JSON.stringify({ id: i, method, params }));
      return new Promise((res, rej) => { pending.set(i, { resolve: res, reject: rej }); });
    }
  };
}

(async () => {
  const chrome = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${userDataDir}`, '--disable-gpu', '--no-first-run',
    '--allow-file-access-from-files', 'about:blank'], { stdio: 'ignore' });
  try {
    await waitUp();
    const t = await pageTarget();
    const ws = new WebSocket(t.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', rej, { once: true });
    });
    const c = cdp(ws);
    await c.send('Page.enable');
    await c.send('Emulation.setEmulatedMedia', { media: 'print' });
    await c.send('Emulation.setDeviceMetricsOverride', {
      width: W, height: H, deviceScaleFactor: 1, mobile: false,
    });
    await c.send('Page.navigate', { url: fileUrl });
    await sleep(3500);
    fs.mkdirSync(outDir, { recursive: true });

    const shots = [0, 1, 2, 3];
    for (const k of shots) {
      await c.send('Runtime.evaluate',
        { expression: `window.scrollTo(0, ${k * H});` });
      await sleep(500);
      const r = await c.send('Page.captureScreenshot', { format: 'png' });
      const f = path.join(outDir, `shot-${k + 1}.png`);
      fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
      console.log('shot ->', f);
    }
    ws.close();
  } catch (e) {
    console.error('FAILED:', e.message);
  } finally {
    try { chrome.kill(); } catch (_) {}
    try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch (_) {}
    process.exit(0);
  }
})();
