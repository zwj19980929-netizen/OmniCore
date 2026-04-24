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

// Shadow DOM deep-traversal helpers (depth-limited, optimised for custom elements)
const querySelectorAllDeep = (selector, root, depth) => {
  if ((depth = depth || 0) > 10) return [];
  root = root || document;
  const results = Array.from(root.querySelectorAll(selector));
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) {
      results.push(...querySelectorAllDeep(selector, el.shadowRoot, depth + 1));
    }
  }
  return results;
};
const querySelectorDeep = (selector, root, depth) => {
  if ((depth = depth || 0) > 10) return null;
  root = root || document;
  const found = root.querySelector(selector);
  if (found) return found;
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) {
      const inner = querySelectorDeep(selector, el.shadowRoot, depth + 1);
      if (inner) return inner;
    }
  }
  return null;
};
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

    const modalSelectors = ['dialog[open]', '[role="dialog"]', '[aria-modal="true"]', '.modal.show'];
    const hasModal = modalSelectors.some(sel => {
      const el = document.querySelector(sel);
      return el && isVisible(el);
    });
    const hasPassword = !!document.querySelector('input[type="password"]');
    const forms = document.querySelectorAll('form');
    let textInputCount = 0;
    if (forms.length) {
      textInputCount = document.querySelectorAll('form input[type="text"], form input[type="email"], form input[type="tel"], form input[type="number"], form input:not([type]), form textarea, form select').length;
    }

    // Detect list-like content on page (used by multiple checks below)
    const mainContent = document.querySelector('main, article, [role="main"]');
    const mainContentLen = mainContent ? normalize(mainContent.innerText || mainContent.textContent || '').length : 0;
    const listCandidates = document.querySelectorAll('main li, main article, [role="main"] li, [role="main"] article, table tbody tr, [role="listitem"], [class*="card"]:not(nav *), [class*="result"]:not(nav *), [class*="item"]:not(nav *):not(li)');
    const hasListContent = listCandidates.length >= 4;

    const inferPageType = () => {
      // Search results: search engine host OR any site with search URL pattern + list content
      const urlLooksLikeSearch = looksLikeSearchResultsUrl();
      if (isSearchHost && urlLooksLikeSearch) return 'serp';
      if (!isSearchHost && urlLooksLikeSearch && hasListContent) return 'serp';
      if (hasPassword) return 'login';
      // Modal: only if no substantial content behind it
      if (hasModal && mainContentLen < __MODAL_THRESHOLD__) return 'modal';
      // Detail page: has article heading or datetime
      if (mainContent && document.querySelector('article h1, main h1, article time, article [datetime]')) return 'detail';
      // List page: has 4+ list-like items (check BEFORE form, since search results pages have filter forms)
      if (hasListContent) return 'list';
      // Form: only if no list content (a page with forms + list items is a filtered list, not a form)
      if (forms.length && textInputCount >= 2) return 'form';
      if (mainContent) return 'detail';
      // Fallback: pages without semantic elements (main/article/[role=main]) but with
      // substantial body text should be treated as detail pages, not unknown.
      const bodyLen = normalize(document.body?.innerText || document.body?.textContent || '').length;
      if (bodyLen >= 200) return 'detail';
      if (hasModal) return 'modal';
      return 'unknown';
    };

    const blockedSignals = [];
    const urlText = `${location.pathname || ''} ${location.search || ''}`.toLowerCase();
    const titleText = normalize(document.title || '');
    const bodyText = normalize(document.body?.innerText || document.body?.textContent || '').slice(0, 2000);
    const blockedChecks = [
      ['url', /\/(sorry|captcha|verify|challenge|blocked|forbidden)/i, urlText],
      ['title', /(unusual traffic|robot check|captcha|forbidden|access denied|blocked|人机身份验证|异常流量|验证码|安全验证|访问受限|请解决以下难题)/i, titleText],
      ['body', /(unusual traffic|robot check|captcha|forbidden|access denied|blocked|人机身份验证|异常流量|验证码|安全验证|访问受限|请解决以下难题)/i, bodyText],
    ];
    for (const [kind, pattern, source] of blockedChecks) {
      const match = String(source || '').match(pattern);
      if (match && match[0]) blockedSignals.push(`${kind}:${String(match[0]).slice(0, 60)}`);
    }

    const pageType = inferPageType();
    const contentRoot = document.querySelector('main, article, [role="main"]') || document.body;
    const mainTextLen = normalize(contentRoot?.innerText || contentRoot?.textContent || '').length;
    const hasResults = hasListContent || !!document.querySelector('#b_results .b_algo, #search .g, #content_left .result, .results .result') || mainTextLen >= 120;

    const inferPageStage = () => {
      if (blockedSignals.length) return 'blocked';
      if (pageType === 'modal') return 'dismiss_modal';
      if (pageType === 'serp') return hasResults ? 'selecting_source' : 'searching';
      if (pageType === 'list') return hasResults ? 'extracting' : 'loading';
      if (pageType === 'detail') return mainTextLen >= 120 ? 'extracting' : 'loading';
      if (pageType === 'form' || pageType === 'login') return 'interacting';
      if (hasResults || mainTextLen >= 120) return 'extracting';
      if (hasModal) return 'dismiss_modal';
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
      .reduce((acc, el) => {
        if (!isVisible(el)) return acc;
        const m = regionMetrics(el);
        if (!m.text_sample && m.control_count === 0 && m.link_count === 0) return acc;
        if (m.kind === 'navigation' && m.link_count < 3) return acc;
        if (m.kind === 'section' && m.text_length < 80 && m.item_count < 2) return acc;
        acc.push(m);
        return acc;
      }, [])
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

      // ARIA dynamic state signals
      const ariaState = {};
      const expanded = el.getAttribute('aria-expanded');
      if (expanded !== null) ariaState.expanded = expanded === 'true';
      const selected = el.getAttribute('aria-selected');
      if (selected !== null) ariaState.selected = selected === 'true';
      const checked = el.getAttribute('aria-checked');
      if (checked !== null) ariaState.checked = checked === 'true';
      const pressed = el.getAttribute('aria-pressed');
      if (pressed !== null) ariaState.pressed = pressed === 'true';
      const current = el.getAttribute('aria-current');
      if (current && current !== 'false') ariaState.current = current;
      const busy = el.getAttribute('aria-busy');
      if (busy === 'true') ariaState.busy = true;
      const live = el.getAttribute('aria-live');
      if (live && live !== 'off') ariaState.live = live;

      // Form state signals
      const formState = {};
      if (el.required || el.getAttribute('aria-required') === 'true') formState.required = true;
      if (el.validity && !el.validity.valid) formState.invalid = true;
      const errMsg = el.getAttribute('aria-errormessage') || el.getAttribute('aria-describedby');
      if (errMsg && formState.invalid) {
        const errEl = document.getElementById(errMsg);
        if (errEl) formState.error = normalize(errEl.textContent || '').slice(0, 100);
      }
      if (el.readOnly) formState.readOnly = true;
      // Fieldset/legend grouping
      const fieldset = el.closest('fieldset');
      if (fieldset) {
        const legend = fieldset.querySelector('legend');
        if (legend) formState.group = normalize(legend.textContent || '').slice(0, 80);
      }

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
        aria_state: Object.keys(ariaState).length ? ariaState : undefined,
        form_state: Object.keys(formState).length ? formState : undefined,
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
    const seen = new Set();
    let rank = 0;

    // Detect if current page looks like a search results page (any site)
    const urlPath = (location.pathname || '').toLowerCase();
    const urlParams = new URLSearchParams(location.search || '');
    const looksLikeSearchPage = isSearchHost ||
      (urlPath.includes('/search') && (urlParams.has('q') || urlParams.has('query')));

    const buildCard = (container, anchor, allowSameHost) => {
      if (!container || !anchor || !isVisible(container) || !isVisible(anchor)) return false;
      const resolved = resolveSearchResultUrl(container, anchor);
      const href = resolved.link; const rawHref = resolved.rawHref || href;
      if (!href || /^javascript:/i.test(href)) return false;
      const host = hostOf(href); if (!host) return false;
      // For search engines, skip same-host links (results should point externally)
      // For other sites (e.g. GitHub search), same-host results are normal
      if (!allowSameHost && host === currentHost && !resolved.targetUrl && !isSearchIntermediaryUrl(rawHref)) return false;
      const titleNode = container.querySelector('h1, h2, h3, h4') || anchor;
      const title = normalize(titleNode?.innerText || titleNode?.textContent || anchor.getAttribute('aria-label') || anchor.getAttribute('title') || '');
      if (title.length < 3) return false;
      const snippetNode = container.querySelector('.b_caption p, .snippet, .st, .c-abstract, .compText, p, [data-testid="result-snippet"]');
      const sourceNode = container.querySelector('cite, .cite, .b_attribution, .source, .news-source, [data-testid="result-source"]');
      const dateNode = container.querySelector('time, .news-date, .timestamp, .date, [datetime]');
      let snippet = normalize(snippetNode?.innerText || snippetNode?.textContent || '');
      if (!snippet) snippet = normalize((container.innerText || container.textContent || '').replace(title, ''));
      const source = normalize(sourceNode?.innerText || sourceNode?.textContent || '');
      const date = normalize(dateNode?.innerText || dateNode?.textContent || '');
      const key = `${title}|${resolved.targetUrl || href}`;
      if (seen.has(key)) return false; seen.add(key);
      rank++;
      cards.push({ ref: `card_${rank}`, card_type: 'search_result', title: title.slice(0, __CARD_TITLE_CHARS__), source: source.slice(0, __CARD_SOURCE_CHARS__), snippet: snippet.slice(0, __CARD_SNIPPET_CHARS__), date: date.slice(0, 80), host, link: href, raw_link: rawHref, target_url: resolved.targetUrl, rank, target_ref: '', target_selector: selectorOf(anchor) });
      return true;
    };

    // Phase 0: Inline answer boxes (weather, knowledge panels, calculators, etc.)
    // Generic heuristic: find top-level result blocks whose links are mostly
    // same-host (= not organic results pointing to external sites). These are
    // typically the search engine's own answer widgets.
    if (isSearchHost) {
      const resultsRoot = document.querySelector('main, [role="main"], #b_results, #search, #content_left, .results') || document.body;
      // Grab direct children or shallow containers that look like result items
      const candidates = Array.from(resultsRoot.querySelectorAll(':scope > *, :scope > * > *'));
      for (const box of candidates) {
        if (!isVisible(box)) continue;
        // Skip tiny or navigation elements
        const rect = box.getBoundingClientRect();
        if (rect.height < 40 || rect.width < 200) continue;
        if (box.closest('nav, header, footer, aside')) continue;

        const text = normalize(box.innerText || box.textContent || '');
        if (text.length < 30 || text.length > 4000) continue;

        // Heuristic: count links — answer boxes have few/no external links
        const anchors = Array.from(box.querySelectorAll('a[href]'));
        const externalCount = anchors.filter(a => {
          try { return cleanHost(new URL(a.href, location.href).hostname) !== currentHost; } catch (_) { return false; }
        }).length;
        // If most links are external, this is a normal result cluster, skip
        if (externalCount > 2) continue;
        // Must have meaningful content density (not just a row of links)
        const linkTextLen = anchors.reduce((s, a) => s + (a.innerText || '').trim().length, 0);
        if (linkTextLen > 0 && text.length / linkTextLen < 1.5) continue;

        // Deduplicate
        const key = `answer_box|${text.slice(0, 80)}`;
        if (seen.has(key)) continue;
        seen.add(key);
        rank++;
        cards.push({
          ref: `card_${rank}`, card_type: 'answer_box',
          title: text.slice(0, __CARD_TITLE_CHARS__),
          source: currentHost, snippet: text.slice(0, __CARD_SNIPPET_CHARS__),
          date: '', host: currentHost, link: location.href,
          raw_link: location.href, target_url: '', rank,
          target_ref: '', target_selector: selectorOf(box),
        });
        if (cards.length >= 3) break;  // answer boxes are few, cap to avoid noise
      }
    }

    // Phase 1: Search-engine-specific selectors
    if (isSearchHost) {
      const selectorMap = {
        'bing.com': ['#b_results li.b_algo', '#b_results .b_algo', '.b_algo'],
        'google.com': ['#search .tF2Cxc', '#search .g', '#search .MjjYud', '[data-sokoban-container]'],
        'baidu.com': ['#content_left .result', '#content_left .c-container', '#content_left .xpath-log'],
        'duckduckgo.com': ['.results .result', '.result', '.result__body', '[data-testid="result"]'],
        'sogou.com': ['.results .vrwrap', '.results .rb', '.results .fb', '.vrwrap', '.rb'],
      };
      let selectors = [];
      for (const [host, vals] of Object.entries(selectorMap)) {
        if (currentHost === host || currentHost.endsWith('.'+host)) { selectors = vals; break; }
      }
      if (selectors.length) {
        const containers = Array.from(document.querySelectorAll(selectors.join(', ')));
        for (const c of containers) {
          if (!isVisible(c)) continue;
          const anchor = c.matches('a[href]') ? c : c.querySelector('h2 a, h3 a, a[href]');
          if (buildCard(c, anchor, false) && cards.length >= __MAX_CARDS__) break;
        }
      }
    }

    // Phase 2: Generic fallback — for any search-like or list page
    // Also trigger on pages with list-like content (tables, repeated items) even if URL doesn't look like search
    const contentRoot2 = document.querySelector('main, [role="main"]') || document.body;
    const listCandidates2 = contentRoot2.querySelectorAll('li, article, table tbody tr, [role="listitem"], [class*="card"]:not(nav *), [class*="result"]:not(nav *), [class*="item"]:not(nav *):not(li), [class*="entry"]:not(nav *)');
    const hasListContent = listCandidates2.length >= 4;
    if (!cards.length && (looksLikeSearchPage || hasListContent)) {
      const contentRoot = document.querySelector('main, [role="main"]') || document.body;
      // Try list items, articles, sections, and divs with links inside the content area
      const fallbackSels = 'li, article, section, [role="listitem"], [class*="result"], [class*="item"]:not(li), [class*="card"], [class*="repo"], [class*="entry"], table tbody tr';
      const fallback = Array.from(contentRoot.querySelectorAll(fallbackSels));
      for (const c of fallback) {
        if (!isVisible(c)) continue;
        // Skip navigation, sidebar, filters, footer, modal elements
        if (c.closest('nav, header, footer, aside, dialog, [role="dialog"], [aria-modal="true"]')) continue;
        const text = normalize(c.innerText || c.textContent || '');
        if (text.length < 12 || text.length > 3000) continue;
        // Find a meaningful anchor within the container
        const anchor = Array.from(c.querySelectorAll('a[href]')).find(a => {
          if (!isVisible(a)) return false;
          const t = normalize(a.innerText || a.textContent || a.getAttribute('aria-label') || a.getAttribute('title') || '');
          return t.length >= 3 && !/^(sign|log|next|prev|more|load)/i.test(t);
        });
        if (anchor) {
          if (buildCard(c, anchor, true) && cards.length >= __MAX_CARDS__) break;
        } else if (hasListContent && !looksLikeSearchPage) {
          // On non-search list pages, capture anchor-less items as data cards
          const titleNode = c.querySelector('h1, h2, h3, h4, th, td:first-child, strong, b');
          const title = normalize(titleNode?.innerText || titleNode?.textContent || text.slice(0, 80));
          if (title.length >= 3) {
            const key = `${title}|${text.slice(0, 40)}`;
            if (!seen.has(key)) {
              seen.add(key);
              rank++;
              cards.push({ ref: `card_${rank}`, card_type: 'list_item', title: title.slice(0, __CARD_TITLE_CHARS__), source: currentHost, snippet: text.slice(0, __CARD_SNIPPET_CHARS__), date: '', host: currentHost, link: location.href, raw_link: location.href, target_url: '', rank, target_ref: '', target_selector: selectorOf(c) });
              if (cards.length >= __MAX_CARDS__) break;
            }
          }
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

    // Find the best content root: prefer a specific content area over the broad <main> that may include sidebars
    const findContentRoot = () => {
      // On search engine pages, use the broad main/body to capture ALL content
      // (weather widgets, answer boxes, knowledge panels, AND organic results).
      // Narrowing to a specific article/result would lose inline answer cards.
      if (isSearchHost) {
        return document.querySelector('#b_results, #search, #content_left, .results, main, [role="main"]') || document.body;
      }
      // Try specific content area selectors first (more targeted than <main>)
      const specific = document.querySelector(
        '[role="main"] > [class*="content"], main > [class*="content"], ' +
        '[role="main"] > [class*="result"], main > [class*="result"], ' +
        '[data-testid*="result"], [data-testid*="content"], ' +
        'article, [role="article"]'
      );
      if (specific && isVisible(specific)) {
        const text = normalize(specific.innerText || specific.textContent || '');
        if (text.length >= 100) return specific;
      }
      return document.querySelector('main, [role="main"]') || document.body;
    };
    const contentRoot = findContentRoot();

    // ── Structure-aware text extraction ──
    // Preserves table/list/heading structure so downstream chunking
    // and embedding can match on individual items rather than flattened blobs.
    const extractStructuredText = (root) => {
      if (!root) return '';
      const parts = [];
      const walk = (node) => {
        if (!node) return;
        // Skip invisible / script / style
        if (node.nodeType === 1) {
          const tag = node.tagName;
          if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return;
          if (!isVisible(node)) return;
        }
        // Text node
        if (node.nodeType === 3) {
          const t = (node.textContent || '').replace(/\s+/g, ' ').trim();
          if (t) parts.push(t);
          return;
        }
        if (node.nodeType !== 1) return;
        const tag = node.tagName;
        // Headings → markdown-style prefix
        if (/^H[1-4]$/.test(tag)) {
          const level = parseInt(tag.charAt(1), 10);
          const prefix = '#'.repeat(level) + ' ';
          const t = normalize(node.innerText || node.textContent || '');
          if (t) parts.push('\n' + prefix + t + '\n');
          return;  // don't recurse into heading children
        }
        // Table → tab-separated columns, newline per row
        if (tag === 'TABLE') {
          const rows = node.querySelectorAll('tr');
          for (const row of rows) {
            const cells = row.querySelectorAll('th, td');
            const cellTexts = Array.from(cells).map(c => normalize(c.innerText || c.textContent || ''));
            if (cellTexts.some(c => c.length > 0)) parts.push(cellTexts.join('\t'));
          }
          parts.push('');  // blank line after table
          return;
        }
        // Ordered list → numbered items
        if (tag === 'OL') {
          let idx = 1;
          for (const li of node.children) {
            if (li.tagName === 'LI') {
              const t = normalize(li.innerText || li.textContent || '');
              if (t) parts.push(idx + '. ' + t);
              idx++;
            }
          }
          parts.push('');
          return;
        }
        // Unordered list → dash-prefixed items
        if (tag === 'UL') {
          for (const li of node.children) {
            if (li.tagName === 'LI') {
              const t = normalize(li.innerText || li.textContent || '');
              if (t) parts.push('- ' + t);
            }
          }
          parts.push('');
          return;
        }
        // Definition list → key: value
        if (tag === 'DL') {
          let lastDt = '';
          for (const child of node.children) {
            if (child.tagName === 'DT') {
              lastDt = normalize(child.innerText || child.textContent || '');
            } else if (child.tagName === 'DD') {
              const dd = normalize(child.innerText || child.textContent || '');
              if (lastDt && dd) parts.push(lastDt + ': ' + dd);
              else if (dd) parts.push(dd);
            }
          }
          parts.push('');
          return;
        }
        // Paragraph / blockquote → double newline separation
        if (tag === 'P' || tag === 'BLOCKQUOTE') {
          const t = normalize(node.innerText || node.textContent || '');
          if (t) parts.push('\n' + t + '\n');
          return;
        }
        // Default: recurse into children
        for (const child of node.childNodes) {
          walk(child);
        }
      };
      walk(root);
      // Join and clean up excessive blank lines
      return parts.join('\n').replace(/\n{3,}/g, '\n\n').trim();
    };

    let mainText = extractStructuredText(contentRoot || document.body);
    if (!mainText) {
      // Fallback to raw innerText if structured extraction yields nothing
      mainText = normalize(
        contentRoot ? (contentRoot.innerText || contentRoot.textContent || '')
                    : (document.body?.innerText || document.body?.textContent || '')
      );
    }
    mainText = mainText.slice(0, 10000);

    // Headings with hierarchy (depth tracking)
    const headings = [];
    const depthStack = [];  // track nesting: [{level, index}]
    document.querySelectorAll('h1, h2, h3, h4').forEach(h => {
      const text = (h.textContent || '').trim();
      if (!text || text.length >= 200) return;
      const level = h.tagName.toLowerCase();
      const levelNum = parseInt(level.charAt(1), 10);
      // Pop stack until we find a parent with a smaller heading level
      while (depthStack.length > 0 && depthStack[depthStack.length - 1].levelNum >= levelNum) {
        depthStack.pop();
      }
      const depth = depthStack.length;
      const parentIdx = depthStack.length > 0 ? depthStack[depthStack.length - 1].index : -1;
      const idx = headings.length;
      headings.push({ level, text, depth, parent_index: parentIdx });
      depthStack.push({ levelNum, index: idx });
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
      if (visibleTextBlocks.length >= 24) break;
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
    const modalRoot = findVisibleAction(['dialog[open]', '[role="dialog"]', '[aria-modal="true"]', '.modal.show']);

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


def build_page_meta_script(modal_content_threshold: int = 200) -> str:
    """Return SCRIPT_PAGE_META with configurable modal content threshold injected."""
    return SCRIPT_PAGE_META.replace("__MODAL_THRESHOLD__", str(int(modal_content_threshold)))


def build_content_cards_script(
    max_cards: int = 12,
    card_title_chars: int = 240,
    card_source_chars: int = 120,
    card_snippet_chars: int = 400,
) -> str:
    """Return SCRIPT_CONTENT_CARDS with configurable extraction limits injected."""
    script = SCRIPT_CONTENT_CARDS
    script = script.replace("__MAX_CARDS__", str(int(max_cards)))
    script = script.replace("__CARD_TITLE_CHARS__", str(int(card_title_chars)))
    script = script.replace("__CARD_SOURCE_CHARS__", str(int(card_source_chars)))
    script = script.replace("__CARD_SNIPPET_CHARS__", str(int(card_snippet_chars)))
    return script


# ── Fallback semantic snapshot script (used by BrowserAgent._build_fallback_semantic_snapshot) ──

SCRIPT_FALLBACK_SEMANTIC_SNAPSHOT = r"""() => {
                const normalize = (v) => String(v || '').replace(/\s+/g, ' ').trim();
                const bodyText = document.body && document.body.innerText ? document.body.innerText : '';
                const blockCandidates = Array.from(document.querySelectorAll('main, article, section, [role="main"], [data-testid], p, h1, h2, h3, li'))
                    .map((node) => {
                        const text = normalize(node.innerText || node.textContent || '');
                        if (!text) return null;
                        const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : { width: 0, height: 0 };
                        return {
                            text: text.slice(0, 320),
                            tag: (node.tagName || '').toLowerCase(),
                            role: node.getAttribute ? (node.getAttribute('role') || '') : '',
                            width: Math.round(rect.width || 0),
                            height: Math.round(rect.height || 0),
                        };
                    })
                    .filter(Boolean)
                    .slice(0, 16);

                // 提取交互元素（简化版）
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const selectorOf = (el) => {
                    if (!el) return '';
                    if (el.id) return '#' + el.id;
                    const name = el.getAttribute('name');
                    if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
                    const ph = el.getAttribute('placeholder');
                    if (ph) return el.tagName.toLowerCase() + '[placeholder="' + ph + '"]';
                    const href = el.getAttribute('href');
                    if (href && href.length <= 120) return el.tagName.toLowerCase() + '[href="' + href + '"]';
                    return el.tagName.toLowerCase();
                };

                // Shadow DOM deep-traversal helper (inline, depth-limited)
                const querySelectorAllDeep = (selector, root, depth) => {
                    if ((depth = depth || 0) > 10) return [];
                    root = root || document;
                    const results = Array.from(root.querySelectorAll(selector));
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot) results.push(...querySelectorAllDeep(selector, el.shadowRoot, depth + 1));
                    }
                    return results;
                };
                const interactiveNodes = querySelectorAllDeep(
                    'a[href], button, input:not([type="hidden"]), textarea, select, [role="button"], [role="link"], [contenteditable="true"]'
                ).filter(isVisible).slice(0, 60);

                const elements = interactiveNodes.map((el, idx) => {
                    const tag = el.tagName.toLowerCase();
                    const inputType = normalize(el.getAttribute('type') || '').toLowerCase();
                    let role = normalize(el.getAttribute('role') || '').toLowerCase();
                    if (!role) {
                        if (tag === 'a') role = 'link';
                        else if (tag === 'button') role = 'button';
                        else if (tag === 'input') role = (inputType === 'search') ? 'searchbox' : 'textbox';
                        else if (tag === 'textarea') role = 'textbox';
                        else if (tag === 'select') role = 'combobox';
                        else role = tag;
                    }
                    return {
                        ref: 'el_' + (idx + 1),
                        role: role,
                        tag: tag,
                        type: inputType || tag,
                        text: normalize(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').slice(0, 160),
                        href: el.href || el.getAttribute('href') || '',
                        value: typeof el.value === 'string' ? String(el.value || '').slice(0, 160) : '',
                        label: '',
                        placeholder: normalize(el.getAttribute('placeholder') || '').slice(0, 120),
                        selector: selectorOf(el),
                        visible: true,
                        enabled: !el.disabled,
                        region: 'body',
                    };
                });

                // 检测 modal
                const hasModal = !!document.querySelector('dialog[open], [role="dialog"], [aria-modal="true"], .modal.show');

                // 检测搜索框
                const searchInput = document.querySelector('input[type="search"], input[name*="search"], input[placeholder*="search" i], input[placeholder*="搜索"]');
                const hasSearchBox = !!searchInput;
                const searchSelector = searchInput ? selectorOf(searchInput) : '';

                // 检测当前焦点元素（对于刚打开的 modal 很重要）
                const focused = document.activeElement;
                let focusedInfo = null;
                if (focused && focused !== document.body && isVisible(focused)) {
                    focusedInfo = {
                        tag: focused.tagName.toLowerCase(),
                        type: normalize(focused.getAttribute('type') || '').toLowerCase(),
                        selector: selectorOf(focused),
                        placeholder: normalize(focused.getAttribute('placeholder') || ''),
                        role: normalize(focused.getAttribute('role') || ''),
                    };
                }

                return {
                    bodyText: bodyText.slice(0, 6000),
                    visibleTextBlocks: blockCandidates,
                    elements: elements,
                    hasModal: hasModal,
                    hasSearchBox: hasSearchBox,
                    searchSelector: searchSelector,
                    focusedElement: focusedInfo,
                };
            }"""


# ── Interactive element extraction script (used by BrowserAgent._extract_interactive_elements) ──

SCRIPT_EXTRACT_INTERACTIVE_ELEMENTS = r"""
            () => {
              // Shadow DOM deep-traversal helper (depth-limited)
              const querySelectorAllDeep = (selector, root, depth) => {
                if ((depth = depth || 0) > 10) return [];
                root = root || document;
                const results = Array.from(root.querySelectorAll(selector));
                for (const el of root.querySelectorAll('*')) {
                  if (el.shadowRoot) results.push(...querySelectorAllDeep(selector, el.shadowRoot, depth + 1));
                }
                return results;
              };
              // 🔥 扩展选择器：同时提取交互元素和内容元素（含 Shadow DOM）
              const interactiveNodes = querySelectorAllDeep('a, button, input, textarea, select, [role="button"], [role="link"], [contenteditable="true"]');

              function textOf(el) {
                return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
              }
              function isVisible(el) {
                const rects = el.getClientRects();
                return !!(el.offsetWidth || el.offsetHeight || rects.length);
              }
              function labelOf(el) {
                if (el.labels && el.labels.length) {
                  return Array.from(el.labels).map(x => textOf(x)).filter(Boolean).join(' ');
                }
                const id = el.getAttribute('id');
                if (id) {
                  const label = document.querySelector(`label[for="${id}"]`);
                  if (label) return textOf(label);
                }
                const parent = el.closest('label');
                return parent ? textOf(parent) : '';
              }
              function selectorOf(el) {
                if (el.id) return `#${CSS.escape(el.id)}`;
                const name = el.getAttribute('name');
                if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
                const placeholder = el.getAttribute('placeholder');
                if (placeholder) return `${el.tagName.toLowerCase()}[placeholder="${CSS.escape(placeholder)}"]`;
                const parts = [];
                let cur = el;
                while (cur && cur.nodeType === 1 && parts.length < 4) {
                  let part = cur.tagName.toLowerCase();
                  const parent = cur.parentElement;
                  if (parent) {
                    const siblings = Array.from(parent.children).filter(x => x.tagName === cur.tagName);
                    if (siblings.length > 1) {
                      part += `:nth-of-type(${siblings.indexOf(cur) + 1})`;
                    }
                  }
                  parts.unshift(part);
                  cur = parent;
                }
                return parts.join(' > ');
              }
              function normalizedType(el) {
                const tag = el.tagName.toLowerCase();
                const inputType = (el.getAttribute('type') || '').toLowerCase();
                if (tag === 'a') return 'link';
                if (tag === 'button') return 'button';
                if (tag === 'input' && ['submit', 'button', 'reset'].includes(inputType)) return 'button';
                if (tag === 'input' && inputType) return inputType;
                return (inputType || tag);
              }

              // 🔥 新增：提取元素周围的上下文文本
              function extractContext(el) {
                const contextBefore = [];
                const contextAfter = [];

                // 向前查找文本节点（最多3个兄弟节点）
                let prev = el.previousSibling;
                let count = 0;
                while (prev && count < 3) {
                  if (prev.nodeType === Node.TEXT_NODE) {
                    const text = textOf(prev);
                    if (text.length > 0) {
                      contextBefore.unshift(text);
                      count++;
                    }
                  } else if (prev.nodeType === Node.ELEMENT_NODE) {
                    const text = textOf(prev);
                    if (text.length > 0 && text.length < 200) {
                      contextBefore.unshift(text);
                      count++;
                    }
                  }
                  prev = prev.previousSibling;
                }

                // 向后查找文本节点（最多3个兄弟节点）
                let next = el.nextSibling;
                count = 0;
                while (next && count < 3) {
                  if (next.nodeType === Node.TEXT_NODE) {
                    const text = textOf(next);
                    if (text.length > 0) {
                      contextAfter.push(text);
                      count++;
                    }
                  } else if (next.nodeType === Node.ELEMENT_NODE) {
                    const text = textOf(next);
                    if (text.length > 0 && text.length < 200) {
                      contextAfter.push(text);
                      count++;
                    }
                  }
                  next = next.nextSibling;
                }

                // 如果兄弟节点没有上下文，尝试从父元素提取
                if (contextBefore.length === 0 && contextAfter.length === 0) {
                  const parent = el.parentElement;
                  if (parent) {
                    const parentText = textOf(parent);
                    const elementText = textOf(el);
                    // 提取父元素中不属于当前元素的文本
                    const beforeText = parentText.split(elementText)[0];
                    const afterText = parentText.split(elementText)[1];
                    if (beforeText && beforeText.length > 0) {
                      contextBefore.push(beforeText.slice(-100));
                    }
                    if (afterText && afterText.length > 0) {
                      contextAfter.push(afterText.slice(0, 100));
                    }
                  }
                }

                return {
                  before: contextBefore.join(' ').slice(0, 150),
                  after: contextAfter.join(' ').slice(0, 150)
                };
              }

              return interactiveNodes
                .filter(el => isVisible(el))
                .slice(0, 60)
                .map((el, idx) => {
                  const context = extractContext(el);
                  return {
                    index: idx,
                    tag: el.tagName.toLowerCase(),
                    text: textOf(el).slice(0, 160),
                    element_type: normalizedType(el),
                    selector: selectorOf(el),
                    attributes: {
                      id: el.getAttribute('id') || '',
                      name: el.getAttribute('name') || '',
                      type: el.getAttribute('type') || '',
                      role: el.getAttribute('role') || '',
                      href: el.getAttribute('href') || '',
                      value: (typeof el.value === 'string' ? el.value : '') || '',
                      placeholder: el.getAttribute('placeholder') || '',
                      ariaLabel: el.getAttribute('aria-label') || '',
                      title: el.getAttribute('title') || '',
                      labelText: labelOf(el).slice(0, 120),
                    },
                    is_visible: true,
                    is_clickable: !el.disabled,
                    context_before: context.before,
                    context_after: context.after,
                  };
                });
            }
            """


# ── Deep interactive scan (fallback when DOM distillation misses a control) ──
#
# Runs a broader selector set than ``SCRIPT_INTERACTIVE_ELEMENTS`` and picks
# up controls that standard extraction skips, namely:
#   * hidden ``input[type=checkbox|radio]`` paired with a visible ``<label for=…>``
#     — the W3C-standard "label-as-affordance" pattern common across antd,
#     Material, Headless UI, and custom CSS frameworks.
#   * ARIA roles beyond the common four (``checkbox``, ``radio``, ``switch``,
#     ``tab``, ``menuitem``, ``option``, ``combobox``, ``listbox``, ``slider``).
#
# Output shape matches ``SCRIPT_INTERACTIVE_ELEMENTS`` so results can be merged
# straight into ``snapshot["elements"]``. Refs are namespaced ``deep_el_N`` to
# avoid colliding with the primary extraction.

SCRIPT_DEEP_INTERACTIVE_SCAN = r"""
((args) => {
  try {
    """ + SCRIPT_COMMON_UTILS + r"""
    const vh = window.innerHeight || document.documentElement.clientHeight || 768;
    const maxElements = Math.max(Number(args?.max_elements || 40), 10);
    const existingSelectors = new Set(Array.from(args?.existing_selectors || []));

    const labelTextFor = (el) => {
      if (el.labels && el.labels.length) {
        return normalize(Array.from(el.labels).map(l => l.innerText || l.textContent || '').join(' '));
      }
      const id = el.getAttribute('id');
      if (id) {
        const lab = document.querySelector('label[for="' + CSS.escape(id) + '"]');
        if (lab) return normalize(lab.innerText || lab.textContent || '');
      }
      const wrapperLabel = el.closest('label');
      if (wrapperLabel) return normalize(wrapperLabel.innerText || wrapperLabel.textContent || '');
      return '';
    };

    const results = [];
    const seen = new Set();

    // 1) Hidden checkbox/radio inputs — prefer the label as the clickable affordance.
    const hiddenInputs = Array.from(document.querySelectorAll(
      'input[type="checkbox"], input[type="radio"]'
    ));
    for (const input of hiddenInputs) {
      if (seen.has(input)) continue;
      const visibleInput = isVisible(input);
      const label = labelTextFor(input);
      let anchor = input;
      if (!visibleInput) {
        // Find a clickable label/ancestor that IS visible.
        const id = input.getAttribute('id');
        let candidate = null;
        if (id) {
          candidate = document.querySelector('label[for="' + CSS.escape(id) + '"]');
        }
        if (!candidate || !isVisible(candidate)) {
          candidate = input.closest('label');
        }
        if (candidate && isVisible(candidate)) {
          anchor = candidate;
        } else {
          continue; // Truly hidden and no visible affordance — skip.
        }
      }
      const rect = anchor.getBoundingClientRect();
      if (rect.bottom < -50 || rect.top > vh + 400) continue;
      const sel = selectorOf(anchor);
      if (existingSelectors.has(sel)) continue;
      seen.add(input);

      results.push({
        ref: `deep_el_${results.length + 1}`,
        role: input.type === 'radio' ? 'radio' : 'checkbox',
        tag: anchor.tagName.toLowerCase(),
        type: input.type,
        text: label,
        href: '',
        value: typeof input.value === 'string' ? String(input.value || '').slice(0, 220) : '',
        label: label.slice(0, 220),
        placeholder: '',
        selector: sel,
        visible: true,
        enabled: !input.disabled,
        region: regionOf(anchor),
        parent_ref: '',
        bbox: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) },
        aria_state: input.checked ? { checked: true } : undefined,
        form_state: input.required ? { required: true } : undefined,
        _source: 'deep_scan',
      });
      if (results.length >= maxElements) break;
    }

    if (results.length < maxElements) {
      // 2) ARIA roles the primary extractor doesn't query for.
      const auxRoles = [
        '[role="checkbox"]', '[role="radio"]', '[role="switch"]', '[role="tab"]',
        '[role="menuitem"]', '[role="option"]', '[role="combobox"]',
        '[role="listbox"]', '[role="slider"]', '[role="spinbutton"]',
      ];
      const ariaNodes = Array.from(document.querySelectorAll(auxRoles.join(', ')));
      for (const el of ariaNodes) {
        if (seen.has(el)) continue;
        if (!isVisible(el)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.bottom < -50 || rect.top > vh + 400) continue;
        const sel = selectorOf(el);
        if (existingSelectors.has(sel)) continue;
        seen.add(el);

        const text = normalize(
          el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || ''
        ).slice(0, 220);
        const role = (el.getAttribute('role') || '').toLowerCase();
        const ariaState = {};
        const checked = el.getAttribute('aria-checked');
        if (checked !== null) ariaState.checked = checked === 'true';
        const selected = el.getAttribute('aria-selected');
        if (selected !== null) ariaState.selected = selected === 'true';

        results.push({
          ref: `deep_el_${results.length + 1}`,
          role,
          tag: el.tagName.toLowerCase(),
          type: role,
          text,
          href: '',
          value: '',
          label: text,
          placeholder: '',
          selector: sel,
          visible: true,
          enabled: !el.getAttribute('aria-disabled') || el.getAttribute('aria-disabled') === 'false',
          region: regionOf(el),
          parent_ref: '',
          bbox: { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) },
          aria_state: Object.keys(ariaState).length ? ariaState : undefined,
          form_state: undefined,
          _source: 'deep_scan',
        });
        if (results.length >= maxElements) break;
      }
    }

    return { elements: results, found: results.length };
  } catch (e) {
    return { elements: [], found: 0, error: String(e) };
  }
})
"""
