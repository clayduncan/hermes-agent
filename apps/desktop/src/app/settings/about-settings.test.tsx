// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import type { DesktopUpdateStatus, DesktopVersionInfo } from '@/global'
import {
  $desktopVersion,
  $updateChecking,
  $updateStatus,
  REQUIRED_BACKEND_CONTRACT,
  resetUpdateApplyState
} from '@/store/updates'

import { AboutSettings } from './about-settings'

const TEST_VERSION: DesktopVersionInfo = {
  appVersion: '7.7.7-test-runtime',
  electronBundleVersion: '3.3.3-test-bundle',
  electronVersion: '40.10.2-test-electron',
  hermesRoot: '/tmp/hermes-test-root',
  nodeVersion: '22.22.0-test-node',
  platform: 'darwin'
}

const TEST_STATUS: DesktopUpdateStatus = {
  branch: 'main',
  currentSha: 'abcdef1234567890',
  fetchedAt: Date.now(),
  supported: true
}

beforeEach(() => {
  $desktopVersion.set(TEST_VERSION)
  $updateStatus.set(TEST_STATUS)
  $updateChecking.set(false)
  resetUpdateApplyState()
})

afterEach(() => {
  cleanup()
  $desktopVersion.set(null)
  $updateStatus.set(null)
  resetUpdateApplyState()
})

describe('AboutSettings', () => {
  it('renders the runtime version, Electron bundle version, compatibility contract, and build commit as distinct labeled values', () => {
    render(<AboutSettings />)

    expect(screen.getByText('Runtime version')).toBeTruthy()
    expect(screen.getByText(TEST_VERSION.appVersion)).toBeTruthy()

    expect(screen.getByText('Electron bundle version')).toBeTruthy()
    expect(screen.getByText(TEST_VERSION.electronBundleVersion)).toBeTruthy()

    expect(screen.getByText('Compatibility contract')).toBeTruthy()
    expect(screen.getByText(String(REQUIRED_BACKEND_CONTRACT))).toBeTruthy()

    const shortSha = TEST_STATUS.currentSha!.slice(0, 7)
    expect(screen.getByText('Build commit')).toBeTruthy()
    expect(screen.getByText(shortSha)).toBeTruthy()

    // Distinct supplied values prove each label is wired to its own field
    // instead of four rows accidentally rendering the same string.
    const values = [
      TEST_VERSION.appVersion,
      TEST_VERSION.electronBundleVersion,
      String(REQUIRED_BACKEND_CONTRACT),
      shortSha
    ]
    expect(new Set(values).size).toBe(values.length)
  })

  it('falls back to "Unavailable" instead of rendering an unlabeled value when data is missing', () => {
    $desktopVersion.set(null)
    $updateStatus.set(null)

    render(<AboutSettings />)

    // The runtime version, Electron bundle version, and build commit all
    // depend on IPC data that can be absent; each must still show its label
    // paired with an explicit fallback rather than an empty or bare cell.
    expect(screen.getByText('Runtime version')).toBeTruthy()
    expect(screen.getByText('Electron bundle version')).toBeTruthy()
    expect(screen.getByText('Build commit')).toBeTruthy()
    expect(screen.getAllByText('Unavailable')).toHaveLength(3)

    // The compatibility contract is a compiled-in constant, so it always
    // resolves even when the IPC version/update payloads are unavailable.
    expect(screen.getByText('Compatibility contract')).toBeTruthy()
    expect(screen.getByText(String(REQUIRED_BACKEND_CONTRACT))).toBeTruthy()
  })
})
