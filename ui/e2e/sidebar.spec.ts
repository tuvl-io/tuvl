import { expect, test } from '@playwright/test'

// The collapsed menu is a rail of icons. Its expand button used to sit below the icons, so on a window shorter than
// the rail (about 900 px) it was off screen and the menu could not be opened.
test.describe('the collapsed menu on a short window', () => {
  test.use({ viewport: { width: 1280, height: 520 } })

  test('the expand button is on screen, and the pages below the fold are reachable', async ({ page }) => {
    await page.goto('/insight/settings')
    await expect(page.getByRole('button', { name: 'Expand the menu' })).toBeInViewport()
    // the icons scroll instead of running off the window
    const settings = page.locator('aside nav').getByRole('link', { name: 'Settings', exact: true })
    await settings.scrollIntoViewIfNeeded()
    await expect(settings).toBeInViewport()
  })

  test('expanding keeps the collapse button on screen, and collapsing brings the rail back', async ({ page }) => {
    await page.goto('/insight/settings')
    await page.getByRole('button', { name: 'Expand the menu' }).click()
    const collapse = page.getByRole('button', { name: 'Collapse the menu' })
    await expect(collapse).toBeInViewport()
    await expect(collapse).toHaveAttribute('aria-expanded', 'true')
    await collapse.click()
    await expect(page.getByRole('button', { name: 'Expand the menu' })).toBeInViewport()
  })
})

test.describe('the menu on a narrow window', () => {
  test.use({ viewport: { width: 800, height: 600 } })

  test('the top bar opens the menu as an overlay, and picking a page closes it', async ({ page }) => {
    await page.goto('/insight/settings')
    await expect(page.getByRole('button', { name: 'Expand the menu' })).toBeHidden() // the rail is for wide windows
    await page.getByRole('button', { name: 'Toggle sidebar' }).click()
    await expect(page.getByRole('button', { name: 'Collapse the menu' })).toBeInViewport()
    await page.getByRole('link', { name: 'Runs' }).click()
    await expect(page.getByRole('button', { name: 'Collapse the menu' })).toBeHidden()
  })
})
