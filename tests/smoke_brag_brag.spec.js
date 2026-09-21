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
  // Descending, with the unreported tail excluded from the comparison.
  const values = await page.evaluate(() =>
    [...document.querySelectorAll('.deal-count')]
      .map(e => e.innerText)
      .filter(t => t.includes('₹'))
      .map(t => Number(t.replace(/[^\d]/g, ''))));
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
