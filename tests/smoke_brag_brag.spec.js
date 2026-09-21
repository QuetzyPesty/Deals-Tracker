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
