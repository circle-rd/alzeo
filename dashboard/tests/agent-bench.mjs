// The Agent tab against a real opencode: this check OWNS everything it drives, so it runs in CI as on
// any box. It fetches the opencode release install.sh pins (this machine's asset, checked against its
// sha256), starts it on loopback with a password, a HOME and a config of its own, starts a dry-run
// cockpit whose Agent relay binds loopback and points at that opencode, and drives both with a headless
// browser: the relay's gate, the Agent tab, the page opencode sends with the phone layer linked into it,
// and a terminal opened through the relay with a harmless command run in it.
//
//   node dashboard/tests/agent-bench.mjs
//
// The live checks (agent-check.mjs, mobile-check.mjs) need a box with the Agent view installed and were
// run by hand: an opencode release that changed what it sends broke the tab between two of those runs
// twice (1.19 sent its page chunked and the phone layer was lost; 1.19 checks the Origin of every
// terminal connection), found on the box after an update instead of before it (2026-10-22). This one
// would have failed on both.
//
// Nothing it does reaches the box's own cockpit, relay, opencode-web or engines: free ports on
// loopback, its own key, password, HOME and config directory under the system temporary directory, a
// PATH whose docker, systemctl, journalctl, nvidia-smi, sudo, git, curl, tailscale and opencode answer
// nothing for the cockpit, and a browser that resolves no name. It takes the browser from CHROME and an
// already downloaded release from AGENT_BENCH_TARBALL (checked against the same sha256).
import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { createHash, randomBytes } from 'node:crypto';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { portOf, portsOf } from './localports.mjs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');
const COCKPIT = join(HERE, '..', 'cockpit.py');
const CHROME = process.env.CHROME || ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  '/snap/bin/chromium'].find(existsSync);
// install.sh pins the linux-arm64 asset, the one a DGX Spark runs; the CI runner is x86_64, so the bench
// carries that asset's digest for the same release (GitHub's digest, checked against the download on
// 2026-10-23, and 1.19.5's on 2026-10-30). A new pin in install.sh fails here until its x64 digest is added:
// two pins, one release.
const X64_SHA256 = { '1.19.5': '588310a336fa3b9df36b3f0225b26f679d453efaad0cf3d66c3651ef3ec1647a' };
const checks = [];
const ok = (name, cond, detail = '') => { checks.push({ name, ok: !!cond, detail: String(detail).slice(0, 220) }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
// The cockpit and its relay answer on 127.0.0.2, opencode on 127.0.0.1, as a box's answer on its tailnet
// address: opencode accepts a terminal from its own host and from http://127.0.0.1 and localhost origins only,
// so on 127.0.0.1 the relay's Origin rewrite would go untested. 127.0.0.2 is loopback all the same: nothing
// outside the machine reaches it.
const HOST = '127.0.0.2';
// Ports (review of PR #60, 2026-10-29): a number read from a socket that is then closed is anyone's until the
// server binds it, and ci-local.sh runs the workflow's jobs side by side. opencode starts on port 0, the kernel's
// own free port, read back once it listens (localports.mjs). The cockpit's port and its relay's have to be known
// before the cockpit starts: the relay names the cockpit's in the frame-ancestors of the page it frames (a cockpit on
// port 0 wrote ":0" there and the frame was refused), and the cockpit's pages name the relay's. Both are taken below
// the range the kernel hands out for port 0 (ip_local_port_range), where no program given port 0 can land, and
// taken again if a program asked for one of those very numbers.
const EPHEMERAL_LOW = Number(readFileSync('/proc/sys/net/ipv4/ip_local_port_range', 'utf8').trim().split(/\s+/)[0]) || 32768;
const belowEphemeral = () => EPHEMERAL_LOW - 1 - Math.floor(Math.random() * 12000);

const root = mkdtempSync(join(tmpdir(), 'agent-bench-'));
let ocServer = null, cockpit = null, chrome = null;
process.on('exit', () => {
  try { if (chrome && chrome.exitCode === null && chrome.signalCode === null) chrome.kill('SIGKILL'); } catch { /* gone */ }
  for (const p of [cockpit, ocServer]) {
    try { if (p && p.exitCode === null && p.signalCode === null) process.kill(-p.pid, 'SIGTERM'); } catch { /* gone */ }
  }
  rmSync(root, { recursive: true, force: true });
});
process.on('SIGINT', () => process.exit(130));
process.on('SIGTERM', () => process.exit(143));
// a terminal closed under a run by hand (an SSH session dropped) ended node without 'exit', and the servers it
// started in sessions of their own stayed up on loopback
process.on('SIGHUP', () => process.exit(129));
function report() {
  for (const c of checks) console.log(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}${c.ok || !c.detail ? '' : `  (${c.detail})`}`);
  console.log(`\n${checks.filter(c => c.ok).length}/${checks.length} checks passed`);
}
function stop(code, why) { if (why) console.error(why); report(); process.exit(code); }
// a run takes two minutes or so; a browser or a server that stops answering must not hold a CI job for the six
// hours a runner allows, nor a local run until someone notices (found in review, 2026-10-23)
const RUN_LIMIT_S = Number(process.env.AGENT_BENCH_TIMEOUT_S || 600);
setTimeout(() => stop(1, `the bench did not finish within ${RUN_LIMIT_S} s`), RUN_LIMIT_S * 1000).unref();

// ── the pinned opencode, this machine's asset ────────────────────────────────
const installSh = readFileSync(join(REPO, 'install.sh'), 'utf8');
const VERSION = installSh.match(/^OPENCODE_VERSION="\$\{OPENCODE_VERSION:-([0-9.]+)\}"/m)?.[1];
const ARM64_SHA256 = installSh.match(/^OPENCODE_SHA256="\$\{OPENCODE_SHA256:-([0-9a-f]{64})\}"/m)?.[1];
if (!VERSION || !ARM64_SHA256) stop(2, 'install.sh names no OPENCODE_VERSION or OPENCODE_SHA256 default');
const ASSET = { arm64: 'opencode-linux-arm64.tar.gz', x64: 'opencode-linux-x64.tar.gz' }[process.arch];
const SHA256 = { arm64: ARM64_SHA256, x64: X64_SHA256[VERSION] }[process.arch];
if (!ASSET) stop(2, `no opencode release asset for ${process.arch}`);
// AGENT_BENCH_OPENCODE runs the bench against another opencode binary, a release to try before it is pinned:
// no checksum then, and the tab's line is read against that binary's version.
const CANDIDATE = process.env.AGENT_BENCH_OPENCODE;
let OC, UNDER_TEST = VERSION;
if (CANDIDATE) {
  const v = spawnSync(CANDIDATE, ['--version'], { encoding: 'utf8', env: { PATH: process.env.PATH, HOME: join(root, 'version-home') } });
  UNDER_TEST = (v.stdout || '').match(/(\d+\.\d+\.\d+\S*)/)?.[1];   // 1.19.4, "opencode v2.1.3", a pre-release whole
  if (v.status !== 0 || !UNDER_TEST) stop(2, `${CANDIDATE} --version: ${v.stderr || v.stdout}`);
  console.log(`  note a candidate opencode ${UNDER_TEST} (${CANDIDATE}), not the ${VERSION} install.sh pins: no checksum`);
  OC = CANDIDATE;
} else {
  if (!SHA256) stop(2, `install.sh pins opencode ${VERSION}: add GitHub's digest of ${ASSET} for that release to X64_SHA256 here`);
  let tarball = process.env.AGENT_BENCH_TARBALL;
  if (!tarball) {
    tarball = join(root, ASSET);
    const r = await fetch(`https://github.com/anomalyco/opencode/releases/download/v${VERSION}/${ASSET}`, { signal: AbortSignal.timeout(300000) });
    if (!r.ok) stop(2, `downloading opencode ${VERSION}: HTTP ${r.status}`);
    writeFileSync(tarball, Buffer.from(await r.arrayBuffer()));
  }
  const got = createHash('sha256').update(readFileSync(tarball)).digest('hex');
  ok(`opencode ${VERSION} (${ASSET}) matches its pinned sha256`, got === SHA256, got);
  if (got !== SHA256) stop(1);
  const ocDir = join(root, 'opencode');
  mkdirSync(ocDir);
  const untar = spawnSync('tar', ['-xzf', tarball, '-C', ocDir, 'opencode'], { encoding: 'utf8' });
  if (untar.status !== 0) stop(2, `tar: ${untar.stderr}`);
  OC = join(ocDir, 'opencode');
}

// ── opencode serve: loopback, its own HOME, config and password ──────────────
// opencode reads the .opencode folders and opencode.json files of every parent of its working
// directory: under the system temporary directory there should be none, and a run that found one
// would let a stranger's config into the bench, so it stops instead.
const work = join(root, 'work');
mkdirSync(work);
// the temporary files of opencode, the cockpit and the browser go under the root too, removed with it:
// in the system's they were left behind (a com.google.Chrome.* and an opencode folder per run)
const scratch = join(root, 'tmp');
mkdirSync(scratch);
for (let d = dirname(work); ; d = dirname(d)) {
  const found = ['.opencode', 'opencode.json', 'opencode.jsonc'].map(n => join(d, n)).find(existsSync);
  if (found) stop(2, `${found} is above the bench's working directory and opencode would read it: set TMPDIR elsewhere`);
  if (d === dirname(d)) break;
}
const home = join(root, 'home');
const xdg = { XDG_CONFIG_HOME: join(home, '.config'), XDG_DATA_HOME: join(home, '.local', 'share'),
  XDG_STATE_HOME: join(home, '.local', 'state'), XDG_CACHE_HOME: join(home, '.cache') };
for (const d of Object.values(xdg)) mkdirSync(d, { recursive: true });
// what install.sh writes, reduced to one provider on a closed port: nothing here talks to a model
const ocConfig = join(root, 'opencode.json');
writeFileSync(ocConfig, JSON.stringify({
  $schema: 'https://opencode.ai/config.json', autoupdate: false, model: 'bench/bench-model',
  provider: { bench: { npm: '@ai-sdk/openai-compatible', name: 'bench (closed port)',
    options: { baseURL: 'http://127.0.0.1:1/v1', apiKey: 'not-a-key' }, models: { 'bench-model': { name: 'bench model' } } } },
}, null, 1));
const password = randomBytes(18).toString('hex');
let OC_PORT = 0;   // the port given to opencode: 0, then the one it listens on
// the unit install-agent.sh would write for this server, AGENT_AUTO=1's permission line in it: the cockpit
// reads the binary it runs and the permission mode from it (COCKPIT_AGENT_UNIT_PATH, never the box's own)
const unitPath = join(root, 'opencode-web.service');
// as install-agent.sh builds it since v1.40.4: the shell rules after its "*" (oc-merge-limits.py)
const AUTO_PERMISSION = execFileSync('python3', [join(HERE, '..', '..', 'oc-merge-limits.py'), '--agent-auto-permission'],
  { encoding: 'utf8' }).trim();
const AUTO_LINE = `Environment='OPENCODE_PERMISSION=${AUTO_PERMISSION}'`;
const unitText = readFileSync(join(HERE, '..', 'opencode-web.service.template'), 'utf8')
  .replaceAll('__OPENCODE_BIN__', OC).replaceAll('__AUTO_LINE__', AUTO_LINE).replaceAll('__HOME__', home)
  .replaceAll('__PATH__', process.env.PATH).replaceAll('__OUTPUT_TOKEN_MAX__', '32000').replaceAll('__USER__', 'bench')
  .replaceAll('__GROUP__', 'bench').replaceAll('__OPENCODE_PORT__', String(OC_PORT));
writeFileSync(unitPath, unitText);
ok('the unit template renders with no placeholder left', !/__[A-Z_]+__/.test(unitText), (unitText.match(/__[A-Z_]+__/g) || []).join(' '));
// opencode starts with the unit's own command line: a flag the unit passes and a release refuses (2.1.3 takes
// --log-level in lower case only, and refuses INFO) fails here, not at a box's next restart
const execLine = unitText.split('\n').find(l => l.startsWith('ExecStart=')) || '';
const [exe, ...ocArgs] = execLine.slice('ExecStart='.length).trim().split(/\s+/);
ok('the unit runs this opencode, on the port given to it', exe === OC && ocArgs.join(' ').includes(`--port ${OC_PORT}`), execLine);
// stays 0 in the unit (port 0) until opencode listens: read back below
const ocLog = openSync(join(root, 'opencode.log'), 'a');
ocServer = spawn(exe, ocArgs, {
  cwd: work, detached: true, stdio: ['ignore', ocLog, ocLog],
  // the unit's environment (dashboard/opencode-web.service.template), every path under the bench's root,
  // and AGENT_AUTO=1's permission line, which our boxes run with
  env: { PATH: process.env.PATH, HOME: home, TMPDIR: scratch, ...xdg, OPENCODE_SERVER_USERNAME: 'opencode', OPENCODE_SERVER_PASSWORD: password,
         OPENCODE_CONFIG: ocConfig, OPENCODE_PERMISSION: AUTO_PERMISSION, OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX: '32000',
         OPENCODE_DISABLE_AUTOUPDATE: '1', OPENCODE_DISABLE_MODELS_FETCH: '1', OPENCODE_DISABLE_PROJECT_CONFIG: '1',
         OPENCODE_DISABLE_CLAUDE_CODE: '1', OPENCODE_DISABLE_LSP_DOWNLOAD: '1' },
});
try { OC_PORT = await portOf(ocServer, { within: 90000 }); } catch (e) { stop(1, `opencode never listened: ${e.message}\n` + readFileSync(join(root, 'opencode.log'), 'utf8').slice(-1500)); }
const basic = 'Basic ' + Buffer.from(`opencode:${password}`).toString('base64');
// 1.x answers its health as JSON on /global/health; a 2.x sends its page there and its version on /api/info
const json = async path => {
  const r = await fetch(`http://127.0.0.1:${OC_PORT}${path}`, { headers: { authorization: basic }, signal: AbortSignal.timeout(2000) });
  return r.ok && /json/.test(r.headers.get('content-type') || '') ? r.json() : null;
};
let health = null, healthPath = null;
for (const t0 = Date.now(); Date.now() - t0 < 90000 && !health; await sleep(500)) {
  for (const path of ['/global/health', '/api/info']) {
    try { health = await json(path); } catch { health = null; }
    if (health) { healthPath = path; break; }
  }
}
ok(`opencode serve answers on loopback, version ${UNDER_TEST}`, health && health.version === UNDER_TEST && health.healthy !== false,
   `${healthPath}: ${JSON.stringify(health)}`);
if (!health) stop(1, readFileSync(join(root, 'opencode.log'), 'utf8').slice(-1500));
ok('opencode serve refuses a request without its password',
   (await fetch(`http://127.0.0.1:${OC_PORT}${healthPath}`, { signal: AbortSignal.timeout(5000) })).status === 401);

// ── a dry-run cockpit whose Agent relay binds loopback ───────────────────────
const cfgDir = join(root, 'config');
const fence = join(root, 'bin');
mkdirSync(cfgDir); mkdirSync(fence);
const key = randomBytes(24).toString('hex');
writeFileSync(join(cfgDir, 'api-key'), key + '\n', { mode: 0o600 });
writeFileSync(join(cfgDir, 'opencode-web.env'), `OPENCODE_SERVER_USERNAME=opencode\nOPENCODE_SERVER_PASSWORD=${password}\n`, { mode: 0o600 });
for (const cmd of ['docker', 'systemctl', 'journalctl', 'nvidia-smi', 'sudo', 'git', 'curl', 'tailscale', 'opencode']) {
  writeFileSync(join(fence, cmd), '#!/bin/sh\nexit 1\n'); chmodSync(join(fence, cmd), 0o755);
}
const CLOSED = 'http://127.0.0.1:1';
let PORT = 0, RELAY_PORT = 0;
for (let attempt = 1; ; attempt++) {
  PORT = belowEphemeral();
  do { RELAY_PORT = belowEphemeral(); } while (RELAY_PORT === PORT);
  cockpit = spawn('python3', [COCKPIT], {
    env: { ...process.env, COCKPIT_DRY_RUN: '1', COCKPIT_PORT: String(PORT), COCKPIT_CONFIG_DIR: cfgDir, COCKPIT_BIND: HOST,
           COCKPIT_AGENT_PORT: String(RELAY_PORT), COCKPIT_AGENT_BIND: HOST, COCKPIT_AGENT_UPSTREAM: `http://127.0.0.1:${OC_PORT}`,
           COCKPIT_AGENT_UNIT_PATH: unitPath,
           COCKPIT_AUTOHEAL: '0', COCKPIT_AUTOFIT: '0', COCKPIT_UPDATE_CHECK: '0',
           COCKPIT_ENGINE: CLOSED, COCKPIT_PROXY: CLOSED, COCKPIT_IMAGE: CLOSED, COCKPIT_VIDEO: CLOSED, HOME: root, TMPDIR: scratch, PATH: `${fence}:${process.env.PATH}` },
    stdio: ['ignore', 'ignore', 'ignore'], detached: true,
  });
  // both listening, or another try: a cockpit whose port was taken ends, one whose relay's was taken goes on without it
  let both = false;
  for (const t0 = Date.now(); Date.now() - t0 < 30000 && !both; await sleep(200)) {
    const ports = portsOf(cockpit.pid);
    both = ports.includes(PORT) && ports.includes(RELAY_PORT);
    if (!both && (cockpit.exitCode !== null || cockpit.signalCode !== null)) break;
  }
  if (both) break;
  try { process.kill(-cockpit.pid, 'SIGTERM'); } catch { /* gone */ }
  await Promise.race([new Promise(r => cockpit.once('exit', r)), sleep(5000)]);
  if (attempt === 3) stop(1, `the cockpit and its relay never bound ports below ${EPHEMERAL_LOW} in 3 tries`);
}
const BASE = `http://${HOST}:${PORT}`, RELAY = `http://${HOST}:${RELAY_PORT}`;
let up = false;
for (const t0 = Date.now(); Date.now() - t0 < 30000 && !up; await sleep(400)) {
  try { up = (await fetch(`${BASE}/api/health`, { signal: AbortSignal.timeout(1500) })).ok; } catch { /* starting */ }
}
if (!up) stop(1, 'the spawned cockpit never became healthy');
const login = await fetch(`${BASE}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
const cookie = (login.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
if (!cookie) stop(1, `login failed: HTTP ${login.status}`);
let agent = {};
for (const t0 = Date.now(); Date.now() - t0 < 30000; await sleep(1000)) {
  agent = ((await (await fetch(`${BASE}/api/state`, { headers: { cookie: `cockpit=${cookie}` } })).json()).agent || {}).data || {};
  if (agent.relay && agent.relay.listening && agent.server && agent.server.healthy && agent.auto_live != null) break;
}
ok('the cockpit sees its relay listening and opencode healthy', agent.relay && agent.relay.listening && agent.server && agent.server.healthy,
   JSON.stringify(agent).slice(0, 200));
ok('the cockpit reads the permission mode through the relay (tool calls run without asking)', agent.auto_live === true, String(agent.auto_live));
ok('the cockpit reads the unit: its binary and its permission line', agent.unit_installed === true && agent.binary === UNDER_TEST && agent.auto === true,
   JSON.stringify({ unit_installed: agent.unit_installed, binary: agent.binary, auto: agent.auto }));

// ── the relay's gate, from node ──────────────────────────────────────────────
ok('relay: 401 without a session', (await fetch(`${RELAY}/global/health`)).status === 401);
ok('relay: 403 with a foreign origin', (await fetch(`${RELAY}/global/health`, { headers: { cookie: `cockpit=${cookie}`, origin: 'http://evil.example' } })).status === 403);
// the route that answered opencode's health above: /global/health on 1.x, /api/info on OpenCode 2
const h = await fetch(`${RELAY}${healthPath}`, { headers: { cookie: `cockpit=${cookie}` } });
const hBody = await h.text();
let hJson = null;
try { hJson = JSON.parse(hBody); } catch { /* a page, not a health answer */ }
ok('relay: 200 and opencode healthy through it', h.status === 200 && hJson && hJson.version === UNDER_TEST && hJson.healthy !== false,
   `${h.status} ${(h.headers.get('content-type') || '').split(';')[0]} ${hBody.slice(0, 60).replace(/\s+/g, ' ')}`);
ok('relay: frame-ancestors names the cockpit', (h.headers.get('content-security-policy') || '').includes(`frame-ancestors 'self' ${BASE}`),
   h.headers.get('content-security-policy'));
// asked the way a browser loads the frame (Sec-Fetch-Dest, and the compression a browser accepts): a
// release that compresses its page passes the relay untouched, without the layer (OpenCode 2.1.3 does)
const page = await fetch(`${RELAY}/`, { headers: { cookie: `cockpit=${cookie}`, 'accept-encoding': 'gzip, deflate, br',
  'sec-fetch-dest': 'iframe', accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8' } });
const html = await page.text();
ok('relay: the page links the phone layer into what opencode sends', page.status === 200
   && html.includes('/__cockpit/agent-mobile.js') && html.includes('/__cockpit/agent-mobile.css'), `${page.status} ${html.length} bytes`);
// opencode sends its page with no Cache-Control, and Safari then served it from its heuristic cache, the
// phone layer with it never reaching the browser: the relay sends it with no-cache
ok('relay: the page is revalidated at every load (no-cache)', /no-cache/.test(page.headers.get('cache-control') || ''), page.headers.get('cache-control'));
// and as a browser asks for it on a box's tailnet address: http://100.x is no secure context, where browsers send
// no Sec-Fetch-* header at all, so only its Accept says it wants a page. 127.0.0.2 is loopback, which browsers do
// count as secure: the browser checks below cannot see that case, nor an API kept to secure contexts
// (crypto.randomUUID, the clipboard) that a release would start to need.
const plain = await fetch(`${RELAY}/`, { headers: { cookie: `cockpit=${cookie}`, 'accept-encoding': 'gzip, deflate, br',
  accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8' } });
const plainHtml = await plain.text();
ok('relay: the page links the phone layer when asked without Sec-Fetch headers, as from a tailnet address',
   plain.status === 200 && plainHtml.includes('/__cockpit/agent-mobile.js'), `${plain.status} ${plainHtml.length} bytes`);

// ── the browser ──────────────────────────────────────────────────────────────
if (!CHROME) stop(2, 'no browser: set CHROME to a Chromium or Chrome binary');
chrome = spawn(CHROME, ['--headless=new', '--remote-debugging-pipe', '--no-first-run', '--disable-gpu',
  '--no-sandbox', `--user-data-dir=${join(root, 'chrome')}`, '--hide-scrollbars', '--window-size=1400,900',
  '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions',
  '--disable-default-apps', '--no-default-browser-check', '--disable-domain-reliability', '--no-pings',
  '--disable-client-side-phishing-detection', '--disable-features=OptimizationHints,Translate,MediaRouter',
  `--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1 , EXCLUDE ${HOST}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'],
  env: { ...process.env, TMPDIR: scratch } });
let id = 0; const pending = new Map(); const events = [];
let inbox = '';
chrome.stdio[4].on('data', d => {
  inbox += d.toString();
  let cut;
  while ((cut = inbox.indexOf('\0')) >= 0) {
    const m = JSON.parse(inbox.slice(0, cut)); inbox = inbox.slice(cut + 1);
    if (m.id && pending.has(m.id)) { const call = pending.get(m.id); pending.delete(m.id); call.resolve(m); } else if (m.method) events.push(m);
  }
});
// every call answers, or fails: when the browser exits, or after 90 s without an answer
const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
  const i = ++id;
  const timer = setTimeout(() => { pending.delete(i); reject(new Error(`${method}: no answer from the browser in 90 s`)); }, 90000);
  pending.set(i, { resolve: m => { clearTimeout(timer); resolve(m); }, reject: e => { clearTimeout(timer); reject(e); } });
  chrome.stdio[3].write(JSON.stringify({ id: i, method, params, sessionId }) + '\0');
});
chrome.once('exit', () => { for (const call of pending.values()) call.reject(new Error('the browser exited')); pending.clear(); });
async function openTab({ w, h: ht, dpr = 1, mobile = false }) {
  const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
  const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
  await send('Runtime.enable', {}, sessionId); await send('Page.enable', {}, sessionId); await send('Network.enable', {}, sessionId);
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: ht, deviceScaleFactor: dpr, mobile }, sessionId);
  await send('Emulation.setTouchEmulationEnabled', { enabled: mobile, maxTouchPoints: mobile ? 5 : 0 }, sessionId);
  await send('Emulation.setEmitTouchEventsForMouse', { enabled: mobile, configuration: mobile ? 'mobile' : 'desktop' }, sessionId);
  // one cookie for the cockpit and its relay: cookies are per host, not per port
  await send('Network.setCookie', { name: 'cockpit', value: cookie, url: BASE, httpOnly: true, sameSite: 'Strict' }, sessionId);
  const evalJs = async expression => {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
    if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || 'eval threw');
    return r.result.result.value;
  };
  const waitFor = async (expression, ms = 20000) => {
    for (const t0 = Date.now(); ; await sleep(300)) {
      try { if (await evalJs(expression)) return true; } catch { /* navigating */ }
      if (Date.now() - t0 > ms) return false;
    }
  };
  const mine = () => events.filter(m => m.sessionId === sessionId);
  const jsErrors = () => mine().filter(m => m.method === 'Runtime.exceptionThrown').map(m => m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  const responses = () => mine().filter(m => m.method === 'Network.responseReceived').map(m => m.params.response);
  const key = async (k, code, vk, text, modifiers = 0) => {
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers, ...(text ? { text, unmodifiedText: text } : {}) }, sessionId);
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers }, sessionId);
  };
  const go = url => send('Page.navigate', { url }, sessionId);
  return { sessionId, evalJs, waitFor, jsErrors, responses, mine, key, go };
}

try {
  // 1. the cockpit's Agent tab holds opencode through the relay
  const d = await openTab({ w: 1400, h: 900 });
  await d.go(`${BASE}/#agent`);
  ok('tab: the chip says ready', await d.waitFor(`document.getElementById('ag-cap').textContent.trim() === 'ready'`, 30000),
     await d.evalJs(`document.getElementById('ag-cap').textContent`));
  ok('tab: the frame points at the relay', await d.waitFor(`document.getElementById('ag-iframe')?.src === '${RELAY}/'`, 20000),
     await d.evalJs(`document.getElementById('ag-iframe')?.src || 'none'`));
  // a candidate gets the line's word on the pin between the two (newer than, or install.sh brings it in line)
  ok(`tab: the line names opencode ${UNDER_TEST} and the permission mode`,
     await d.waitFor(UNDER_TEST === VERSION ? `document.getElementById('ag-line').textContent === 'opencode ${VERSION}, tool calls run without asking'`
       : `(t => t.startsWith('opencode ${UNDER_TEST}, ') && t.endsWith(', tool calls run without asking'))(document.getElementById('ag-line').textContent)`, 15000),
     await d.evalJs(`document.getElementById('ag-line').textContent`));
  // the frame is another origin (the relay's port): what it loaded is read from the network, not its DOM
  await sleep(8000);
  const fromRelay = d.responses().filter(r => r.url.startsWith(RELAY));
  ok('tab: the frame loaded opencode through the relay (its page and its scripts)', fromRelay.some(r => r.status === 200 && r.mimeType === 'text/html')
     && fromRelay.some(r => /javascript/.test(r.mimeType)), `${fromRelay.length} responses from the relay`);
  ok('tab: the frame opened the event stream', fromRelay.some(r => /\/(global|api)\/event(\?|$)/.test(r.url) && r.status === 200),
     fromRelay.filter(r => r.url.includes('event')).map(r => r.status).join(','));
  ok('tab: no 4xx or 5xx from the relay', !fromRelay.some(r => r.status >= 400), fromRelay.filter(r => r.status >= 400).map(r => `${r.status} ${r.url}`).join(' | '));
  ok('tab: the cockpit page raised no exception', d.jsErrors().length === 0, d.jsErrors().join(' | '));

  // 2. a terminal through the relay: opencode checks the Origin of every terminal connection since 1.19,
  // and the relay has to present one it accepts. The toggle lives on a session page: an empty session is
  // made in the bench's working directory (no message is sent, so no model is asked), the line is typed only
  // when the terminal holds the focus (elsewhere it went to the agent as a prompt, 2026-10-22).
  const t = await openTab({ w: 1400, h: 900 });
  await t.go(`${RELAY}/`);
  await t.waitFor(`document.title === 'OpenCode'`, 30000);
  const dirQ = 'directory=' + encodeURIComponent(work);
  // 1.x makes a session on /session?directory= and opens it at /<directory>/session/<id>, its terminals on
  // /pty?directory=; OpenCode 2 on /api/session (the directory in the body) and /server/<this origin>/session/<id>,
  // its terminals on /api/pty?location[directory]= (both measured, 2.1.3 on 2026-10-23); the keybind, the terminal's
  // textarea and its WebSocket through the relay are the same
  const v2 = Number(UNDER_TEST.split('.')[0]) >= 2;
  const locQ = 'location%5Bdirectory%5D=' + encodeURIComponent(work);
  const sid = await t.evalJs(v2
    ? `fetch('/api/session', { method: 'POST', headers: { 'content-type': 'application/json' }, body: ${JSON.stringify(JSON.stringify({ location: { directory: work } }))} })
       .then(r => r.json()).then(j => (j.data || j).id).catch(e => 'err ' + e)`
    : `fetch(${JSON.stringify('/session?' + dirQ)}, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' })
       .then(r => r.json()).then(j => j.id).catch(e => 'err ' + e)`);
  // what came back is written into the scripts below only when it is a session id: an error message
  // holding a quote broke their syntax and ended the run with no report (OpenCode 2.1.3, 2026-10-23)
  const sidOk = typeof sid === 'string' && /^ses_[A-Za-z0-9]+$/.test(sid);
  ok('terminal: an empty session is made through the relay', sidOk, String(sid));
  const marker = join(work, 'bench-marker');
  let opened = false, hs = [], focused = false, ptys = 'no session', gone = 'no session';
  if (sidOk) {
    await t.go(v2 ? `${RELAY}/server/${Buffer.from(RELAY).toString('base64url')}/session/${sid}`
                  : `${RELAY}/${Buffer.from(work).toString('base64url')}/session/${sid}`);
    await t.waitFor(`!!document.querySelector('[data-component=prompt-input], [data-component=composer], textarea')`, 30000);
    await sleep(1500);
    const seen = t.mine().length;
    await t.key('`', 'Backquote', 192, null, 2);
    opened = await t.waitFor(`!!document.querySelector('[data-component=terminal] textarea')`, 15000);
    await sleep(3000);
    hs = t.mine().slice(seen).filter(m => m.method === 'Network.webSocketHandshakeResponseReceived');
    focused = await t.evalJs(`(() => { const x = document.querySelector('[data-component=terminal] textarea'); if (!x) return false; x.focus(); return document.activeElement === x; })()`);
    if (focused) {
      await send('Input.insertText', { text: `echo AGENT-BENCH-$((6*7)) > ${marker}` }, t.sessionId);
      await sleep(300);
      if (await t.evalJs(`document.activeElement === document.querySelector('[data-component=terminal] textarea')`)) {
        await t.key('Enter', 'Enter', 13, '\r');
        for (const t0 = Date.now(); Date.now() - t0 < 10000 && !existsSync(marker); await sleep(300)) { /* the shell writes it */ }
      }
    }
    ptys = await t.evalJs(v2
      ? `fetch(${JSON.stringify('/api/pty?' + locQ)}).then(r => r.json()).then(async j => { let n = 0; for (const s of (j.data || [])) {
          if (s && s.id && (await fetch('/api/pty/' + s.id + ${JSON.stringify('?' + locQ)}, { method: 'DELETE' })).ok) n++; } return n; }).catch(e => -1)`
      : `fetch(${JSON.stringify('/pty?' + dirQ)}).then(r => r.json()).then(async l => { let n = 0; for (const s of (l || [])) {
          if (s && s.id && (await fetch('/pty/' + s.id + ${JSON.stringify('?' + dirQ)}, { method: 'DELETE' })).ok) n++; } return n; }).catch(e => -1)`);
    gone = await t.evalJs(v2 ? `fetch('/api/session/${sid}', { method: 'DELETE' }).then(r => r.status).catch(e => 'err ' + e)`
                             : `fetch(${JSON.stringify('/session/' + sid + '?' + dirQ)}, { method: 'DELETE' }).then(r => r.status).catch(e => 'err ' + e)`);
  }
  ok('terminal: it opens by its keybind', opened, sidOk ? '' : 'no session to open');
  ok('terminal: its WebSocket is accepted (101) through the relay, from a page on another host than opencode\'s', hs.some(m => m.params.response.status === 101),
     hs.map(m => m.params.response.status).join(',') || 'no handshake');
  ok('terminal: a command typed in it runs in the shell', existsSync(marker) && readFileSync(marker, 'utf8').trim() === 'AGENT-BENCH-42',
     focused ? (existsSync(marker) ? readFileSync(marker, 'utf8').trim() : 'no marker') : 'the terminal never held the focus: nothing typed');
  ok('terminal: its session is removed through the relay', ptys >= 1, String(ptys));
  ok('terminal: the empty session is deleted through the relay', gone === 200 || gone === 204, String(gone));
  ok('terminal: the pages raised no exception', t.jsErrors().length === 0, t.jsErrors().join(' | '));

  // 3. a phone: the layer is at work on the page opencode sends
  const p = await openTab({ w: 393, h: 852, dpr: 3, mobile: true });
  await p.go(`${RELAY}/`);
  await p.waitFor(`document.title === 'OpenCode'`, 30000);
  await sleep(4000);
  ok('phone: the browser reports a finger (coarse pointer)', await p.evalJs(`matchMedia('(pointer: coarse)').matches`));
  ok('phone: the layer is linked into the page', await p.evalJs(`!!document.querySelector('script[src="/__cockpit/agent-mobile.js"]')`));
  const scheme = await p.evalJs(`localStorage.getItem('opencode-color-scheme')`);
  ok('phone: the layer set its colour default', scheme === 'dark' || scheme === 'light', String(scheme));
  ok('phone: the session list the layer reads answers', (await p.evalJs(`fetch('/api/session?limit=8&order=desc').then(r => r.status).catch(e => 0)`)) === 200);
  ok('phone: the page raised no exception', p.jsErrors().length === 0, p.jsErrors().join(' | '));
} catch (e) {
  // what a release answers wrong is reported, not thrown: an element missing, a call the browser refused
  ok('the browser checks ran to the end', false, String((e && e.stack) || e).replace(/\s+/g, ' '));
} finally {
  const ended = child => (!child || child.exitCode !== null || child.signalCode !== null) ? null
    : Promise.race([new Promise(r => child.once('exit', r)), sleep(5000)]);
  const browser = ended(chrome);
  try { if (chrome) chrome.kill('SIGTERM'); } catch { /* gone */ }
  await browser;
  for (const p of [cockpit, ocServer]) {
    const w = ended(p);
    try { if (p) process.kill(-p.pid, 'SIGTERM'); } catch { /* gone */ }
    await w;
  }
}
report();
process.exit(checks.every(c => c.ok) ? 0 : 1);
