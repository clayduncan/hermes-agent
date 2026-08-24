import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n/context'
import type * as UpdatesStore from '@/store/updates'

const startUpdateForSpy = vi.fn()

// Partial mock: the overlay reads the real atoms, but we intercept the single
// Update action so this file asserts the WIRING (which action the button runs)
// without dragging the whole apply pipeline into a component test. The two-stage
// behaviour of startUpdateFor itself is covered in store/updates.test.ts.
vi.mock('@/store/updates', async importOriginal => {
  const actual = await importOriginal<typeof UpdatesStore>()

  return {
    ...actual,
    startUpdateFor: (...args: unknown[]) => startUpdateForSpy(...args)
  }
})

const { $updateOverlayOpen, $updateOverlayTarget, $updateStatus, $backendUpdateStatus, resetUpdateApplyState } =
  await import('@/store/updates')

const { UpdatesOverlay } = await import('./updates-overlay')

async function renderUpdatesOverlay() {
  await act(async () => {
    render(
      <I18nProvider configClient={{ getConfig: async () => ({}), saveConfig: async () => ({ ok: true }) }}>
        <UpdatesOverlay />
      </I18nProvider>
    )
  })
}

const available = (over = {}) => ({
  supported: true,
  behind: 3,
  updateAvailable: true,
  targetSha: 'sha-a',
  fetchedAt: 0,
  commits: [{ at: 1, author: 'Nous', sha: 'abc1234', summary: 'feat: x' }],
  ...over
})

describe('UpdatesOverlay "Update now"', () => {
  beforeEach(() => {
    startUpdateForSpy.mockReset()
    resetUpdateApplyState()
    $updateStatus.set(null)
    $backendUpdateStatus.set(null)
    $updateOverlayOpen.set(true)
  })

  afterEach(() => {
    cleanup()
    $updateOverlayOpen.set(false)
    $updateOverlayTarget.set('client')
  })

  // FAIL-BEFORE: this button called applyBackendUpdate() directly, so the most
  // travelled Update path in remote mode ("Update ready" toast → See what's new
  // → Update now) updated the backend and left this desktop build behind it —
  // the same skew the About panel's identically-labelled button now avoids.
  it('runs the two-stage backend action, not a bare backend apply, in remote mode', async () => {
    $updateOverlayTarget.set('backend')
    $backendUpdateStatus.set(available())

    await renderUpdatesOverlay()
    fireEvent.click(screen.getByText('Update now'))

    expect(startUpdateForSpy).toHaveBeenCalledWith('backend')
  })

  it('runs the client action when the overlay is targeting the local app', async () => {
    $updateOverlayTarget.set('client')
    $updateStatus.set(available())

    await renderUpdatesOverlay()
    fireEvent.click(screen.getByText('Update now'))

    expect(startUpdateForSpy).toHaveBeenCalledWith('client')
  })
})
