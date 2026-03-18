"""
Decomposed browser perception JS scripts.

Splits the monolithic semantic_snapshot JS blob into independent sub-scripts
that can be executed individually with per-script error isolation.
"""

from typing import Any, Dict, List, Optional


# ── Shared JS utility functions (prepended to each sub-script) ──────────

SCRIPT_COMMON_UTILS = r"""
const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim();
const cleanHost = (v) => String(v || '').replace(/^www\./, '').toLowerCase();

const isVisible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  if (!style || style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};

const isDisabled = (el) => {
  if (!el) return true;
  const ad = normalize(el.getAttribute('aria-disabled') || '').toLowerCase();
  return !!(el.disabled || ad === 'true' || el.classList.contains('disabled') || el.classList.contains('is-disabled'));
};

const selectorOf = (el) => {
  if (!el) return '';
  const stableAttrs = ['data-testid', 'data-id', 'data-cy', 'data-qa', 'data-test'];
  for (const attr of stableAttrs) {
    const v = el.getAttribute(attr);
    if (v) return `[${attr}="${CSS.escape(v)}"]`;
  }
  if (el.id) return `#${CSS.escape(el.id)}`;
  const name = el.getAttribute('name');
  if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
  const ph = el.getAttribute('placeholder');
  if (ph) return `${el.tagName.toLowerCase()}[placeholder="${CSS.escape(ph)}"]`;
  const href = el.getAttribute('href');
  if (href && href.length <= 200) return `${el.tagName.toLowerCase()}[href="${CSS.escape(href)}"]`;
  const parts = [];
  let cur = el;
  while (cur && cur.nodeType === 1 && parts.length < 5) {
    let part = cur.tagName.toLowerCase();
    const p = cur.parentElement;
    if (p) {
      const sibs = Array.from(p.children).filter(c => c.tagName === cur.tagName);
      if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
    }
    parts.unshift(part);
    cur = p;
  }
  return parts.join(' > ');
};

const labelOf = (el) => {
  if (!el) return '';
  if (el.labels && el.labels.length) return normalize(Array.from(el.labels).map(l => l.innerText || l.textContent || '').join(' '));
  const id = el.getAttribute('id');
  if (id) { const lab = document.querySelector(`label[for="${id}"]`); if (lab) return normalize(lab.innerText || lab.textContent || ''); }
  const pLab = el.closest('label');
  return pLab ? normalize(pLab.innerText || pLab.textContent || '') : '';
};

const roleOf = (el) => {
  const r = normalize(el.getAttribute('role') || '').toLowerCase();
  if (r) return r;
  const tag = el.tagName.toLowerCase();
  const t = normalize(el.getAttribute('type') || '').toLowerCase();
  if (tag === 'a') return 'link';
  if (tag === 'button') return 'button';
  if (tag === 'select') return 'combobox';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'input') {
    if (['submit','button','reset'].includes(t)) return 'button';
    if (t === 'search') return 'searchbox';
    if (t === 'checkbox') return 'checkbox';
    if (t === 'radio') return 'radio';
    return 'textbox';
  }
  return tag;
};

const elementTypeOf = (el) => {
  const tag = el.tagName.toLowerCase();
  const t = normalize(el.getAttribute('type') || '').toLowerCase();
  if (tag === 'a') return 'link';
  if (tag === 'button') return 'button';
  if (tag === 'select') return 'select';
  if (tag === 'textarea') return 'textarea';
  if (tag === 'input' && t) return t;
  return tag;
};

const regionOf = (el) => {
  if (!el) return 'body';
  const pairs = [
    ['main, [role="main"]', 'main'],
    ['header, [role="banner"]', 'header'],
    ['footer, [role="contentinfo"]', 'footer'],
    ['nav, [role="navigation"]', 'navigation'],
    ['aside, [role="complementary"]', 'aside'],
    ['dialog, [role="dialog"], [aria-modal="true"], .modal, .dialog', 'modal'],
    ['form', 'form'],
  ];
  for (const [sel, name] of pairs) { const r = el.closest(sel); if (r) return name; }
  return 'body';
};

const findVisibleAction = (selectors, matcher) => {
  for (const sel of selectors) {
    for (const el of Array.from(document.querySelectorAll(sel))) {
      if (!isVisible(el) || isDisabled(el)) continue;
      if (typeof matcher === 'function' && !matcher(el)) continue;
      return el;
    }
  }
  return null;
};

const currentHost = cleanHost(location.hostname || '');
const searchEngineHosts = ['google.com','bing.com','duckduckgo.com','baidu.com','sogou.com'];
const isSearchHost = searchEngineHosts.some(h => currentHost === h || currentHost.endsWith('.'+h));
"""


# ── Sub-script 1: Page metadata ─────────────────────────────────────────

SCRIPT_PAGE_META = r"""
(() => {
  try {
    """ + SCRIPT_COMMON_UTILS + r"""

    const looksLikeSearchResultsUrl = () => {
      const path = (location.pathname || '').toLowerCase();
      const params = new URLSearchParams(location.search || '');
      const hasAny = (...keys) => keys.some(k => params.has(k));
      if (currentHost === 'bing.com' || currentHost.endsWith('.bing.com')) return path.includes('/search') && hasAny('q');
      if (currentHost === 'google.com' || currentHost.endsWith('.google.com')) return path.includes('/search') && hasAny('q');
      if (currentHost === 'baidu.com' || currentHost.endsWith('.baidu.com')) return (path === '/s' || path.startsWith('/s')) && (hasAny('wd','word') || !!document.querySelector('#content_left .result, #content_left .c-container'));
      if (currentHost === 'duckduckgo.com' || currentHost.endsWith('.duckduckgo.com')) return (path === '/' || path.startsWith('/html') || path.startsWith('/lite')) && hasAny('q');
      return (path.includes('/search') && hasAny('q','query')) || !!document.querySelector('#b_results .b_algo, #search .g, #content_left .result, .results .result');
    };

    const hasModal = !!document.querySelector('dialog[open], [role="dialog"], [aria-modal="true"], .modal.show, .dialog');
    const hasPassword = !!document.querySelector('input[type="password"]');
    const forms = document.querySelectorAll('form');
    let textInputCount = 0;
    if (forms.length) {
      textInputCount = document.querySelectorAll('form input[type="text"], form input[type="email"], form input[type="tel"], form input[type="number"], form input:not([type]), form textarea, form select').length;
    }

    const inferPageType = () => {
      if (hasModal) return 'modal';
      if (isSearchHost && looksLikeSearchResultsUrl()) return 'serp';
      if (hasPassword) return 'login';
      if (forms.length && textInputCount >= 2) return 'form';
      if (document.querySelector('article h1, main h1, article time, article [datetime]')) return 'detail';
      const listCandidates = document.querySelectorAll('main li, main article, [role="main"] li, [role="main"] article, table tbody tr, [role="listitem"], [class*="card"]:not(nav *), [class*="result"]:not(nav *), [class*="item"]:not(nav *):not(li)');
      if (listCandidates.length >= 4) return 'list';
      if (document.querySelector('article, main, [role="main"]')) return 'detail';
      return 'unknown';
    };

    const blockedSignals = [];
    const urlText = `${location.pathname || ''} ${location.search || ''}`.toLowerCase();
    const titleText = normalize(document.title || '');
    const bodyText = normalize(document.body?.innerText || document.body?.textContent || '').slice(0, 2000);
    const blockedChecks = [
      ['url', /\/(sorry|captcha|verify|challenge|blocked|forbidden)/i, urlText],
      ['title', /(unusual traffic|robot check|captcha|forbidden|access denied|blocked|人机身份验证|异常流量|验证码|安全验证|访问受限)/i, titleText],
      ['body', /(unusual traffic|robot check|captcha|forbidden|access denied|blocked|人机身份验证|异常流量|验证码|安全验证|访问受限)/i, bodyText],
    ];
    for (const [kind, pattern, source] of blockedChecks) {
      const match = String(source || '').match(pattern);
      if (match && match[0]) blockedSignals.push(`${kind}:${String(match[0]).slice(0, 60)}`);
    }

    const pageType = inferPageType();
    const contentRoot = document.querySelector('main, article, [role="main"]') || document.body;
    const mainTextLen = normalize(contentRoot?.innerText || contentRoot?.textContent || '').length;
    const hasResults = !!document.querySelector('#b_results .b_algo, #search .g, #content_left .result, .results .result') || mainTextLen >= 120;

    const inferPageStage = () => {
      if (blockedSignals.length) return 'blocked';
      if (hasModal) return 'dismiss_modal';
      if (pageType === 'serp') return hasResults ? 'selecting_source' : 'searching';
      if (pageType === 'list') return hasResults ? 'extracting' : 'loading';
      if (pageType === 'detail') return mainTextLen >= 120 ? 'extracting' : 'loading';
      if (pageType === 'form' || pageType === 'login') return 'interacting';
      if (hasResults || mainTextLen >= 120) return 'extracting';
      return 'unknown';
    };

    const focused = document.activeElement;
    let focusedElement = null;
    if (focused && focused !== document.body && isVisible(focused)) {
      focusedElement = {
        tag: focused.tagName.toLowerCase(),
        type: normalize(focused.getAttribute('type') || '').toLowerCase(),
        selector: selectorOf(focused),
        placeholder: normalize(focused.getAttribute('placeholder') || ''),
        role: normalize(focused.getAttribute('role') || ''),
      };
    }

    return {
      url: location.href,
      title: document.title || '',
      page_type: pageType,
      page_stage: inferPageStage(),
      has_modal: hasModal,
      blocked_signals: blockedSignals,
      focused_element: focusedElement,
      is_search_host: isSearchHost,
    };
  } catch (e) {
    return { url: location.href, title: document.title || '', page_type: 'unknown', page_stage: 'unknown', has_modal: false, blocked_signals: [], focused_element: null, is_search_host: false, error: String(e) };
  }
})()
"""


# ── Sub-script 2: Regions ────────────────────────────────────────────────

SCRIPT_REGIONS = r"""
(() => {
  try {
    """ + SCRIPT_COMMON_UTILS + r"""

    const inferRegionKind = (el) => {
      if (!el) return 'section';
      if (el.matches('dialog, [role="dialog"], [aria-modal="true"], .modal, .dialog')) return 'modal';
      if (el.matches('nav, [role="navigation"]')) return 'navigation';
      if (el.matches('form')) return 'form';
      const tableRows = el.querySelectorAll('table tbody tr, tbody tr, tr').length;
      if (el.matches('table') || tableRows >= 3) return 'table';
      const listItems = el.querySelectorAll('li, article, [role="listitem"], [data-testid*="result"], .result, .item').length;
      if (el.matches('ul, ol') || listItems >= 4) return 'list';
      if (el.matches('article') || el.querySelector('h1, h2, time, [datetime]')) return 'detail';
      if (el.matches('main, [role="main"]')) return 'main';
      if (el.matches('aside, [role="complementary"]')) return 'aside';
      if (el.matches('section')) return 'section';
      return regionOf(el);
    };

    const regionMetrics = (el) => {
      const rect = el.getBoundingClientRect();
      const text = normalize(el.innerText || el.textContent || '');
      const headingNode = el.querySelector('h1, h2, h3, legend, caption, th');
      const heading = normalize(headingNode?.innerText || headingNode?.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
      const listItems = Array.from(el.querySelectorAll('li, article, [role="listitem"], table tbody tr, tbody tr')).filter(isVisible).length;
      const links = Array.from(el.querySelectorAll('a[href]')).filter(isVisible).length;
      const controls = Array.from(el.querySelectorAll('input, textarea, select, button, [role="button"], [contenteditable="true"]')).filter(isVisible).length;
      const samples = [];
      for (const sn of Array.from(el.querySelectorAll('h1, h2, h3, li, article, p, table tbody tr, tbody tr, figcaption'))) {
        if (!isVisible(sn)) continue;
        const st = normalize(sn.innerText || sn.textContent || '');
        if (!st || samples.includes(st)) continue;
        samples.push(st.slice(0, 160));
        if (samples.length >= 3) break;
      }
      const kind = inferRegionKind(el);
      let score = Math.min(Math.round((rect.width * rect.height) / 40000), 8);
      score += Math.min(Math.round(text.length / 120), 6);
      score += Math.min(listItems, 6) + Math.min(links, 4) + Math.min(controls, 3);
      if (heading) score += 2;
      if (kind === 'detail') score += 3;
      if (kind === 'table' || kind === 'list') score += 2;
      if (kind === 'main') score += 2;
      if (kind === 'navigation') score -= 2;
      return { node: el, score, kind, selector: selectorOf(el), text_sample: text.slice(0, 320), heading: heading.slice(0, 160), text_length: text.length, item_count: listItems, link_count: links, control_count: controls, region: regionOf(el), bbox: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) }, sample_items: samples };
    };

    const rawRegions = Array.from(document.querySelectorAll('main, [role="main"], article, section, form, table, ul, ol, nav, aside, dialog[open], [role="dialog"], [aria-modal="true"]'))
      .filter(el => {
        if (!isVisible(el)) return false;
        const m = regionMetrics(el);
        if (!m.text_sample && m.control_count === 0 && m.link_count === 0) return false;
        if (m.kind === 'navigation' && m.link_count < 3) return false;
        if (m.kind === 'section' && m.text_length < 80 && m.item_count < 2) return false;
        return true;
      })
      .map(el => regionMetrics(el))
      .sort((a, b) => b.score - a.score);

    const regions = [];
    const regionEntries = [];
    for (const m of rawRegions) {
      const overlaps = regionEntries.some(ex => {
        if (!ex || !ex.node) return false;
        if (!ex.node.contains(m.node)) return false;
        if (ex.kind === m.kind) return true;
        const maxLen = Math.max(ex.text_length || 0, m.text_length || 0, 1);
        return Math.min(ex.text_length || 0, m.text_length || 0) / maxLen >= 0.75;
      });
      if (overlaps) continue;
      regionEntries.push(m);
      regions.push({ ref: `region_${regions.length + 1}`, kind: m.kind, selector: m.selector, heading: m.heading, text_sample: m.text_sample, sample_items: m.sample_items, item_count: m.item_count, link_count: m.link_count, control_count: m.control_count, region: m.region, bbox: m.bbox });
      if (regions.length >= 8) break;
    }

    return { regions };
  } catch (e) {
    return { regions: [], error: String(e) };
  }
})()
"""


# ── Sub-script 3: Interactive elements (with DOM distillation) ───────────

SCRIPT_INTERACTIVE_ELEMENTS = r"""
((args) => {
  try {
    """ + SCRIPT_COMMON_UTILS + r"""
    const maxElements = Math.max(Number(args?.max_elements || 80), 20);
    const vh = window.innerHeight || document.documentElement.clientHeight || 768;

    const rawNodes = Array.from(document.querySelectorAll(
      'a[href], button, input, textarea, select, [role="button"], [role="link"], [role="textbox"], [contenteditable="true"]'
    ));

    // DOM distillation: filter invisible/offscreen, dedupe links, cap elements
    const seenHrefs = new Map();
    const filtered = [];
    for (const el of rawNodes) {
      if (!isVisible(el)) continue;
      const style = window.getComputedStyle(el);
      if (style.opacity === '0') continue;
      const rect = el.getBoundingClientRect();
      if (rect.bottom < -50 || rect.top > vh + 200) continue;

      // Collapse duplicate links with same href + similar text
      const href = el.getAttribute('href') || '';
      if (href && el.tagName.toLowerCase() === 'a') {
        const text = normalize(el.innerText || el.textContent || '');
        const key = href + '||' + text.slice(0, 30).toLowerCase();
        if (seenHrefs.has(key)) continue;
        seenHrefs.set(key, true);
      }
      filtered.push(el);
    }

    // Prioritize viewport-visible elements (sort by distance from viewport center)
    const vcY = vh / 2;
    filtered.sort((a, b) => {
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      const da = Math.abs((ra.top + ra.height / 2) - vcY);
      const db = Math.abs((rb.top + rb.height / 2) - vcY);
      return da - db;
    });

    // Ensure at least 5 from each region
    const regionCounts = {};
    const minPerRegion = 5;
    const result = [];
    const added = new Set();

    // First pass: add elements respecting region minimums
    for (const el of filtered) {
      if (result.length >= maxElements) break;
      const reg = regionOf(el);
      regionCounts[reg] = (regionCounts[reg] || 0) + 1;
      result.push(el);
      added.add(el);
    }

    // Second pass: ensure minimum per region
    for (const el of filtered) {
      if (added.has(el)) continue;
      if (result.length >= maxElements + 20) break;
      const reg = regionOf(el);
      if ((regionCounts[reg] || 0) < minPerRegion) {
        result.push(el);
        added.add(el);
        regionCounts[reg] = (regionCounts[reg] || 0) + 1;
      }
    }

    const elements = result.slice(0, maxElements).map((el, idx) => {
      const rect = el.getBoundingClientRect();
      const text = normalize(
        el.innerText || el.textContent || el.value ||
        el.getAttribute('aria-label') || el.getAttribute('title') ||
        el.getAttribute('placeholder') || ''
      ).slice(0, 220);
      return {
        ref: `el_${idx + 1}`,
        role: roleOf(el),
        tag: el.tagName.toLowerCase(),
        type: elementTypeOf(el),
        text,
        href: el.href || el.getAttribute('href') || '',
        value: typeof el.value === 'string' ? String(el.value || '').slice(0, 220) : '',
        label: labelOf(el).slice(0, 220),
        placeholder: normalize(el.getAttribute('placeholder') || '').slice(0, 220),
        selector: selectorOf(el),
        visible: true,
        enabled: !el.disabled,
        region: regionOf(el),
        parent_ref: '',
        bbox: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) },
      };
    });

    return { elements, total_before_filter: rawNodes.length, total_after_filter: result.length };
  } catch (e) {
    return { elements: [], total_before_filter: 0, total_after_filter: 0, error: String(e) };
  }
})
"""


# ── Sub-script 4: Content cards & collections ────────────────────────────

SCRIPT_CONTENT_CARDS = r"""
((args) => {
  try {
    """ + SCRIPT_COMMON_UTILS + r"""
    const elementRefs = args?.elementRefs || {};

    const toAbsoluteUrl = (v) => {
      const t = normalize(v); if (!t || /^javascript:/i.test(t)) return '';
      try { return new URL(t, location.href).toString(); } catch (_) { return ''; }
    };
    const hostOf = (v) => { try { return cleanHost(new URL(v, location.href).hostname); } catch (_) { return ''; } };
    const decodeParamValue = (v) => {
      let t = normalize(v); if (!t) return '';
      for (let i = 0; i < 2; i++) { try { const d = decodeURIComponent(t); if (d === t) break; t = d; } catch (_) { break; } }
      return /^https?:/i.test(t) ? t : '';
    };
    const extractRedirectTarget = (v) => {
      const href = toAbsoluteUrl(v); if (!href) return '';
      try { const p = new URL(href, location.href); return ['uddg','u','url','q','target','redirect','imgurl'].flatMap(k => p.searchParams.getAll(k)).map(decodeParamValue).filter(Boolean)[0] || ''; } catch (_) { return ''; }
    };
    const parseDataLog = (v) => {
      const t = normalize(v); if (!t) return '';
      try { const p = JSON.parse(t); return normalize(p.mu || p.url || p.target || p.lmu || p.land_url || (p.data && (p.data.mu || p.data.url || p.data.target)) || ''); } catch (_) { return ''; }
    };
    const isSearchIntermediaryUrl = (v) => {
      const href = toAbsoluteUrl(v); if (!href) return false;
      try {
        const p = new URL(href, location.href); const host = cleanHost(p.hostname);
        if (!host || host !== currentHost) return false;
        const path = (p.pathname || '').toLowerCase();
        const params = new Set(Array.from(p.searchParams.keys()).map(k => String(k||'').trim().toLowerCase()));
        if (path === '/s' && (params.has('wd') || params.has('word'))) return true;
        if (path.startsWith('/link') || path.startsWith('/url') || path.startsWith('/ck/a')) return true;
        return path.includes('/search') && (params.has('q') || params.has('query') || params.has('wd') || params.has('word'));
      } catch (_) { return false; }
    };

    const resolveSearchResultUrl = (container, anchor) => {
      const rawHref = toAbsoluteUrl(anchor?.href || anchor?.getAttribute('href') || '');
      const candidates = [
        extractRedirectTarget(rawHref),
        anchor?.getAttribute('mu'), anchor?.getAttribute('data-landurl'), anchor?.getAttribute('data-url'), anchor?.getAttribute('data-target'),
        container?.getAttribute('mu'), container?.getAttribute('data-landurl'), container?.getAttribute('data-url'), container?.getAttribute('data-target'),
        parseDataLog(anchor?.getAttribute('data-log') || ''), parseDataLog(container?.getAttribute('data-log') || ''),
      ].map(toAbsoluteUrl).filter(Boolean);
      const ext = candidates.find(v => { const h = hostOf(v); return h && h !== currentHost; }) || '';
      return { rawHref, targetUrl: ext || candidates[0] || '', link: ext || candidates[0] || rawHref };
    };

    const cards = [];
    if (isSearchHost) {
      const selectorMap = {
        'bing.com': ['#b_results li.b_algo', '#b_results li.b_ans', '#b_results .b_algo', '.b_algo'],
        'google.com': ['#search .tF2Cxc', '#search .g', '#search .MjjYud', '[data-sokoban-container]'],
        'baidu.com': ['#content_left .result', '#content_left .c-container', '#content_left .result-op', '#content_left .xpath-log'],
        'duckduckgo.com': ['.results .result', '.result', '.result__body', '[data-testid="result"]'],
        'sogou.com': ['.results .vrwrap', '.results .rb', '.results .fb', '.vrwrap', '.rb'],
      };
      let selectors = ['main a[href]'];
      for (const [host, vals] of Object.entries(selectorMap)) {
        if (currentHost === host || currentHost.endsWith('.'+host)) { selectors = vals; break; }
      }

      const seen = new Set();
      let rank = 0;
      const buildCard = (container, anchor) => {
        if (!container || !anchor || !isVisible(container) || !isVisible(anchor)) return false;
        const resolved = resolveSearchResultUrl(container, anchor);
        const href = resolved.link; const rawHref = resolved.rawHref || href;
        if (!href || /^javascript:/i.test(href)) return false;
        const host = hostOf(href); if (!host) return false;
        if (host === currentHost && !resolved.targetUrl && !isSearchIntermediaryUrl(rawHref)) return false;
        const titleNode = container.querySelector('h1, h2, h3') || anchor;
        const title = normalize(titleNode?.innerText || titleNode?.textContent || anchor.getAttribute('aria-label') || anchor.getAttribute('title') || '');
        if (title.length < 3) return false;
        const snippetNode = container.querySelector('.b_caption p, .snippet, .st, .c-abstract, .compText, p, [data-testid="result-snippet"]');
        const sourceNode = container.querySelector('cite, .cite, .b_attribution, .source, .news-source, [data-testid="result-source"]');
        const dateNode = container.querySelector('time, .news-date, .timestamp, .date');
        let snippet = normalize(snippetNode?.innerText || snippetNode?.textContent || '');
        if (!snippet) snippet = normalize((container.innerText || container.textContent || '').replace(title, ''));
        const source = normalize(sourceNode?.innerText || sourceNode?.textContent || '');
        const date = normalize(dateNode?.innerText || dateNode?.textContent || '');
        const key = `${title}|${resolved.targetUrl || href}`;
        if (seen.has(key)) return false; seen.add(key);
        rank++;
        cards.push({ ref: `card_${rank}`, card_type: 'search_result', title: title.slice(0, 240), source: source.slice(0, 120), snippet: snippet.slice(0, 400), date: date.slice(0, 80), host, link: href, raw_link: rawHref, target_url: resolved.targetUrl, rank, target_ref: '', target_selector: selectorOf(anchor) });
        return true;
      };

      const containers = Array.from(document.querySelectorAll(selectors.join(', ')));
      for (const c of containers) {
        if (!isVisible(c)) continue;
        const anchor = c.matches('a[href]') ? c : c.querySelector('h2 a, h3 a, a[href]');
        if (buildCard(c, anchor) && cards.length >= 10) break;
      }

      if (!cards.length) {
        const fallback = Array.from(document.querySelectorAll('main li, main article, main section, main div, [role="main"] li, [role="main"] article, [role="main"] section, [role="main"] div'));
        for (const c of fallback) {
          if (!isVisible(c) || c.closest('nav, header, footer, aside, form, dialog, [role="dialog"], [aria-modal="true"]')) continue;
          if (normalize(c.innerText || c.textContent || '').length < 12) continue;
          const anchor = Array.from(c.querySelectorAll('a[href]')).find(a => { if (!isVisible(a)) return false; const t = normalize(a.innerText || a.textContent || a.getAttribute('aria-label') || a.getAttribute('title') || ''); if (t.length < 3) return false; const r = resolveSearchResultUrl(c, a); return !!r.link && !!hostOf(r.link); });
          if (buildCard(c, anchor) && cards.length >= 10) break;
        }
      }
    }

    // Collections
    const buildCollection = (kind, nodes, prefix) => {
      const vis = nodes.filter(isVisible); if (!vis.length) return null;
      const samples = [];
      for (const n of vis) { const t = normalize(n.innerText || n.textContent || ''); if (t && !samples.includes(t)) { samples.push(t.slice(0, 200)); } if (samples.length >= 5) break; }
      return { ref: `${prefix}_${kind}`, kind, item_count: vis.length, sample_items: samples };
    };

    const collections = [];
    const tableRows = Array.from(document.querySelectorAll('main table tbody tr, [role="main"] table tbody tr, table tbody tr')).filter(isVisible);
    if (tableRows.length >= 2) { const c = buildCollection('table', tableRows, 'collection_1'); if (c) collections.push(c); }
    const listItems = Array.from(document.querySelectorAll('main li, article li, [role="main"] li, section li, main article, [role="main"] article')).filter(isVisible);
    if (listItems.length >= 4) { const c = buildCollection('list', listItems, 'collection_2'); if (c) collections.push(c); }
    if (collections.length < 3) {
      const cardRoot = document.querySelector('main, article, [role="main"]') || document.body;
      const cardSels = ['[role="listitem"]', '[class*="card"]:not(nav [class*="card"])', '[class*="item"]:not(nav [class*="item"]):not(li)', '[class*="result"]:not(nav [class*="result"])', '[class*="post"]:not(nav [class*="post"])', '[class*="entry"]:not(nav [class*="entry"])'];
      for (const cs of cardSels) {
        if (collections.length >= 3) break;
        const cn = Array.from(cardRoot.querySelectorAll(cs)).filter(n => { if (!isVisible(n)) return false; const t = normalize(n.innerText || n.textContent || ''); return t.length >= 20 && t.length < 2000; });
        if (cn.length >= 3) { const c = buildCollection('cards', cn, `collection_${collections.length+1}`); if (c) collections.push(c); }
      }
    }

    return { cards, collections };
  } catch (e) {
    return { cards: [], collections: [], error: String(e) };
  }
})
"""


# ── Sub-script 5: Text content & headings ────────────────────────────────

SCRIPT_TEXT_CONTENT = r"""
(() => {
  try {
    """ + SCRIPT_COMMON_UTILS + r"""

    const contentRoot = document.querySelector('main, article, [role="main"]') || document.body;
    const mainText = normalize(
      contentRoot ? (contentRoot.innerText || contentRoot.textContent || document.body?.innerText || document.body?.textContent || '')
                  : (document.body?.innerText || document.body?.textContent || '')
    ).slice(0, 6000);

    // Headings
    const headings = [];
    document.querySelectorAll('h1, h2, h3').forEach(h => {
      const text = (h.textContent || '').trim();
      if (text && text.length < 200) {
        headings.push({ level: h.tagName.toLowerCase(), text });
      }
    });

    // Visible text blocks
    const visibleTextBlocks = [];
    const seenTexts = new Set();
    const blockNodes = Array.from((contentRoot || document.body).querySelectorAll(
      'h1, h2, h3, h4, p, li, article, section, table tbody tr, tbody tr, dd, dt, figcaption, blockquote, [class*="content"], [class*="summary"], [class*="desc"]'
    ));
    for (const node of blockNodes) {
      if (!isVisible(node)) continue;
      const text = normalize(node.innerText || node.textContent || '');
      if (!text || text.length < (isSearchHost ? 6 : 16)) continue;
      if (seenTexts.has(text)) continue;
      seenTexts.add(text);
      visibleTextBlocks.push({
        kind: node.tagName.toLowerCase(),
        text: text.slice(0, 320),
        selector: selectorOf(node),
        parent_ref: '',
      });
      if (visibleTextBlocks.length >= 16) break;
    }

    return { main_text: mainText, visible_text_blocks: visibleTextBlocks, headings: headings.slice(0, 10) };
  } catch (e) {
    return { main_text: '', visible_text_blocks: [], headings: [], error: String(e) };
  }
})()
"""


# ── Sub-script 6: Controls & affordances ─────────────────────────────────

SCRIPT_CONTROLS = r"""
(() => {
  try {
    """ + SCRIPT_COMMON_UTILS + r"""

    const nextPageEl = findVisibleAction(
      ['a[rel="next"]', 'button[rel="next"]', 'a[aria-label*="next" i]', 'button[aria-label*="next" i]', 'a[aria-label*="下一页"]', 'button[aria-label*="下一页"]', '.pagination a', '.pagination button', '.pager a', '.pager button', '[class*="pagination"] a', '[class*="pagination"] button', '[class*="pager"] a', '[class*="pager"] button'],
      (el) => /^(next|>|»|下一页|下页)$/i.test(normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || '')) || /next|下一页|pager-next|pagination-next/i.test(`${normalize(el.className || '')} ${normalize(el.getAttribute('aria-label') || '')}`)
    );
    const loadMoreEl = findVisibleAction(
      ['button', 'a', '[role="button"]'],
      (el) => /(load more|show more|view more|more results|加载更多|查看更多|更多|展开更多)/i.test(normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
    );
    const searchInputEl = findVisibleAction(['input[type="search"]', 'input[name*="search" i]', 'input[placeholder*="search" i]', 'input[placeholder*="搜索"]']);
    const modalRoot = findVisibleAction(['dialog[open]', '[role="dialog"]', '[aria-modal="true"]', '.modal.show', '.dialog', '[class*="modal"]', '[class*="dialog"]']);

    const findModalAction = (patterns) => {
      if (!modalRoot) return null;
      const candidates = Array.from(modalRoot.querySelectorAll('button, a[href], [role="button"], input[type="button"], input[type="submit"]'));
      for (const el of candidates) {
        if (!isVisible(el) || isDisabled(el)) continue;
        const text = normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('value') || '');
        if (!text) continue;
        if (patterns.some(p => p.test(text))) return el;
      }
      return null;
    };

    const modalPrimaryEl = findModalAction([/accept/i, /agree/i, /allow/i, /continue/i, /ok/i, /okay/i, /got it/i, /同意/, /接受/, /允许/, /继续/, /确定/, /好的/, /知道了/]);
    const modalSecondaryEl = findModalAction([/reject/i, /decline/i, /deny/i, /not now/i, /skip/i, /later/i, /拒绝/, /暂不/, /稍后/, /跳过/, /关闭/, /取消/]);
    const modalCloseEl = findModalAction([/^×$/, /^x$/i, /close/i, /dismiss/i, /cancel/i, /关闭/, /取消/, /知道了/]);

    const controls = [];
    const reg = (kind, el) => {
      if (!el) return;
      controls.push({ ref: `ctl_${kind}`, kind, text: normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').slice(0, 120), selector: selectorOf(el) });
    };
    reg('next_page', nextPageEl);
    reg('load_more', loadMoreEl);
    reg('search_input', searchInputEl);
    reg('modal_primary', modalPrimaryEl);
    reg('modal_secondary', modalSecondaryEl);
    reg('modal_close', modalCloseEl);

    const hasPagination = !!nextPageEl || !!document.querySelector('.pagination, .pager, [class*="page-"], a[href*="page="]');

    return {
      controls,
      affordances: {
        has_search_box: !!searchInputEl,
        search_input_ref: searchInputEl ? 'ctl_search_input' : '',
        search_input_selector: searchInputEl ? selectorOf(searchInputEl) : '',
        has_pagination: hasPagination,
        next_page_ref: nextPageEl ? 'ctl_next_page' : '',
        next_page_selector: nextPageEl ? selectorOf(nextPageEl) : '',
        has_load_more: !!loadMoreEl,
        load_more_ref: loadMoreEl ? 'ctl_load_more' : '',
        load_more_selector: loadMoreEl ? selectorOf(loadMoreEl) : '',
        has_modal: !!modalRoot,
        modal_primary_ref: modalPrimaryEl ? 'ctl_modal_primary' : '',
        modal_primary_selector: modalPrimaryEl ? selectorOf(modalPrimaryEl) : '',
        modal_secondary_ref: modalSecondaryEl ? 'ctl_modal_secondary' : '',
        modal_secondary_selector: modalSecondaryEl ? selectorOf(modalSecondaryEl) : '',
        modal_close_ref: modalCloseEl ? 'ctl_modal_close' : '',
        modal_close_selector: modalCloseEl ? selectorOf(modalCloseEl) : '',
        has_login_form: !!document.querySelector('input[type="password"]'),
        has_results: false,
        collection_item_count: 0,
      },
    };
  } catch (e) {
    return { controls: [], affordances: {}, error: String(e) };
  }
})()
"""


# ── Assembly function ────────────────────────────────────────────────────

def assemble_semantic_snapshot(
    page_meta: Optional[Dict[str, Any]] = None,
    regions: Optional[Dict[str, Any]] = None,
    elements: Optional[Dict[str, Any]] = None,
    cards_and_collections: Optional[Dict[str, Any]] = None,
    text_content: Optional[Dict[str, Any]] = None,
    controls: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge sub-script results into the existing semantic snapshot format."""
    meta = page_meta or {}
    reg = regions or {}
    elems = elements or {}
    cc = cards_and_collections or {}
    txt = text_content or {}
    ctrl = controls or {}

    cards_list = cc.get("cards") or []
    collections_list = cc.get("collections") or []
    elements_list = elems.get("elements") or []
    affordances = ctrl.get("affordances") or {}

    # Compute derived affordance fields
    collection_item_count = 0
    for col in collections_list:
        count = int(col.get("item_count", 0) or 0)
        if count > collection_item_count:
            collection_item_count = count
    if len(cards_list) > collection_item_count:
        collection_item_count = len(cards_list)

    has_results = len(cards_list) > 0 or collection_item_count > 0
    affordances["has_results"] = has_results
    affordances["collection_item_count"] = collection_item_count

    return {
        "url": meta.get("url") or "",
        "title": meta.get("title") or "",
        "page_type": meta.get("page_type") or "unknown",
        "page_stage": meta.get("page_stage") or "unknown",
        "main_text": txt.get("main_text") or "",
        "visible_text_blocks": txt.get("visible_text_blocks") or [],
        "headings": txt.get("headings") or [],
        "blocked_signals": meta.get("blocked_signals") or [],
        "regions": reg.get("regions") or [],
        "elements": elements_list,
        "cards": cards_list,
        "collections": collections_list,
        "controls": ctrl.get("controls") or [],
        "affordances": affordances,
        "focused_element": meta.get("focused_element"),
        "is_search_host": meta.get("is_search_host", False),
        # Diagnostics
        "_element_count_before_filter": elems.get("total_before_filter", 0),
        "_element_count_after_filter": elems.get("total_after_filter", 0),
    }
