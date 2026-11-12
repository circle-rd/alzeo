"use strict";
/* Image: Qwen-Image 2.1 and MiniMax-H3-Image, every control each actually has.

   The view addresses the image lane that serves, else the one installed (imgUnit), and
   takes that lane's controls, sizes and rules. Qwen-Image 2.1's defaults are the model's
   own, read out of its config: 1024x1024, 40 steps, one image, CFG off, the RNG on the CPU.
   MiniMax-H3-Image's: 2048x2048, its six steps fixed by the checkpoint, one image per call,
   no guidance and no negative prompt (its sampling parameters refuse both). Each lane serves
   one request at a time and cannot abort one: cancelling restarts it. Its phase comes from
   its journal (cockpit.py image_progress). */

const IMG_DEFAULTS = {mode: 't2i', size: '1024x1024', w: 1024, h: 1024, steps: 40, n: 1, bg: 'auto', fmt: 'png', seed: '', cfg: '', shift: '', dev: 'cpu', neg: '', prompt: ''};
// Qwen publishes seven aspect ratios, each a multiple of 32, offered at the cookbook's
// verified 1024-equivalent area; two also at the size the model card prints (four
// times the cost). The card's 4:3 at 2400x1792 is past the largest call measured here.
const IMG_SIZES = [['1024x1024', 'Square, 1024 × 1024'], ['1184x896', '4:3, 1184 × 896'], ['896x1184', '3:4, 896 × 1184'],
  ['1248x832', '3:2, 1248 × 832'], ['832x1248', '2:3, 832 × 1248'], ['1376x768', '16:9, 1376 × 768'], ['768x1376', '9:16, 768 × 1376'],
  ['512x512', 'Square, 512 × 512, a quick draft'], ['768x768', 'Square, 768 × 768'], ['2048x2048', 'Square, 2048 × 2048, four times the cost'],
  ['2752x1536', '16:9, 2752 × 1536, Qwen’s own size'], ['custom', 'Custom, any multiple of 32']];
const IMG_EXAMPLES = {
  'Capybara': {mode: 't2i', prompt: 'A capybara reading a book by candlelight'},
  'Text rendering': {mode: 't2i', size: '1376x768', prompt: 'A neon shop sign that reads "QWEN IMAGE 2.1", rainy night, reflections on wet pavement'},
  'Transparent cutout': {mode: 't2i', bg: 'transparent', prompt: 'This is an RGBA image with transparency. A single fluffy orange cat sitting, full body, isolated on a transparent background. The image has an alpha channel and the background is transparent. A clean cutout, no floor, no shadow, no background.'},
  'Transparent sticker': {mode: 't2i', bg: 'transparent', prompt: 'This is an RGBA image with transparency. A cute cartoon dragon sticker. The image has an alpha channel and the background is transparent.'},
  'Local edit': {mode: 'edit', prompt: 'Change the red teapot to blue, keeping its shape, table, window, and lighting unchanged.'},
  'Combine two': {mode: 'edit', prompt: 'Combine the subjects from Picture 1 and Picture 2 into one coherent scene, preserving their appearance.'}};
// The most pixels one call may ask for, its images together: the largest call measured
// on this box (one 2752x1536 image, 45.5 GiB at its peak). cockpit.py refuses the same.
const IMG_MAX_PIXELS = 2752 * 1536;
// MiniMax-H3-Image's sizes: its model card's 16-megapixel sizes up to the largest measured on this box
// (6272 x 2688, cockpit.py IMAGE_H3_MAX_PIXELS refuses past it; the card's 5024 x 3360 is just over),
// and 2048 x 2048, its quickest: a size under that area is made at it and resized (min_generation_area).
const IMG_H3_SIZES = [['2048x2048', 'Square, 2048 × 2048, the quickest (about 16 s)'], ['4096x4096', 'Square, 4096 × 4096, 16.8 megapixels'],
  ['4736x3552', '4:3, 4736 × 3552'], ['3552x4736', '3:4, 3552 × 4736'], ['5472x3072', '16:9, 5472 × 3072'], ['3072x5472', '9:16, 3072 × 5472'],
  ['6272x2688', '21:9, 6272 × 2688, the largest measured here'], ['1024x1024', 'Square, 1024 × 1024, made at 2048 × 2048 then resized'],
  ['custom', 'Custom, any multiple of 32']];
const IMG_H3_MAX_PIXELS = 6272 * 2688;
const IMG_H3_STEPS = 6;
// its licence's Excluded Territories (definition 5 and clause V.4 of its LICENSE), said where the view says its licence
const IMG_H3_LICENCE = 'Its licence, the MiniMax-H3-Image Community License, is not granted in the European Union, the United Kingdom, South Korea or the United States (its Excluded Territories: definition 5 and clause V.4 of its LICENSE).';
// The image lane this view addresses, by cockpit.py's rule (image_unit): the one up or on its way up,
// one coming up before one stopping, else the one installed, Qwen-Image 2.1 first.
function imgUnit(){
  const en = engines(), units = [IMAGE_UNIT, IMAGE_H3_UNIT], up = units.filter(u => en[u] && !UNIT_DOWN.has(en[u].state));
  if (up.length) return up.find(u => en[u].state !== 'stopping') || up[0];
  // a lane that failed before one merely installed: its failure is what the view says (cockpit.py's rule)
  return units.find(u => en[u] && en[u].state === 'failed') || units.find(u => en[u]) || IMAGE_UNIT;
}
const imgH3 = () => imgUnit() === IMAGE_H3_UNIT;
const imgSizes = () => imgH3() ? IMG_H3_SIZES : IMG_SIZES;
const imgMaxPixels = () => imgH3() ? IMG_H3_MAX_PIXELS : IMG_MAX_PIXELS;
// The Turbo checkpoint (./switch-model.sh image-turbo) samples on the eight-step sigma grid
// its model_index.json carries, whatever num_inference_steps a request says, so the field is
// left out of its requests (as the cookbook says) and the slider stops offering a choice.
const IMG_TURBO_STEPS = 8;
// what the engine serves when it says so, what its unit's next start loads otherwise
const imgTurbo = () => !imgH3() && laneTarget(IMAGE_UNIT) === 'image-turbo';
const imgSteps = () => imgH3() ? IMG_H3_STEPS : imgTurbo() ? IMG_TURBO_STEPS : Number($('img-steps').value);
const imgCount = () => imgH3() ? 1 : Number($('img-n').value);
const IS = {mode: 't2i', refs: [], inflight: null, run: null, busy: false, available: false, busyLabel: '', stageAt: {stage: '', at: 0}, watching: null, history: [], port: 30020, host: '127.0.0.1'};
const imgVal = id => ($(id) ? $(id).value.trim() : '');
function imgSize(){
  if ($('img-size').value !== 'custom'){ const [w, h] = $('img-size').value.split('x').map(Number); return {w, h}; }
  return {w: Number($('img-w').value) || 0, h: Number($('img-h').value) || 0};
}
const imgEditing = () => IS.mode === 'edit';
// Height and width must be positive multiples of 32: the engine answers anything else
// with a bare HTTP 500, so refusing here costs nothing.
function imgProblem(){
  const {w, h} = imgSize();
  if (!w || !h) return 'Set a width and a height.';
  if (w % 32 || h % 32) return `${w} × ${h} is not a multiple of 32, which the engine refuses. Nearest: ${Math.max(32, Math.round(w / 32) * 32)} × ${Math.max(32, Math.round(h / 32) * 32)}.`;
  const steps = imgSteps(); if (!(steps >= 1 && steps <= 100)) return 'Steps run from 1 to 100; the model default is 40.';
  const n = imgCount(); if (!(n >= 1 && n <= 10)) return 'Between 1 and 10 images per call.';
  const max = imgMaxPixels();
  if (n * w * h > max) return `${n} image${n > 1 ? 's' : ''} of ${w} × ${h} is ${(n * w * h / 1e6).toFixed(1)} megapixels in one call; the largest measured here is ${(max / 1e6).toFixed(1)} (${imgH3() ? '92.3 GB for the engine' : '45.5 GiB'} at its peak). The images of a call run as one batch, and running out of memory hangs this machine. Ask for ${imgH3() ? 'a smaller image' : 'fewer or smaller images'}.`;
  const cfg = Number(imgVal('img-cfg') || 1);
  if (!imgH3() && cfg > 1 && !imgVal('img-neg')) return 'A CFG scale above 1 does nothing without a negative prompt: the engine needs both.';
  if (!imgH3() && imgVal('img-neg') && cfg <= 1) return 'A negative prompt does nothing without a CFG scale above 1: the engine needs both.';
  if (imgEditing() && !IS.refs.length) return 'Editing needs at least one reference image.';
  const seed = imgVal('img-seed'); if (seed && !/^\d+$/.test(seed)) return 'The seed is a whole number, or empty for a random one.';
  return '';
}
// Fitted to this box on the v0.5.21 runtime (2026-10-10), to the medians of both checkpoints
// at six sizes, 8 and 40 steps (512x512 at 8 steps in 1.8 s to 2752x1536 at 40 in 185 s),
// within 4.9 % of all twelve: a part per image (the text encoding, the VAE decode) and a part
// per step (the denoiser), px = pixels / 1024^2. An edit adds about 1.6 s per reference and
// 0.15 s per reference and step (one reference measured at 8 and at 40 steps).
// MiniMax-H3-Image's, fitted to what the reference box measured at this lane's SGLang commit (2026-11-11):
// 2048 x 2048 in 15.6 s, 4736 x 3552 in 81.1 s and 6272 x 2688 in 82.3 s, a size under 2048 x 2048 made at
// that area; an edit of a 2048 x 2048 image with one reference 1024 pixels wide in 23.6 s, about 8 s a reference.
function imgEstimate(q){
  q = q || imgFormRequest(); if (!q.w || !q.h || !q.steps) return null;
  const refs = q.editing ? Math.max(1, q.refs || 0) : 0;
  if (q.h3) return (15.6 * Math.pow(Math.max(q.w * q.h, 2048 * 2048) / (4 * 1024 * 1024), 1.19) + refs * 8) * (q.n || 1);
  const px = (q.w * q.h) / (1024 * 1024);
  return (0.42 + 0.25 * Math.pow(px, 2.08) + q.steps * 0.883 * Math.pow(px, 1.16) + refs * (1.6 + 0.15 * q.steps)) * (q.n || 1);
}
function imgFormRequest(){ const {w, h} = imgSize(); return {w, h, steps: imgSteps() || 0, n: imgCount() || 1, editing: imgEditing(), refs: IS.refs.length, h3: imgH3()}; }
function imgPayload(){
  const {w, h} = imgSize();
  // output_format is always explicit: left out, the engine falls back to JPEG, this model
  // returns RGBA, and the request 500s
  const p = {prompt: imgVal('img-prompt'), width: w, height: h, num_inference_steps: Number($('img-steps').value), n: Number($('img-n').value),
             output_format: $('img-fmt').value, response_format: 'b64_json', generator_device: $('img-dev').value};
  if (imgTurbo()) delete p.num_inference_steps;
  // MiniMax-H3-Image: its six steps and one image are the checkpoint's, and it has no background,
  // guidance, flow shift or negative prompt to take
  if (imgH3()){
    delete p.num_inference_steps; delete p.n;
    if (imgVal('img-seed') !== '') p.seed = Number(imgVal('img-seed'));
    return p;
  }
  if ($('img-bg').value !== 'auto') p.background = $('img-bg').value;
  if (imgVal('img-seed') !== '') p.seed = Number(imgVal('img-seed'));
  if (imgVal('img-cfg') !== '') p.true_cfg_scale = Number(imgVal('img-cfg'));
  // the editing endpoint has no flow_shift field and drops it unread
  if (imgVal('img-shift') !== '' && !imgEditing()) p.flow_shift = Number(imgVal('img-shift'));
  if (imgVal('img-neg') !== '') p.negative_prompt = imgVal('img-neg');
  return p;
}
function imgCurl(){
  const p = imgPayload(), base = `http://${IS.host}:${IS.port}/v1/images`;
  const where = `# run this ON the box: the ${LANE_NAME[imgUnit()]} lane listens on loopback and has no API key\n`;
  let text;
  if (!imgEditing()) text = where + `curl -sS ${base}/generations \\\n  -H 'Content-Type: application/json' \\\n  -d ${shq(JSON.stringify(p, null, 2))}`;
  else {
    const fields = Object.entries(p).filter(([k]) => k !== 'width' && k !== 'height').map(([k, v]) => `  --form-string ${shq(k + '=' + v)}`);
    fields.unshift(`  --form-string ${shq('size=' + p.width + 'x' + p.height)}`);
    // curl reads its own syntax inside a -F value, so the name is double-quoted with \ and "
    // escaped, and the whole value single-quoted for the shell: a name like x$(cmd).png
    // must never run cmd for whoever pastes the line
    const refs = (IS.refs.length ? IS.refs : [{name: 'input.png'}]).map(r => `  -F ${shq('image[]=@"' + String(r.name).replace(/[\\"]/g, '\\$&') + '";type=image/png')}`);
    text = where + `curl -sS ${base}/edits \\\n` + [...fields, ...refs].join(' \\\n');
  }
  setText('img-curl', text);
}
// The size list of the lane the view addresses, filled again when that lane changes, at that lane's default:
// 1024 x 1024 for Qwen-Image 2.1, 2048 x 2048 for MiniMax-H3-Image (a smaller size costs it as much).
function imgFillSizes(){
  const unit = imgUnit(); if (IS.sizesOf === unit) return;
  const sel = $('img-size'); clear(sel);
  imgSizes().forEach(([v, label]) => { const o = el('option', null, label); o.value = v; sel.append(o); });
  IS.sizesOf = unit;
  sel.value = imgSizes()[0][0];
}
function imgSync(){
  imgFillSizes();
  const h3 = imgH3();
  const custom = $('img-size').value === 'custom'; show('img-custom', custom);
  if (!custom){ const [w, h] = $('img-size').value.split('x'); $('img-w').value = w; $('img-h').value = h; }
  document.querySelectorAll('#img-mode button').forEach(b => b.setAttribute('aria-checked', String(b.dataset.mode === IS.mode)));
  show('img-refbox', imgEditing());
  setText('img-run', imgEditing() ? 'Edit' : 'Generate');
  // Under the Turbo the slider shows the eight steps the checkpoint runs, its thumb too (it
  // stayed on the base's 40 beside an 8, seen on the reference box), and keeps the base's
  // setting aside for when the base comes back.
  const turbo = imgTurbo(), fixed = turbo || h3, st = $('img-steps');
  const fixedTo = String(h3 ? IMG_H3_STEPS : IMG_TURBO_STEPS);
  if (fixed && st.value !== fixedTo){ if (IS.baseSteps == null) IS.baseSteps = st.value; st.value = fixedTo; }
  else if (!fixed && IS.baseSteps != null){ st.value = IS.baseSteps; IS.baseSteps = null; }
  st.disabled = fixed;
  setText('img-steps-hint', h3 ? 'fixed at 6 by the checkpoint' : turbo ? 'fixed at 8 by the Turbo checkpoint' : 'model default 40');
  setText('img-steps-o', String(imgSteps())); setText('img-n-o', $('img-n').value);
  // MiniMax-H3-Image takes none of these: one image per call, no background, guidance, flow shift or negative prompt
  ['img-n-f', 'img-bg-f', 'img-cfg-f', 'img-shift-f', 'img-neg-f'].forEach(id => show(id, !h3));
  document.querySelectorAll('#img-examples button').forEach(b => { b.hidden = h3 && !!(IMG_EXAMPLES[b.textContent] || {}).bg; });
  setText('img-lede', h3 ? 'MiniMax-H3-Image on this box: create, or edit with up to ten references, from 2048 × 2048 to 16.9 megapixels in six steps.'
    : 'Qwen-Image 2.1 on this box: create, edit with up to ten references, and cut out with a real alpha channel.');
  // each lane's licence, as its model card states it: MiniMax-H3-Image's asks for its name in the product, which this view shows
  if (IS.licenseOf !== imgUnit()){
    IS.licenseOf = imgUnit(); const lic = $('img-license'); clear(lic);
    if (h3) lic.append('MiniMax-H3-Image: ' + IMG_H3_LICENCE + ' Nothing leaves this machine either way.');
    else lic.append('Qwen Research License: research and evaluation, ', el('b', null, 'not commercial use'), '. Nothing leaves this machine either way.');
  }
  [['img-steps', 1, 100], ['img-n', 1, 10]].forEach(([id, a, b]) => { const v = id === 'img-steps' ? imgSteps() : Number($(id).value); $(id).style.setProperty('--pct', (100 * (v - a) / (b - a)).toFixed(1) + '%'); });
  const problem = imgProblem();
  show('img-problem', !!problem && (IS.touched || !!imgVal('img-prompt'))); setText('img-problem', problem);
  const why = laneWorkWaits() || (!IS.available ? imgLaneWhy() : IS.inflight ? 'Your image is being made.' : IS.busy ? 'Someone else’s image is being made: one at a time on this lane.' : !imgVal('img-prompt') ? 'Write a prompt first.' : problem);
  $('img-run').disabled = !!why;
  show('img-why', !!why && why !== problem); setText('img-why', why);
  show('img-cancel', !!(IS.inflight || IS.busy));
  const secs = imgEstimate(), {w, h} = imgSize();
  setText('img-cost-v', secs ? 'about ' + fmtDur(secs) : '…');
  setText('img-cost-k', w && h ? `${(w * h / 1e6).toFixed(2)} megapixels${imgCount() > 1 ? ' each' : ''}` : '');
  setText('img-cost-b', h3 ? 'Fitted to three sizes and an edit measured on this box; the engine holds 92.3 GB at its peak, alone on the box.'
    : w * h >= 3.5e6 ? 'Peaks at 45.5 GiB instead of 34.8: four times the pixels cost five times the time.' : 'Fitted to six sizes of both checkpoints measured on this box.');
  const cnt = imgVal('img-prompt').length; setText('img-count', cnt ? cnt + ' characters' : '');
  imgCurl();
}
function imgLaneWhy(){
  const e = engines()[imgUnit()];
  if (!F.life) return 'Checking the lane…';
  if (!e) return 'The image lane is not installed on this box.';
  if (TRANSITIONAL.has(e.state)) return 'The lane is booting: Generate turns on by itself when it answers.';
  return 'The image lane is not serving. Load it first.';
}
function imgReset(){
  const d = IMG_DEFAULTS; IS.mode = d.mode; imgFillSizes();
  const size = imgSizes()[0][0], [sw, sh] = size.split('x');
  $('img-size').value = size; $('img-w').value = sw; $('img-h').value = sh; $('img-steps').value = d.steps; $('img-n').value = d.n; IS.baseSteps = null;
  $('img-bg').value = d.bg; $('img-fmt').value = d.fmt; $('img-dev').value = d.dev; $('img-seed').value = d.seed; $('img-cfg').value = d.cfg;
  $('img-shift').value = d.shift; $('img-neg').value = d.neg; $('img-prompt').value = d.prompt; $('img-adv').open = false; IS.touched = false;
  imgSync();
}
function imgLoadExample(name){
  const ex = IMG_EXAMPLES[name]; if (!ex) return;
  imgReset(); IS.mode = ex.mode; $('img-prompt').value = ex.prompt;
  if (ex.size && imgSizes().some(([v]) => v === ex.size)) $('img-size').value = ex.size;
  if (ex.bg && !imgH3()){ $('img-bg').value = ex.bg; $('img-adv').open = true; }
  document.querySelectorAll('#img-examples button').forEach(b => b.setAttribute('aria-pressed', String(b.textContent === name)));
  imgSync();
}
const IMG_REF_MAX = 1280;
function imgAddFiles(files){
  const room = 10 - IS.refs.length;
  if (files.length > room) toast(`Ten references is the model's maximum; taking the first ${room}.`, 'warn');
  [...files].slice(0, Math.max(0, room)).forEach(f => {
    const fr = new FileReader();
    fr.onload = () => { const im = new Image();
      im.onload = () => {
        // the count is re-read here, when the image is decoded: two batches picked at once
        // computed their room from the same length and queued twelve
        if (IS.refs.length >= 10) return toast('Ten references is the maximum.', 'warn');
        const scale = Math.min(1, IMG_REF_MAX / Math.max(im.width, im.height)), c = document.createElement('canvas');
        c.width = Math.round(im.width * scale); c.height = Math.round(im.height * scale); c.getContext('2d').drawImage(im, 0, 0, c.width, c.height);
        IS.refs.push({name: f.name, dataUrl: c.toDataURL('image/png'), w: c.width, h: c.height}); imgDrawRefs(); };
      im.onerror = () => toast(`${f.name} is not an image the browser can read.`, 'err'); im.src = fr.result; };
    fr.readAsDataURL(f);
  });
}
function imgDrawRefs(){
  const box = $('img-refs'); clear(box);
  IS.refs.forEach((r, i) => {
    const b = el('button'); b.type = 'button'; b.title = `${r.name}: click to remove`;
    const im = el('img'); im.src = r.dataUrl; im.alt = r.name; b.append(im, el('span', null, 'Picture ' + (i + 1)));
    b.addEventListener('click', () => { IS.refs.splice(i, 1); imgDrawRefs(); });
    box.append(b);
  });
  setText('img-refcount', IS.refs.length + ' of 10'); imgSync();
}
function imgShow(out, fmt){
  const sc = $('img-screen'); clear(sc); sc.dataset.mode = 'show';
  const imgs = out.data || [];
  const {w, h} = imgSize(), kind = fmt === 'webp' ? 'webp' : 'png', stamp = Date.now();
  const name = i => `${imgH3() ? 'minimax-h3-image' : 'qwen-image'}-${stamp}${imgs.length > 1 ? '-' + (i + 1) : ''}.${fmt}`;
  // each image opens at full size, the size it says once decoded or the one asked for
  const zoomable = (im, i) => { im.addEventListener('click', () => openViewer(im.src, im.naturalWidth || w, im.naturalHeight || h, name(i))); return im; };
  if (imgs.length === 1){ const im = zoomable(el('img'), 0); im.src = `data:image/${kind};base64,${imgs[0].b64_json}`;
    im.alt = 'The generated image: click to see it at full size';
    fitScreen(sc, w, h);   // the size asked for, until the image says its own
    im.addEventListener('load', () => { if (im.naturalWidth && im.naturalHeight) fitScreen(sc, im.naturalWidth, im.naturalHeight); });
    sc.append(im); }
  else if (imgs.length){ fitScreen(sc, 0, 0); const g = el('div', 'gallery'); g.style.cssText = 'width:100%; padding:12px; align-self:start';
    imgs.forEach((d, i) => { const im = zoomable(el('img'), i); im.src = `data:image/${kind};base64,${d.b64_json}`; im.alt = `Image ${i + 1}: click to see it at full size`;
      im.style.cssText = 'width:100%; border-radius:10px; cursor:zoom-in'; g.append(im); });
    sc.append(g); }
  else fitScreen(sc, w, h);
  const meta = $('img-meta'); clear(meta);
  const bytes = imgs.reduce((a, d) => a + (d.b64_json || '').length * 0.75, 0);
  const dl = el('button', 'btn primary', imgs.length > 1 ? `Download ${imgs.length} images` : 'Download'); dl.type = 'button';
  dl.addEventListener('click', () => imgs.forEach((d, i) => { const a = document.createElement('a'); a.href = 'data:application/octet-stream;base64,' + d.b64_json;
    a.download = name(i); a.click(); }));
  meta.append(dl);
  if (imgs.length){ const big = el('button', 'btn', 'Enlarge'); big.type = 'button';
    big.addEventListener('click', () => { const first = sc.querySelector('img'); if (first) first.click(); });
    meta.append(big); }
  if (imgs.length){ const again = el('button', 'btn', 'Edit this one'); again.type = 'button';
    again.addEventListener('click', () => { if (IS.refs.length >= 10) return toast('Ten references is the maximum.', 'warn');
      IS.refs.push({name: 'previous-output.png', dataUrl: 'data:image/png;base64,' + imgs[0].b64_json, w: 0, h: 0}); IS.mode = 'edit'; imgDrawRefs();
      toast(`Added as Picture ${IS.refs.length}: describe the change and press Edit.`, 'ok'); });
    meta.append(again); }
  [['size', (bytes / 1e6).toFixed(1) + ' MB'], ['peak memory', out.peak_memory_mb ? (out.peak_memory_mb / 1024).toFixed(1) + ' GiB' : null],
   ['engine time', out.inference_time_s ? out.inference_time_s.toFixed(1) + ' s' : null]].forEach(([k, v]) => { if (v != null) meta.append(el('span', 'tag', `${k} ${v}`)); });
  if (imgs.length){ IS.history = [{b64: imgs[0].b64_json, fmt, out}].concat(IS.history).slice(0, 12); imgHistory(); }
}
function imgHistory(){
  const box = $('img-history'); clear(box); $('img-history-box').hidden = !IS.history.length;
  IS.history.forEach(h => { const b = el('button'); b.type = 'button'; b.style.aspectRatio = '1 / 1';
    // whole, not cut to the square tile: a portrait cut to a square hid most of it
    const im = el('img'); im.src = 'data:image/' + (h.fmt === 'webp' ? 'webp' : 'png') + ';base64,' + h.b64; im.alt = ''; im.style.objectFit = 'contain'; b.append(im);
    b.addEventListener('click', () => imgShow(h.out, h.fmt)); box.append(b); });
}
function imgWorking(label, pct){
  const sc = $('img-screen'); const {w, h} = imgSize(); fitScreen(sc, w, h);
  if (sc.dataset.mode !== 'work'){ sc.dataset.mode = 'work'; clear(sc); ringInto(sc); }
  ringSet(sc, pct == null ? null : pct, pct == null ? '…' : Math.round(pct) + ' %', pct == null ? '' : 'about', label);
}
// the bar inside a stage: the NAME is the engine's own and exact, the number is this
// page's clock against the measured cost, and says "about"
function imgProgress(p){
  const card = $('img-prog');
  if (!IS.inflight && !IS.busy){ card.hidden = true; return; }
  card.hidden = false;
  const bar = $('img-prog-bar');
  if (!p || !p.label){ setText('img-prog-lab', IS.busy ? 'Someone else’s image is being made' : 'Waiting for the lane'); setText('img-prog-eta', ''); bar.className = 'meter indet'; return; }
  if (p.stage !== IS.stageAt.stage) IS.stageAt = {stage: p.stage, at: Date.now()};
  const secs = (Date.now() - IS.stageAt.at) / 1000;
  setText('img-prog-lab', (IS.busy ? 'Someone else’s image: ' : '') + cap1(p.label));
  if (p.stage !== 'denoising' || !IS.inflight){ setText('img-prog-eta', fmtDur(secs)); bar.className = 'meter indet'; imgWorking(cap1(p.label)); return; }
  const req = IS.run || imgFormRequest(), budget = Math.max(1, imgEstimate(req) - 3), frac = Math.min(secs / budget, 0.99);
  setText('img-prog-eta', `about ${Math.round(frac * 100)} %, ${fmtDur(Math.max(0, budget - secs))} left`);
  bar.className = 'meter'; bar.firstChild.style.width = (frac * 100).toFixed(1) + '%';
  imgWorking(`Denoising, ${req.steps} steps`, frac * 100);
}
async function imgLane(){
  try{
    const r = await fetch('/api/image'); if (r.status === 401) return;
    const d = await r.json();
    IS.port = d.port || 30020; IS.host = d.host || '127.0.0.1';
    Object.assign(IMG_NOW, {port: IS.port, unit: d.unit || null});
    const p = d.progress || {};
    // someone else generating counts: this lane serves one at a time, and a second request
    // holds another 60 GB of the box
    IS.busy = !IS.inflight && p.kind === 'stage'; IS.busyLabel = p.label || '';
    IMG_NOW.label = (IS.busy || IS.inflight) && p.label ? p.label : '';
    imgProgress(p.kind === 'boot' ? null : p);
    imgWatch(IS.busy || !!IS.inflight);
    imgRenderLane(); imgSync();
  }catch{ cap('img-cap', 'unknown', 'warn'); }
}
function imgWatch(on){
  const want = on && (IS.inflight || (!document.hidden && activeView === 'image'));
  if (want && IS.watching) return;
  if (IS.watching){ clearInterval(IS.watching); IS.watching = null; }
  if (want) IS.watching = setInterval(() => { if (!IS.inflight && (document.hidden || activeView !== 'image')){ clearInterval(IS.watching); IS.watching = null; return; } imgLane(); }, IS.inflight ? 1500 : 2000);
}
function imgRenderLane(){
  const unit = imgUnit(), box = $('img-lane'), e = engines()[unit], s = servingEngine();
  const ready = !!e && (e.state === 'ready' || e.state === 'degraded'); IS.available = ready;
  // every image lane installed, each with its Load when none serves
  const others = [IMAGE_UNIT, IMAGE_H3_UNIT].filter(u => engines()[u]);
  const st = !F.life ? 'wait' : !e ? (laneFits(unit) ? 'absent' : 'absent:64') : ready ? 'ready' : TRANSITIONAL.has(e.state) ? 'boot' : 'down:' + (s ? s[0] : '') + ':' + others.join(',');
  const sig = st + '|' + unit;
  if (box.dataset.sig !== sig){
    box.dataset.sig = sig; clear(box);
    // on a 64 GB box the command would be refused: the lane needs a 128 GB one (review, 2026-10-23)
    if (st.startsWith('absent')){ const b = el('div', 'banner info'); b.append(el('span', 'lamp')); const d = el('div'); d.append(el('b', null, 'The image lane is not installed. '), el('span', null, st === 'absent' ? 'On the box: ./install.sh --with-image (46 GB, about 25 min).' : `It ${NEEDS_128}, and this box is a 64 GB one.`)); b.append(d); box.append(b); }
    if (st === 'boot'){ const card = el('div', 'progress-card'); card.style.marginBottom = 'var(--s-5)'; const top = el('div', 'top'); top.append(el('b', null, `Loading ${LANE_NAME[unit]}`), el('span', 'eta'));
      const m = el('div', 'meter'); m.append(el('i')); card.append(top, m); box.append(card); }
    if (st.startsWith('down')){ const card = el('div', 'banner info'); card.style.alignItems = 'center'; card.append(el('span', 'lamp' + (e.state === 'failed' ? ' err' : '')));
      const d = el('div'); d.style.cssText = 'display:flex; flex-wrap:wrap; align-items:center; gap:var(--s-3)';
      const t = el('span', null, e.state === 'failed' ? 'The image lane failed. Its journal is in Logs.' : s ? `${laneLabel(s[0])} holds the box. Loading the image lane stops it first.` : 'The image lane is stopped.'); t.style.cssText = 'flex:1; min-width:240px';
      d.append(t);
      others.forEach((u, i) => { const b = el('button', i ? 'btn' : 'btn primary', u === IMAGE_H3_UNIT ? 'Load MiniMax-H3-Image' : 'Load Qwen-Image'); b.type = 'button';
        b.addEventListener('click', () => askJourney(laneTarget(u) || LANE_TARGETS[u][0])); d.append(b); });
      card.append(d); box.append(card); }
  }
  if (st === 'boot'){ const card = box.firstChild, eta = e.eta || READY_DEFAULT[unit], pct = eta && e.elapsed ? Math.min(97, 100 * e.elapsed / eta) : 6;
    card.querySelector('.meter i').style.width = pct.toFixed(1) + '%'; setText(card.querySelector('.eta'), e.elapsed && eta ? `about ${fmtDur(Math.max(0, eta - e.elapsed))} left` : ''); }
  cap('img-cap', !e ? 'not installed' : ready ? (IS.busy || IS.inflight ? 'making an image' : 'ready') : STATE_LABEL[e.state] || e.state,
      !e ? '' : ready ? (IS.busy || IS.inflight ? 'warn' : 'ok') : stateKind(e.state), ready && (IS.busy || IS.inflight) ? 'live' : e ? stateLive(e.state) : false);
  badge('image', IS.inflight || IS.busy ? '●' : '', 'live');
}
async function imgRun(){
  IS.touched = true;
  const wait = laneWorkWaits(); if (wait) return toast(wait, 'warn');
  const problem = imgProblem(); if (problem) return toast(problem, 'warn');
  if (!imgVal('img-prompt')) return toast('Write a prompt first.', 'warn');
  const editing = imgEditing(), t0 = Date.now();
  IS.inflight = t0; IS.run = imgFormRequest(); IS.stageAt = {stage: '', at: 0};
  const before = [...$('img-screen').childNodes], beforeMode = $('img-screen').dataset.mode;
  const beforeFit = [$('img-screen').style.aspectRatio, $('img-screen').style.maxWidth, $('img-screen').classList.contains('fit')];
  imgWorking('Sending the request'); imgProgress(null); imgSync(); imgWatch(true);
  try{
    const body = imgPayload(); if (editing) body.images = IS.refs.map(r => r.dataUrl);
    const {status, ok, out} = await postJSON(editing ? '/api/image/edit' : '/api/image/generate', body);
    if (!ok && out.crashed){ toast('The lane crashed while making the image: ' + out.error, 'err', 9000); return; }
    if (!ok && (out.interrupted || IMG_INTERRUPTED_AT.t > t0)){ toast('Cancelled: the lane was stopped or restarted, the only way this runtime ends a generation early.', 'warn'); return; }
    if (!ok){
      const why = out.error || (out.refused ? JSON.stringify(out.refused).slice(0, 300) : 'HTTP ' + status);
      // a busy lane leaves the last image in place
      if (status === 409){ const sc = $('img-screen'); clear(sc); sc.append(...before); sc.dataset.mode = beforeMode || '';
        sc.style.aspectRatio = beforeFit[0]; sc.style.maxWidth = beforeFit[1]; sc.classList.toggle('fit', beforeFit[2]); toast(why, 'warn', 7000); return; }
      toast((status === 504 ? '' : `Refused (${status}): `) + why, status === 504 ? 'warn' : 'err', 8000); return;
    }
    imgShow(out.image || out, body.output_format);
    toast(`Done in ${out.seconds != null ? out.seconds.toFixed(1) + ' s' : fmtDur((Date.now() - t0) / 1000)}.`, 'ok', 3000);
  }catch(e){ toast('The cockpit could not reach the image lane: ' + e.message, 'err'); }
  finally{
    IS.inflight = null; IS.run = null; $('img-prog').hidden = true;
    if ($('img-screen').dataset.mode === 'work'){ const sc = $('img-screen'); clear(sc); sc.dataset.mode = ''; fitScreen(sc);
      const d = el('div', 'empty'); d.append(el('b', null, 'Your image appears here')); sc.append(d); }
    imgLane();
  }
}
function imgCancel(){
  const unit = imgUnit();
  askAction('unit', {verb: 'restart', unit},
    [(IS.busy && !IS.inflight ? 'This image is not yours: someone else started it. ' : '') + `The runtime cannot abort a request, so cancelling restarts the lane: the image being made is lost, and the lane answers again in ${readyIn(unit)}.`], {danger: true});
}
// The sample is a generation like any other: the same frame, the same bar, the same lock.
// Its subject is the cookbook's own edit example, so "Local edit" describes what is in it.
async function imgSample(){
  const wait = laneWorkWaits(); if (wait) return toast(wait, 'warn');
  if (IS.refs.length >= 10) return toast('Ten references is the maximum.', 'warn');
  if (IS.inflight || IS.busy) return toast('The lane is already making an image; one at a time.', 'warn');
  const t0 = Date.now(); IS.inflight = t0; IS.run = {w: 1024, h: 1024, steps: imgH3() ? IMG_H3_STEPS : imgTurbo() ? IMG_TURBO_STEPS : 20, n: 1, editing: false, h3: imgH3()};
  imgWorking('Making a sample reference: a red teapot'); imgSync(); imgWatch(true);
  try{
    const body = {prompt: 'A bright daylight photograph of a red teapot on a wooden table next to a window, even natural light',
      width: 1024, height: 1024, num_inference_steps: 20, n: 1, output_format: 'png', response_format: 'b64_json', generator_device: 'cpu', seed: 42};
    if (imgTurbo() || imgH3()) delete body.num_inference_steps;   // its grid, or its checkpoint, sets the steps, as for any request
    const {status, ok, out} = await postJSON('/api/image/generate', body);
    if (!ok) return toast(status === 409 ? out.error : 'Could not make a sample: ' + (out.error || status), status === 409 ? 'warn' : 'err', 7000);
    const first = ((out.image || out).data || [])[0]; if (!first || !first.b64_json) return toast('The lane answered with no image in it.', 'err');
    imgShow(out.image || out, 'png');
    if (IS.refs.length >= 10) return toast('The sample is shown, but ten references is the maximum: it was not added.', 'warn', 7000);
    IS.refs.push({name: 'sample-teapot.png', dataUrl: 'data:image/png;base64,' + first.b64_json, w: 1024, h: 1024}); imgDrawRefs();
    toast(`Sample added as Picture ${IS.refs.length}: a red teapot. The "Local edit" example turns it blue.`, 'ok', 5000);
  }catch(e){ toast('Could not make a sample: ' + e.message, 'err'); }
  finally{ IS.inflight = null; IS.run = null; $('img-prog').hidden = true; imgLane(); }
}
function wireImage(){
  imgFillSizes();
  Object.keys(IMG_EXAMPLES).forEach(name => { const b = el('button', null, name); b.type = 'button'; b.setAttribute('aria-pressed', 'false'); b.addEventListener('click', () => imgLoadExample(name)); $('img-examples').append(b); });
  ['img-size', 'img-w', 'img-h', 'img-steps', 'img-n', 'img-bg', 'img-fmt', 'img-dev', 'img-seed', 'img-cfg', 'img-shift', 'img-neg', 'img-prompt'].forEach(id => {
    const e = $(id); e.addEventListener('input', () => { if (id !== 'img-prompt') IS.touched = true; imgSync(); }); e.addEventListener('change', imgSync); });
  document.querySelectorAll('#img-mode button').forEach(b => b.addEventListener('click', () => { IS.mode = b.dataset.mode; imgSync(); }));
  $('img-reset').addEventListener('click', () => { IS.refs = []; imgDrawRefs(); imgReset(); toast(imgH3() ? 'Back to the model’s defaults: 2048 × 2048, its 6 steps, one image.' : `Back to the model’s defaults: 1024 × 1024, ${imgTurbo() ? 'the Turbo’s 8 steps' : '40 steps'}, one image, CFG off.`, 'ok', 2600); });
  $('img-reffile').addEventListener('change', e => { imgAddFiles(e.target.files); e.target.value = ''; });
  $('img-refsample').addEventListener('click', imgSample);
  $('img-run').addEventListener('click', imgRun);
  $('img-cancel').addEventListener('click', imgCancel);
  wireCopyButton($('img-copy'), () => $('img-curl').textContent, 'Copy the call');
  document.addEventListener('visibilitychange', () => { if (!document.hidden && activeView === 'image') imgLane(); });
  imgReset(); imgLoadExample(Object.keys(IMG_EXAMPLES)[0]);
}
onShow('image', imgLane);
onLoadChange(imgSync);
// a lifecycle that changes the image lane the view addresses changes its sizes and its controls at once
afterApply(() => { imgRenderLane(); if (IS.sizesOf !== imgUnit()) imgSync(); });
