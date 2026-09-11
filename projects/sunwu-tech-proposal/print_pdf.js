// 通过 Chrome DevTools Protocol 把 final.html 打印为带页码页脚的 A4 PDF。
// 用法: node print_pdf.js <html绝对路径> <pdf输出绝对路径>
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = 9333;

const htmlPath = process.argv[2];
const pdfPath = process.argv[3];
if (!htmlPath || !pdfPath) {
  console.error('usage: node print_pdf.js <html> <pdf>');
  process.exit(1);
}

const fileUrl = 'file:///' + path.resolve(htmlPath).replace(/\\/g, '/');
const userDataDir = path.join(require('os').tmpdir(), 'sunwu-pdf-profile-' + Date.now());

const FOOTER = `
<div style="width:100%;font-size:7.5pt;color:#8a94a6;
  font-family:'Microsoft YaHei','Noto Sans CJK SC',sans-serif;
  padding:0 20mm;display:flex;justify-content:space-between;align-items:center;">
  <span>孙武侦查官 · 确定性侦查推演内核 —— 技术方案书</span>
  <span><span class="pageNumber"></span> / <span class="totalPages"></span></span>
</div>`;

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function waitForDevTools(timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) return true;
    } catch (_) { /* not up yet */ }
    await sleep(400);
  }
  throw new Error('Chrome DevTools 端口未就绪');
}

// 取一个 page 级 target（version 端点是 browser 级，不支持 Page.* 域）
async function getPageTarget() {
  for (let i = 0; i < 25; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const page = list.find(t => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch (_) { /* retry */ }
    await sleep(400);
  }
  throw new Error('未找到 page 级 target');
}

function cdp(ws) {
  let id = 0;
  const pending = new Map();
  const listeners = [];

  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    } else if (msg.method) {
      listeners.forEach(fn => fn(msg));
    }
  });

  return {
    send(method, params = {}) {
      const msgId = ++id;
      ws.send(JSON.stringify({ id: msgId, method, params }));
      return new Promise((resolve, reject) => {
        pending.set(msgId, { resolve, reject });
        setTimeout(() => {
          if (pending.has(msgId)) {
            pending.delete(msgId);
            reject(new Error('CDP timeout: ' + method));
          }
        }, 120000);
      });
    },
    on(fn) { listeners.push(fn); }
  };
}

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${userDataDir}`,
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--allow-file-access-from-files',
    'about:blank',
  ], { stdio: 'ignore', detached: false });

  let ok = false;
  try {
    await waitForDevTools();
    const target = await getPageTarget();
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', rej, { once: true });
    });

    const c = cdp(ws);
    const loaded = new Promise((resolve) => {
      c.on(m => { if (m.method === 'Page.loadEventFired') resolve(); });
    });

    await c.send('Page.enable');
    await c.send('Page.navigate', { url: fileUrl });
    await Promise.race([loaded, sleep(15000)]);
    await sleep(2500); // 让字体与布局稳定

    const res = await c.send('Page.printToPDF', {
      landscape: false,
      displayHeaderFooter: true,
      printBackground: true,
      preferCSSPageSize: true,
      marginTop: 0.866,     // 22mm
      marginBottom: 0.709,  // 18mm
      marginLeft: 0.787,    // 20mm
      marginRight: 0.787,
      headerTemplate: '<div style="display:none"></div>',
      footerTemplate: FOOTER,
      transferMode: 'ReturnAsBase64',
      ...(process.argv[4] ? { pageRanges: process.argv[4] } : {}),
    });

    fs.writeFileSync(pdfPath, Buffer.from(res.data, 'base64'));
    const kb = (fs.statSync(pdfPath).size / 1024).toFixed(1);
    console.log('PDF OK ->', pdfPath, `(${kb} KB)`);
    ok = true;
    ws.close();
  } catch (e) {
    console.error('FAILED:', e.message);
  } finally {
    try { chrome.kill(); } catch (_) {}
    try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch (_) {}
    process.exit(ok ? 0 : 1);
  }
})();
