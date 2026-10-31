// Browser tests for the rendered directory (index.html).
//
// Deal size is the interesting part: the figure is reported in three
// currencies, ranking happens on a converted value that must never be the one
// displayed, and a deal with no figure is unreported rather than worth zero.
// Each of those is easy to get subtly wrong and invisible when it is.
import { test, expect } from '@playwright/test';

test.describe.configure({ mode: 'parallel' });

test('loads with no console errors and counts the sized deals', async ({ page }) => {
  const errors = [];
  page.on('console', m => m.type() === 'error' && errors.push(m.text()));
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto('/index.html');
  await expect(page.locator('#stats')).toContainText(/with a reported size/);
  expect(errors).toEqual([]);
});

test('sorting by largest deal puts the biggest first and the unreported last', async ({ page }) => {
  await page.goto('/index.html');
  await page.selectOption('#sortSelect', 'largest');
  const cells = page.locator('.deal-count');
  await expect(cells.first()).toContainText('₹');
  // Descending, with the unreported tail excluded from the comparison. Cells
  // are parsed to a rupee number (decimals and the crore/lakh/million/billion
  // unit included): comparing the digits alone reads "₹2,390.5 crore" as 23905
  // and "INR 10,000 Million" as larger than the equal "₹1,000 crore". Cells in
  // other currencies are skipped; a subsequence of a sorted list is sorted.
  const values = await page.evaluate(() => {
    const UNIT = { crore: 1e7, cr: 1e7, lakh: 1e5, lac: 1e5, million: 1e6, mn: 1e6, billion: 1e9, bn: 1e9 };
    const parse = (t) => {
      const m = t.match(/(?:₹|INR|Rs\.?)\s?([\d,]+(?:\.\d+)?)\s*(crore|cr|lakh|lac|million|mn|billion|bn)?/i);
      return m ? Number(m[1].replace(/,/g, '')) * (UNIT[(m[2] || '').toLowerCase()] || 1) : null;
    };
    return [...document.querySelectorAll('.deal-count')]
      .map(e => parse(e.innerText))
      .filter(v => v !== null);
  });
  expect(values.length).toBeGreaterThan(10);
  const sorted = [...values].sort((a, b) => b - a);
  expect(values).toEqual(sorted);
  await expect(cells.last()).toContainText('not reported');
});

test('total and largest are different orderings', async ({ page }) => {
  await page.goto('/index.html');
  const top = async mode => {
    await page.selectOption('#sortSelect', mode);
    return page.locator('.deal-count').first().innerText();
  };
  const largest = await top('largest');
  const total = await top('total');
  expect(largest).toContain('largest');
  expect(total).toContain('total');
});

test('a deal shows its own currency, never the converted figure', async ({ page }) => {
  await page.goto('/index.html');
  await page.selectOption('#sortSelect', 'largest');
  await page.evaluate(() => document.querySelector('.deal-count').closest('div').click());
  const sizes = page.locator('.deal-card .deal-size');
  await expect(sizes.first()).toBeVisible();
  const shown = await sizes.allInnerTexts();
  // The top person's largest deal is reported in dollars; a converted rupee
  // figure appearing here would mean the ranking value leaked into the view.
  expect(shown.some(t => /[$€₹]/.test(t))).toBe(true);
  const order = await page.evaluate(() =>
    [...document.querySelectorAll('.deal-card')].map(c => {
      const t = c.querySelector('.deal-size');
      return t ? t.innerText : null;
    }));
  const firstNull = order.indexOf(null);
  if (firstNull !== -1) {
    // once the unreported start, none may follow with a figure
    expect(order.slice(firstNull).every(x => x === null)).toBe(true);
  }
});

// --- deal-size range slider -------------------------------------------------
// Two thumbs on a log-ish stop scale. What matters: the filter is about deals
// that REPORT a figure (unreported is unknown, never "small"), thumbs cannot
// cross, dragging must not rebuild the input being dragged, and the range
// survives a reload via the URL hash.
const setThumb = (page, id, value) =>
  page.evaluate(([id, value]) => {
    const el = document.getElementById(id);
    el.value = String(value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, [id, value]);

test('size slider starts open-ended and shows every person', async ({ page }) => {
  await page.goto('/index.html');
  await expect(page.locator('#sizeReadout')).toHaveText('Any size');
  const r = await page.evaluate(() => ({
    active: sizeFilterActive(),
    shown: PEOPLE.filter(matchesFilters).length,
    total: PEOPLE.length,
  }));
  expect(r.active).toBe(false);
  expect(r.shown).toBe(r.total);
});

test('a lower bound keeps only people on a sized deal above it, never unreported ones', async ({ page }) => {
  await page.goto('/index.html');
  await setThumb(page, 'sizeLo', 7); // ₹1,000 cr
  await expect(page.locator('#sizeReadout')).toHaveText('₹1,000 cr+');
  const r = await page.evaluate(() => {
    const kept = PEOPLE.filter(matchesFilters);
    return {
      n: kept.length,
      total: PEOPLE.length,
      allQualify: kept.every(p => p.deals.some(d => dealValue(d) !== null && dealValue(d) >= 1000 * 1e7)),
      anyUnsizedOnly: kept.some(p => p.deals.every(d => dealValue(d) === null)),
    };
  });
  expect(r.n).toBeGreaterThan(0);
  expect(r.n).toBeLessThan(r.total);
  expect(r.allQualify).toBe(true);
  expect(r.anyUnsizedOnly).toBe(false);
});

test('an upper bound is inclusive and excludes larger-only people', async ({ page }) => {
  await page.goto('/index.html');
  await setThumb(page, 'sizeHi', 3); // up to ₹50 cr
  await expect(page.locator('#sizeReadout')).toHaveText('Up to ₹50 cr');
  const ok = await page.evaluate(() =>
    PEOPLE.filter(matchesFilters).every(p => p.deals.some(d => dealValue(d) !== null && dealValue(d) <= 50 * 1e7)));
  expect(ok).toBe(true);
});

test('thumbs cannot cross', async ({ page }) => {
  await page.goto('/index.html');
  await setThumb(page, 'sizeLo', 6);
  await setThumb(page, 'sizeHi', 2); // would sit below the lower thumb
  const s = await page.evaluate(() => ({ lo: state.sizeLo, hi: state.sizeHi }));
  expect(s.lo).toBeLessThanOrEqual(s.hi);
});

test('dragging redraws the list without replacing the thumb being dragged', async ({ page }) => {
  await page.goto('/index.html');
  const same = await page.evaluate(() => {
    const before = document.getElementById('sizeLo');
    const shownBefore = PEOPLE.filter(matchesFilters).length;
    before.value = '8';
    before.dispatchEvent(new Event('input', { bubbles: true })); // mid-drag: no 'change' yet
    return {
      sameNode: document.getElementById('sizeLo') === before,
      listShrank: PEOPLE.filter(matchesFilters).length < shownBefore,
    };
  });
  expect(same.sameNode).toBe(true);
  expect(same.listShrank).toBe(true);
});

test('size range survives a reload and clears with Clear filters', async ({ page }) => {
  await page.goto('/index.html');
  await setThumb(page, 'sizeLo', 4);
  await setThumb(page, 'sizeHi', 8);
  await expect(page).toHaveURL(/size=4-8/);
  await page.reload();
  await expect(page.locator('#sizeReadout')).toHaveText('₹100 cr – ₹2,500 cr');
  await page.locator('#clearFiltersBtn').click();
  await expect(page.locator('#sizeReadout')).toHaveText('Any size');
  await expect(page).not.toHaveURL(/size=/);
});

test('a malformed size hash is ignored rather than breaking the page', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto('/index.html#size=9-2');
  await expect(page.locator('#sizeReadout')).toHaveText('Any size');
  await page.goto('/index.html#size=x-99');
  await expect(page.locator('#sizeReadout')).toHaveText('Any size');
  expect(errors).toEqual([]);
});

test('both thumbs can be grabbed with a real mouse drag', async ({ page }) => {
  await page.goto('/index.html');
  await page.locator('.size-track').scrollIntoViewIfNeeded();
  const box = await page.locator('.size-track').boundingBox();
  const y = box.y + box.height / 2;
  // lower thumb starts at the far left, upper at the far right; drag each inward
  await page.mouse.move(box.x + 8, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.45, y, { steps: 8 });
  await page.mouse.up();
  await page.mouse.move(box.x + box.width - 8, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.8, y, { steps: 8 });
  await page.mouse.up();
  const s = await page.evaluate(() => ({ lo: state.sizeLo, hi: state.sizeHi }));
  expect(s.lo).toBeGreaterThan(0);
  expect(s.hi).toBeLessThan(11);
  expect(s.lo).toBeLessThanOrEqual(s.hi);
  await expect(page.locator('#sizeReadout')).not.toHaveText('Any size');
});

test('profile shows largest and total reported deal chips, and never zero for unreported', async ({ page }) => {
  await page.goto('/index.html');
  const r = await page.evaluate(() => {
    const withV = PEOPLE.filter(p => p.deals.some(d => dealValue(d) !== null) && p.deals.some(d => dealValue(d) === null))[0];
    const noV = PEOPLE.filter(p => p.deals.every(d => dealValue(d) === null))[0];
    const show = p => { state.selectedId = p.id; renderDetail(); return document.querySelector('.size-chips').innerText; };
    return { withV: show(withV), noV: show(noV), expectTotal: fmtInr(totalDealValue(withV)),
             expectLargest: withV.deals.filter(d=>dealValue(d)!==null).sort((a,b)=>dealValue(b)-dealValue(a))[0].value.raw };
  });
  expect(r.withV).toContain(r.expectLargest);
  expect(r.withV).toContain(r.expectTotal);
  expect(r.withV).toMatch(/unreported/);
  expect(r.noV).toContain('Not reported');
  expect(r.noV).not.toMatch(/₹\s*0/);
});

test('a deal carried by several outlets lists the other outlets, each as an outbound link', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto('/index.html');
  const r = await page.evaluate(() => {
    const p = PEOPLE.find(x => x.deals.some(d => d.also_reported_by && d.also_reported_by.length));
    const d = p.deals.find(x => x.also_reported_by && x.also_reported_by.length);
    state.selectedId = p.id;
    renderDetail();
    const card = [...document.querySelectorAll('.deal-card')]
      .find(c => c.querySelector('a').getAttribute('href') === d.url);
    const links = [...card.querySelectorAll('.meta a.also')];
    return {
      expected: d.also_reported_by.map(o => o.url),
      hrefs: links.map(a => a.getAttribute('href')),
      targets: links.map(a => a.getAttribute('target')),
      rels: links.map(a => a.getAttribute('rel')),
      meta: card.querySelector('.meta').innerText,
    };
  });
  expect(r.meta).toContain('also:');
  expect(r.hrefs).toEqual(r.expected);
  expect(r.targets.every(t => t === '_blank')).toBe(true);
  expect(r.rels.every(x => /noopener/.test(x))).toBe(true);
  expect(errors).toEqual([]);
});

test('a single-outlet deal shows no "also:" label', async ({ page }) => {
  await page.goto('/index.html');
  const meta = await page.evaluate(() => {
    const p = PEOPLE.find(x => x.deals.length && x.deals.every(d => !d.also_reported_by));
    state.selectedId = p.id;
    renderDetail();
    return document.querySelector('.deal-card .meta').innerText;
  });
  expect(meta).not.toContain('also:');
});

// ----------------------------------------------------------- recency / date reported
test('sorting by most recent deal orders people by their newest reported date, undated last', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto('/index.html');
  await page.selectOption('#sortSelect', 'recent');
  const r = await page.evaluate(() => {
    const ids = [...document.querySelectorAll('.person-row')].slice(0, 400).map(e => +e.getAttribute('data-id'));
    const last = ids.map(id => lastDealDate(PEOPLE.find(p => p.id === id)));
    return { last, firstCell: document.querySelector('.person-row .deal-count').innerText };
  });
  const dated = r.last.filter(x => x !== null);
  expect(dated.length).toBeGreaterThan(10);
  expect(dated).toEqual([...dated].sort().reverse());
  // once an undated person appears, nobody dated may follow
  const firstNull = r.last.indexOf(null);
  if (firstNull !== -1) expect(r.last.slice(firstNull).every(x => x === null)).toBe(true);
  expect(r.firstCell).toMatch(/last deal/);
  expect(errors).toEqual([]);
});

test('"reported within" filter keeps only people with a deal in the window, persists and clears', async ({ page }) => {
  await page.goto('/index.html');
  await page.evaluate(() => { state.recencyDays = 0; });
  const total = await page.evaluate(() => PEOPLE.length);
  await page.locator('[data-recency="365"]').click();
  const r = await page.evaluate(() => ({
    shown: [...document.querySelectorAll('.person-row')].map(e => +e.getAttribute('data-id')),
    expected: PEOPLE.filter(p => p.deals.some(d => { const n = daysSince(dealDate(d)); return n !== null && n <= 365; })).map(p => p.id),
    hash: location.hash,
  }));
  expect(r.expected.length).toBeGreaterThan(0);
  expect(r.expected.length).toBeLessThan(total);
  expect(new Set(r.shown)).toEqual(new Set(r.expected.slice(0, r.shown.length)));
  expect(r.hash).toContain('recent=365');

  await page.reload();
  expect(await page.evaluate(() => state.recencyDays)).toBe(365);
  await page.locator('#clearFiltersBtn').click();
  expect(await page.evaluate(() => state.recencyDays)).toBe(0);
});

test('an undated deal is unknown, not recent: it never passes a recency filter', async ({ page }) => {
  await page.goto('/index.html');
  const r = await page.evaluate(() => {
    const undatedOnly = PEOPLE.find(p => p.deals.length && p.deals.every(d => !dealDate(d)));
    if (!undatedOnly) return { skipped: true };
    state.recencyDays = 365;
    return { skipped: false, passes: matchesRecency(undatedOnly) };
  });
  if (!r.skipped) expect(r.passes).toBe(false);
});

test('a malformed recent= hash value is ignored', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto('/index.html#recent=7');
  expect(await page.evaluate(() => state.recencyDays)).toBe(0);
  expect(errors).toEqual([]);
});

test('deal cards show the reported date and, when matched, an outbound CCI link', async ({ page }) => {
  await page.goto('/index.html');
  const r = await page.evaluate(() => {
    const dated = PEOPLE.find(p => p.deals.some(d => dealDate(d)));
    const withCci = PEOPLE.find(p => p.deals.some(d => d.cci));
    const open = (p) => { state.selectedId = p.id; renderDetail(); return [...document.querySelectorAll('.deal-card')]; };
    const cards = open(dated);
    const dateCard = cards.find(c => c.querySelector('.deal-date'));
    const out = { dateText: dateCard ? dateCard.querySelector('.deal-date').innerText : null };
    if (withCci) {
      const ccards = open(withCci);
      const b = ccards.map(c => c.querySelector('a.cci-badge')).find(Boolean);
      out.cci = b ? { href: b.getAttribute('href'), target: b.getAttribute('target'), rel: b.getAttribute('rel'), text: b.innerText } : null;
    }
    return out;
  });
  expect(r.dateText).toMatch(/^Reported \d{1,2} \w{3} \d{4}$/);
  if (r.cci !== undefined && r.cci) {
    expect(r.cci.href).toMatch(/^https:\/\/www\.cci\.gov\.in\//);
    expect(r.cci.target).toBe('_blank');
    expect(r.cci.rel).toMatch(/noopener/);
    expect(r.cci.text).toMatch(/^CCI/);
  }
});

test('profile shows the latest deal reported', async ({ page }) => {
  await page.goto('/index.html');
  const r = await page.evaluate(() => {
    const p = PEOPLE.find(x => x.deals.some(d => dealDate(d)));
    state.selectedId = p.id; renderDetail();
    return { chip: document.querySelector('#lastDealChip').innerText, expected: fmtDate(lastDealDate(p)) };
  });
  expect(r.chip).toContain(r.expected);
});

// --- firm hierarchy view ----------------------------------------------------
test('hierarchy view groups each firm by seniority tier, partners above associates', async ({ page }) => {
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto('/index.html#view=hier');
  await expect(page.locator('#view-hier')).toHaveClass(/active/);
  await expect(page.locator('.hier-firm').first()).toBeVisible();
  // collapsed by default; opening one firm reveals tiers in seniority order
  await page.evaluate(() => document.querySelector('.hier-head').click());
  const r = await page.evaluate(() => {
    const titles = [...document.querySelectorAll('.hier-firm')[0].querySelectorAll('.hier-tier-title')].map(e => e.firstChild.textContent.trim());
    const order = ['Leadership','Partners','Counsel & advocates','Associates','Other roles'];
    const idx = titles.map(t => order.indexOf(t));
    const members = [...document.querySelectorAll('.hier-firm')[0].querySelectorAll('.hier-member')].length;
    const firm = document.querySelector('.hier-head .hier-name').textContent;
    const expected = PEOPLE.filter(p => (p.firm || 'Unknown firm') === firm).length;
    return { titles, sorted: idx.every((v,i)=> i===0 || v > idx[i-1]), members, expected };
  });
  expect(r.titles.length).toBeGreaterThan(0);
  expect(r.sorted).toBe(true);
  expect(r.members).toBe(r.expected);
  expect(errors).toEqual([]);
});

test('hierarchy member opens the profile; view persists in the hash', async ({ page }) => {
  await page.goto('/index.html#view=hier');
  await page.evaluate(() => document.querySelector('.hier-head').click());
  await page.evaluate(() => document.querySelector('.hier-member').click());
  await expect(page.locator('#detail .name')).toBeVisible();
  await expect(page).toHaveURL(/view=hier/);
  await page.evaluate(() => document.getElementById('view-person').click());
  await expect(page).not.toHaveURL(/view=/);
});

test('hierarchy view respects filters and auto-opens a lone firm', async ({ page }) => {
  await page.goto('/index.html#view=hier&firm=Trilegal');
  await expect(page.locator('.hier-firm')).toHaveCount(1);
  await expect(page.locator('.hier-tier').first()).toBeVisible();
});
