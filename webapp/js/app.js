'use strict';

/* ======================================================================
   NexDonate Mini App — vanilla JS, no build step.
   Flow: pack -> player ID (auto-verified) -> payment -> live order status
   ====================================================================== */

const tg = window.Telegram && window.Telegram.WebApp;
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n) => String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');

let lang = 'uz';
const t = (key, vars = {}) => {
  let s = (I18N[lang] && I18N[lang][key]) || I18N.uz[key] || key;
  for (const [k, v] of Object.entries(vars)) s = s.split('{' + k + '}').join(v);
  return s;
};
/* ---- currency: customers pay in so'm or rubles (their choice is remembered) ---- */
const CUR_KEY = 'nx_cur';
const loadCur = () => { try { return localStorage.getItem(CUR_KEY) === 'RUB' ? 'RUB' : 'UZS'; } catch (_) { return 'UZS'; } };
let CUR = loadCur();
const money = (n, cur = CUR) => (cur === 'RUB' ? `${fmt(n)} ₽` : `${fmt(n)} ${t('som')}`);
const priceOf = (p, cur = CUR) => (cur === 'RUB' ? p.price_rub : p.price);
const curSeg = () => `<div class="seg cur-seg">${['UZS', 'RUB'].map((c) => `<button class="${CUR === c ? 'on' : ''}" data-act="set-cur" data-cur="${c}">${c === 'RUB' ? '₽' : esc(t('som'))}</button>`).join('')}</div>`;

/* ---------------------------------------------------------------- icons */
const ICONS = {
  home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
  orders: '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>',
  support: '<path d="M4 13v-1a8 8 0 0116 0v1"/><rect x="3" y="13" width="4" height="6" rx="1.5"/><rect x="17" y="13" width="4" height="6" rx="1.5"/><path d="M19 19c0 1.5-2 2.5-5 2.5"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  chevron: '<path d="M9 5l7 7-7 7"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 012-2h9"/>',
  bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  gem: '<path d="M6 3h12l4 6-10 12L2 9z"/><path d="M2 9h20M9 3l3 6 3-6M12 21L9 9M12 21l3-12"/>',
  tag: '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="8.5" r="1.2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
  id: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><circle cx="9" cy="11" r="2"/><path d="M6.5 16c.5-1.6 4.5-1.6 5 0M14 10h4M14 14h3"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M5 20h14"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 115 0c0 2-2.5 2-2.5 4M12 17h.01"/>',
  alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>',
  send: '<path d="M21 3L3 11l7 3 3 7z"/><path d="M10 14l11-11"/>',
  card: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3 10h18M7 15h4"/>',
  bag: '<path d="M5 8h14l-1 12H6z"/><path d="M9 8V6a3 3 0 016 0v2"/>',
};
const ic = (name, cls = '') =>
  `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;

const LOGO = '<img class="brand-mark" src="assets/logo.webp" alt="NexDonate" width="42" height="42">';

/* ---- PUBG Mobile "UC" plate: dark chamfered plate, cream-gold bevelled border, big embossed lettering ---- */
const BARCODE = (() => {
  const w = [1.2, .7, 1.6, .7, 1, 1.8, .7];
  let x = 38, i = 0, s = '';
  while (x < 80) { s += `<rect x="${x.toFixed(1)}" y="64.6" width="${w[i % w.length]}" height="3.6"/>`; x += w[i % w.length] + 1; i++; }
  return s;
})();
// label -> text layout inside the 120x80 plate (font size, squeezed width, baseline per line)
const PLATE_TEXT = {
  UC: { fs: 42, tl: 62, ys: [51] }, WOW: { fs: 34, tl: 64, ys: [49] }, PRIME: { fs: 27, tl: 68, ys: [47] },
  'PRIME+': { fs: 25, tl: 74, ys: [46] }, PACK: { fs: 31, tl: 62, ys: [49] }, 'ELITE|PASS': { fs: 19, tl: 58, ys: [38, 59] },
};
const plate = (label = 'UC', o = {}) => {
  const blue = o.tone === 'blue';
  const rim = blue ? 'plateRimB' : 'plateRim', letter = blue ? 'plateLetterB' : 'plateLetter';
  const L = PLATE_TEXT[label] || PLATE_TEXT.UC;
  const lines = label.split('|');
  const text = lines.map((ln, i) => {
    const common = `x="60" y="${L.ys[i]}" text-anchor="middle" font-size="${L.fs}" font-weight="900" font-family="Montserrat,Arial,sans-serif" textLength="${L.tl}" lengthAdjust="spacingAndGlyphs"`;
    return `<text ${common} fill="#000" fill-opacity=".55" transform="translate(.9 1.5)">${ln}</text><text ${common} fill="url(#${letter})" stroke="#5f5026" stroke-width=".5" paint-order="stroke">${ln}</text>`;
  }).join('');
  const cap = o.cap || 'PUBG MOBILE · UNKNOWN CASH';
  return `<svg class="plate" viewBox="0 0 120 80" aria-hidden="true">
  <path d="M13 1.5H107L118.5 13V67L107 78.5H13L1.5 67V13Z" fill="url(#${rim})" stroke="#5c4d26" stroke-opacity=".7" stroke-width=".8"/>
  <path d="M15 6.5H105L113.5 15V65L105 73.5H15L6.5 65V15Z" fill="url(#plateFace)"/>
  <path d="M18.5 10H101.5L109.5 18V62L101.5 70H18.5L10.5 62V18Z" fill="none" stroke="#efe3b8" stroke-opacity=".3" stroke-width=".8"/>
  ${o.detail === false ? '' : `<text x="60" y="16.4" text-anchor="middle" font-size="3.6" letter-spacing=".6" font-weight="700" font-family="Montserrat,Arial,sans-serif" fill="#efe3b8" fill-opacity=".5">${cap}</text><g fill="#efe3b8" fill-opacity=".45">${BARCODE}</g>`}
  ${text}
  <path d="M13 1.5H66L40 22H6V13Z" fill="#fff" fill-opacity=".1"/></svg>`;
};

// A fan of plates behind each other, like the in-game stack (back plates sit up and to the right).
const platePile = (n, label, o = {}) => {
  const W = o.w || (n >= 4 ? 76 : 86), H = (W * 2) / 3, dx = 9, dy = 7;
  const boxW = W + (n - 1) * dx + 6, boxH = H + (n - 1) * dy;
  let html = '';
  for (let i = n - 1; i >= 0; i--) {
    html += `<span style="left:${(i * dx).toFixed(0)}px;bottom:${(i * dy).toFixed(0)}px;width:${W}px;height:${H.toFixed(1)}px;z-index:${n - i};transform:rotate(${(-7 + i * 3.5).toFixed(1)}deg);filter:brightness(${(1 - i * 0.09).toFixed(2)})">${plate(label, { ...o, detail: i === 0 })}</span>`;
  }
  return `<div class="plate-pile" style="width:${boxW}px;height:${boxH.toFixed(1)}px">${html}</div>`;
};
/* ---- Official PUBG Mobile UC artwork (assets/uc/uc1..uc6.png = the six store packs, 60 .. 8100 UC) ---- */
const OFFICIAL_LEVEL = { 60: 1, 325: 2, 660: 3, 1800: 4, 3850: 5, 8100: 6 };
const ucLevel = (uc) => OFFICIAL_LEVEL[uc] || (uc <= 60 ? 1 : uc <= 400 ? 2 : uc <= 1000 ? 3 : uc <= 2500 ? 4 : uc <= 6000 ? 5 : 6);
const ucImg = (lv, cls = '') => `<img class="ucimg ${cls}" src="assets/uc/uc${lv}.png" alt="UC" width="144" height="144" loading="lazy">`;
// A combined amount (e.g. 720 = 660 + 60) shows the official art of its biggest packs side by side.
function ucLevels(p) {
  const lv = [];
  (p.items || []).forEach((it) => { for (let i = 0; i < (it.qty || 1); i++) lv.push(ucLevel(it.uc)); });
  lv.sort((a, b) => b - a);
  return lv.length ? lv.slice(0, 3) : [ucLevel(p.uc || 60)];
}
const ucArt = (p) => { const lv = ucLevels(p); return `<div class="ucart n${lv.length}">${lv.map((l, i) => ucImg(l, 'u' + i)).join('')}</div>`; };

/* ---- catalog helpers: every supplier product is shown, grouped into sections ---- */
const SECTIONS = ['uc', 'prime', 'pass', 'packs', 'wow'];
const sectionOf = (id) => (/^\d+_uc$/.test(id) ? 'uc' : id.startsWith('prime') ? 'prime' : id.startsWith('elite_pass') ? 'pass' : id.endsWith('_wow_coins') ? 'wow' : 'packs');
function cardText(p) {
  const id = p.offer_id || '';
  switch (p.section) {
    case 'uc': return { main: fmt(p.uc), sub: 'UC' };
    case 'prime': { const m = id.match(/^prime(_plus)?_(\d+)_months?$/); return { main: m && m[1] ? 'Prime+' : 'Prime', sub: m ? t('months', { n: m[2] }) : '' }; }
    case 'pass': { const m = id.match(/lv1_(\d+)$/); return { main: id.includes('plus') ? 'Elite Pass+' : 'Elite Pass', sub: m ? `1–${m[1]}` : '' }; }
    case 'wow': return { main: fmt(parseInt(id, 10)), sub: 'WOW' };
    default: return { main: p.name, sub: '', long: true };
  }
}
const packTitle = (p) => { const c = cardText(p); return `${c.main}${c.sub ? ' ' + c.sub : ''}`; };
// Orders from the server only carry offer_id/name/uc; rebuild the same label.
const orderTitle = (o) => (o.uc ? `${fmt(o.uc)} UC` : packTitle({ offer_id: o.offer_id, section: sectionOf(o.offer_id || ''), name: o.offer_name }));
const breakdown = (items) => (items && (items.length > 1 || (items[0] && items[0].qty > 1)) ? items.map((i) => `${i.qty} × ${i.uc ? fmt(i.uc) + ' UC' : esc(i.name)}`).join(' + ') : '');

function packArt(p) {
  let art;
  if (p.section === 'uc') art = ucArt(p);
  else if (p.section === 'prime') art = platePile(1, (p.offer_id || '').includes('plus') ? 'PRIME+' : 'PRIME', { cap: 'PUBG MOBILE · PRIME' });
  else if (p.section === 'pass') art = platePile(1, 'ELITE|PASS', { cap: 'PUBG MOBILE · ELITE PASS' });
  else if (p.section === 'wow') art = platePile(1, 'WOW', { tone: 'blue', cap: 'PUBG MOBILE · WOW' });
  else art = platePile(1, 'PACK', { cap: 'PUBG MOBILE · BUNDLE' });
  return `<div class="art">${art}</div>`;
}

// Each game's own store icon lives in assets/games/<id>.webp (drop a new file in to replace it).
const gameIcon = (id, name, cls = '') =>
  `<img class="gicon ${cls}" src="assets/games/${esc(id)}.webp" alt="${esc(name)}" width="96" height="96" loading="lazy">`;

/* ---------------------------------------------------------------- state */
const S = {
  user: null, stats: { orders: 0, spent: 0 }, games: [], savedIds: [], recent: [],
  support: '', provider: 'mock', packs: null, ucOptions: [], bestUc: null,
  tab: 'uc',
  stack: [{ name: 'home' }],
  co: null, vseq: 0, vtimer: null, timer: null, viewOrder: null, ordersList: null,
};
const freshCo = () => ({ pack: null, pid: '', nick: null, vstate: 'idle', save: true, order: null, orderKey: null, receipt: null, busy: false });
S.co = freshCo();

const TABS = ['home', 'orders', 'support', 'profile'];
const FINAL = ['completed', 'failed', 'cancelled', 'expired'];

/* ---------------------------------------------------------------- helpers */
const haptic = (k = 'light') => { try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(k); } catch (_) {} };
const notice = (k) => { try { tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred(k); } catch (_) {} };

let toastTimer;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

function openExternal(url) {
  if (tg && tg.openLink) tg.openLink(url); else window.open(url, '_blank');
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (__) {}
    ta.remove();
  }
  toast(t('copied')); notice('success');
}

async function api(path, opts = {}) {
  const headers = { 'X-Init-Data': (tg && tg.initData) || '' };
  let body = opts.body;
  if (body && !(body instanceof FormData)) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(body); }
  const res = await fetch('/api' + path, { method: opts.method || 'GET', headers, body });
  if (!res.ok) {
    let code = 'error';
    try { const j = await res.json(); if (typeof j.detail === 'string') code = j.detail; } catch (_) {}
    throw Object.assign(new Error(code), { status: res.status, code });
  }
  return res.json();
}

const errText = (e) => {
  const map = {
    invalid_id: 'err_invalid_id', too_many_pending: 'err_pending', rate_limited: 'err_rate',
    catalog_unavailable: 'err_catalog', validation_unavailable: 'err_validation', cannot_pay: 'err_state', cannot_cancel: 'err_state',
  };
  return t(map[e.code] || 'err_generic');
};

const when = (ts) => new Date(ts * 1000).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });

const STATUS_TONE = { awaiting_payment: 'warn', payment_review: 'warn', paid: 'gold', processing: 'gold', completed: 'ok', failed: 'err', cancelled: 'mute', expired: 'mute' };
const tag = (st) => `<span class="tag ${STATUS_TONE[st] || 'mute'}">${t('st_' + st)}</span>`;

/* ---------------------------------------------------------------- navigation */
function go(name, params = {}) { S.stack.push({ name, params }); haptic('light'); render(); }
function tab(name) { if (S.stack.length === 1 && S.stack[0].name === name) return; S.stack = [{ name }]; haptic('light'); render(); }
function back() { if (S.stack.length > 1) { S.stack.pop(); render(); } }
function setStack(list) { S.stack = list; render(); }
const cur = () => S.stack[S.stack.length - 1];

function clearTimers() { clearInterval(S.timer); S.timer = null; }

/* ---------------------------------------------------------------- components */
const stepper = (n) => {
  const labels = [t('step_pack'), t('step_id'), t('step_pay')];
  return `<div class="stepper">${labels.map((l, i) => {
    const k = i + 1, cls = k < n ? 'done' : k === n ? 'on' : '';
    return `<div class="step ${cls}"><span class="dot">${k < n ? ic('check') : k}</span>${l}</div>${k < 3 ? `<div class="step-line ${k < n ? 'done' : ''}"></div>` : ''}`;
  }).join('')}</div>`;
};

const plateLabel = (p) => (p.section === 'prime' ? ((p.offer_id || '').includes('plus') ? 'PRIME+' : 'PRIME') : { uc: 'UC', pass: 'ELITE|PASS', wow: 'WOW' }[p.section] || 'PACK');
const thumbHTML = (p) => (p.section === 'uc'
  ? `<div class="mini-uc">${ucImg(ucLevels(p)[0])}</div>`
  : `<div class="mini-plate">${plate(plateLabel(p), { detail: false, tone: p.section === 'wow' ? 'blue' : 'gold' })}</div>`);

// The bar is built inside the screen's HTML but hoisted out of #view (see hoistCta): a fixed element inside an
// animated container is not pinned to the screen in Telegram's browser, so it only showed up at the page bottom.
const cta = (total, label, disabled, act, hidden = false) => `<div class="cta-bar${hidden ? ' empty' : ''}">
  <div class="cta-thumb" id="cta-thumb">${S.co.pack ? thumbHTML(S.co.pack) : ''}</div>
  <div class="cta-sum"><span>${t('total')}</span><b id="cta-total">${total || '—'}</b></div>
  <button class="btn gold" id="cta-btn" data-act="${act}" ${disabled ? 'disabled' : ''}>${label} ${ic('arrow', 'sm')}</button></div>`;

const orderRow = (o) => `<button class="orow" data-act="open-order" data-id="${o.id}">
  ${gameIcon('pubg', 'PUBG Mobile')}<div class="mid"><div class="t">${esc(orderTitle(o))}</div><div class="s">${o.code} · ${when(o.created_at)}</div></div>
  <div class="end"><div class="a">${money(o.amount, o.currency)}</div>${tag(o.status)}</div></button>`;

/* ---------------------------------------------------------------- screens */
const screens = {};
const after = {};

screens.home = () => {
  const name = esc(S.user.first_name || '');
  const pubg = S.games.find((g) => g.id === 'pubg');
  const soon = S.games.filter((g) => !g.active);
  const trust = [['bolt', 'trust_fast', 'c-orange'], ['shield', 'trust_safe', 'c-green'], ['support', 'trust_help', 'c-blue'], ['tag', 'trust_price', 'c-violet']];
  return `
  <header class="topbar">
    <div class="brand">${LOGO}<div><div class="brand-name">NEXDONATE</div><div class="brand-sub">${t('brand_sub')}</div></div></div>
    <div class="top-actions">${curSeg()}<button class="icon-btn" data-act="tab" data-tab="support" aria-label="${t('tab_support')}">${ic('support')}</button></div>
  </header>

  <section class="hero">
    <div>
      <div class="eyebrow">${t('hello', { name })}</div>
      <h1>${t('hero_title')}</h1>
      <p>${t('hero_sub')}</p>
      <button class="btn gold sm" data-act="open-game" data-game="pubg">${t('start')} ${ic('arrow', 'sm')}</button>
    </div>
    <div class="hero-art">${gameIcon('pubg', 'PUBG Mobile', 'i1')}<div class="i-plates">${ucImg(4)}</div>${gameIcon('free_fire', 'Free Fire', 'i2')}</div>
  </section>

  <section class="trust">${trust.map(([i, k, c]) => `<div class="trust-item"><div class="chipic ${c}">${ic(i)}</div><span>${t(k)}</span></div>`).join('')}</section>

  <h2 class="sec"><span>${t('games')}</span></h2>
  <button class="featured" data-act="open-game" data-game="pubg">
    <div class="featured-bg" style="background-image:url(assets/games/pubg.webp)"></div>
    <div class="featured-in">
      ${gameIcon('pubg', 'PUBG Mobile')}
      <div class="game-info">
        <div class="game-name">PUBG Mobile</div>
        <div class="game-meta">Unknown Cash (UC)</div>
        <div class="game-from">${pubg && priceOf({ price: pubg.from_price, price_rub: pubg.from_price_rub }) ? t('from_price', { price: `<b>${money(priceOf({ price: pubg.from_price, price_rub: pubg.from_price_rub }))}</b>` }) : t('top_up')}</div>
      </div>
    </div>
    <span class="go">${ic('arrow')}</span>
  </button>
  <div class="games-soon">${soon.slice(0, 6).map((g) => `<div class="game-soon">${gameIcon(g.id, g.name)}<div class="name">${esc(g.name)}</div><span class="pill">${t('soon')}</span></div>`).join('')}</div>

  ${S.recent.length ? `<h2 class="sec"><span>${t('recent')}</span><button class="link" data-act="tab" data-tab="orders">${t('all')}</button></h2>${S.recent.map(orderRow).join('')}` : ''}`;
};

/* ----- step 1: pick a product ----- */
// Official pack contents (base + bonus UC), shown on the matching cards.
const BONUS = { 325: '300 + 25', 660: '600 + 60', 1800: '1500 + 300', 3850: '3000 + 850', 8100: '6000 + 2100' };
const tierOf = (uc) => (uc < 200 ? 0 : uc < 1000 ? 1 : uc < 3000 ? 2 : uc < 6000 ? 3 : 4);

// A UC option from /packs becomes the "pack" the checkout works with.
const ucPack = (q) => ({ section: 'uc', uc: q.uc, price: q.price, price_rub: q.price_rub, items: q.items, offer_id: q.offer_id, custom: !q.offer_id, name: `${q.uc} UC` });
const isSel = (p) => {
  const c = S.co.pack; if (!c) return false;
  return p.section === 'uc' || p.uc ? c.section === 'uc' && c.uc === p.uc : c.offer_id === p.offer_id;
};

const packCard = (p, i) => {
  const isUc = p.section === 'uc' || (p.uc && !p.section);
  const pack = isUc ? { ...p, section: 'uc' } : p;
  const c = cardText(pack);
  const tier = isUc ? tierOf(p.uc) : 1;
  const best = isUc && S.bestUc === p.uc;
  let foot = '';
  if (isUc) foot = p.offer_id && BONUS[p.uc] ? `<span class="bonus">${BONUS[p.uc]}</span> <i>${t('bonus')}</i>` : (p.items.length > 1 || p.items[0].qty > 1 ? p.items.map((i) => (i.qty > 1 ? `${i.qty}×${i.uc}` : `${i.uc}`)).join(' + ') : '');
  return `<button class="pack t${tier} ${isSel(pack) ? 'sel' : ''}" style="--i:${i}" data-act="pick-pack" ${isUc ? `data-uc="${p.uc}"` : `data-offer="${p.offer_id}"`}>
  ${best ? `<span class="ribbon">${t('best')}</span>` : ''}<span class="tick">${ic('check')}</span>${packArt(pack)}
  <div class="uc ${c.long ? 'long' : ''}">${esc(c.main)}${c.sub ? `<small>${esc(c.sub)}</small>` : ''}</div>
  <div class="price">${money(priceOf(p))}</div>
  <div class="per">${foot}</div></button>`;
};

// Section tabs only appear when more than one product type is on sale (right now only UC is).
const tabsHTML = () => {
  const secs = SECTIONS.filter((s) => s === 'uc' || S.packs.some((p) => p.section === s));
  return secs.length < 2 ? '' : `<div class="tabs">${secs.map((s) => `<button class="tabchip ${S.tab === s ? 'on' : ''}" data-act="set-tab" data-sec="${s}">${t('sec_' + s)}</button>`).join('')}</div>`;
};

function renderPacks() {
  const wrap = $('#packs-wrap'); if (!wrap || cur().name !== 'game') return;
  const list = S.tab === 'uc' ? S.ucOptions : S.packs.filter((p) => p.section === S.tab);
  wrap.innerHTML = `${tabsHTML()}<div class="packs" id="packs">${list.map((p, i) => packCard(p, i)).join('')}</div>`;
  const active = wrap.querySelector('.tabchip.on'); if (active && active.scrollIntoView) active.scrollIntoView({ inline: 'center', block: 'nearest' });
}

function syncCta() {
  const p = S.co.pack, b = $('#cta-btn'), tot = $('#cta-total'), th = $('#cta-thumb'), bar = $('.cta-bar');
  if (tot) tot.textContent = p ? money(priceOf(p)) : '—';
  if (th) th.innerHTML = p ? thumbHTML(p) : '';
  if (b) b.disabled = !p;
  if (bar && cur().name === 'game') bar.classList.toggle('empty', !p); // slides up as soon as something is picked
}

// Move the bottom action bar out of #view into its own fixed slot (also when a screen re-renders part of itself).
function hoistCta() {
  const bar = $('#view .cta-bar'), slot = $('#ctaslot');
  if (bar && slot) slot.replaceChildren(bar);
}
new MutationObserver(hoistCta).observe(document.getElementById('view'), { childList: true, subtree: true });

screens.game = () => {
  const co = S.co;
  return `${stepper(1)}
  <section class="gbanner">
    <div class="gbanner-bg" style="background-image:url(assets/games/pubg.webp)"></div>
    <div class="gbanner-art">${ucImg(4)}</div>
    <div class="gbanner-in">${gameIcon('pubg', 'PUBG Mobile')}
      <div><h2>PUBG Mobile</h2>
        <div class="gchips"><span>${ic('globe', 'sm')}Global</span><span>${ic('id', 'sm')}Player ID</span><span>${ic('bolt', 'sm')}${t('trust_fast')}</span></div></div></div>
  </section>
  <div class="cur-row"><span>${ic('card', 'sm')}${t('currency_label')}</span>${curSeg()}</div>
  <div id="packs-wrap"><div class="packs" id="packs">${Array.from({ length: 6 }, () => '<div class="skeleton"></div>').join('')}</div></div>
  ${cta(co.pack ? money(priceOf(co.pack)) : '', t('continue'), !co.pack, 'next', !co.pack)}`;
};
after.game = async () => {
  try {
    if (!S.packs) {
      const r = await api('/games/pubg/packs');
      S.packs = r.packs; S.ucOptions = r.uc_options;
      const cheapest = S.ucOptions.reduce((a, b) => (b.per_uc < a.per_uc ? b : a), S.ucOptions[0]);
      S.bestUc = cheapest ? cheapest.uc : null;
    }
    renderPacks();
  } catch (e) {
    const w = $('#packs-wrap'); if (!w) return;
    w.innerHTML = `<div class="errbox" id="packs"><h3>${t('err_catalog')}</h3><button class="btn ghost" data-act="retry-packs">${t('retry')}</button></div>`;
  }
};

/* ----- step 2: player ID ----- */
const vboxHTML = () => {
  const co = S.co;
  if (co.vstate === 'checking') return `<div class="vcard wait"><div class="badge"><div class="spinner"></div></div><div class="msg">${t('checking')}</div></div>`;
  if (co.vstate === 'valid') return `<div class="vcard ok"><div class="badge">${ic('check')}</div><div><div class="small">${t('account_found')}</div><div class="big">${esc(co.nick || '—')}</div></div></div>`;
  if (co.vstate === 'invalid') return `<div class="vcard bad"><div class="badge">${ic('x')}</div><div class="msg">${t('id_not_found')}</div></div>`;
  if (co.vstate === 'error') return `<div class="vcard bad"><div class="badge">${ic('alert')}</div><div class="msg">${t('err_validation')} <button style="color:var(--info);font-weight:700;text-decoration:underline" data-act="revalidate">${t('retry')}</button></div></div>`;
  return '';
};
const fieldClass = () => ({ valid: 'ok', invalid: 'bad', error: 'bad' }[S.co.vstate] || '');
const trailHTML = () => (S.co.vstate === 'checking' ? '<div class="spinner"></div>' : S.co.vstate === 'valid' ? `<span style="color:var(--ok)">${ic('check')}</span>` : '');

screens.id = () => {
  const co = S.co;
  const saved = S.savedIds.filter((s) => s.game === 'pubg');
  return `${stepper(2)}
  <div class="card mini-sum">${thumbHTML(co.pack)}
    <div class="t">${esc(packTitle(co.pack))}${co.pack.custom ? `<small>${esc(breakdown(co.pack.items))}</small>` : ''}</div><div class="p">${money(priceOf(co.pack))}</div></div>
  <label class="field-label" for="pid">${t('player_id')}</label>
  <div class="field ${fieldClass()}" id="field">
    <span class="lead">${ic('id')}</span>
    <input id="pid" inputmode="numeric" autocomplete="off" maxlength="14" placeholder="${t('id_placeholder')}" value="${esc(co.pid)}">
    <span class="trail" id="trail">${trailHTML()}</span>
  </div>
  <div class="vbox" id="vbox">${vboxHTML()}</div>
  ${saved.length ? `<div class="chips">${saved.map((s) => `<button class="chip" data-act="use-id" data-pid="${esc(s.player_id)}">${ic('id', 'sm')}${esc(s.nick || s.player_id)}<small>${esc(s.player_id)}</small></button>`).join('')}</div>` : ''}
  <button class="check ${co.save ? 'on' : ''}" id="savechk" data-act="toggle-save"><span class="box">${ic('check')}</span>${t('save_id')}</button>
  <details class="help"><summary>${ic('help', 'sm')}${t('where_id')}</summary><p>${t('where_id_a')}</p></details>
  ${cta(money(priceOf(co.pack)), t('continue'), co.vstate !== 'valid', 'next')}`;
};
after.id = () => {
  const input = $('#pid');
  input.addEventListener('input', () => {
    const v = input.value.replace(/\D/g, '').slice(0, 14);
    input.value = v;
    S.co.pid = v; S.co.nick = null;
    queueValidate();
  });
  if (!S.co.pid) setTimeout(() => input.focus(), 350);
  else if (S.co.vstate === 'idle') queueValidate();
};

function refreshIdUI() {
  const f = $('#field'); if (!f) return;
  f.className = 'field ' + fieldClass();
  $('#trail').innerHTML = trailHTML();
  $('#vbox').innerHTML = vboxHTML();
  const b = $('#cta-btn'); if (b) b.disabled = S.co.vstate !== 'valid';
}

function queueValidate() {
  clearTimeout(S.vtimer);
  S.vseq++;
  const co = S.co;
  if (co.pid.length < 8) { co.vstate = 'idle'; refreshIdUI(); return; }
  co.vstate = 'checking'; refreshIdUI();
  const seq = S.vseq, pid = co.pid;
  S.vtimer = setTimeout(async () => {
    try {
      const r = await api('/validate', { method: 'POST', body: { game: 'pubg', player_id: pid } });
      if (seq !== S.vseq) return;
      co.vstate = r.valid ? 'valid' : 'invalid'; co.nick = r.nick;
      notice(r.valid ? 'success' : 'error');
    } catch (e) {
      if (seq !== S.vseq) return;
      co.vstate = e.code === 'rate_limited' ? 'idle' : 'error';
      if (e.code === 'rate_limited') toast(t('err_rate'));
    }
    refreshIdUI();
  }, 550);
}

/* ----- step 3: payment ----- */
async function ensureOrder() {
  const co = S.co;
  const key = co.pack ? `${co.pack.custom ? 'c' + co.pack.uc : co.pack.offer_id}|${co.pid}|${CUR}` : null;
  if (co.order && co.order.status === 'awaiting_payment' && (!co.orderKey || co.orderKey === key)) return co.order;
  if (co.order && co.order.status === 'awaiting_payment') await api(`/orders/${co.order.id}/cancel`, { method: 'POST' }).catch(() => {});
  co.order = await api('/orders', { method: 'POST', body: { game: 'pubg', ...(co.pack.custom ? { custom_uc: co.pack.uc } : { offer_id: co.pack.offer_id }), player_id: co.pid, save_id: co.save, currency: CUR } });
  co.orderKey = key;
  if (co.save) api('/bootstrap').then((b) => { S.savedIds = b.saved_ids; }).catch(() => {});
  return co.order;
}

const summaryCard = (o) => `<div class="card sum-card">
  <div class="sum-row"><span>${t('product')}</span><b>${esc(orderTitle(o))}</b></div>
  ${breakdown(o.items) ? `<div class="sum-row"><span>${t('contents')}</span><b>${breakdown(o.items)}</b></div>` : ''}
  <div class="sum-row"><span>${t('account')}</span><b>${esc(o.nick || '—')}</b></div>
  <div class="sum-row"><span>${t('player_id')}</span><b>${esc(o.player_id)}</b></div>
  <div class="sum-row total"><span>${t('to_pay')}</span><b>${money(o.amount, o.currency)}</b></div></div>`;

const timerText = (o) => {
  const left = Math.max(0, o.expires_at - Math.floor(Date.now() / 1000));
  return `${String(Math.floor(left / 60)).padStart(2, '0')}:${String(left % 60).padStart(2, '0')}`;
};

function payBody() {
  const o = S.co.order;
  const p = o.payment || {};
  let pay;
  if (p.provider === 'manual_card') {
    const r = S.co.receipt;
    pay = `<div class="card pay-card">
      <div class="pay-title"><span>${t('card_transfer')}</span><span class="timer">${ic('clock', 'sm')}<span id="timer">${timerText(o)}</span></span></div>
      ${p.available ? '' : `<div class="notice" style="margin-bottom:12px">${ic('alert', 'sm')}<span>${t('pay_unavailable')}</span></div>`}
      <div class="cardnum"><div><div class="num">${esc(p.card_number || '— — — —')}</div><div class="holder">${esc(p.holder || '')}${p.bank ? ` · ${esc(p.bank)}` : ''}</div></div>
        <button class="copy" data-act="copy" data-text="${esc((p.card_number || '').replace(/\s/g, ''))}" aria-label="copy">${ic('copy', 'sm')}</button></div>
      <ol class="steps-list" style="margin-top:18px">
        <li>${t('pay_step1', { amount: `<b>${money(o.amount, o.currency)}</b>` })}</li>
        <li>${t('pay_step2')}</li><li>${t('pay_step3')}</li></ol>
      <label class="upload" for="receipt">${r ? `<img src="${r.url}" alt=""><span class="name">${esc(r.file.name)}</span>${ic('check', 'sm')}` : `${ic('upload')}<span class="name">${t('upload_receipt')}</span>`}</label>
      <input id="receipt" type="file" accept="image/*" hidden></div>
      <div class="notice mt">${ic('shield', 'sm')}<span>${t('review_note')}</span></div>`;
  } else if (p.provider === 'click') {
    pay = `<div class="card pay-card">
      <div class="pay-title"><span>${t('click_title')}</span><span class="timer">${ic('clock', 'sm')}<span id="timer">${timerText(o)}</span></span></div>
      ${p.available ? '' : `<div class="notice" style="margin-bottom:12px">${ic('alert', 'sm')}<span>${t('pay_unavailable')}</span></div>`}
      <div class="clickbox"><div class="clicklogo">click</div><div><b>${money(o.amount, o.currency)}</b><span>${t('click_sub')}</span></div></div>
      <ol class="steps-list" style="margin-top:18px">
        <li>${t('click_step1')}</li><li>${t('click_step2')}</li><li>${t('click_step3')}</li></ol></div>
      <div class="notice mt">${ic('shield', 'sm')}<span>${t('click_note')}</span></div>`;
  } else {
    pay = `<div class="card pay-card"><div class="pay-title"><span>${t('demo_title')}</span><span class="timer">${ic('clock', 'sm')}<span id="timer">${timerText(o)}</span></span></div>
      <div class="notice">${ic('alert', 'sm')}<span>${t('demo_note')}</span></div></div>`;
  }
  const lbl = p.provider === 'manual_card' ? t('sent_receipt') : p.provider === 'click' ? t('pay_click') : t('demo_pay');
  const dis = (p.provider === 'manual_card' && (!p.available || !S.co.receipt)) || (p.provider === 'click' && !p.available);
  return `${stepper(3)}${summaryCard(o)}${pay}
    <button class="text-btn" data-act="cancel-order">${t('cancel_order')}</button>
    ${cta(money(o.amount, o.currency), lbl, dis, 'pay')}`;
}

screens.pay = () => `<div id="paybox">${S.co.order && S.co.order.status === 'awaiting_payment' ? payBody() : `${stepper(3)}<div class="skeleton" style="height:220px"></div>`}</div>`;
after.pay = async () => {
  try {
    await ensureOrder();
    const box = $('#paybox'); if (!box || cur().name !== 'pay') return;
    box.innerHTML = payBody();
    bindReceipt();
    startPayTimer();
  } catch (e) {
    const box = $('#paybox'); if (!box) return;
    const pending = e.code === 'too_many_pending';
    box.innerHTML = `<div class="errbox"><h3>${errText(e)}</h3><button class="btn ghost" data-act="${pending ? 'goto-orders' : 'back'}">${pending ? t('tab_orders') : t('back')}</button></div>`;
  }
};
function bindReceipt() {
  const inp = $('#receipt'); if (!inp) return;
  inp.addEventListener('change', () => {
    const f = inp.files && inp.files[0]; if (!f) return;
    if (S.co.receipt) URL.revokeObjectURL(S.co.receipt.url);
    S.co.receipt = { file: f, url: URL.createObjectURL(f) };
    const box = $('#paybox'); box.innerHTML = payBody(); bindReceipt();
    haptic('light');
  });
}
function startPayTimer() {
  clearTimers();
  S.timer = setInterval(() => {
    const el = $('#timer'); const o = S.co.order;
    if (!el || !o) return clearTimers();
    el.textContent = timerText(o);
    if (o.expires_at <= Math.floor(Date.now() / 1000)) { clearTimers(); toast(t('order_expired')); S.co.order = null; setStack([{ name: 'home' }]); }
  }, 1000);
}

/* ----- order detail ----- */
const STEP_OF = { awaiting_payment: 1, payment_review: 1, paid: 2, processing: 2, completed: 4, failed: 2, cancelled: 0, expired: 0 };
const ORB = {
  awaiting_payment: ['warn', 'clock'], payment_review: ['warn', 'clock'], paid: ['gold', 'bolt'], processing: ['gold', 'bolt'],
  completed: ['ok', 'check'], failed: ['err', 'alert'], cancelled: ['mute', 'x'], expired: ['mute', 'clock'],
};

screens.order = () => {
  const o = S.viewOrder;
  if (!o) return `<div class="skeleton" style="height:240px"></div>`;
  const [tone, icon] = ORB[o.status] || ['mute', 'clock'];
  const spin = ['paid', 'processing', 'payment_review'].includes(o.status);
  const idx = STEP_OF[o.status] ?? 0;
  const steps = [
    ['tl_created', null], ['tl_payment', o.status === 'payment_review' ? 'tl_review' : null],
    ['tl_delivery', o.status === 'failed' ? 'tl_failed_sub' : null], ['tl_done', null],
  ];
  const tl = o.status === 'cancelled' || o.status === 'expired' ? '' : `<div class="card timeline">${steps.map(([k, sub], i) => {
    let cls = i < idx ? 'done' : i === idx ? 'now' : '';
    if (o.status === 'failed' && i === 2) cls = 'err';
    if (o.status === 'completed') cls = 'done';
    return `<div class="tl ${cls}"><span class="pt">${cls === 'done' ? ic('check') : ''}</span><div><div class="tx">${t(k)}</div>${sub ? `<div class="sub">${t(sub)}</div>` : ''}</div></div>`;
  }).join('')}</div>`;
  return `
  <div class="status-hero">
    <div class="status-orb ${tone} ${spin ? 'spin' : ''}">${ic(icon)}</div>
    <h2>${t('hero_' + o.status)}</h2><p>${t('hero_' + o.status + '_sub')}</p>
  </div>
  <div class="oprod">
    <div class="oprod-art">${packArt({ section: o.uc ? 'uc' : sectionOf(o.offer_id || ''), uc: o.uc, offer_id: o.offer_id, items: o.items })}</div>
    <div class="oprod-tx"><b>${esc(orderTitle(o))}</b><span>→ ${esc(o.nick || '—')}</span></div>
  </div>
  ${tl}
  <div class="card sum-card mt">
    <div class="sum-row"><span>${t('order_no')}</span><b>${o.code}</b></div>
    <div class="sum-row"><span>${t('product')}</span><b>${esc(orderTitle(o))}</b></div>
    ${breakdown(o.items) ? `<div class="sum-row"><span>${t('contents')}</span><b>${breakdown(o.items)}</b></div>` : ''}
    <div class="sum-row"><span>${t('account')}</span><b>${esc(o.nick || '—')}</b></div>
    <div class="sum-row"><span>${t('player_id')}</span><b>${esc(o.player_id)}</b></div>
    <div class="sum-row"><span>${t('date')}</span><b>${when(o.created_at)}</b></div>
    <div class="sum-row total"><span>${t('amount')}</span><b>${money(o.amount, o.currency)}</b></div></div>
  <div class="mt" style="display:grid;gap:10px">
    ${o.status === 'awaiting_payment' ? `<button class="btn gold block" data-act="resume-pay">${t('continue_pay')} ${ic('arrow', 'sm')}</button>` : ''}
    ${['completed', 'cancelled', 'expired'].includes(o.status) ? `<button class="btn gold block" data-act="open-game" data-game="pubg">${t('buy_again')}</button>` : ''}
    ${['failed', 'payment_review', 'processing', 'paid'].includes(o.status) && S.support ? `<button class="btn line block" data-act="contact">${ic('send', 'sm')}${t('contact_support')}</button>` : ''}
  </div>`;
};
after.order = () => {
  clearTimers();
  const id = cur().params.id;
  const load = async () => {
    try {
      const o = await api('/orders/' + id);
      const changed = !S.viewOrder || S.viewOrder.status !== o.status;
      S.viewOrder = o;
      if (changed) { $('#view').innerHTML = screens.order(); if (o.status === 'completed') notice('success'); if (o.status === 'failed') notice('error'); }
      if (FINAL.includes(o.status)) { clearTimers(); refreshBoot(); }
    } catch (_) {}
  };
  load();
  S.timer = setInterval(load, 3000);
};

/* ----- orders list ----- */
screens.orders = () => `<h1 class="page-title">${t('tab_orders')}</h1><div id="olist">${Array.from({ length: 3 }, () => '<div class="skeleton" style="height:78px;margin-bottom:10px"></div>').join('')}</div>`;
after.orders = async () => {
  try {
    const { orders } = await api('/orders');
    $('#olist').innerHTML = orders.length ? orders.map(orderRow).join('') : `<div class="empty"><div class="orb">${ic('bag')}</div><h3>${t('no_orders')}</h3><p>${t('no_orders_sub')}</p><button class="btn gold" data-act="open-game" data-game="pubg">${t('start')} ${ic('arrow', 'sm')}</button></div>`;
  } catch (_) { $('#olist').innerHTML = `<div class="errbox"><h3>${t('err_generic')}</h3><button class="btn ghost" data-act="retry-orders">${t('retry')}</button></div>`; }
};

/* ----- support ----- */
screens.support = () => {
  const faq = [1, 2, 3, 4, 5, 6].map((i) => `<details class="faq"><summary><span>${t('faq_q' + i)}</span>${ic('chevron')}</summary><p>${t('faq_a' + i)}</p></details>`).join('');
  return `<h1 class="page-title">${t('tab_support')}</h1>
  <div class="card-hl" style="padding:22px 20px;text-align:center">
    <div class="status-orb gold" style="width:70px;height:70px;margin-bottom:14px">${ic('support')}</div>
    <h3 style="font-size:17px;font-weight:600">${t('help_title')}</h3>
    <p style="font-size:12.5px;color:var(--muted);margin:8px 0 16px;line-height:1.55">${t('help_sub')}</p>
    ${S.support ? `<button class="btn gold block" data-act="contact">${ic('send', 'sm')}${t('contact_support')}</button>` : `<div class="tag mute">${t('support_soon')}</div>`}
  </div>
  <h2 class="sec">${t('faq')}</h2><div class="list">${faq}</div>`;
};

/* ----- profile ----- */
screens.profile = () => {
  const u = S.user;
  const name = [u.first_name, u.last_name].filter(Boolean).join(' ') || 'User';
  const avatar = u.photo_url ? `<img class="avatar" src="${esc(u.photo_url)}" alt="">` : `<div class="avatar">${esc(name[0].toUpperCase())}</div>`;
  const saved = S.savedIds;
  return `<h1 class="page-title">${t('tab_profile')}</h1>
  <div class="card-hl profile-head">${avatar}<div><div class="n">${esc(name)}</div><div class="u">${u.username ? '@' + esc(u.username) + ' · ' : ''}ID ${u.id}</div></div></div>
  <div class="stats"><div class="stat"><span>${t('stat_orders')}</span><b>${fmt(S.stats.orders)}</b></div>
    <div class="stat"><span>${t('stat_spent')}</span><b>${money(S.stats.spent.UZS || 0, 'UZS')}</b>${S.stats.spent.RUB ? `<b>${money(S.stats.spent.RUB, 'RUB')}</b>` : ''}</div></div>
  <h2 class="sec">${t('saved_ids')}</h2>
  <div class="list">${saved.length ? saved.map((s) => `<div class="li">${ic('id')}<div class="grow"><div>${esc(s.nick || 'PUBG Mobile')}</div><div class="sub">PUBG Mobile · ${esc(s.player_id)}</div></div><button class="del" data-act="del-id" data-id="${s.id}" aria-label="delete">${ic('trash', 'sm')}</button></div>`).join('') : `<div class="li" style="color:var(--muted);font-size:13px">${t('no_saved')}</div>`}</div>
  <h2 class="sec">${t('settings')}</h2>
  <div class="list">
    <div class="li">${ic('card')}<div class="grow">${t('currency_label')}</div>${curSeg()}</div>
    <div class="li">${ic('globe')}<div class="grow">${t('language')}</div>
      <div class="seg"><button class="${lang === 'uz' ? 'on' : ''}" data-act="lang" data-lang="uz">UZ</button><button class="${lang === 'ru' ? 'on' : ''}" data-act="lang" data-lang="ru">RU</button></div></div>
    <button class="li" data-act="tab" data-tab="support">${ic('help')}<div class="grow">${t('faq')}</div>${ic('chevron', 'sm')}</button>
    ${S.support ? `<button class="li" data-act="contact">${ic('send')}<div class="grow">${t('contact_support')}</div>${ic('chevron', 'sm')}</button>` : ''}
  </div>
  <div class="foot">NEXDONATE</div>`;
};

/* ---------------------------------------------------------------- render */
function renderTabbar(active) {
  const items = [['home', 'home', 'tab_home'], ['orders', 'orders', 'tab_orders'], ['support', 'support', 'tab_support'], ['profile', 'user', 'tab_profile']];
  $('#tabbar').innerHTML = items.map(([name, icon, key]) => `<button class="tab ${name === active ? 'on' : ''}" data-act="tab" data-tab="${name}"><span class="pillbg">${ic(icon)}</span><span>${t(key)}</span></button>`).join('');
}

function render() {
  clearTimers();
  clearTimeout(S.vtimer);
  const c = cur();
  const isTab = S.stack.length === 1 && TABS.includes(c.name);
  const view = $('#view');
  view.className = 'view' + (['game', 'id', 'pay'].includes(c.name) ? ' has-cta' : '');
  $('#ctaslot').replaceChildren(); // the previous screen's bar goes away with the screen
  view.innerHTML = screens[c.name](c.params || {});
  hoistCta();
  view.style.animation = 'none'; void view.offsetWidth; view.style.animation = '';
  const tb = $('#tabbar');
  tb.hidden = !isTab;
  if (isTab) renderTabbar(c.name);
  window.scrollTo(0, 0);
  if (tg && tg.BackButton) { S.stack.length > 1 ? tg.BackButton.show() : tg.BackButton.hide(); }
  if (after[c.name]) after[c.name](c.params || {});
}

/* ---------------------------------------------------------------- actions */
function openGame() {
  S.co = freshCo();
  S.tab = 'uc';
  S.stack = [{ name: 'home' }, { name: 'game' }];
  haptic('light'); render();
}

const actions = {
  tab: (el) => tab(el.dataset.tab),
  back: () => back(),
  'open-game': () => openGame(),
  'goto-orders': () => tab('orders'),
  'retry-packs': () => { S.packs = null; render(); },
  'retry-orders': () => render(),
  'open-order': (el) => { S.viewOrder = null; go('order', { id: el.dataset.id }); },
  'pick-pack': (el) => {
    const cur_ = S.co.pack;
    const same = cur_ && (el.dataset.uc ? cur_.section === 'uc' && cur_.uc === Number(el.dataset.uc) : cur_.offer_id === el.dataset.offer);
    if (same) { // tapping the chosen pack again un-selects it
      S.co.pack = null;
      document.querySelectorAll('.pack').forEach((b) => b.classList.remove('sel'));
      haptic('light'); syncCta();
      return;
    }
    S.co.pack = el.dataset.uc
      ? ucPack(S.ucOptions.find((x) => x.uc === Number(el.dataset.uc)))
      : S.packs.find((x) => x.offer_id === el.dataset.offer);
    haptic('light');
    document.querySelectorAll('.pack').forEach((b) => b.classList.toggle('sel', b === el));
    syncCta();
  },
  'set-tab': (el) => { S.tab = el.dataset.sec; haptic('light'); renderPacks(); },
  'set-cur': (el) => {
    const c = el.dataset.cur === 'RUB' ? 'RUB' : 'UZS';
    if (c === CUR) return;
    CUR = c; haptic('light');
    try { localStorage.setItem(CUR_KEY, c); } catch (_) {}
    if (cur().name === 'game') {
      document.querySelectorAll('.cur-seg').forEach((s) => { s.innerHTML = curSeg().replace(/^<div[^>]*>|<\/div>$/g, ''); });
      renderPacks(); syncCta();
    } else render();
  },
  next: () => {
    if (cur().name === 'game' && S.co.pack) go('id');
    else if (cur().name === 'id' && S.co.vstate === 'valid') go('pay');
  },
  'use-id': (el) => { S.co.pid = el.dataset.pid; const i = $('#pid'); i.value = S.co.pid; queueValidate(); haptic('light'); },
  revalidate: () => queueValidate(),
  'toggle-save': (el) => { S.co.save = !S.co.save; el.classList.toggle('on', S.co.save); haptic('light'); },
  copy: (el) => copyText(el.dataset.text),
  'cancel-order': async () => {
    const o = S.co.order;
    if (o) await api(`/orders/${o.id}/cancel`, { method: 'POST' }).catch(() => {});
    S.co.order = null; S.co.receipt = null; setStack([{ name: 'home' }]);
  },
  'resume-pay': () => {
    const o = S.viewOrder;
    S.co = { ...freshCo(), order: o, pid: o.player_id, nick: o.nick };
    go('pay');
  },
  pay: async (el) => {
    const co = S.co, o = co.order; if (!o || co.busy) return;
    co.busy = true; el.disabled = true;
    try {
      let done;
      if (o.payment && o.payment.provider === 'click') {
        // Click confirms the payment to our server by itself; open its page and watch the order status.
        openExternal(o.payment.url);
        done = o;
      } else if (o.payment && o.payment.provider === 'manual_card') {
        const fd = new FormData(); fd.append('file', co.receipt.file);
        done = await api(`/orders/${o.id}/receipt`, { method: 'POST', body: fd });
      } else {
        done = await api(`/orders/${o.id}/mock-pay`, { method: 'POST' });
      }
      notice('success');
      S.viewOrder = done; S.co = freshCo();
      setStack([{ name: 'home' }, { name: 'order', params: { id: done.id } }]);
      refreshBoot();
    } catch (e) { toast(errText(e)); notice('error'); el.disabled = false; }
    co.busy = false;
  },
  'del-id': async (el) => {
    await api('/ids/' + el.dataset.id, { method: 'DELETE' }).catch(() => {});
    S.savedIds = S.savedIds.filter((s) => String(s.id) !== el.dataset.id); render();
  },
  lang: (el) => {
    lang = el.dataset.lang; api('/lang', { method: 'POST', body: { lang } }).catch(() => {});
    document.documentElement.lang = lang; render();
  },
  contact: () => {
    const url = `https://t.me/${S.support}`;
    if (tg && tg.openTelegramLink) tg.openTelegramLink(url); else window.open(url, '_blank');
  },
};

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-act]');
  if (el && actions[el.dataset.act]) actions[el.dataset.act](el, e);
});

/* ---------------------------------------------------------------- boot */
async function refreshBoot() {
  try {
    const b = await api('/bootstrap');
    S.stats = b.stats; S.recent = b.recent_orders; S.savedIds = b.saved_ids; S.games = b.games;
  } catch (_) {}
}

async function boot() {
  if (tg) {
    tg.ready(); tg.expand();
    try { tg.setHeaderColor('#0a0f20'); tg.setBackgroundColor('#0a0f20'); tg.setBottomBarColor && tg.setBottomBarColor('#0c1228'); } catch (_) {}
    try { tg.disableVerticalSwipes && tg.disableVerticalSwipes(); } catch (_) {}
    tg.BackButton && tg.BackButton.onClick(back);
  }
  try {
    const b = await api('/bootstrap');
    S.user = b.user; S.stats = b.stats; S.games = b.games; S.savedIds = b.saved_ids; S.recent = b.recent_orders;
    S.support = b.support_username; S.provider = b.payment_provider;
    lang = b.user.lang || 'uz';
    document.documentElement.lang = lang;
    $('#app').hidden = false;
    render();
  } catch (e) {
    $('#app').hidden = false;
    $('#view').innerHTML = `<div class="errbox"><h3>${t(e.status === 401 ? 'open_in_tg' : 'err_generic')}</h3><p>${t(e.status === 401 ? 'open_in_tg_sub' : 'err_generic_sub')}</p></div>`;
  }
  setTimeout(() => { const s = $('#splash'); s.classList.add('out'); setTimeout(() => s.remove(), 450); }, 350);
}
boot();
