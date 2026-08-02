import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import WindowsTitlebarMenu from '../components/WindowsTitlebarMenu'

type MenuAPI = {
  getAppMenuItems: ReturnType<typeof vi.fn>
  executeAppMenuItem: ReturnType<typeof vi.fn>
}

function installMenuAPI() {
  const api: MenuAPI = {
    getAppMenuItems: vi.fn(async (id: string) => id === 'file-menu'
      ? [
          { type: 'normal', index: 0, label: 'Settings…', accelerator: 'CmdOrCtrl+,', enabled: true, checked: false },
          { type: 'separator', index: 1 },
          { type: 'normal', index: 2, label: 'Exit', accelerator: '', enabled: true, checked: false },
        ]
      : [{ type: 'normal', index: 0, label: 'Reload', accelerator: 'CmdOrCtrl+R', enabled: true, checked: false }]),
    executeAppMenuItem: vi.fn(),
  }
  ;(window as Window & { electronAPI?: MenuAPI }).electronAPI = api
  return api
}

describe('WindowsTitlebarMenu', () => {
  afterEach(() => {
    delete (window as Window & { electronAPI?: unknown }).electronAPI
    delete document.documentElement.dataset.mode
  })

  it('rests as a hamburger and expands into the application menu labels', async () => {
    const api = installMenuAPI()
    const onExpandedChange = vi.fn()
    render(<header><WindowsTitlebarMenu onExpandedChange={onExpandedChange} /></header>)

    const hamburger = screen.getByRole('button', { name: 'Open menu' })
    expect(screen.queryByText('File')).toBeNull()
    fireEvent.click(hamburger)

    expect(screen.getAllByRole('button').map(item => item.textContent)).toEqual([
      'File',
      'Edit',
      'View',
      'Connection',
      'Window',
      'Help',
    ])
    expect(api.getAppMenuItems).toHaveBeenCalledWith('file-menu')
    expect(await screen.findByRole('menuitem', { name: /Settings/ })).toBeTruthy()
    expect(onExpandedChange).toHaveBeenCalledWith(true)
  })

  it('switches the active submenu when another label is hovered', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const view = screen.getByText('View')
    vi.spyOn(view, 'getBoundingClientRect').mockReturnValue({
      x: 91,
      y: 4,
      left: 91,
      top: 4,
      right: 137,
      bottom: 32,
      width: 46,
      height: 28,
      toJSON: () => ({}),
    })
    vi.spyOn(view.closest('header') as HTMLElement, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 800,
      bottom: 42,
      width: 800,
      height: 42,
      toJSON: () => ({}),
    })

    fireEvent.mouseEnter(view)

    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('view-menu'))
    expect(await screen.findByRole('menuitem', { name: /Reload/ })).toBeTruthy()
    expect(screen.getByText('File')).toBeTruthy()
    expect(view.getAttribute('aria-expanded')).toBe('true')
  })

  it('collapses to the hamburger on Escape', () => {
    installMenuAPI()
    const onExpandedChange = vi.fn()
    render(<header><WindowsTitlebarMenu onExpandedChange={onExpandedChange} /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })

    expect(screen.queryByText('File')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
    expect(onExpandedChange).toHaveBeenLastCalledWith(false)
  })

  it('collapses when the user clicks outside the menu session', () => {
    installMenuAPI()
    render(<div><header><WindowsTitlebarMenu /></header><button type="button">Outside</button></div>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Outside' }))

    expect(screen.queryByText('File')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('executes a selected command in Electron and collapses', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    fireEvent.click(await screen.findByRole('menuitem', { name: /Settings/ }))

    expect(api.executeAppMenuItem).toHaveBeenCalledWith('file-menu', 0)
    expect(screen.queryByText('File')).toBeNull()
  })

  it('collapses when the already-open label is clicked again', async () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    // File opens with the hamburger, so its label is the active one.
    expect(await screen.findByRole('menuitem', { name: /Settings/ })).toBeTruthy()

    fireEvent.click(screen.getByText('File'))

    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('collapses rather than stranding an empty popup when the IPC read fails', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockRejectedValueOnce(new Error('ipc gone'))
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    // A failed read must not leave the labels expanded over an empty popup —
    // the menu session ends and the hamburger comes back.
    await waitFor(() => expect(screen.queryByText('File')).toBeNull())
    expect(screen.getByRole('button', { name: 'Open menu' })).toBeTruthy()
  })

  it('walks top-level menus with ArrowRight / ArrowLeft, wrapping at both ends', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const nav = screen.getByLabelText('Open menu', { selector: 'nav' })

    // File (index 0) is active -> ArrowRight lands on Edit and opens it.
    fireEvent.keyDown(nav, { key: 'ArrowRight' })
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('edit-menu'))
    expect(screen.getByText('Edit')).toHaveFocus()

    // ArrowLeft twice from Edit wraps past File round to Help.
    fireEvent.keyDown(nav, { key: 'ArrowLeft' })
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('file-menu'))
    fireEvent.keyDown(nav, { key: 'ArrowLeft' })
    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('help-menu'))
    expect(screen.getByText('Help')).toHaveFocus()
  })

  it('ignores keys that are not menu navigation', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await screen.findByRole('menuitem', { name: /Settings/ })
    const callsBefore = api.getAppMenuItems.mock.calls.length

    fireEvent.keyDown(screen.getByLabelText('Open menu', { selector: 'nav' }), { key: 'a' })

    expect(api.getAppMenuItems.mock.calls).toHaveLength(callsBefore)
    expect(screen.getByRole('menu')).toBeTruthy()
  })

  it('moves focus from the labels into the popup on ArrowDown', async () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const settings = await screen.findByRole('menuitem', { name: /Settings/ })

    fireEvent.keyDown(screen.getByLabelText('Open menu', { selector: 'nav' }), { key: 'ArrowDown' })

    // First enabled item, skipping the separator.
    expect(settings).toHaveFocus()
  })

  it('cycles popup items with ArrowDown / ArrowUp / Home / End', async () => {
    installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const settings = await screen.findByRole('menuitem', { name: /Settings/ })
    const exit = screen.getByRole('menuitem', { name: /Exit/ })
    const popup = screen.getByRole('menu')

    // Enter the popup the way a user does — ArrowDown from the labels — so the
    // cycling below starts from a real focused item rather than from the
    // container itself.
    fireEvent.keyDown(screen.getByLabelText('Open menu', { selector: 'nav' }), { key: 'ArrowDown' })
    expect(settings).toHaveFocus()

    // The separator is not focusable, so Settings and Exit are the only stops.
    fireEvent.keyDown(popup, { key: 'ArrowDown' })
    expect(exit).toHaveFocus()

    // Wraps forward off the end, and back off the front.
    fireEvent.keyDown(popup, { key: 'ArrowDown' })
    expect(settings).toHaveFocus()
    fireEvent.keyDown(popup, { key: 'ArrowUp' })
    expect(exit).toHaveFocus()

    fireEvent.keyDown(popup, { key: 'Home' })
    expect(settings).toHaveFocus()
    fireEvent.keyDown(popup, { key: 'End' })
    expect(exit).toHaveFocus()
  })

  it('renders a checkbox item with its check state and a rewritten accelerator', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      { type: 'checkbox', index: 0, label: 'Keep on Top', accelerator: '', enabled: true, checked: true },
      { type: 'normal', index: 1, label: 'Zoom In', accelerator: 'CommandOrControl+Plus', enabled: true, checked: false },
      { type: 'normal', index: 2, label: 'Unavailable', accelerator: '', enabled: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))

    const toggle = await screen.findByRole('menuitemcheckbox', { name: /Keep on Top/ })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    // The Electron accelerator token is rewritten to the cap the user reads.
    expect(screen.getByRole('menuitem', { name: /Zoom In/ })).toHaveTextContent('Ctrl+Plus')
    expect(screen.getByRole('menuitem', { name: /Unavailable/ })).toBeDisabled()
  })

  it('opens a different menu when its label is clicked without a hover first', async () => {
    const api = installMenuAPI()
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    await screen.findByRole('menuitem', { name: /Settings/ })

    // Touch and some AT drivers deliver a click with no preceding mouseEnter,
    // so the click handler has to open a non-active menu on its own.
    fireEvent.click(screen.getByText('View'))

    await waitFor(() => expect(api.getAppMenuItems).toHaveBeenLastCalledWith('view-menu'))
    expect(await screen.findByRole('menuitem', { name: /Reload/ })).toBeTruthy()
  })

  it('does not dispatch a disabled item', async () => {
    const api = installMenuAPI()
    api.getAppMenuItems.mockResolvedValueOnce([
      { type: 'normal', index: 0, label: 'Unavailable', accelerator: '', enabled: false, checked: false },
    ])
    render(<header><WindowsTitlebarMenu /></header>)
    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }))
    const item = await screen.findByRole('menuitem', { name: /Unavailable/ })

    fireEvent.click(item)

    expect(api.executeAppMenuItem).not.toHaveBeenCalled()
  })
})
