export default async function run(page, ui) {
  const results = [];
  const targets = ['home','alerts','inbox','settings','sync','live'];
  for (const t of targets) {
    await page.evaluate((t) => {
      const link = document.querySelector(`.sidebar-link[data-page="${t}"]`);
      link.click();
    }, t);
    await page.waitForTimeout(300);
    const active = await page.evaluate(() => document.querySelector('.page.active')?.id || 'NONE');
    const visible = await page.evaluate((t) => {
      const p = document.getElementById('page-' + t);
      if (!p) return 'MISSING';
      const cs = getComputedStyle(p);
      return cs.display !== 'none' && cs.visibility !== 'hidden';
    }, t);
    results.push({ clicked: t, activePage: active, visible });
  }
  return results;
}
