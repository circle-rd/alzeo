// Does the cockpit actually TELL you a newer version exists? The check behind it is
// server-side and unit-tested; what a browser has to prove is that the banner appears when
// the box is behind, names the command, disappears when it is not, that a cockpit running
// older code than its files says so with the file and the remedy, and that the Setup line
// reads correctly behind, offline and with the check switched off.
//
//   node dashboard/tests/update-banner-check.mjs
//
// It OWNS its cockpit, like resilience-check.mjs: a dry-run instance on loopback with a key
// of its own, its engine, proxy and image lane on a closed port, its own update check off,
// and a PATH whose box commands answer nothing. Each state is put into the page and read
// back in one evaluation, so the page's own stream cannot draw over it in between. It used
// to drive a live cockpit by functions the redesign of 2026-09-30 renamed (banners(),
// rUpdate(), #upd), so 7 of its 9 checks failed on every cockpit since, and it ran nowhere
// to say so (found 2026-10-02).
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, existsSync, rmSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { portOf } from './localports.mjs';
const HERE = dirname(fileURLToPath(import.meta.url));
const COCKPIT = join(HERE, '..', 'cockpit.py');
const CHROME = process.env.CHROME || ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  '/snap/bin/chromium'].find(existsSync);
const checks = [];
const ok = (name, cond, detail = '') => { checks.push({ name, ok: !!cond, detail: String(detail).slice(0, 180) }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const root = mkdtempSync(join(tmpdir(), 'cockpit-update-banner-'));
let server = null, chrome = null;
// process.exit() skips every finally: an early exit (the cockpit never healthy, the login
// refused, no browser) left the cockpit this spawned running on a port nobody owned. The
// exit signals what is still alive, the browser with SIGKILL so it writes no profile back.
process.on('exit', () => {
  try { if (chrome && chrome.exitCode === null && chrome.signalCode === null) chrome.kill('SIGKILL'); } catch { /* gone */ }
  try { if (server && server.exitCode === null && server.signalCode === null) process.kill(-server.pid, 'SIGTERM'); } catch { /* gone */ }
  rmSync(root, { recursive: true, force: true });
});
process.on('SIGINT', () => process.exit(130));
// and SIGTERM, which a CI stopped through its process group sends: it ended node without
// 'exit', and the cockpit, in a session of its own, kept its port (found 2026-10-07)
process.on('SIGTERM', () => process.exit(143));
const cfgDir = join(root, 'config');
const fence = join(root, 'bin');
mkdirSync(cfgDir); mkdirSync(fence);
const key = randomBytes(24).toString('hex');
writeFileSync(join(cfgDir, 'api-key'), key + '\n', { mode: 0o600 });
for (const cmd of ['docker', 'systemctl', 'journalctl', 'nvidia-smi', 'sudo', 'git', 'curl', 'tailscale', 'opencode']) {
  writeFileSync(join(fence, cmd), '#!/bin/sh\nexit 1\n'); chmodSync(join(fence, cmd), 0o755);
}
const CLOSED = 'http://127.0.0.1:1';
// port 0, the kernel's own free port, read back once the cockpit listens: a number read from a socket that is
// then closed is anyone's until the cockpit binds it (localports.mjs)
server = spawn('python3', [COCKPIT], {
  env: { ...process.env, COCKPIT_DRY_RUN: '1', COCKPIT_PORT: '0', COCKPIT_CONFIG_DIR: cfgDir,
         COCKPIT_BIND: '127.0.0.1', COCKPIT_AGENT_PORT: '0', COCKPIT_AUTOHEAL: '0', COCKPIT_AUTOFIT: '0',
         COCKPIT_UPDATE_CHECK: '0', COCKPIT_ENGINE: CLOSED, COCKPIT_PROXY: CLOSED, COCKPIT_IMAGE: CLOSED, COCKPIT_VIDEO: CLOSED,
         HOME: root, PATH: `${fence}:${process.env.PATH}` },
  stdio: ['ignore', 'ignore', 'ignore'], detached: true,
});
let PORT;
try { PORT = await portOf(server); } catch (e) { console.error(`the cockpit did not come up: ${e.message}`); process.exit(2); }
const BASE = `http://127.0.0.1:${PORT}`;
// the same cockpit by a name that is not this machine's, as a box's LAN or Tailscale address is:
// plain HTTP there is no secure context, and the browser has no clipboard API
const FAR_NAME = 'cockpit.test', FAR = `http://${FAR_NAME}:${PORT}`;
try {
  const t0 = Date.now();
  for (;;) {
    try { const r = await fetch(`${BASE}/api/health`, { signal: AbortSignal.timeout(1500) }); if (r.ok) break; } catch { /* booting */ }
    if (Date.now() - t0 > 25000) { console.error('the cockpit did not come up'); process.exit(2); }
    await sleep(400);
  }
  const login = await fetch(`${BASE}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                  body: JSON.stringify({ key }) });
  const cookie = (login.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
  if (!cookie) { console.error(`login failed: HTTP ${login.status}`); process.exit(2); }
  if (!CHROME) { console.error('no browser: set CHROME to a Chromium or Chrome binary'); process.exit(2); }
  chrome = spawn(CHROME, ['--headless=new', '--remote-debugging-pipe', '--no-first-run', '--disable-gpu',
    '--no-sandbox', `--user-data-dir=${join(root, 'chrome')}`, '--hide-scrollbars', '--window-size=1400,900',
    '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions',
    '--disable-default-apps', '--no-default-browser-check', '--disable-domain-reliability', '--no-pings',
    '--disable-client-side-phishing-detection', '--disable-features=OptimizationHints,Translate,MediaRouter',
    `--host-resolver-rules=MAP ${FAR_NAME} 127.0.0.1, MAP * ~NOTFOUND , EXCLUDE 127.0.0.1`, 'about:blank'], { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] });
  let id = 0; const pending = new Map(); let events = [];
  let inbox = '';
  chrome.stdio[4].on('data', d => {
    inbox += d.toString();
    let cut;
    while ((cut = inbox.indexOf('\0')) >= 0) {
      const m = JSON.parse(inbox.slice(0, cut)); inbox = inbox.slice(cut + 1);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } else if (m.method) events.push(m);
    }
  });
  const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pending.set(i, r); chrome.stdio[3].write(JSON.stringify({ id: i, method, params, sessionId }) + '\0'); });
  const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
  const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
  await send('Runtime.enable', {}, sessionId); await send('Page.enable', {}, sessionId); await send('Network.enable', {}, sessionId);
  await send('Network.setCookie', { name: 'cockpit', value: cookie, url: BASE, httpOnly: true, sameSite: 'Strict' }, sessionId);
  const evalJs = async expression => {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
    if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || 'eval threw');
    return r.result.result.value;
  };
  const waitFor = async (expression, ms = 20000) => {
    const t0 = Date.now();
    for (;;) {
      try { if (await evalJs(expression)) return true; } catch { /* navigating */ }
      if (Date.now() - t0 > ms) return false;
      await sleep(300);
    }
  };
  const jsErrors = () => events.filter(m => m.method === 'Runtime.exceptionThrown').map(m => m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text || 'exception');
  await send('Page.navigate', { url: BASE + '/' }, sessionId);
  ok('the page is live', await waitFor("document.getElementById('conn-lbl').textContent === 'live'"),
     await evalJs("document.getElementById('conn-lbl').textContent"));
  // one evaluation per state: set it, draw, read
  const banners = upd => evalJs(`(() => { F.update = ${JSON.stringify(upd)}; renderBanners(lastState || {}, lastErrors || {});
    return [...document.querySelectorAll('#banners .banner')].map(e => e.textContent).join(' | '); })()`);
  const setupLine = upd => evalJs(`(() => { (HANDLERS.update || []).forEach(f => f(${JSON.stringify(upd)}));
    return document.getElementById('rp-upd').textContent; })()`);

  let s = await banners({ installed: 'v1.15.1', latest: 'v1.15.1', behind: false });
  ok('up to date: no update banner', !/is out; this box runs/.test(s), s);
  s = await banners({ installed: 'v1.15.0', latest: 'v1.99.0', behind: true });
  ok('behind: the banner names both versions', /Version v1\.99\.0 is out; this box runs v1\.15\.0/.test(s), s);
  // the update is the one command, run from this checkout: install.sh alone fetches nothing,
  // and a git pull skips get.sh's guards (found in review, 2026-10-14)
  const ONE = 'curl -fsSL https://raw.githubusercontent.com/hasso5703/alzeo/main/get.sh | bash';
  // the checks below put the state in and draw it in the very evaluation that reads it, b the update
  // banner, and the page's own stream leaves F.update alone until that evaluation ends: it draws the
  // real state, a message a second, and took the banner and its button away in between (found in
  // review, 2026-10-21)
  const BEHIND = { installed: 'v1.15.0', latest: 'v1.99.0', behind: true };
  const onBehind = body => evalJs(`(async () => { const keep = HANDLERS.update; HANDLERS.update = [];
    try { F.update = ${JSON.stringify(BEHIND)}; renderBanners(lastState || {}, lastErrors || {});
      const b = [...document.querySelectorAll('#banners .banner')].find(e => /is out; this box runs/.test(e.textContent));
      ${body} } finally { HANDLERS.update = keep; } })()`);
  // the command apart from the sentence, with a Copy button (2026-10-21)
  const block = await onBehind(`const c = b && b.querySelector('.bcmd .cmd'), btn = b && b.querySelector('.bcmd button');
    return JSON.stringify({ cmd: c ? c.textContent : null, btn: btn ? btn.textContent : null });`);
  const blk = JSON.parse(block);
  ok('and gives the command, apart, with a Copy button', /^cd \S+ && /.test(blk.cmd || '') && (blk.cmd || '').endsWith(` && ${ONE}`)
     && blk.btn === 'Copy' && !/git pull/.test(s), block);
  // every Copy button of the page, the banner's included, is drawn as a button: a ghost one read as
  // plain text at rest (Hasan, 2026-10-21: "visible partout")
  const looks = await onBehind(`return JSON.stringify([...document.querySelectorAll('button')].filter(x => x.textContent.trim() === 'Copy')
    .map(x => ({ id: x.id || 'banner', ghost: x.classList.contains('ghost'), border: getComputedStyle(x).borderTopColor })));`);
  const drawn = JSON.parse(looks);
  ok('every Copy button is drawn as a button, its border seen at rest', drawn.length >= 7 && drawn.some(c => c.id === 'banner')
     && drawn.every(c => !c.ghost && !/^rgba\(.*,\s*0\)$/.test(c.border) && c.border !== 'transparent'), looks);
  // what the button copies, by the clipboard API where the page is a secure context and by a
  // selected textarea where it is not (plain HTTP on the box's LAN or Tailscale address)
  const copied = viaApi => onBehind(`
    const btn = b && b.querySelector('.bcmd button');
    if (!btn) return 'no Copy button';
    let got = null;
    const api = navigator.clipboard, exec = document.execCommand.bind(document);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, get: () => ${viaApi} ? { writeText: async t => { got = 'api:' + t; } } : undefined });
    document.execCommand = c => { if (c === 'copy') { const ta = document.querySelector('textarea[data-copy]'); got = 'exec:' + (ta ? ta.value.slice(ta.selectionStart, ta.selectionEnd) : ''); return true; } return exec(c); };
    btn.focus(); btn.click();
    const focus = document.activeElement === btn;
    await new Promise(r => setTimeout(r, 50));
    Object.defineProperty(navigator, 'clipboard', { configurable: true, get: () => api }); document.execCommand = exec;
    return JSON.stringify({ got, focus });`);
  const copiedJson = async viaApi => { const r = await copied(viaApi); try { return JSON.parse(r); } catch { return { got: r }; } };
  let got = await copiedJson(true);
  ok('Copy puts the exact command on the clipboard', got.got === 'api:' + blk.cmd, got.got);
  got = await copiedJson(false);
  ok('and without the clipboard API, by a selected textarea', got.got === 'exec:' + blk.cmd, got.got);
  ok('and gives the focus back to the button, for a keyboard', got.focus === true, JSON.stringify(got));
  ok('which it does not leave in the page', await evalJs("document.querySelectorAll('textarea[data-copy]').length") === 0);
  const cmd = await evalJs("document.getElementById('st-update').textContent");
  ok('Settings gives the same command', /^cd \S+ && /.test(cmd) && cmd.endsWith(` && ${ONE}`), cmd);
  // an install that runs (install.sh keeps a file while it lasts): the banner says so, and the ones
  // that ask for an update, a run of the installer, the defaults it moves or a restart of this
  // cockpit wait for its end (2026-10-21)
  const running = { started: Math.round(Date.now() / 1000) - 750, to: 'v1.99.0' };
  const moves = { release: 'v1.99.0', items: [{ group: 'moves', knob: 'ENGINE_BIND', value: '0.0.0.0', recommended: '127.0.0.1',
                                                why: 'box-before', old: 'v1.17.0' }] };
  s = await banners({ installed: 'v1.15.0', latest: 'v1.99.0', behind: true, behind_clone: true, behind_clone_why: 'commit',
                      clone: 'v1.99.0', stale_code: ['static/js/base.js'], conformity: moves, install_running: running });
  ok('an install running: the banner says so, and since when', /Installing v1\.99\.0\./.test(s) && /It started 1[23] min ago/.test(s), s);
  ok('and asks for nothing it does', !/is out; this box runs/.test(s) && !/clone moved on/.test(s) && !/older code/.test(s)
     && !/Run the installer/.test(s) && !/recommends/.test(s), s);
  s = await banners({ installed: 'v1.15.1', conformity: moves });
  ok('which the defaults banner says once it ends', /recommends/.test(s), s);
  s = await banners({ installed: 'v1.15.1', latest: 'v1.15.1', behind: false, install_running: { started: running.started, to: null } });
  ok('a run of a clone that names no release is the installer running', /The installer is running\./.test(s) && !/Installing/.test(s), s);
  s = await banners({ installed: 'v1.15.0', latest: 'v1.99.0', behind: true });
  ok('once it ends, the update banner is back', /is out; this box runs/.test(s) && !/is running/.test(s), s);
  s = await banners({ installed: 'v1.15.1', stale_code: ['static/js/base.js'] });
  ok('older code than its files: the banner says so', /runs older code than the files on disk/.test(s), s);
  ok('and names the file and the remedy', /static\/js\/base\.js/.test(s) && /systemctl restart qwen38-dashboard/.test(s), s);
  s = await banners({ installed: 'v1.15.1', latest: 'v1.15.1', behind: false });
  ok('up to date again: the banner goes', !/is out; this box runs/.test(s) && !/older code/.test(s), s);
  let line = await setupLine({ installed: 'v1.15.0', latest: 'v1.99.0', behind: true });
  ok('Setup line, behind', /v1\.15\.0/.test(line) && /v1\.99\.0 is out/.test(line) && !/git pull/.test(line), line);
  line = await setupLine({ installed: 'v1.15.0', latest: 'v1.99.0', behind: true, install_running: { started: 1, to: 'v1.99.0' } });
  ok('Setup line, an install running', /installing v1\.99\.0/.test(line) && !/command below/.test(line), line);
  line = await setupLine({ installed: 'v1.15.1', latest: null });
  ok('Setup line, offline', /unknown/.test(line), line);
  line = await setupLine({ installed: 'v1.15.1', checked: false });
  ok('Setup line, check switched off', /check is off/.test(line), line);
  // A real click there, the browser's own input and no stand-in, puts the command on the clipboard,
  // which a secure page then reads back (the Copy buttons said the browser refused on every box
  // opened by its address, 2026-10-21)
  await send('Network.setCookie', { name: 'cockpit', value: cookie, url: FAR, httpOnly: true, sameSite: 'Strict' }, sessionId);
  await send('Emulation.setFocusEmulationEnabled', { enabled: true }, sessionId);
  await send('Page.navigate', { url: FAR + '/' }, sessionId);
  ok('the page by another name, over plain HTTP, is live', await waitFor("document.getElementById('conn-lbl').textContent === 'live'"),
     await evalJs("document.getElementById('conn-lbl').textContent"));
  const ctx = await evalJs("JSON.stringify({ secure: window.isSecureContext, api: !!navigator.clipboard })");
  ok('and is no secure context: the browser gives it no clipboard API', ctx === JSON.stringify({ secure: false, api: false }), ctx);
  // the page's own stream keeps its hands off the state put in for the click
  const at = JSON.parse(await evalJs(`(() => { HANDLERS.update = []; F.update = { installed: 'v1.15.0', latest: 'v1.99.0', behind: true };
    renderBanners(lastState || {}, lastErrors || {});
    const b = [...document.querySelectorAll('#banners .banner')].find(e => /is out; this box runs/.test(e.textContent));
    const btn = b && b.querySelector('.bcmd button');
    if (!btn) return '{}';
    btn.scrollIntoView({ block: 'center' });
    const r = btn.getBoundingClientRect();
    return JSON.stringify({ x: r.x + r.width / 2, y: r.y + r.height / 2, cmd: b.querySelector('.bcmd .cmd').textContent }); })()`));
  if (at.x != null) for (const type of ['mousePressed', 'mouseReleased'])
    await send('Input.dispatchMouseEvent', { type, x: at.x, y: at.y, button: 'left', clickCount: 1 }, sessionId);
  await sleep(400);
  const said = await evalJs("[...document.querySelectorAll('#toasts .toast')].map(e => e.textContent).join(' | ')");
  ok('a real click on Copy there says Copied', /^Copied\.$/.test(said), said);
  await send('Browser.grantPermissions', { origin: BASE, permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite'] });
  await send('Page.navigate', { url: BASE + '/' }, sessionId);
  await waitFor("document.readyState === 'complete'");
  const held = await evalJs("navigator.clipboard.readText()");
  ok('and the clipboard holds the exact command', held === at.cmd && /^cd \S+ && curl -fsSL /.test(held), held);
  // the proxy's role 8 (v6.48): a tool call that named a host of Cohere's report in the last 24 h
  // is a red banner with its count, host, tool and route; none of them, no banner
  const tool = tt => evalJs(`(() => { F.toolTargets = ${JSON.stringify(tt)}; renderBanners(lastState || {}, lastErrors || {});
    const b = [...document.querySelectorAll('#banners .banner')].find(e => /asked a tool to reach/.test(e.textContent));
    return b ? b.className + ' :: ' + b.textContent : ''; })()`);
  await waitFor("document.getElementById('conn-lbl').textContent === 'live'");
  ok('no tool call naming the hosts: no banner for it', (await tool({ count: 0, last: null })) === '');
  s = await tool({ count: 2, last: { at: Date.now() / 1000 - 125, hosts: 'aliyuncs.com', tool: 'bash', path: '/v1/chat/completions' } });
  ok('two of them: a red banner with the host, the count, the tool and the route',
     /\berr\b/.test(s) && /reach aliyuncs\.com\./.test(s) && /2 tool calls in the last 24 h/.test(s)
     && /in a bash call on \/v1\/chat\/completions/.test(s) && /agents-and-network/.test(s), s);
  // it says what the proxy saw, not what a client did with it: it claimed opencode refused the call,
  // untrue for a config of the user's own (found in review)
  ok('and claims nothing a client did', /only reports it/.test(s) && !/refuses/.test(s), s);
  await tool(null);
  ok('no exception', jsErrors().length === 0, jsErrors().join(' | '));
} finally {
  const gone = child => (!child || child.exitCode !== null || child.signalCode !== null) ? null
    : Promise.race([new Promise(r => child.once('exit', r)), sleep(5000)]);
  const browser = gone(chrome);
  try { if (chrome) chrome.kill('SIGTERM'); } catch { /* gone */ }
  await browser;
  const cockpit = gone(server);
  try { process.kill(-server.pid, 'SIGTERM'); } catch { try { server.kill('SIGTERM'); } catch { /* gone */ } }
  await cockpit;
}

const failed = checks.filter(c => !c.ok);
for (const c of checks) console.log(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}${c.ok || !c.detail ? '' : '  <- ' + c.detail}`);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
