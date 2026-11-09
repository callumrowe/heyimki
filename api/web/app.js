// ── Config storage ────────────────────────────────────────────────────────────
// The PWA keeps its connection config (just the DESKTOP_SECRET) in localStorage.
if (!window.ki) {
  window.ki = {
    loadConfig: () => {
      try { return JSON.parse(localStorage.getItem('ki-config')) } catch { return null }
    },
    saveConfig: (c) => {
      if (c) localStorage.setItem('ki-config', JSON.stringify(c))
      else localStorage.removeItem('ki-config')
    },
    getEnv: () => null,
  }
}

// ── State ─────────────────────────────────────────────────────────────────────
let config = null        // { secret }
let isStreaming = false
let currentKiBubble = null
let currentToolRow = null
let msgIdCounter = 0
const reactions = new Map() // msgId → Set of reaction strings
const tsIndex  = new Map() // msgId → ts  (stable key for persistence)
let latestMessageTs = null   // ISO ts of last message shown (for new-message polling)
const _renderedImages = new Set() // fingerprints of images already in DOM (dedup across SSE / poll / _postStreamImageSync)

// Fingerprint for image dedup — combines length with a slice that reaches past
// shared JPEG/PNG header bytes (SOI+APP0+DQT+DHT = ~400 binary bytes = ~533 base64 chars).
// Using length alone isn't enough; using a prefix alone causes false positives.
function _imageFingerprint(data) {
  if (!data) return ''
  if (data.length > 800) {
    // Deep enough past the JPEG header into unique compressed scan data
    return data.length + '|' + data.slice(600, 900)
  }
  return data.length + '|' + data
}
let replyTo = null           // text of message being replied to
let attachment = null        // { type: 'image'|'document', data, media_type, filename }
const messageQueue = []      // [{ text, attachment }, ...] — pending while streaming
// msgId → Set of emoji — reactions clicked while the bubble was still streaming.
// Flushed to the server once finaliseKiBubble() clears the .streaming class.
const pendingStreamReactions = new Map()

// ── Voice mode state ──────────────────────────────────────────────────────────
let voiceMode = false
let mediaRecorder = null
let audioChunks = []
let isRecording = false
let lastKiAudioEl = null
let _lastVoiceResponseTs = null  // set when audio SSE event fires; used to deduplicate poll

// AudioContext kept alive after first user gesture — lets us auto-play audio
// in async SSE callbacks on iOS/Safari where audioEl.play() is blocked.
let _audioCtx = null
function _ensureAudioCtx() {
  if (_audioCtx) return _audioCtx
  try { _audioCtx = new (window.AudioContext || window.webkitAudioContext)() } catch (_) {}
  return _audioCtx
}
async function _autoPlayAudio(base64, mimeType) {
  const ctx = _ensureAudioCtx()
  if (!ctx || !base64) return
  try {
    if (ctx.state === 'suspended') await ctx.resume()
    const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0))
    const buf = await ctx.decodeAudioData(bytes.buffer)
    const src = ctx.createBufferSource()
    src.buffer = buf
    src.connect(ctx.destination)
    src.start()
  } catch (_) {}
}

const SVG_PLAY_LAST = `<svg viewBox="0 0 16 16" fill="currentColor" width="18" height="18"><polygon points="3,1 13,8 3,15"/></svg>`
const SVG_PAUSE_LAST = `<svg viewBox="0 0 16 16" fill="currentColor" width="18" height="18"><rect x="2" y="1" width="4" height="14" rx="1"/><rect x="10" y="1" width="4" height="14" rx="1"/></svg>`

function setPlayLastIcon(state) {
  const btn = document.getElementById('play-last-btn')
  if (btn) btn.innerHTML = state === 'pause' ? SVG_PAUSE_LAST : SVG_PLAY_LAST
}

// ── Logs panel state ──────────────────────────────────────────────────────────
let logsPollInterval = null
let logsActiveRow = null

const TOOL_ICONS = {
  search_vault:            '⌕',
  read_note:               '◈',
  append_to_note:          '◈',
  write_note:              '◈',
  browser_navigate:        '⊕',
  browser_act:             '⊕',
  get_balances:            '◎',
  get_runway:              '◎',
  get_spending:            '◎',
  get_recent_transactions: '◎',
  delegate_coding_task:    '⚙',
  request_confirmation:    '◉',
  send_file:               '⊙',
}

const TOOL_NAMES = {
  search_vault:            'search vault',
  read_note:               'read note',
  append_to_note:          'append to note',
  write_note:              'write note',
  browser_navigate:        'navigate',
  browser_act:             'browser',
  get_balances:            'balances',
  get_runway:              'runway',
  get_spending:            'spending',
  get_recent_transactions: 'transactions',
  delegate_coding_task:    'coding agent',
  request_confirmation:    'confirm',
  send_file:               'send file',
}

// Maps tool name → log service for the side panel.
// 'api' = show api container logs; 'coding-agent' = show coding-agent logs.
const TOOL_LOG_SERVICE = {
  browser_navigate:     'browser',
  browser_act:          'browser',
  delegate_coding_task: 'coding-agent',
}

// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  initLightbox()
  initLogsPanel()
  document.getElementById('setup-subtitle').textContent = 'Enter your DESKTOP_SECRET'
  config = await window.ki.loadConfig()
  if (config && config.secret) {
    showChat()
  } else {
    showSetup()
  }

  if (window.visualViewport) {
    const inputArea = document.querySelector('.input-area');
    const onViewportResize = () => {
      const keyboardOpen = window.visualViewport.height < window.innerHeight * 0.75;
      inputArea?.classList.toggle('keyboard-open', keyboardOpen);
    };
    window.visualViewport.addEventListener('resize', onViewportResize);
    onViewportResize();
  }
})

// ── Double-tap to show message actions (touch/mobile only) ───────────────────
function closeAllActions() {
  document.querySelectorAll('.message-row.actions-open').forEach(r => r.classList.remove('actions-open'))
  document.getElementById('messages-inner')?.classList.remove('has-open-actions')
}

function attachDoubleTap(row) {
  let lastTap = 0

  row.addEventListener('contextmenu', e => e.preventDefault())

  // Non-passive so we can preventDefault on the second tap, blocking iOS
  // double-tap-to-select-word before our touchend has a chance to fire.
  row.addEventListener('touchstart', e => {
    const delta = Date.now() - lastTap
    if (delta < 500 && delta > 0) e.preventDefault()
  }, { passive: false })

  row.addEventListener('touchend', e => {
    const now = Date.now()
    const delta = now - lastTap
    if (delta < 500 && delta > 0) {
      closeAllActions()
      row.classList.add('actions-open')
      document.getElementById('messages-inner')?.classList.add('has-open-actions')
      if (navigator.vibrate) navigator.vibrate(10)
      lastTap = 0
    } else {
      lastTap = now
    }
  })
}

// Tap outside the action bar to dismiss
document.addEventListener('touchstart', e => {
  if (!document.querySelector('.message-row.actions-open')) return
  if (!e.target.closest('.bubble-actions')) closeAllActions()
}, { passive: true })

// ── Setup screen ──────────────────────────────────────────────────────────────
function showSetup() {
  document.getElementById('setup-screen').classList.remove('hidden')
  document.getElementById('chat-screen').classList.add('hidden')
  document.getElementById('setup-secret').focus()
}

document.getElementById('setup-connect').addEventListener('click', async () => {
  const secret = document.getElementById('setup-secret').value.trim()
  const errEl  = document.getElementById('setup-error')
  const btn    = document.getElementById('setup-connect')

  if (!secret)               { showSetupError('Secret is required.');       return }

  btn.disabled = true
  btn.textContent = 'Connecting...'
  errEl.classList.add('hidden')

  try {
    const check = await fetch(`/desktop/history?channel=desktop&limit=1`, {
      headers: { 'Authorization': `Bearer ${secret}` }
    })
    if (check.status === 401) throw new Error('Wrong secret — check DESKTOP_SECRET.')
    if (!check.ok) throw new Error(`Server error ${check.status}`)

    config = { secret }
    await window.ki.saveConfig(config)
    showChat()
  } catch (err) {
    showSetupError(err.message)
    btn.disabled = false
    btn.textContent = 'Connect'
  }
})

document.getElementById('setup-secret').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('setup-connect').click()
})

function showSetupError(msg) {
  const el = document.getElementById('setup-error')
  el.textContent = msg
  el.classList.remove('hidden')
}

// ── Chat screen ───────────────────────────────────────────────────────────────

// Poll /health until the API responds — keeps the full-page overlay visible
// during Docker startup so the user never sees empty error states on any page.
async function waitUntilHealthy() {
  while (true) {
    try {
      const res = await apiFetch('/health')
      if (res.ok) return
    } catch {
      // Network error — services not up yet, keep waiting
    }
    await new Promise(r => setTimeout(r, 2000))
  }
}

// Silently re-subscribes to web push if the VAPID key has changed since the
// subscription was created, or if there's an existing subscription with no
// stored key (subscribed before key tracking was added).
async function _checkAndRefreshVapidSubscription() {
  if (!('Notification' in window) || Notification.permission !== 'granted') return
  if (!('serviceWorker' in navigator)) return
  try {
    const swReg = await navigator.serviceWorker.ready
    const sub = await swReg.pushManager.getSubscription()
    if (!sub) return  // not subscribed — nothing to check
    const vapidRes = await apiFetch('/push/vapid-public-key')
    if (!vapidRes.ok) return
    const { publicKey } = await vapidRes.json()
    const storedKey = localStorage.getItem('ki-vapid-key')
    if (storedKey === publicKey) return  // keys match — nothing to do
    // VAPID key rotated or was never tracked — force a silent re-subscribe
    const oldEndpoint = sub.endpoint
    await sub.unsubscribe()
    // Tell the server to remove the old (now-invalid) endpoint before saving the new one
    await apiFetch('/push/unsubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: oldEndpoint }),
    })
    const newSub = await swReg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(publicKey),
    })
    const subJson = newSub.toJSON()
    await apiFetch('/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint: subJson.endpoint, keys: subJson.keys }),
    })
    localStorage.setItem('ki-vapid-key', publicKey)
  } catch { /* best-effort — don't block startup */ }
}

async function showChat() {
  document.getElementById('setup-screen').classList.add('hidden')
  document.getElementById('chat-screen').classList.remove('hidden')
  setConnection('connecting')
  // Keep the full-page overlay up until the API is actually healthy.
  // This prevents empty error states on chat/admin during Docker startup.
  await waitUntilHealthy()
  const chatStartTs = Date.now()
  await loadHistory()
  // Hold the overlay for at least 600 ms from when services became healthy
  // so the screen never flashes blank before messages have painted.
  await new Promise(r => setTimeout(r, Math.max(0, 600 - (Date.now() - chatStartTs))))
  setConnection('online')
  await checkAndResumeTask()
  document.getElementById('message-input').focus()
  // Background: refresh the web push subscription
  _checkAndRefreshVapidSubscription()
}

// ── Hotkeys (Cmd+X = admin) ───────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (!e.metaKey) return
  if (e.key === 'x') {
    e.preventDefault()
    const adminScreen = document.getElementById('admin-screen')
    if (adminScreen.classList.contains('hidden')) showAdmin()
    else returnToChat()
  }
})


// ── Load history ──────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const res = await apiFetch(`/desktop/history?channel=desktop&limit=60`)
    const { messages } = await res.json()
    clearMessages()
    if (!messages || messages.length === 0) {
      appendEmptyState()
      return
    }

    let lastDate = null
    for (const msg of messages) {
      const date = msg.ts ? new Date(msg.ts).toLocaleDateString('en-NZ', { weekday: 'short', month: 'short', day: 'numeric' }) : null
      if (date && date !== lastDate) {
        appendDaySeparator(date)
        lastDate = date
      }
      const isVoiceMsg = msg.voice_audio || (msg.role === 'user' && msg.content && msg.content.startsWith('[Voice message]:'))
      if (voiceMode && !isVoiceMsg) continue
      if (!voiceMode && isVoiceMsg) continue
      if (isVoiceMsg) {
        appendVoiceBubble(msg.role === 'user' ? 'user' : 'ki', msg.voice_audio || null, msg.voice_audio ? 'audio/mpeg' : null, msg.ts, msg.content)
      } else {
        const attachInfo = msg.image_data
          ? { type: 'image', data: msg.image_data, media_type: msg.image_media_type }
          : null
        appendBubble(msg.role === 'user' ? 'user' : 'ki', msg.content, false, false, msg.ts, attachInfo)
        if (msg.role !== 'user' && msg.tool_calls && msg.tool_calls.length > 0) {
          for (const tc of msg.tool_calls) {
            appendActivityRow(tc.name, tc.input || '', 'done')
          }
        }
      }
    }
    if (messages.length > 0) {
      latestMessageTs = messages[messages.length - 1].ts
    }
    restoreReactions()
    // Scroll to bottom immediately (non-smooth) so the browser starts painting
    // from the correct position, then wait until the messages container is
    // actually non-empty and has a real scrollHeight before we let the overlay
    // disappear.  We loop rAFs (max 20 frames ≈ ~333 ms at 60 fps) so we never
    // hang forever if something goes wrong.
    scrollToBottom(false)
    await new Promise(resolve => {
      let frames = 0
      function check() {
        const inner = document.getElementById('messages-inner')
        const scroller = document.getElementById('messages')
        if (
          (inner && inner.children.length > 0 && scroller && scroller.scrollHeight > 100) ||
          frames >= 20
        ) {
          resolve()
        } else {
          frames++
          requestAnimationFrame(check)
        }
      }
      requestAnimationFrame(check)
    })
  } catch (err) {
    console.error('Failed to load history:', err)
    appendSystemNote(`Couldn't load history: ${err.message}`)
  }
}

// ── Sending messages ──────────────────────────────────────────────────────────
const inputEl      = document.getElementById('message-input')
const sendBtn      = document.getElementById('send-btn')
const messagesInner = document.getElementById('messages-inner')

// Keep messages-inner bottom padding in sync with the input area height so
// content is never obscured when the queue bar or attachment bar grows it.
new ResizeObserver(([entry]) => {
  messagesInner.style.paddingBottom = entry.contentRect.height + 28 + 'px'
}).observe(document.querySelector('.input-area'))

inputEl.addEventListener('input', () => {
  // Auto-resize
  inputEl.style.height = 'auto'
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + 'px'
  sendBtn.disabled = inputEl.value.trim() === '' && !attachment
  // Slash palette
  const val = inputEl.value
  if (val.startsWith('/') && !val.includes(' ')) {
    showSlashPalette(val)
  } else {
    hideSlashPalette()
  }
})

inputEl.addEventListener('keydown', e => {
  const paletteVisible = !slashPalette.classList.contains('hidden')
  if (paletteVisible) {
    const items = slashPalette.querySelectorAll('.slash-palette-item')
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      updateSlashActiveItem(slashActiveIndex <= 0 ? items.length - 1 : slashActiveIndex - 1)
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      updateSlashActiveItem(slashActiveIndex >= items.length - 1 ? 0 : slashActiveIndex + 1)
      return
    }
    if (e.key === 'Tab') {
      e.preventDefault()
      if (slashActiveIndex >= 0 && slashPalette._matches[slashActiveIndex]) {
        selectSlashCommand(slashPalette._matches[slashActiveIndex].cmd)
      } else if (slashPalette._matches.length === 1) {
        selectSlashCommand(slashPalette._matches[0].cmd)
      }
      return
    }
    if (e.key === 'Enter' && slashActiveIndex >= 0) {
      e.preventDefault()
      selectSlashCommand(slashPalette._matches[slashActiveIndex].cmd)
      return
    }
    if (e.key === 'Escape') {
      hideSlashPalette()
      return
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    if (!sendBtn.disabled) sendMessage()
  }
})

sendBtn.addEventListener('click', sendMessage)

// ── Slash command palette ──────────────────────────────────────────────────────
const SLASH_COMMANDS = [
  { cmd: '/today',      short: '/t',  desc: "Open today's daily note" },
  { cmd: '/morning',    short: '/m',  desc: 'Add today\'s morning pages' },
  { cmd: '/do',         short: '/d',  desc: 'Add a task to today\'s note' },
  { cmd: '/remind',     short: '/r',  desc: 'Set a reminder' },
  { cmd: '/journal',    short: null,  desc: 'Generate Journal PDF' },
  { cmd: '/kill',       short: null,  desc: 'Stop current response' },
]

const slashPalette = document.getElementById('slash-palette')
let slashActiveIndex = -1

function showSlashPalette(query) {
  const q = query.toLowerCase()
  const matches = SLASH_COMMANDS.filter(c =>
    c.cmd.startsWith(q) || (c.short && c.short.startsWith(q))
  )
  if (matches.length === 0) { hideSlashPalette(); return }

  slashPalette.innerHTML = ''
  slashActiveIndex = -1
  slashPalette._matches = matches

  matches.forEach((c, i) => {
    const item = document.createElement('div')
    item.className = 'slash-palette-item'

    const cmd = document.createElement('span')
    cmd.className = 'slash-palette-cmd'
    cmd.textContent = c.cmd
    item.appendChild(cmd)

    if (c.short) {
      const short = document.createElement('span')
      short.className = 'slash-palette-short'
      short.textContent = c.short
      item.appendChild(short)
    }

    const desc = document.createElement('span')
    desc.className = 'slash-palette-desc'
    desc.textContent = c.desc
    item.appendChild(desc)

    item.addEventListener('mousedown', e => {
      e.preventDefault()
      selectSlashCommand(c.cmd)
    })
    slashPalette.appendChild(item)
  })

  slashPalette.classList.remove('hidden')
}

function hideSlashPalette() {
  slashPalette.classList.add('hidden')
  slashPalette.innerHTML = ''
  slashActiveIndex = -1
}

function selectSlashCommand(cmd) {
  inputEl.value = cmd
  inputEl.style.height = 'auto'
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + 'px'
  sendBtn.disabled = false
  hideSlashPalette()
  inputEl.focus()
}

function updateSlashActiveItem(newIndex) {
  slashPalette.querySelectorAll('.slash-palette-item').forEach((el, i) =>
    el.classList.toggle('active', i === newIndex)
  )
  slashActiveIndex = newIndex
}

document.addEventListener('click', e => {
  if (!slashPalette.classList.contains('hidden') &&
      !slashPalette.contains(e.target) && e.target !== inputEl) {
    hideSlashPalette()
  }
})

// ── SSE stream reader (shared by sendMessage and checkAndResumeTask) ──────────
async function readStream(res) {
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split('\n\n')
    buffer = parts.pop()

    let terminal = false
    for (const part of parts) {
      const line = part.trim()
      if (!line.startsWith('data: ')) continue
      try {
        const event = JSON.parse(line.slice(6))
        handleStreamEvent(event)
        if (event.type === 'done' || event.type === 'error' || event.type === 'stopped') {
          terminal = true
        }
      } catch (_) {}
    }
    if (terminal) {
      reader.cancel()
      break
    }
  }
}

// ── Resume a background task that survived a previous disconnect ──────────────
async function checkAndResumeTask() {
  try {
    const statusRes = await apiFetch('/desktop/task-status?channel=desktop')
    const { running, active_tool } = await statusRes.json()
    if (!running) return

    // A task is running from before the app opened — reattach to its SSE stream
    console.log('[ki] background task running, reattaching to stream')
    isStreaming = true
    setKitt(true)
    sendBtn.disabled = true

    // Show whatever tool is currently mid-execution so the UI isn't blank
    if (active_tool) {
      currentToolRow = appendActivityRow(active_tool.name, active_tool.label || '', 'running')
      scrollToBottom()
    }

    const reattachSyncFloor = latestMessageTs || new Date(Date.now() - 120000).toISOString()
    try {
      const res = await apiFetch('/desktop/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: '', channel: 'desktop', reattach: true }),
      })
      if (res.ok) await readStream(res)
    } catch (err) {
      console.error('[ki] reattach stream error:', err)
    } finally {
      finaliseKiBubble()
      finaliseCurrentToolRow()
      isStreaming = false
      setKitt(false)
      await _postStreamImageSync(reattachSyncFloor)
      sendBtn.disabled = inputEl.value.trim() === '' && !attachment
      scrollToBottom()
      if (messageQueue.length > 0) {
        const next = messageQueue.shift()
        renderQueueBar()
        await _dispatchMessage(next.text, next.attachment)
      }
    }
  } catch (err) {
    console.error('[ki] checkAndResumeTask error:', err)
  }
}

function renderQueueBar() {
  const bar = document.getElementById('queue-bar')
  if (messageQueue.length === 0) {
    bar.classList.add('hidden')
    bar.innerHTML = ''
    return
  }
  bar.classList.remove('hidden')
  bar.innerHTML = ''
  messageQueue.forEach((item, i) => {
    const row = document.createElement('div')
    row.className = 'queue-item'
    const label = document.createElement('span')
    label.className = 'queue-label'
    label.textContent = `${i + 1}`
    const preview = document.createElement('span')
    preview.className = 'queue-preview'
    preview.textContent = item.text.slice(0, 80) + (item.text.length > 80 ? '…' : '')
    const cancel = document.createElement('button')
    cancel.className = 'queue-cancel'
    cancel.textContent = '✕'
    cancel.addEventListener('click', () => {
      messageQueue.splice(i, 1)
      renderQueueBar()
    })
    row.appendChild(label)
    row.appendChild(preview)
    row.appendChild(cancel)
    bar.appendChild(row)
  })
}

async function sendMessage() {
  _ensureAudioCtx()
  const rawText = inputEl.value.trim()
  if (!rawText && !attachment) return

  // Intercept /today and /t — open the daily note modal, no message bubble.
  if (rawText.toLowerCase() === '/today' || rawText.toLowerCase() === '/t') {
    inputEl.value = ''
    inputEl.style.height = 'auto'
    sendBtn.disabled = inputEl.value.trim() === '' && !attachment
    openTodayModal()
    return
  }

  // Intercept /journal — generate and download the Journal PDF.
  if (rawText.toLowerCase() === '/journal') {
    inputEl.value = ''
    inputEl.style.height = 'auto'
    sendBtn.disabled = inputEl.value.trim() === '' && !attachment
    triggerJournalDownload()
    return
  }

  // Intercept /kill — cancel the running loop without queuing it as a message.
  if (rawText.toLowerCase() === '/kill') {
    inputEl.value = ''
    inputEl.style.height = 'auto'
    sendBtn.disabled = inputEl.value.trim() === '' && !attachment
    let killed = false
    try {
      const res = await apiFetch('/kill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel: 'desktop' }),
      })
      if (res.ok) {
        const data = await res.json()
        killed = data.killed
      }
    } catch (err) {
      console.error('[kill] failed:', err)
    }
    if (killed) {
      messageQueue.length = 0
      renderQueueBar()
    }
    appendBubble('ki', killed ? 'Stopped.' : 'Nothing running to stop.', false, false, new Date().toISOString())
    scrollToBottom()
    return
  }

  const text = replyTo ? `> ${replyTo.slice(0, 300)}\n\n${rawText}` : rawText
  const sentAttachment = attachment
  clearReply()
  clearAttachment()
  inputEl.value = ''
  inputEl.style.height = 'auto'
  sendBtn.disabled = true

  if (isStreaming) {
    messageQueue.push({ text, attachment: sentAttachment })
    renderQueueBar()
    sendBtn.disabled = inputEl.value.trim() === '' && !attachment
    return
  }

  await _dispatchMessage(text, sentAttachment)
}

async function _dispatchMessage(text, sentAttachment) {
  isStreaming = true
  setKitt(true)
  sendBtn.disabled = true

  // Capture last known DB timestamp before overwriting with client clock.
  // Used as the floor for post-stream image sync — avoids clock-drift misses where
  // client "now" is ahead of the server's DB timestamp for the saved image.
  const syncFloor = latestMessageTs || new Date(Date.now() - 120000).toISOString()
  appendBubble('user', text, false, false, new Date().toISOString(), sentAttachment)
  scrollToBottom()

  let body = { text: text || '', channel: 'desktop' }
  if (sentAttachment) {
    if (sentAttachment.type === 'image') {
      body.image = { data: sentAttachment.data, media_type: sentAttachment.media_type }
    } else {
      body.document = { data: sentAttachment.data, media_type: sentAttachment.media_type, filename: sentAttachment.filename }
    }
  }

  try {
    const res = await apiFetch('/desktop/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) {
      const errText = await res.text()
      throw new Error(`HTTP ${res.status}: ${errText}`)
    }
    await readStream(res)
  } catch (err) {
    finaliseKiBubble()
    appendBubble('ki', `Error: ${err.message}`, false, true)
    console.error('Stream error:', err)
  } finally {
    finaliseKiBubble()
    finaliseCurrentToolRow()
    isStreaming = false
    setKitt(false)
    sendBtn.disabled = inputEl.value.trim() === '' && !attachment
    scrollToBottom()
    // Post-stream sync: advances latestMessageTs to the server DB timestamp
    // (not client clock) and catches any image SSE events that were dropped.
    await _postStreamImageSync(syncFloor)
    if (messageQueue.length > 0) {
      const next = messageQueue.shift()
      renderQueueBar()
      await _dispatchMessage(next.text, next.attachment)
    }
  }
}

function handleStreamEvent(event) {
  switch (event.type) {
    case 'text':
      if (voiceMode) break
      if (!currentKiBubble) {
        currentKiBubble = appendBubble('ki', '', true, false, new Date().toISOString())
      }
      currentKiBubble.textContent += event.text
      scrollToBottom()
      break

    case 'markdown': {
      if (voiceMode) break
      finaliseKiBubble()
      const mdBubble = appendBubble('ki', '', false, false, new Date().toISOString())
      mdBubble.classList.add('markdown-bubble')
      try {
        if (typeof marked !== 'undefined') {
          mdBubble.innerHTML = marked.parse(event.text)
        } else {
          mdBubble.textContent = event.text
        }
      } catch (e) {
        console.error('[ki] markdown render failed:', e)
        mdBubble.textContent = event.text
      }
      scrollToBottom()
      break
    }

    case 'vault_inject':
      if (voiceMode) break
      appendActivityRow('vault', event.files || [], 'done')
      break

    case 'tool_start':
      if (voiceMode) break
      finaliseKiBubble()
      currentToolRow = appendActivityRow(event.name, event.label || '', 'running')
      break

    case 'tool_done': {
      if (voiceMode) break
      const doneRow = currentToolRow
      if (doneRow) {
        doneRow.classList.replace('running', 'done')
        currentToolRow = null
        // If the logs panel is showing this tool row, do one final refresh
        if (logsActiveRow === doneRow && logsPollInterval) {
          clearInterval(logsPollInterval)
          logsPollInterval = null
          fetchAndDisplayLogs(doneRow.dataset.tool)
        }
      }
      break
    }

    case 'file':
      finaliseKiBubble()
      appendFileRow(event.filename, event.data, event.media_type)
      break

    case 'image':
      finaliseKiBubble()
      if (shadowModeActive) {
        shadowImages.push({ data: event.data, media_type: event.media_type, ts: new Date().toISOString() })
      }
      appendBubble('ki', '', false, false, new Date().toISOString(), { type: 'image', data: event.data, media_type: event.media_type, prompt: event.prompt || '' })
      scrollToBottom()
      break

    case 'audio': {
      finaliseKiBubble()
      _lastVoiceResponseTs = new Date().toISOString()
      const audioBubble = appendVoiceBubble('ki', event.data, event.mime_type || 'audio/mpeg', _lastVoiceResponseTs)
      const audioEl = audioBubble.querySelector('audio')
      if (audioEl) audioEl.play().catch(() => _autoPlayAudio(event.data, event.mime_type || 'audio/mpeg'))
      scrollToBottom()
      break
    }

    case 'stopped':
      finaliseKiBubble()
      finaliseCurrentToolRow()
      break

    case 'done':
      finaliseKiBubble()
      finaliseCurrentToolRow()
      break

    case 'error':
      finaliseKiBubble()
      finaliseCurrentToolRow()
      if (currentKiBubble) {
        currentKiBubble.classList.add('error-bubble')
        if (!currentKiBubble.textContent) {
          currentKiBubble.textContent = `Error: ${event.message}`
        }
      } else {
        // No text was streamed — create a visible error bubble so Ki doesn't just go silent
        appendBubble('ki', `Sorry, hit an error: ${event.message || 'unknown'}. Try again.`, false, true)
      }
      break
  }
}

// ── DOM helpers ───────────────────────────────────────────────────────────────
function renderBold(text) {
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
  return escaped
    .replace(/```([a-z]*)\n?([\s\S]*?)```/g, (_, _lang, code) => `<pre><code>${code.trim()}</code></pre>`)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([\s\S]*?)\*\*/g, '<strong>$1</strong>')
    .replace(/__([\s\S]*?)__/g, '<strong>$1</strong>')
    .replace(/\*([\s\S]*?)\*/g, '<em>$1</em>')
    .replace(/_([\s\S]*?)_/g, '<em>$1</em>')
}

function renderMarkdown(text) {
  if (typeof marked !== 'undefined') return marked.parse(text)
  return renderBold(text)
}

function setBubbleContent(bubble, text) {
  bubble.classList.add('markdown-bubble')
  const match = text.match(/^> ([\s\S]*?)\n\n([\s\S]*)$/)
  if (match) {
    const quoteEl = document.createElement('div')
    quoteEl.className = 'reply-quote'
    quoteEl.textContent = match[1]
    const textEl = document.createElement('div')
    textEl.innerHTML = renderMarkdown(match[2])
    bubble.appendChild(quoteEl)
    bubble.appendChild(textEl)
  } else {
    bubble.innerHTML = renderMarkdown(text)
  }
}

function appendBubble(role, text, streaming = false, isError = false, ts = null, attachInfo = null) {
  const id = ++msgIdCounter
  const row = document.createElement('div')
  row.className = `message-row ${role}`
  row.dataset.msgId = id
  if (ts) tsIndex.set(id, ts)

  // Bubble + reactions bar
  const wrap = document.createElement('div')
  wrap.className = 'bubble-wrap'

  const bubble = document.createElement('div')
  bubble.className = `bubble${streaming ? ' streaming' : ''}${isError ? ' error-bubble' : ''}`
  if (attachInfo) {
    if (attachInfo.type === 'image') {
      const img = document.createElement('img')
      img.src = `data:${attachInfo.media_type};base64,${attachInfo.data}`
      img.className = 'bubble-image'
      img.alt = attachInfo.filename || 'image'
      if (attachInfo.prompt) img.dataset.prompt = attachInfo.prompt
      img.dataset.imagePrefix = (attachInfo.data || '').slice(0, 100)
      img.addEventListener('click', () => openLightbox(img.src, img.dataset.prompt || ''))
      img.onload = () => scrollToBottom(true)
      if (attachInfo.data) _renderedImages.add(_imageFingerprint(attachInfo.data))
      bubble.appendChild(img)
    } else {
      const pill = document.createElement('div')
      pill.className = 'bubble-file-pill'
      pill.textContent = `◈  ${attachInfo.filename || 'document'}`
      bubble.appendChild(pill)
    }
    if (text) {
      const caption = document.createElement('div')
      caption.className = 'bubble-caption'
      setBubbleContent(caption, text)
      bubble.appendChild(caption)
    }
  } else if (!streaming) {
    setBubbleContent(bubble, text)
  }

  const reactionsBar = document.createElement('div')
  reactionsBar.className = 'reactions-bar'
  reactionsBar.id = `reactions-${id}`

  // Floating action bar — absolutely positioned above bubble, never affects layout
  const actions = document.createElement('div')
  actions.className = 'bubble-actions'

  // Reaction buttons (Ki messages only)
  if (role === 'ki') {
    for (const emoji of ['👍', '❤️', '😂', '👎']) {
      const btn = document.createElement('button')
      btn.className = 'reaction-btn'
      btn.dataset.reaction = emoji
      btn.textContent = emoji
      btn.addEventListener('click', () => { toggleReaction(id, emoji); closeAllActions() })
      actions.appendChild(btn)
    }
    const sep = document.createElement('span')
    sep.className = 'action-sep'
    actions.appendChild(sep)
  }

  // Reply button
  const replyBtn = document.createElement('button')
  replyBtn.className = 'reply-btn'
  replyBtn.textContent = '↩'
  replyBtn.title = 'Reply'
  replyBtn.addEventListener('click', () => {
    let replyText = text
    if (!replyText && attachInfo) {
      replyText = attachInfo.type === 'image' ? '[image]' : `[${attachInfo.filename || 'document'}]`
    }
    const match = replyText ? replyText.match(/^> [\s\S]*?\n\n([\s\S]*)$/) : null
    setReply(match ? match[1] : replyText)
    closeAllActions()
  })
  actions.appendChild(replyBtn)

  // Timestamp footer — inside bubble-wrap, always visible, no layout shift
  const footer = document.createElement('div')
  footer.className = 'bubble-footer'
  const timeEl = document.createElement('span')
  timeEl.className = 'bubble-time'
  if (ts) {
    timeEl.textContent = new Date(ts).toLocaleTimeString('en-NZ', { hour: '2-digit', minute: '2-digit', hour12: false })
  }
  footer.appendChild(timeEl)

  // Always-visible 👎 button for Ki messages — shown even during streaming
  // so the user can signal a bad response at any point without needing hover/long-press
  if (role === 'ki') {
    const thumbsDownBtn = document.createElement('button')
    thumbsDownBtn.className = 'thumbs-down-btn'
    thumbsDownBtn.textContent = '👎'
    thumbsDownBtn.title = 'Thumbs down'
    thumbsDownBtn.addEventListener('click', () => {
      toggleReaction(id, '👎')
      closeAllActions()
    })
    footer.appendChild(thumbsDownBtn)
  }

  wrap.appendChild(actions)
  wrap.appendChild(bubble)
  wrap.appendChild(reactionsBar)
  wrap.appendChild(footer)

  row.appendChild(wrap)
  attachDoubleTap(row)
  document.getElementById('messages-inner').appendChild(row)
  return bubble
}

function saveReactions() {
  const out = {}
  for (const [msgId, set] of reactions) {
    const ts = tsIndex.get(msgId)
    if (ts && set.size > 0) out[ts] = [...set]
  }
  localStorage.setItem('ki-reactions', JSON.stringify(out))
}

function restoreReactions() {
  let stored
  try { stored = JSON.parse(localStorage.getItem('ki-reactions') || '{}') } catch { stored = {} }
  const msgIdByTs = new Map()
  for (const [msgId, ts] of tsIndex) msgIdByTs.set(ts, msgId)
  for (const [ts, emojis] of Object.entries(stored)) {
    const msgId = msgIdByTs.get(ts)
    if (!msgId) continue
    reactions.set(msgId, new Set(emojis))
    renderReactions(msgId)
  }
}

function toggleReaction(msgId, emoji) {
  if (!reactions.has(msgId)) reactions.set(msgId, new Set())
  const set = reactions.get(msgId)
  const adding = !set.has(emoji)
  if (adding) { set.add(emoji) } else { set.delete(emoji) }
  renderReactions(msgId)
  saveReactions()

  if (adding) {
    const row = document.querySelector(`[data-msg-id="${msgId}"]`)
    const bubble = row?.querySelector('.bubble')
    const isStillStreaming = bubble?.classList.contains('streaming') ?? false

    if (isStillStreaming) {
      // The DB row for this message hasn't been committed yet — queue the
      // reaction and fire it once finaliseKiBubble() removes .streaming.
      if (!pendingStreamReactions.has(msgId)) pendingStreamReactions.set(msgId, new Set())
      pendingStreamReactions.get(msgId).add(emoji)
      return
    }

    const messageText = bubble?.textContent || ''
    // 👎 logs a thumbs-down signal (sets signal='thumbs_down' on the DB row)
    // All other reactions save a memory via the standard reaction endpoint
    const endpoint = emoji === '👎' ? '/desktop/reaction-negative' : '/desktop/reaction'
    apiFetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_text: messageText, reaction: emoji }),
    }).catch(err => console.error('[reaction]', err))
  }
}

function renderReactions(msgId) {
  const bar = document.getElementById(`reactions-${msgId}`)
  if (!bar) return
  const row = bar.closest('.message-row')
  bar.innerHTML = ''
  const set = reactions.get(msgId) || new Set()
  for (const emoji of ['👍', '❤️', '😂', '👎']) {
    if (!set.has(emoji)) continue
    const pill = document.createElement('button')
    pill.className = 'reaction-pill active'
    pill.textContent = emoji
    pill.addEventListener('click', () => toggleReaction(msgId, emoji))
    bar.appendChild(pill)
  }
  // Keep hover-popup picker buttons in sync
  row?.querySelectorAll('.reaction-btn').forEach(btn => {
    btn.classList.toggle('active', set.has(btn.dataset.reaction))
  })
  // Keep always-visible thumbs-down button in sync
  row?.querySelector('.thumbs-down-btn')?.classList.toggle('active', set.has('👎'))
}

function finaliseKiBubble() {
  if (currentKiBubble) {
    currentKiBubble.classList.remove('streaming')

    const row = currentKiBubble.closest('.message-row')
    const msgId = row ? parseInt(row.dataset.msgId, 10) : null

    if (!currentKiBubble.textContent.trim()) {
      // Remove empty bubble — also discard any pending reactions for it
      if (msgId != null) pendingStreamReactions.delete(msgId)
      row?.remove()
    } else {
      // Apply markdown rendering to streamed text
      const raw = currentKiBubble.textContent
      currentKiBubble.classList.add('markdown-bubble')
      currentKiBubble.innerHTML = renderMarkdown(raw)
    }
    if (msgId != null && pendingStreamReactions.has(msgId)) {
      // Stream is done and the DB row is now committed — fire queued reactions
      const messageText = currentKiBubble?.textContent || ''
      for (const emoji of pendingStreamReactions.get(msgId)) {
        const endpoint = emoji === '👎' ? '/desktop/reaction-negative' : '/desktop/reaction'
        apiFetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message_text: messageText, reaction: emoji }),
        }).catch(err => console.error('[reaction-queued]', err))
      }
      pendingStreamReactions.delete(msgId)
    }

    currentKiBubble = null
  }
}

// ── Voice mode ────────────────────────────────────────────────────────────────
function appendVoiceBubble(role, audioBase64, mimeType, ts, transcription) {
  const id = ++msgIdCounter
  const row = document.createElement('div')
  row.className = `message-row ${role}`
  row.dataset.msgId = id
  const bubbleTs = ts || new Date().toISOString()
  tsIndex.set(id, bubbleTs)

  const wrap = document.createElement('div')
  wrap.className = 'bubble-wrap'

  const bubble = document.createElement('div')
  if (role === 'user') {
    const displayText = transcription
      ? transcription.replace(/^\[Voice message\]:\s*/, '')
      : null
    bubble.className = 'bubble voice-label-bubble' + (displayText ? ' voice-label-transcription' : '')
    bubble.textContent = displayText || '🎙 Voice message'
  } else {
    bubble.className = 'bubble voice-audio-bubble'
    if (audioBase64) {
      const audioEl = document.createElement('audio')
      audioEl.style.cssText = 'position:absolute;width:0;height:0;visibility:hidden'
      bubble.appendChild(audioEl)
      lastKiAudioEl = audioEl
      const playLastBtn = document.getElementById('play-last-btn')
      if (playLastBtn) {
        playLastBtn.disabled = false
        playLastBtn.innerHTML = SVG_PLAY_LAST
      }

      const player = document.createElement('div')
      player.className = 'voice-player'

      const SVG_PLAY = `<svg viewBox="0 0 16 16" fill="currentColor" width="12" height="12"><polygon points="3,1 13,8 3,15"/></svg>`
      const SVG_PAUSE = `<svg viewBox="0 0 16 16" fill="currentColor" width="12" height="12"><rect x="2" y="1" width="4" height="14" rx="1"/><rect x="10" y="1" width="4" height="14" rx="1"/></svg>`

      const playBtn = document.createElement('button')
      playBtn.className = 'voice-play-btn'
      playBtn.innerHTML = SVG_PLAY
      playBtn.dataset.state = 'play'

      const track = document.createElement('div')
      track.className = 'voice-progress-track'
      const fill = document.createElement('div')
      fill.className = 'voice-progress-fill'
      fill.style.width = '0%'
      track.appendChild(fill)

      player.appendChild(playBtn)
      player.appendChild(track)
      bubble.appendChild(player)

      playBtn.addEventListener('click', () => {
        if (audioEl.paused) {
          audioEl.play().catch(() => {})
        } else {
          audioEl.pause()
        }
      })

      audioEl.addEventListener('play', () => {
        playBtn.innerHTML = SVG_PAUSE
        playBtn.dataset.state = 'pause'
        if (audioEl === lastKiAudioEl) setPlayLastIcon('pause')
      })

      audioEl.addEventListener('pause', () => {
        playBtn.innerHTML = SVG_PLAY
        playBtn.dataset.state = 'play'
        if (audioEl === lastKiAudioEl) setPlayLastIcon('play')
      })

      audioEl.addEventListener('timeupdate', () => {
        if (isFinite(audioEl.duration) && audioEl.duration > 0) {
          fill.style.width = (audioEl.currentTime / audioEl.duration * 100) + '%'
        }
      })

      audioEl.addEventListener('loadedmetadata', () => {
        if (isFinite(audioEl.duration) && audioEl.duration > 0) {
          fill.style.width = '0%'
        }
      })

      audioEl.addEventListener('ended', () => {
        playBtn.innerHTML = SVG_PLAY
        playBtn.dataset.state = 'play'
        fill.style.width = '0%'
        if (audioEl === lastKiAudioEl) setPlayLastIcon('play')
      })

      track.addEventListener('click', e => {
        const rect = track.getBoundingClientRect()
        const ratio = (e.clientX - rect.left) / rect.width
        if (audioEl.duration) {
          audioEl.currentTime = ratio * audioEl.duration
        }
      })

      audioEl.src = `data:${mimeType || 'audio/mpeg'};base64,${audioBase64}`

      const cleanTranscription = transcription
        ? transcription.replace(/^\[Voice message\]:\s*/, '').trim()
        : null
      if (cleanTranscription) {
        const transcriptRow = document.createElement('div')
        transcriptRow.className = 'voice-transcript-row'

        const toggleBtn = document.createElement('button')
        toggleBtn.className = 'voice-transcript-toggle'
        toggleBtn.textContent = 'Show text'

        const transcriptText = document.createElement('div')
        transcriptText.className = 'voice-transcript-text'
        transcriptText.style.display = 'none'
        transcriptText.textContent = cleanTranscription

        toggleBtn.addEventListener('click', () => {
          if (transcriptText.style.display === 'none') {
            transcriptText.style.display = 'block'
            toggleBtn.textContent = 'Hide text'
          } else {
            transcriptText.style.display = 'none'
            toggleBtn.textContent = 'Show text'
          }
        })

        transcriptRow.appendChild(toggleBtn)
        transcriptRow.appendChild(transcriptText)
        bubble.appendChild(transcriptRow)
      }
    }
  }

  const footer = document.createElement('div')
  footer.className = 'bubble-footer'
  const timeEl = document.createElement('span')
  timeEl.className = 'bubble-time'
  timeEl.textContent = new Date(bubbleTs).toLocaleTimeString('en-NZ', { hour: '2-digit', minute: '2-digit', hour12: false })
  footer.appendChild(timeEl)

  wrap.appendChild(bubble)
  wrap.appendChild(footer)
  row.appendChild(wrap)
  document.getElementById('messages-inner').appendChild(row)
  return bubble
}

function updateInputMode() {
  const inputRow = document.querySelector('.input-row')
  const voiceControls = document.getElementById('voice-controls')
  if (voiceMode) {
    inputRow.classList.add('hidden')
    voiceControls.classList.remove('hidden')
  } else {
    inputRow.classList.remove('hidden')
    voiceControls.classList.add('hidden')
    if (isRecording) stopRecording(false)
  }
}

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    const mimeType = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : 'audio/ogg'
    mediaRecorder = new MediaRecorder(stream, { mimeType })
    audioChunks = []
    mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data) }
    mediaRecorder.onstop = () => sendVoiceMessage(mimeType)
    mediaRecorder.start()
    isRecording = true
    document.getElementById('record-btn').classList.add('recording')
    document.getElementById('record-label').textContent = 'Recording… tap to send'
  } catch (err) {
    console.error('[voice] microphone error:', err)
    const msg = err.name === 'NotAllowedError'
      ? 'Mic blocked — enable in browser/OS settings'
      : 'Mic error: ' + err.message
    document.getElementById('record-label').textContent = msg
  }
}

function stopRecording(send) {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    if (!send) {
      // Cancel without triggering onstop
      const prevOnStop = mediaRecorder.onstop
      mediaRecorder.onstop = null
      mediaRecorder.stop()
      mediaRecorder.onstop = null
      audioChunks = []
    } else {
      mediaRecorder.stop()
    }
    mediaRecorder.stream.getTracks().forEach(t => t.stop())
  }
  isRecording = false
  document.getElementById('record-btn').classList.remove('recording')
  document.getElementById('record-label').textContent = send ? 'Sending…' : 'Tap to record'
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onloadend = () => resolve(reader.result.split(',')[1])
    reader.onerror = reject
    reader.readAsDataURL(blob)
  })
}

async function sendVoiceMessage(mimeType) {
  if (audioChunks.length === 0) {
    document.getElementById('record-label').textContent = 'Tap to record'
    return
  }
  const blob = new Blob(audioChunks, { type: mimeType })
  audioChunks = []

  let base64
  try {
    base64 = await blobToBase64(blob)
  } catch (err) {
    console.error('[voice] base64 conversion failed:', err)
    document.getElementById('record-label').textContent = 'Tap to record'
    return
  }

  const placeholderBubble = appendVoiceBubble('user', null, null)
  const placeholderRow = placeholderBubble.closest('.message-row')
  if (placeholderRow) placeholderRow.dataset.voicePlaceholder = 'true'
  scrollToBottom()

  isStreaming = true
  setKitt(true)

  try {
    const res = await apiFetch('/desktop/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: '',
        channel: 'desktop',
        audio: { data: base64, mime_type: mimeType },
        voice_mode: true,
      }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    await readStream(res)
    // Update the placeholder bubble with the actual transcript and advance latestMessageTs
    // so pollNewMessages won't re-append any of these messages
    try {
      const histRes = await apiFetch('/desktop/history?channel=desktop&limit=5')
      if (histRes.ok) {
        const { messages } = await histRes.json()
        if (messages && messages.length > 0) {
          const lastUser = [...messages].reverse().find(
            m => m.role === 'user' && m.content && m.content.startsWith('[Voice message]:')
          )
          if (lastUser && placeholderRow) {
            const bubble = placeholderRow.querySelector('.bubble')
            if (bubble) {
              bubble.textContent = lastUser.content.replace(/^\[Voice message\]:\s*/, '')
              bubble.classList.add('voice-label-transcription')
            }
            delete placeholderRow.dataset.voicePlaceholder
          }
          latestMessageTs = messages[messages.length - 1].ts
        }
      }
    } catch {}
  } catch (err) {
    appendBubble('ki', `Error: ${err.message}`, false, true)
    console.error('[voice] stream error:', err)
  } finally {
    finaliseCurrentToolRow()
    isStreaming = false
    setKitt(false)
    document.getElementById('record-label').textContent = 'Tap to record'
    scrollToBottom()
  }
}

function appendActivityRow(toolName, labelOrFiles, state) {
  const row = document.createElement('div')
  row.className = `activity-row ${state}`
  row.dataset.tool = toolName
  row.dataset.startTime = new Date().toISOString()

  const dot = document.createElement('span')
  dot.className = 'act-dot'

  const icon = document.createElement('span')
  icon.className = 'act-icon'
  icon.textContent = toolName === 'vault' ? '⌗' : (TOOL_ICONS[toolName] || '◦')

  const nameEl = document.createElement('span')
  nameEl.className = 'act-name'
  nameEl.textContent = toolName === 'vault' ? 'vault context' : (TOOL_NAMES[toolName] || toolName.replace(/_/g, ' '))

  row.appendChild(dot)
  row.appendChild(icon)
  row.appendChild(nameEl)

  // label: string for tools, array of filenames for vault
  if (Array.isArray(labelOrFiles) && labelOrFiles.length) {
    const lbl = document.createElement('span')
    lbl.className = 'act-label'
    lbl.textContent = labelOrFiles.slice(0, 3).join(', ') + (labelOrFiles.length > 3 ? ` +${labelOrFiles.length - 3}` : '')
    row.appendChild(lbl)
    row.dataset.label = labelOrFiles.join(', ')
  } else if (typeof labelOrFiles === 'string' && labelOrFiles) {
    const lbl = document.createElement('span')
    lbl.className = 'act-label'
    lbl.textContent = labelOrFiles
    row.appendChild(lbl)
    row.dataset.label = labelOrFiles
  }

  // Chevron — visible on hover via CSS
  const chevron = document.createElement('span')
  chevron.className = 'act-chevron'
  chevron.textContent = '›'
  row.appendChild(chevron)

  row.addEventListener('click', () => openLogsPanel(row))

  document.getElementById('messages-inner').appendChild(row)
  scrollToBottom()
  return row
}

function appendFileRow(filename, data, mediaType) {
  const row = document.createElement('div')
  row.className = 'file-row'

  const bubble = document.createElement('div')
  bubble.className = 'file-bubble'

  const icon = document.createElement('span')
  icon.className = 'file-bubble-icon'
  icon.textContent = mediaType && mediaType.startsWith('image/') ? '⊙' : '◈'

  const link = document.createElement('a')
  link.textContent = filename
  link.download = filename
  const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0))
  const blob = new Blob([bytes], { type: mediaType || 'application/octet-stream' })
  link.href = URL.createObjectURL(blob)

  bubble.appendChild(icon)
  bubble.appendChild(link)
  row.appendChild(bubble)
  document.getElementById('messages-inner').appendChild(row)
  scrollToBottom()
}

function finaliseCurrentToolRow() {
  if (currentToolRow) {
    currentToolRow.classList.replace('running', 'done')
    currentToolRow = null
  }
}

// ── Tool logs side panel ──────────────────────────────────────────────────────
function initLogsPanel() {
  document.getElementById('logs-panel-close').addEventListener('click', closeLogsPanel)
  document.getElementById('logs-panel-backdrop').addEventListener('click', closeLogsPanel)
}

function openLogsPanel(row) {
  logsActiveRow = row
  const toolName = row.dataset.tool
  const startTime = row.dataset.startTime
  const label = row.dataset.label || ''
  const isRunning = row.classList.contains('running')

  document.getElementById('logs-panel-icon').textContent = toolName === 'vault' ? '⌗' : (TOOL_ICONS[toolName] || '◦')
  document.getElementById('logs-panel-name').textContent = toolName === 'vault' ? 'vault context' : (TOOL_NAMES[toolName] || toolName.replace(/_/g, ' '))
  document.getElementById('logs-panel-meta').textContent = startTime ? new Date(startTime).toLocaleTimeString() : ''

  const body = document.getElementById('logs-panel-body')
  body.innerHTML = '<div class="logs-panel-spinner">loading logs…</div>'

  const backdrop = document.getElementById('logs-panel-backdrop')
  const panel = document.getElementById('logs-panel')
  backdrop.classList.add('open')
  panel.classList.add('open')

  clearInterval(logsPollInterval)
  logsPollInterval = null

  fetchAndDisplayLogs(toolName)

  if (isRunning) {
    logsPollInterval = setInterval(() => {
      if (logsActiveRow && logsActiveRow.classList.contains('running')) {
        fetchAndDisplayLogs(logsActiveRow.dataset.tool)
      } else {
        clearInterval(logsPollInterval)
        logsPollInterval = null
        if (logsActiveRow) fetchAndDisplayLogs(logsActiveRow.dataset.tool)
      }
    }, 2000)
  }
}

async function fetchAndDisplayLogs(toolName) {
  const service = TOOL_LOG_SERVICE[toolName] || 'api'
  const body = document.getElementById('logs-panel-body')
  if (!body) return
  try {
    const res = await apiFetch(`/admin/logs/${service}?lines=300`)
    if (!res.ok) {
      body.textContent = `Error ${res.status}: could not fetch logs`
      return
    }
    const data = await res.json()
    const text = (data.lines || '').trim()
    const atBottom = body.scrollHeight - body.scrollTop <= body.clientHeight + 40
    body.textContent = text || ''
    if (!text) {
      const empty = document.createElement('span')
      empty.className = 'log-empty'
      empty.textContent = 'No logs yet.'
      body.appendChild(empty)
    }
    if (atBottom) body.scrollTop = body.scrollHeight
  } catch (e) {
    body.textContent = `Failed to load logs: ${e.message}`
  }
}

function closeLogsPanel() {
  clearInterval(logsPollInterval)
  logsPollInterval = null
  logsActiveRow = null

  document.getElementById('logs-panel-backdrop').classList.remove('open')
  document.getElementById('logs-panel').classList.remove('open')
}

function appendDaySeparator(label) {
  const el = document.createElement('div')
  el.className = 'day-separator'
  el.textContent = label
  document.getElementById('messages-inner').appendChild(el)
}

function appendEmptyState() {
  const msg = 'Start a conversation'
  appendSystemNote(msg)
}

function appendSystemNote(text) {
  const el = document.createElement('div')
  el.style.cssText = 'text-align:center;color:var(--text-dimmer);font-size:12px;padding:24px 16px;'
  el.textContent = text
  document.getElementById('messages-inner').appendChild(el)
}

function clearMessages() {
  document.getElementById('messages-inner').innerHTML = ''
  currentKiBubble = null
  currentToolRow = null
  reactions.clear()
  tsIndex.clear()
  msgIdCounter = 0
  pendingStreamReactions.clear()
}

function scrollToBottom(smooth = true) {
  const el = document.getElementById('messages')
  if (smooth) {
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  } else {
    el.scrollTop = el.scrollHeight
  }
}

// ── Connection overlay + KITT scanner ────────────────────────────────────────
// The full-page overlay blocks all interaction while connecting.
// The KITT scanner (waveform bars) shows when Ki is actively generating.

let _overlayHideTimer = null

function setConnection(state) {
  const overlay = document.getElementById('connection-overlay')
  if (!overlay) return

  if (state === 'online' || state === 'offline') {
    // Fade out first, then fully remove from layout
    overlay.classList.add('hidden')
    if (_overlayHideTimer) clearTimeout(_overlayHideTimer)
    _overlayHideTimer = setTimeout(() => overlay.classList.add('gone'), 480)
  } else if (state === 'connecting') {
    // Ensure visible (e.g. on reconnect)
    overlay.classList.remove('gone')
    // Force a reflow so the transition fires if we're re-showing
    void overlay.offsetWidth
    overlay.classList.remove('hidden')
  }
}

function setKitt(active) {
  document.getElementById('kitt-scanner').classList.toggle('hidden', !active)
}

// Background health poll — keeps the dot accurate independently of chat activity
async function pollConnection() {
  try {
    const res = await apiFetch('/health')
    setConnection(res.ok ? 'online' : 'offline')
  } catch {
    setConnection('offline')
  }
}
setInterval(pollConnection, 30_000)

// Post-stream image sync — fetch history for any Ki images that may not have
// rendered via the SSE stream (large payloads can be dropped by proxies).
// save_turn runs before 'done', so the image is already in DB when we query.
async function _postStreamImageSync(afterTs) {
  if (!afterTs) return
  try {
    const res = await apiFetch(`/desktop/history?channel=desktop&limit=10&after=${encodeURIComponent(afterTs)}`)
    if (!res.ok) return
    const { messages } = await res.json()
    if (!messages || messages.length === 0) return
    // Always advance latestMessageTs to the server's DB timestamp — never use
    // client clock, which can be ahead of server and cause polls to skip messages.
    latestMessageTs = messages[messages.length - 1].ts
    for (const msg of messages) {
      if (!msg.image_data || msg.role === 'user') continue
      const fp = _imageFingerprint(msg.image_data)
      if (_renderedImages.has(fp)) {
        // Already rendered — repair if the element decoded as broken (e.g. truncated SSE payload)
        const prefix = msg.image_data.slice(0, 100)
        const existingImg = document.querySelector(`.bubble-image[data-image-prefix="${CSS.escape(prefix)}"]`)
        if (existingImg && existingImg.complete && existingImg.naturalWidth === 0) {
          existingImg.src = `data:${msg.image_media_type};base64,${msg.image_data}`
        }
        continue
      }
      appendBubble('ki', msg.content || '', false, false, msg.ts, {
        type: 'image', data: msg.image_data, media_type: msg.image_media_type,
      })
      scrollToBottom()
    }
  } catch { /* silent */ }
}

// Background message poll — picks up heartbeat proactive messages and any
// other server-originated messages without needing a page reload
let _isPollingMessages = false
async function pollNewMessages() {
  if (!latestMessageTs || _isPollingMessages || isStreaming) return
  _isPollingMessages = true
  try {
    const res = await apiFetch(`/desktop/history?channel=desktop&limit=10&after=${encodeURIComponent(latestMessageTs)}`)
    if (!res.ok) return
    const { messages } = await res.json()
    if (!messages || messages.length === 0) return
    for (const msg of messages) {
      const isVoiceMsg = msg.voice_audio || (msg.role === 'user' && msg.content && msg.content.startsWith('[Voice message]:'))
      if (voiceMode && !isVoiceMsg) continue
      if (!voiceMode && isVoiceMsg) continue
      if (isVoiceMsg) {
        // Skip Ki audio already shown via SSE audio event (within 10s tolerance)
        if (msg.role !== 'user' && _lastVoiceResponseTs) {
          const diff = Math.abs(new Date(msg.ts) - new Date(_lastVoiceResponseTs))
          if (diff < 10000) continue
        }
        // Skip user voice message if placeholder bubble is still pending resolution
        if (msg.role === 'user' && document.querySelector('[data-voice-placeholder]')) continue
        const polledBubble = appendVoiceBubble(msg.role === 'user' ? 'user' : 'ki', msg.voice_audio || null, msg.voice_audio ? 'audio/mpeg' : null, msg.ts, msg.content)
        if (msg.role !== 'user' && polledBubble) {
          const audioEl = polledBubble.querySelector('audio')
          if (audioEl) audioEl.play().catch(() => _autoPlayAudio(msg.voice_audio, 'audio/mpeg'))
        }
      } else {
        const attachInfo = msg.image_data
          ? { type: 'image', data: msg.image_data, media_type: msg.image_media_type }
          : null
        if (attachInfo) {
          // Skip if this exact image is already rendered (e.g. by _postStreamImageSync or SSE)
          if (_renderedImages.has(_imageFingerprint(msg.image_data))) continue
        }
        appendBubble(msg.role === 'user' ? 'user' : 'ki', msg.content, false, false, msg.ts, attachInfo)
      }
    }
    latestMessageTs = messages[messages.length - 1].ts
    requestAnimationFrame(() => requestAnimationFrame(() => scrollToBottom(true)))
  } catch {
    // silent — don't spam console on network errors
  } finally {
    _isPollingMessages = false
  }
}
setInterval(pollNewMessages, 30_000)

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') pollNewMessages()
})
window.addEventListener('focus', () => pollNewMessages())


// ── Attachments ───────────────────────────────────────────────────────────────
function attachFile(file) {
  if (!file) return
  const reader = new FileReader()
  reader.onload = ev => {
    const dataUrl = ev.target.result
    const [prefix, data] = dataUrl.split(',')
    const media_type = prefix.match(/:(.*?);/)[1]
    const isImage = media_type.startsWith('image/')
    attachment = { type: isImage ? 'image' : 'document', data, media_type, filename: file.name }

    const bar = document.getElementById('attachment-bar')
    const thumb = document.getElementById('attachment-thumb')
    const nameEl = document.getElementById('attachment-name')

    if (isImage) {
      thumb.src = dataUrl
      thumb.classList.remove('hidden')
      nameEl.textContent = file.name
    } else {
      thumb.classList.add('hidden')
      nameEl.textContent = `◈  ${file.name}`
    }
    bar.classList.remove('hidden')
    sendBtn.disabled = isStreaming
    document.getElementById('message-input').focus()
  }
  reader.readAsDataURL(file)
}

document.getElementById('file-input').addEventListener('change', e => {
  attachFile(e.target.files[0])
  e.target.value = ''
})

// ── Drag and drop ─────────────────────────────────────────────────────────────
let dragDepth = 0

const chatScreen = document.getElementById('chat-screen')
const dropOverlay = document.getElementById('drop-overlay')

chatScreen.addEventListener('dragenter', e => {
  if (!e.dataTransfer.types.includes('Files')) return
  e.preventDefault()
  dragDepth++
  dropOverlay.classList.remove('hidden')
})

chatScreen.addEventListener('dragleave', () => {
  dragDepth--
  if (dragDepth <= 0) {
    dragDepth = 0
    dropOverlay.classList.add('hidden')
  }
})

chatScreen.addEventListener('dragover', e => {
  if (!e.dataTransfer.types.includes('Files')) return
  e.preventDefault()
})

chatScreen.addEventListener('drop', e => {
  e.preventDefault()
  dragDepth = 0
  dropOverlay.classList.add('hidden')
  const file = e.dataTransfer.files[0]
  if (file) attachFile(file)
})

document.getElementById('attachment-cancel').addEventListener('click', clearAttachment)

function clearAttachment() {
  attachment = null
  document.getElementById('attachment-bar').classList.add('hidden')
  document.getElementById('attachment-thumb').src = ''
  document.getElementById('attachment-thumb').classList.add('hidden')
  document.getElementById('attachment-name').textContent = ''
  const text = document.getElementById('message-input').value.trim()
  sendBtn.disabled = text === '' || isStreaming
}

// ── Reply ─────────────────────────────────────────────────────────────────────
function setReply(text) {
  replyTo = text
  const preview = text.length > 90 ? text.slice(0, 90) + '…' : text
  document.getElementById('reply-preview').textContent = preview
  document.getElementById('reply-bar').classList.remove('hidden')
  document.getElementById('message-input').focus()
}

function clearReply() {
  replyTo = null
  document.getElementById('reply-bar').classList.add('hidden')
  document.getElementById('reply-preview').textContent = ''
}

document.getElementById('reply-cancel').addEventListener('click', clearReply)

// ── API fetch helper ──────────────────────────────────────────────────────────
function apiFetch(path, options = {}) {
  const url = path
  const headers = {
    'Authorization': `Bearer ${config.secret}`,
    ...(options.headers || {}),
  }
  return fetch(url, { ...options, headers })
}

// ── Voice mode toggle ─────────────────────────────────────────────────────────
document.getElementById('voice-btn').addEventListener('click', () => {
  voiceMode = !voiceMode
  document.getElementById('voice-btn').classList.toggle('active', voiceMode)
  updateInputMode()
  clearMessages()
  if (!voiceMode) {
    lastKiAudioEl = null
    const playLastBtn = document.getElementById('play-last-btn')
    if (playLastBtn) {
      playLastBtn.disabled = true
      playLastBtn.innerHTML = SVG_PLAY_LAST
    }
  }
  loadHistory()
})

document.getElementById('play-last-btn').addEventListener('click', () => {
  if (!lastKiAudioEl) return
  if (lastKiAudioEl.paused) {
    lastKiAudioEl.currentTime = 0
    lastKiAudioEl.play().catch(() => {})
  } else {
    lastKiAudioEl.pause()
  }
})

document.getElementById('record-btn').addEventListener('click', async () => {
  _ensureAudioCtx()
  if (isStreaming) return
  if (isRecording) {
    stopRecording(true)
  } else {
    await startRecording()
  }
})

// ── Media screen ─────────────────────────────────────────────────────────────
document.getElementById('media-btn').addEventListener('click', showMedia)
document.getElementById('media-back-btn').addEventListener('click', hideMedia)


function showMedia() {
  document.getElementById('chat-screen').classList.add('hidden')
  document.getElementById('admin-screen').classList.add('hidden')
  document.getElementById('media-screen').classList.remove('hidden')
  document.getElementById('main-titlebar').classList.add('hidden')
  document.querySelector('#media-screen .ki-name').textContent = 'Media'
  loadMedia()
}

function hideMedia() {
  document.getElementById('media-screen').classList.add('hidden')
  document.getElementById('chat-screen').classList.remove('hidden')
  document.getElementById('main-titlebar').classList.remove('hidden')
}

async function loadMedia() {
  const grid = document.getElementById('media-grid')

  grid.innerHTML = '<div class="media-status">Loading…</div>'
  try {
    const res = await apiFetch('/desktop/media?limit=500')
    const { media } = await res.json()
    grid.innerHTML = ''
    if (!media || media.length === 0) {
      grid.innerHTML = '<div class="media-status">No media yet</div>'
      return
    }
    for (const item of media) {
      const cell = document.createElement('div')
      cell.className = `media-cell ${item.role === 'user' ? 'media-cell-user' : 'media-cell-ki'}`

      const img = document.createElement('img')
      img.src = `data:${item.image_media_type};base64,${item.image_data}`
      img.className = 'media-thumb'
      img.loading = 'lazy'
      img.addEventListener('click', () => openLightbox(img.src, null, item.id, cell))

      const overlay = document.createElement('div')
      overlay.className = 'media-overlay'
      const d = new Date(item.ts)
      overlay.textContent = d.toLocaleDateString('en-NZ', { day: 'numeric', month: 'short', year: 'numeric' })

      const delBtn = document.createElement('button')
      delBtn.className = 'media-delete-btn'
      delBtn.setAttribute('aria-label', 'Delete image')
      delBtn.textContent = '×'
      delBtn.addEventListener('click', async (e) => {
        e.stopPropagation()
        delBtn.disabled = true
        try {
          const res = await apiFetch(`/desktop/media/${item.id}`, { method: 'DELETE' })
          if (!res.ok) throw new Error(`Delete failed: ${res.status}`)
          cell.remove()
          if (!grid.querySelector('.media-cell')) {
            grid.innerHTML = '<div class="media-status">No media yet</div>'
          }
        } catch {
          delBtn.disabled = false
        }
      })

      cell.appendChild(img)
      cell.appendChild(overlay)
      cell.appendChild(delBtn)
      grid.appendChild(cell)
    }
  } catch (err) {
    grid.innerHTML = `<div class="media-status media-error">Failed to load: ${escHtml(err.message)}</div>`
  }
}

// ── Admin panel ───────────────────────────────────────────────────────────────
let adminActiveTab = 'state'
let memoriesFilter = 'all'

document.getElementById('admin-btn').addEventListener('click', showAdmin)
document.getElementById('admin-back-btn').addEventListener('click', returnToChat)

function showAdmin() {
  document.getElementById('chat-screen').classList.add('hidden')
  document.getElementById('admin-screen').classList.remove('hidden')
  document.getElementById('main-titlebar').classList.add('hidden')
  loadAdminTab(adminActiveTab)
}

function returnToChat() {
  document.getElementById('admin-screen').classList.add('hidden')
  document.getElementById('chat-screen').classList.remove('hidden')
  document.getElementById('main-titlebar').classList.remove('hidden')
}

document.querySelectorAll('.admin-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab
    if (tab === adminActiveTab) return
    document.querySelectorAll('.admin-tab').forEach(b => b.classList.remove('active'))
    btn.classList.add('active')
    document.querySelectorAll('.admin-panel').forEach(p => p.classList.add('hidden'))
    document.getElementById(`admin-tab-${tab}`).classList.remove('hidden')
    adminActiveTab = tab
    loadAdminTab(tab)
  })
})

async function loadAdminTab(tab) {
  switch (tab) {
    case 'state':     return loadAdminState()
    case 'reminders': return loadAdminReminders()
    case 'memories':  return loadAdminMemories()
    case 'signals':   return loadAdminSignals()
    case 'usage':     return loadAdminUsage()
    case 'system':    return loadAdminSystem()
  }
}

// ── Admin: State ──────────────────────────────────────────────────────────────
let adminStateHistory = []
let adminStateIndex = 0

async function loadAdminState() {
  const el = document.getElementById('admin-tab-state')
  el.innerHTML = '<div class="admin-loading">Loading…</div>'
  try {
    const res = await apiFetch('/admin/states')
    if (!res.ok) throw new Error(`HTTP ${res.status} — try rebuilding the api container`)
    const { states } = await res.json()
    adminStateHistory = states
    adminStateIndex = 0
    renderAdminStateAtIndex(el)
  } catch (err) {
    el.innerHTML = `<div class="admin-error">${escHtml(err.message)}</div>`
  }
}

function renderAdminStateAtIndex(el) {
  if (!adminStateHistory.length) {
    el.innerHTML = '<div class="admin-empty">No heartbeat state yet — trigger a heartbeat first.</div>'
    return
  }

  const total = adminStateHistory.length
  const entry = adminStateHistory[adminStateIndex]
  const state = entry.state
  const runDate = new Date(entry.created_at).toLocaleString('en-NZ', { dateStyle: 'medium', timeStyle: 'short' })

  let html = `<div class="admin-state-nav">
    <button class="admin-nav-btn" id="state-prev-btn" ${adminStateIndex >= total - 1 ? 'disabled' : ''}>← Older</button>
    <span class="admin-state-nav-label">Run ${total - adminStateIndex} of ${total} — ${runDate}</span>
    <button class="admin-nav-btn" id="state-next-btn" ${adminStateIndex <= 0 ? 'disabled' : ''}>Newer →</button>
  </div>`

  // Energy level badge
  if (state.energy_level) {
    html += `<div class="admin-card">
      <div class="admin-card-label">Energy / state</div>
      <div class="admin-card-text" style="color:var(--accent);font-weight:500">${escHtml(state.energy_level)}</div>
    </div>`
  }

  // Things to mention
  if (state.things_to_mention?.length) {
    html += `<div class="admin-card">
      <div class="admin-card-label">Things to mention</div>
      <ul class="admin-list">${state.things_to_mention.map((t, i) => `<li><span style="color:var(--text-dimmer);margin-right:6px">${i + 1}.</span>${escHtml(t)}</li>`).join('')}</ul>
    </div>`
  }

  if (state.proactive_message) {
    html += `<div class="admin-card">
      <div class="admin-card-label">Proactive message</div>
      <div class="admin-card-text">${escHtml(state.proactive_message)}</div>
    </div>`
  }

  // Ki commitments
  if (state.ki_commitments?.length) {
    html += `<div class="admin-card">
      <div class="admin-card-label">Ki promised</div>
      <ul class="admin-list">${state.ki_commitments.map(c => `<li>${escHtml(c)}</li>`).join('')}</ul>
    </div>`
  }

  // Open todos with staleness from item_metadata
  if (state.open_todos?.length) {
    const meta = state.item_metadata || {}
    const todoItems = state.open_todos.map(t => {
      const m = meta[t] || {}
      let badge = ''
      if (m.first_seen) {
        const days = Math.floor((Date.now() - new Date(m.first_seen).getTime()) / 86400000)
        if (days >= 14) badge = `<span style="font-size:10px;color:#ef4444;margin-left:6px;font-weight:600">[stale ${days}d]</span>`
        else if (days >= 7) badge = `<span style="font-size:10px;color:#f97316;margin-left:6px;font-weight:600">[aging ${days}d]</span>`
      }
      return `<li>${escHtml(t)}${badge}</li>`
    }).join('')
    html += `<div class="admin-card">
      <div class="admin-card-label">Open todos</div>
      <ul class="admin-list">${todoItems}</ul>
    </div>`
  }

  // Open loops
  if (state.open_loops?.length) {
    html += `<div class="admin-card">
      <div class="admin-card-label">Open loops</div>
      <ul class="admin-list">${state.open_loops.map(i => `<li>${escHtml(i)}</li>`).join('')}</ul>
    </div>`
  }

  // Current projects
  if (state.current_projects?.length) {
    html += `<div class="admin-card">
      <div class="admin-card-label">Current projects</div>
      <ul class="admin-list">${state.current_projects.map(i => `<li>${escHtml(i)}</li>`).join('')}</ul>
    </div>`
  }

  // Recent themes
  if (state.recent_themes?.length) {
    html += `<div class="admin-card">
      <div class="admin-card-label">Recent themes</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;padding-top:2px">${state.recent_themes.map(t => `<span style="font-size:12px;padding:2px 8px;border-radius:10px;background:var(--bg-hover);color:var(--text-dimmer)">${escHtml(t)}</span>`).join('')}</div>
    </div>`
  }

  // Snoozed — rendered as structured rows
  if (state.snoozed?.length) {
    const rows = state.snoozed.map(s => {
      const until = s.until ? `<span style="color:var(--text-dimmer);font-size:11px"> · until: ${escHtml(s.until)}</span>` : ''
      const resurface = s.resurface_after ? `<span style="color:var(--text-dimmer);font-size:11px"> · resurface: ${escHtml(s.resurface_after)}</span>` : ''
      const reason = s.reason ? `<div style="font-size:11px;color:var(--text-dimmer);margin-top:1px;padding-left:12px">${escHtml(s.reason)}</div>` : ''
      return `<li><div>${escHtml(s.item || JSON.stringify(s))}${until}${resurface}</div>${reason}</li>`
    }).join('')
    html += `<div class="admin-card">
      <div class="admin-card-label">Snoozed</div>
      <ul class="admin-list">${rows}</ul>
    </div>`
  }

  el.innerHTML = html || '<div class="admin-empty">State is empty.</div>'

  el.querySelector('#state-prev-btn')?.addEventListener('click', () => {
    if (adminStateIndex < adminStateHistory.length - 1) { adminStateIndex++; renderAdminStateAtIndex(el) }
  })
  el.querySelector('#state-next-btn')?.addEventListener('click', () => {
    if (adminStateIndex > 0) { adminStateIndex--; renderAdminStateAtIndex(el) }
  })
}

// ── Admin: Reminders ──────────────────────────────────────────────────────────
let _remindersRefreshTimer = null

async function loadAdminReminders() {
  const el = document.getElementById('admin-tab-reminders')
  el.innerHTML = '<div class="admin-loading">Loading…</div>'
  try {
    const res = await apiFetch('/admin/reminders')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const { reminders } = await res.json()
    renderAdminReminders(el, reminders)
  } catch (err) {
    el.innerHTML = `<div class="admin-error">${escHtml(err.message)}</div>`
  }
}

function renderAdminReminders(el, reminders) {
  const now = new Date()

  // Header row with refresh button
  let html = `<div class="reminders-header">
    <span class="admin-card-label" style="text-transform:none;font-size:13px;letter-spacing:0;color:var(--text-dim)">
      ${reminders.length === 0 ? 'No upcoming reminders' : `${reminders.length} upcoming reminder${reminders.length === 1 ? '' : 's'}`}
    </span>
    <button class="admin-action-btn" id="reminders-refresh-btn">↻ Refresh</button>
  </div>`

  if (reminders.length === 0) {
    html += `<div class="reminders-empty">
      <div class="reminders-empty-icon">🔔</div>
      <div class="reminders-empty-text">No upcoming reminders</div>
      <div class="reminders-empty-sub">Ask Ki to set a reminder and it'll appear here</div>
    </div>`
  } else {
    html += '<div class="reminders-list">'
    for (const r of reminders) {
      const remindAt = new Date(r.remind_at_utc)
      const diffMs = remindAt - now
      const diffMins = Math.round(diffMs / 60000)
      const isOverdue = diffMs < 0

      // Format the scheduled time in the browser's local timezone
      const dateStr = remindAt.toLocaleDateString(undefined, {
        weekday: 'short', day: 'numeric', month: 'short', year: 'numeric',
      })
      const timeStr = remindAt.toLocaleTimeString(undefined, {
        hour: '2-digit', minute: '2-digit', hour12: true,
      })

      // Human-readable relative time
      let relative = ''
      if (isOverdue) {
        const overMins = Math.abs(diffMins)
        if (overMins < 60) relative = `${overMins}m overdue`
        else relative = `${Math.floor(overMins / 60)}h overdue`
      } else if (diffMins < 60) {
        relative = `in ${diffMins}m`
      } else if (diffMins < 60 * 24) {
        const h = Math.floor(diffMins / 60), m = diffMins % 60
        relative = m > 0 ? `in ${h}h ${m}m` : `in ${h}h`
      } else {
        const days = Math.floor(diffMins / (60 * 24))
        relative = `in ${days}d`
      }

      const urgencyClass = isOverdue ? 'reminder-overdue' : diffMins < 60 ? 'reminder-soon' : ''

      html += `<div class="reminder-item ${urgencyClass}">
        <div class="reminder-item-left">
          <span class="reminder-bell">${isOverdue ? '🔔' : diffMins < 60 ? '⏰' : '🔔'}</span>
        </div>
        <div class="reminder-item-body">
          <div class="reminder-message">${escHtml(r.message)}</div>
          <div class="reminder-meta">
            <span class="reminder-datetime">${escHtml(dateStr)} · ${escHtml(timeStr)}</span>
            <span class="reminder-relative ${urgencyClass}">${escHtml(relative)}</span>
          </div>
        </div>
        <div class="reminder-item-right">
          <span class="reminder-id">#${r.id}</span>
        </div>
      </div>`
    }
    html += '</div>'
  }

  el.innerHTML = html

  el.querySelector('#reminders-refresh-btn').addEventListener('click', () => loadAdminReminders())

  // Auto-refresh every 60s while this tab is visible
  if (_remindersRefreshTimer) clearInterval(_remindersRefreshTimer)
  _remindersRefreshTimer = setInterval(() => {
    if (adminActiveTab === 'reminders') loadAdminReminders()
  }, 60_000)
}

// ── Admin: Memories ───────────────────────────────────────────────────────────
async function loadAdminMemories() {
  const el = document.getElementById('admin-tab-memories')
  el.innerHTML = '<div class="admin-loading">Loading…</div>'
  try {
    const res = await apiFetch('/admin/memories')
    if (!res.ok) throw new Error(`HTTP ${res.status} — try rebuilding the api container`)
    const { memories } = await res.json()
    renderAdminMemories(el, memories)
  } catch (err) {
    el.innerHTML = `<div class="admin-error">${escHtml(err.message)}</div>`
  }
}

function renderAdminMemories(el, memories) {
  if (!memories || memories.length === 0) {
    el.innerHTML = '<div class="admin-empty">No memories saved yet.</div>'
    return
  }

  const categories = ['all', ...new Set(memories.map(m => m.category))]

  let html = `<div class="admin-filter-bar">${
    categories.map(c => `<button class="admin-filter-btn${c === memoriesFilter ? ' active' : ''}" data-cat="${escHtml(c)}">${escHtml(c)}</button>`).join('')
  }</div><div class="admin-memories-list">`

  for (const m of memories) {
    html += `<div class="admin-memory-item" data-cat="${escHtml(m.category)}">
      <span class="admin-memory-cat">${escHtml(m.category)}</span>
      <span class="admin-memory-content">${escHtml(m.content)}</span>
    </div>`
  }
  html += '</div>'

  el.innerHTML = html

  el.querySelectorAll('.admin-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      memoriesFilter = btn.dataset.cat
      el.querySelectorAll('.admin-filter-btn').forEach(b => b.classList.toggle('active', b.dataset.cat === memoriesFilter))
      el.querySelectorAll('.admin-memory-item').forEach(item => {
        item.style.display = (memoriesFilter === 'all' || item.dataset.cat === memoriesFilter) ? '' : 'none'
      })
    })
  })

  // Apply initial filter
  if (memoriesFilter !== 'all') {
    el.querySelectorAll('.admin-memory-item').forEach(item => {
      item.style.display = item.dataset.cat === memoriesFilter ? '' : 'none'
    })
  }
}

// ── Admin: Signals ────────────────────────────────────────────────────────────
let _signalsRefreshTimer = null

async function loadAdminSignals() {
  const el = document.getElementById('admin-tab-signals')
  el.innerHTML = '<div class="admin-loading">Loading…</div>'
  try {
    const res = await apiFetch('/admin/signals')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    renderAdminSignals(el, data)
  } catch (err) {
    el.innerHTML = `<div class="admin-error">${escHtml(err.message)}</div>`
  }
}

function renderAdminSignals(el, data) {
  const { thumbs_down = [], corrections = [], tool_rules = [] } = data

  let html = `<div class="reminders-header">
    <span class="admin-card-label" style="text-transform:none;font-size:13px;letter-spacing:0;color:var(--text-dim)">
      ${thumbs_down.length} thumbs-down · ${corrections.length} correction${corrections.length === 1 ? '' : 's'} · ${tool_rules.length} rule${tool_rules.length === 1 ? '' : 's'}
    </span>
    <button class="admin-action-btn" id="signals-refresh-btn">↻ Refresh</button>
  </div>`

  // ── Tool Rules section ──
  html += `<div class="admin-card">
    <div class="admin-card-label">🧭 Tool Rules (active in Ki's context)</div>`
  if (tool_rules.length === 0) {
    html += `<div class="admin-empty-inline" style="padding:10px 0 4px">No rules learned yet — 👎 a bad response to generate one</div>`
  } else {
    for (const rule of tool_rules) {
      const ts = new Date(rule.updated_at).toLocaleString('en-NZ', {
        day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
      })
      html += `<div class="signal-item">
        <div class="signal-meta">${escHtml(ts)}</div>
        <div class="signal-content">${escHtml(rule.content)}</div>
      </div>`
    }
  }
  html += `</div>`

  // ── Thumbs Down section ──
  html += `<div class="admin-card">
    <div class="admin-card-label">👎 Thumbs Down</div>`
  if (thumbs_down.length === 0) {
    html += `<div class="admin-empty-inline" style="padding:10px 0 4px">No thumbs-down signals yet</div>`
  } else {
    for (const item of thumbs_down) {
      const ts = new Date(item.created_at).toLocaleString('en-NZ', {
        day: 'numeric', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit', hour12: false,
      })
      const toolsHtml = item.tool_calls && item.tool_calls.length > 0
        ? `<div class="signal-tools">Tools: ${escHtml(item.tool_calls.map(t => `${t.name}(${t.input})`).join(', '))}</div>`
        : `<div class="signal-tools signal-no-tools">No tools used — answered from model knowledge</div>`
      html += `<div class="signal-item signal-thumbsdown">
        <div class="signal-meta">${escHtml(ts)}</div>
        <div class="signal-content">${escHtml(item.content)}</div>
        ${toolsHtml}
      </div>`
    }
  }
  html += `</div>`

  // ── Corrections section ──
  html += `<div class="admin-card">
    <div class="admin-card-label">✏️ Corrections</div>`
  if (corrections.length === 0) {
    html += `<div class="admin-empty-inline" style="padding:10px 0 4px">No corrections captured yet</div>`
  } else {
    for (const item of corrections) {
      const ts = new Date(item.created_at).toLocaleString('en-NZ', {
        day: 'numeric', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit', hour12: false,
      })
      html += `<div class="signal-item signal-correction">
        <div class="signal-meta">${escHtml(ts)}</div>
        <div class="signal-correction-row">
          <span class="signal-label">Ki said</span>
          <span class="signal-content">${escHtml(item.prior_assistant_msg || '—')}</span>
        </div>
        <div class="signal-correction-row">
          <span class="signal-label">Corrected to</span>
          <span class="signal-content signal-correction-text">${escHtml(item.correction_text || '—')}</span>
        </div>
      </div>`
    }
  }
  html += `</div>`

  el.innerHTML = html

  el.querySelector('#signals-refresh-btn').addEventListener('click', () => loadAdminSignals())

  // Auto-refresh every 60s while this tab is visible
  if (_signalsRefreshTimer) clearInterval(_signalsRefreshTimer)
  _signalsRefreshTimer = setInterval(() => {
    if (adminActiveTab === 'signals') loadAdminSignals()
  }, 60_000)
}

// ── Admin: Usage ──────────────────────────────────────────────────────────────
let _costChartInstance = null

async function loadAdminUsage() {
  const el = document.getElementById('admin-tab-usage')
  el.innerHTML = '<div class="admin-loading">Loading…</div>'
  try {
    const res = await apiFetch('/usage')
    const { periods, daily } = await res.json()
    renderAdminUsage(el, periods, daily)
  } catch (err) {
    el.innerHTML = `<div class="admin-error">${escHtml(err.message)}</div>`
  }
}

function fmtTokens(n) {
  if (n == null) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'k'
  return n.toLocaleString()
}

function fmtCacheRate(totals) {
  const total = totals.input_tokens + totals.cache_creation_tokens + totals.cache_read_tokens
  if (total === 0 || totals.cache_read_tokens === 0) return '—'
  return Math.round(totals.cache_read_tokens / total * 100) + '%'
}

function renderAdminUsage(el, periods, daily) {
  if (!periods) {
    el.innerHTML = '<div class="admin-empty">No usage data yet.</div>'
    return
  }

  const PERIOD_LABELS = { '3d': '3 days', '7d': '7 days', '30d': '30 days' }
  const periodKeys = ['3d', '7d', '30d']

  // ── stat tiles ──
  let tiles = '<div class="usage-tiles">'
  for (const key of periodKeys) {
    const { totals } = periods[key]
    const totalTokens = totals.input_tokens + totals.cache_creation_tokens + totals.cache_read_tokens + totals.output_tokens
    tiles += `
    <div class="usage-period-card">
      <div class="usage-period-label">${PERIOD_LABELS[key]}</div>
      <div class="usage-stat">
        <div class="usage-stat-value">$${totals.cost.toFixed(4)}</div>
        <div class="usage-stat-name">cost</div>
      </div>
      <div class="usage-stat">
        <div class="usage-stat-value">${fmtTokens(totalTokens)}</div>
        <div class="usage-stat-name">tokens</div>
      </div>
      <div class="usage-stat">
        <div class="usage-stat-value">${fmtCacheRate(totals)}</div>
        <div class="usage-stat-name">cache hit</div>
      </div>
      <div class="usage-stat">
        <div class="usage-stat-value">${totals.requests.toLocaleString()}</div>
        <div class="usage-stat-name">requests</div>
      </div>
    </div>`
  }
  tiles += '</div>'

  // ── 30d per-model table ──
  const rows = (periods['30d'].rows || []).filter(r => r.requests > 0)

  const PROVIDERS = {
    'claude-':              'Anthropic',
    'text-embedding-':      'OpenAI',
    'whisper-':             'OpenAI',
    'grok-':                'xAI',
    'xai-tts-':             'xAI',
    'dall-e-':              'OpenAI',
  }
  function getProvider(model) {
    for (const [prefix, name] of Object.entries(PROVIDERS)) {
      if (model.startsWith(prefix)) return name
    }
    return 'Other'
  }
  function shortModel(model) {
    return model.replace('claude-', '').replace(/-\d{10}$/, '')
  }
  function isTokenModel(model) {
    return model.startsWith('claude-') || model.startsWith('text-embedding-')
  }

  const totals30 = periods['30d'].totals
  const ucRows = periods['30d'].use_case_rows || []

  const USE_CASE_LABELS = {
    chat:               'Chat',
    heartbeat:          'Heartbeat',
    coding_agent:       'Coding agent',
    image_gen:          'Image gen',
    voice_transcription:'Voice transcription',
    tts:                'Text-to-speech (Ara)',
    memory_classify:    'Memory classification',
    self_improvement:   'Self-improvement',
    bridge:             'Channel bridge',
  }

  // ── Use-case breakdown ──
  let ucTable = `<div class="admin-meta" style="margin-bottom:10px">Last 30 days — by use case</div>`
  if (ucRows.length > 0) {
    const ucTotal = ucRows.reduce((s, r) => s + r.estimated_cost_usd, 0)
    ucTable += `<div class="admin-table-scroll"><table class="admin-table"><thead><tr><th>Use case</th><th>Req</th><th>Cost</th><th></th></tr></thead><tbody>`
    for (const r of ucRows) {
      const pct = ucTotal > 0 ? (r.estimated_cost_usd / ucTotal * 100).toFixed(0) : 0
      const label = USE_CASE_LABELS[r.use_case] || r.use_case
      ucTable += `<tr>
        <td>${escHtml(label)}</td>
        <td>${r.requests.toLocaleString()}</td>
        <td>$${r.estimated_cost_usd.toFixed(4)}</td>
        <td style="width:80px">
          <div style="background:var(--bg-hover);border-radius:3px;height:6px;overflow:hidden">
            <div style="background:var(--accent);height:100%;width:${pct}%"></div>
          </div>
        </td>
      </tr>`
    }
    ucTable += `<tr class="admin-table-total"><td>Total</td><td>${totals30.requests.toLocaleString()}</td><td>$${totals30.cost.toFixed(4)}</td><td></td></tr>`
    ucTable += `</tbody></table></div>`
  } else {
    ucTable += `<div class="admin-empty" style="padding-top:8px;padding-bottom:16px">No use-case data yet — will populate as Ki is used.</div>`
  }

  // ── Per-model table ──
  let table = `<div class="admin-meta" style="margin:18px 0 10px">Last 30 days — by model</div>`
  if (rows.length === 0) {
    table += `<div class="admin-empty" style="padding-top:8px;padding-bottom:16px">No model data yet.</div>`
  } else {
    table += `<div class="admin-table-scroll"><table class="admin-table">
    <thead><tr><th>Provider</th><th>Model</th><th>Req</th><th>In</th><th>Out</th><th>Cache write</th><th>Cache read</th><th>Cost</th></tr></thead>
    <tbody>`
    for (const r of rows) {
      const tokenModel = isTokenModel(r.model)
      table += `<tr>
        <td style="color:var(--text-dimmer);font-size:11px">${escHtml(getProvider(r.model))}</td>
        <td>${escHtml(shortModel(r.model))}</td>
        <td>${r.requests.toLocaleString()}</td>
        <td>${r.input_tokens ? fmtTokens(r.input_tokens) : '—'}</td>
        <td>${r.output_tokens ? fmtTokens(r.output_tokens) : '—'}</td>
        <td>${tokenModel && r.cache_creation_tokens ? fmtTokens(r.cache_creation_tokens) : '—'}</td>
        <td>${tokenModel && r.cache_read_tokens ? fmtTokens(r.cache_read_tokens) : '—'}</td>
        <td>$${r.estimated_cost_usd.toFixed(4)}</td>
      </tr>`
    }
    table += `<tr class="admin-table-total">
      <td colspan="2">Total</td>
      <td>${totals30.requests.toLocaleString()}</td>
      <td>${fmtTokens(totals30.input_tokens)}</td>
      <td>${fmtTokens(totals30.output_tokens)}</td>
      <td>${fmtTokens(totals30.cache_creation_tokens)}</td>
      <td>${fmtTokens(totals30.cache_read_tokens)}</td>
      <td>$${totals30.cost.toFixed(4)}</td>
    </tr></tbody>
    <tfoot><tr><td colspan="8" style="font-size:11px;color:var(--text-dimmer);padding:6px 4px">* For xai-tts-ara, In = characters (billed at $4.20/M chars)</td></tr></tfoot>
    </table></div>`
  }

  // ── Usage by Day table ──
  let dayTable = `<div class="admin-meta" style="margin:18px 0 10px">Last 30 days — by day</div>`
  if (daily && daily.length > 0) {
    dayTable += `<div class="admin-table-scroll"><table class="admin-table">
      <thead><tr><th>Date</th><th>Input Tokens</th><th>Output Tokens</th><th>Total Tokens</th><th>Cost (USD)</th></tr></thead>
      <tbody>`
    for (const r of daily) {
      dayTable += `<tr>
        <td>${escHtml(r.date)}</td>
        <td>${fmtTokens(r.input_tokens)}</td>
        <td>${fmtTokens(r.output_tokens)}</td>
        <td>${fmtTokens(r.total_tokens)}</td>
        <td>$${r.cost.toFixed(4)}</td>
      </tr>`
    }
    dayTable += `</tbody></table></div>`
  } else {
    dayTable += `<div class="admin-empty" style="padding-top:8px;padding-bottom:16px">No daily data yet.</div>`
  }

  const chartHtml = (daily && daily.length > 1) ? `
    <div class="usage-chart-wrap">
      <div class="usage-chart-title">Rolling cost (USD)</div>
      <div style="position:relative;height:200px"><canvas id="cost-chart"></canvas></div>
    </div>` : ''

  el.innerHTML = tiles + chartHtml + ucTable + table + dayTable

  if (daily && daily.length > 1) renderCostChart(daily)
}

function renderCostChart(daily) {
  const canvas = document.getElementById('cost-chart')
  if (!canvas || typeof Chart === 'undefined') return
  if (_costChartInstance) { _costChartInstance.destroy(); _costChartInstance = null }

  const sorted = [...daily].reverse() // API returns reverse-chrono; we need chrono for chart
  const labels = sorted.map(r => r.date.slice(5)) // "MM-DD"

  function rollingSum(window) {
    return sorted.map((_, i) => {
      const start = Math.max(0, i - window + 1)
      return +sorted.slice(start, i + 1).reduce((s, r) => s + r.cost, 0).toFixed(4)
    })
  }

  _costChartInstance = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: '3-day', data: rollingSum(3),  borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.08)', borderWidth: 1.5, pointRadius: 2, tension: 0.3, fill: false },
        { label: '7-day', data: rollingSum(7),  borderColor: '#8b5cf6', backgroundColor: 'rgba(139,92,246,0.08)', borderWidth: 1.5, pointRadius: 2, tension: 0.3, fill: false },
        { label: '30-day', data: rollingSum(30), borderColor: '#10b981', backgroundColor: 'rgba(16,185,129,0.08)', borderWidth: 1.5, pointRadius: 2, tension: 0.3, fill: false },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#888', font: { size: 11 }, boxWidth: 12 } },
        tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: $${ctx.parsed.y.toFixed(4)}` } }
      },
      scales: {
        x: { ticks: { color: '#888', font: { size: 10 }, maxTicksLimit: 10 }, grid: { color: '#2a2a2a' } },
        y: { ticks: { color: '#888', font: { size: 10 }, callback: v => '$' + v.toFixed(2) }, grid: { color: '#2a2a2a' } }
      }
    }
  })
}

// ── Admin: System (merged System + Settings) ──────────────────────────────────
async function loadAdminSystem() {
  const el = document.getElementById('admin-tab-system')
  el.innerHTML = '<div class="admin-loading">Loading…</div>'
  try {
    const [intRes, vaultRes, runsRes, settingsRes, promptsRes, heartbeatPromptRes, sessionRes, googleRes] = await Promise.all([
      apiFetch('/integrations'),
      apiFetch('/admin/vault-status'),
      apiFetch('/admin/schedule-runs'),
      apiFetch('/admin/settings'),
      apiFetch('/admin/prompts'),
      apiFetch('/admin/prompts/heartbeat'),
      apiFetch('/admin/session'),
      apiFetch('/auth/google/status'),
    ])
    if (!vaultRes.ok)  throw new Error(`HTTP ${vaultRes.status} — try rebuilding the api container`)
    if (!runsRes.ok)   throw new Error(`HTTP ${runsRes.status} — try rebuilding the api container`)
    const { integrations } = await intRes.json()
    const vault = await vaultRes.json()
    const { runs, jobs } = await runsRes.json()

    let imageGenModel = 'grok-imagine-image'
    if (settingsRes.ok) {
      const data = await settingsRes.json()
      imageGenModel = data.image_gen_model ?? imageGenModel
    }
    let identityContent = '', instructionsContent = ''
    if (promptsRes.ok) {
      const data = await promptsRes.json()
      identityContent = data.identity ?? ''
      instructionsContent = data.instructions ?? ''
    }
    let heartbeatPromptContent = ''
    if (heartbeatPromptRes.ok) {
      const data = await heartbeatPromptRes.json()
      heartbeatPromptContent = data.content ?? ''
    }
    let sessionInfo = null
    if (sessionRes.ok) sessionInfo = await sessionRes.json()

    let googleStatus = null
    if (googleRes && googleRes.ok) googleStatus = await googleRes.json()

    renderAdminSystem(el, integrations, vault, runs, jobs || {}, imageGenModel, identityContent, instructionsContent, heartbeatPromptContent, sessionInfo, googleStatus)
  } catch (err) {
    el.innerHTML = `<div class="admin-error">${escHtml(err.message)}</div>`
  }
}

function renderAdminSystem(el, integrations, vault, runs, jobs, imageGenModel, identityContent, instructionsContent, heartbeatPromptContent, sessionInfo, googleStatus) {
  let html = ''

  // ── Reload Ki App ──
  html += `<div class="admin-card" id="reload-card">
    <div class="admin-card-label">Reload Ki App</div>
    <div class="admin-kv" style="margin-bottom:8px">
      <span class="admin-kv-key" style="flex:1">Force a full app reload (equivalent to Cmd+R)</span>
    </div>
    <button id="reload-app-btn" class="admin-action-btn">↺ Reload app</button>
  </div>`

  // ── 2. Vault index ──
  html += `<div class="admin-card">
    <div class="admin-card-label">Vault index</div>
    <div class="admin-kv"><span class="admin-kv-key">Last commit</span><span class="admin-kv-val">${vault.vault_commit ? escHtml(vault.vault_commit.slice(0, 8)) : '—'}</span></div>
    <div class="admin-kv"><span class="admin-kv-key">Points indexed</span><span class="admin-kv-val">${vault.point_count ?? '—'}</span></div>
  </div>`

  // ── 3. Heartbeat schedule card ──
  html += renderJobCard('heartbeat', 'Heartbeat', [], [], [], runs, jobs)

  // ── 4. Integrations ──
  if (integrations.length === 0) {
    html += `<div class="admin-card"><div class="admin-card-label">Integrations</div><div class="admin-empty-inline">None loaded</div></div>`
  } else {
    for (const intg of integrations) {
      html += renderJobCard(
        intg.schedules[0] || intg.name,
        intg.name,
        intg.tools,
        intg.schedules,
        intg.required_env_vars || [],
        runs,
        jobs,
      )
    }
  }

  // ── Google Account ──
  {
    const connected = googleStatus?.connected
    const email = googleStatus?.email
    const statusText = connected
      ? `✓ Connected${email ? ` as ${escHtml(email)}` : ''}`
      : 'Not connected'
    html += `<div class="admin-card" id="google-card">
      <div class="admin-card-label">Google Account</div>
      <div class="admin-kv">
        <span class="admin-kv-key">Status</span>
        <span class="admin-kv-val">${statusText}</span>
      </div>
      ${connected
        ? `<button id="google-disconnect-btn" class="admin-action-btn" style="margin-top:8px">Disconnect</button>`
        : `<button id="google-connect-btn" class="admin-action-btn" style="margin-top:8px">Connect Google Account</button>`
      }
    </div>`
  }

  // ── 5. Session ──
  function fmtSessionAge(minutes) {
    if (minutes < 1) return 'just started'
    if (minutes < 60) return `${Math.round(minutes)}m old`
    const h = Math.floor(minutes / 60), m = Math.round(minutes % 60)
    return m > 0 ? `${h}h ${m}m old` : `${h}h old`
  }
  function sessionHtml(info) {
    if (!info) return '<span class="admin-kv-key" style="color:var(--text-dimmer)">Could not load</span>'
    if (!info.active) return '<span class="admin-kv-key" style="color:var(--text-dimmer)">No active session</span>'
    const started = new Date(info.first_message_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    const gapMins = Math.round(info.session_gap_hours * 60)
    return `
      <div class="admin-kv"><span class="admin-kv-key">Status</span><span>${fmtSessionAge(info.age_minutes)}, ${info.message_count} messages</span></div>
      <div class="admin-kv"><span class="admin-kv-key">Started</span><span>${started}</span></div>
      <div class="admin-kv"><span class="admin-kv-key">Gap timeout</span><span>${gapMins} min inactivity</span></div>`
  }
  html += `<div class="admin-card">
    <div class="admin-card-label">Session</div>
    <div id="session-info">${sessionHtml(sessionInfo)}</div>
    <div style="display:flex;align-items:center;gap:8px;margin-top:10px">
      <button class="admin-action-btn" id="session-reset-btn">New session</button>
      <div id="session-reset-status" style="font-size:11px;color:var(--text-dimmer);min-height:16px"></div>
    </div>
  </div>`

  // ── 6. Connection ──
  html += `<div class="admin-card">
    <div class="admin-card-label">Connection</div>
    <button class="admin-action-btn" id="reset-connection-btn">Reset connection settings</button>
  </div>`

  // ── 7. Device permissions ──
  {
    const notifSupported = ('Notification' in window)
    const notifPerm = notifSupported ? Notification.permission : 'unsupported'
    let notifStatusText = '—'
    let notifStatusColor = ''
    let notifBtnLabel = 'Enable Notifications'
    let notifBtnDisabled = false
    let showDisableBtn = false
    if (!notifSupported) {
      notifStatusText = 'Not supported'
      notifBtnLabel = 'Not supported'
      notifBtnDisabled = true
    } else if (notifPerm === 'granted') {
      notifStatusText = '✓ Enabled'
      notifStatusColor = 'color:var(--success,#27ae60)'
      notifBtnLabel = 'Notifications enabled ✓'
      notifBtnDisabled = true
      showDisableBtn = true
    } else if (notifPerm === 'denied') {
      notifStatusText = 'Blocked'
      notifStatusColor = 'color:var(--danger,#c0392b)'
      notifBtnLabel = 'Blocked — enable in browser settings'
      notifBtnDisabled = true
    }
    html += `<div class="admin-card" id="permissions-card">
      <div class="admin-card-label">Device permissions</div>
      <div class="admin-kv">
        <span class="admin-kv-key">Notifications</span>
        <span id="notif-status-val" class="admin-kv-val" style="${notifStatusColor}">${notifStatusText}</span>
      </div>
      <button id="enable-notif-btn" class="admin-action-btn" style="margin-top:8px${showDisableBtn ? ';display:none' : ''}" ${notifBtnDisabled ? 'disabled' : ''}>${notifBtnLabel}</button>
      <button id="disable-notif-btn" class="admin-action-btn" style="margin-top:8px${showDisableBtn ? '' : ';display:none'}">Disable Notifications</button>
      <div id="notif-card-status" style="font-size:11px;color:var(--text-dimmer);margin-top:4px;min-height:16px"></div>
    </div>`
  }


  // ── 8. Image generation ──
  html += `<div class="admin-card">
    <div class="admin-card-label">Image generation</div>
    <div class="admin-kv" style="gap:8px">
      <span class="admin-kv-key">Model</span>
      <input class="admin-input" id="image-gen-model-input" type="text" value="${escHtml(imageGenModel)}" placeholder="e.g. grok-imagine-image" style="flex:1;min-width:0" />
      <button class="admin-action-btn" id="image-gen-model-save" style="white-space:nowrap">Save</button>
    </div>
    <div id="image-gen-model-status" style="font-size:11px;color:var(--text-dimmer);margin-top:4px;min-height:16px"></div>
  </div>`

  // ── 9. Identity ──
  html += `<div class="admin-card">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div class="admin-card-label">Identity</div>
      <button class="admin-action-btn" id="identity-edit">Edit</button>
    </div>
    <div id="identity-editor" style="display:none">
      <textarea class="admin-textarea" id="identity-textarea" rows="14" spellcheck="false" style="margin-top:8px">${escHtml(identityContent)}</textarea>
      <div style="display:flex;align-items:center;gap:8px;margin-top:8px">
        <button class="admin-action-btn" id="identity-save">Save identity</button>
        <div id="identity-status" style="font-size:11px;color:var(--text-dimmer);min-height:16px"></div>
      </div>
    </div>
  </div>`

  // ── 10. Instructions ──
  html += `<div class="admin-card">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div class="admin-card-label">Instructions</div>
      <button class="admin-action-btn" id="instructions-edit">Edit</button>
    </div>
    <div id="instructions-editor" style="display:none">
      <textarea class="admin-textarea" id="instructions-textarea" rows="14" spellcheck="false" style="margin-top:8px">${escHtml(instructionsContent)}</textarea>
      <div style="display:flex;align-items:center;gap:8px;margin-top:8px">
        <button class="admin-action-btn" id="instructions-save">Save instructions</button>
        <div id="instructions-status" style="font-size:11px;color:var(--text-dimmer);min-height:16px"></div>
      </div>
    </div>
  </div>`

  // ── 11. Heartbeat prompt (read-only) ──
  html += `<div class="admin-card">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div class="admin-card-label">Heartbeat prompt</div>
      <button class="admin-action-btn" id="heartbeat-prompt-toggle">View</button>
    </div>
    <div id="heartbeat-prompt-viewer" style="display:none;margin-top:8px">
      <pre class="prompt-readonly-pre">${escHtml(heartbeatPromptContent)}</pre>
      <div class="admin-meta" style="margin-top:6px">Read-only — edit <code>api/prompts/heartbeat.md</code> to change</div>
    </div>
  </div>`

  el.innerHTML = html

  // Wire up Google connect
  const googleConnectBtn = el.querySelector('#google-connect-btn')
  if (googleConnectBtn) {
    googleConnectBtn.addEventListener('click', () => {
      window.location.href = '/auth/google'
    })
  }

  // Wire up Google disconnect
  const googleDisconnectBtn = el.querySelector('#google-disconnect-btn')
  if (googleDisconnectBtn) {
    googleDisconnectBtn.addEventListener('click', async () => {
      googleDisconnectBtn.disabled = true
      googleDisconnectBtn.textContent = 'Disconnecting…'
      try {
        const r = await apiFetch('/auth/google/disconnect', { method: 'POST' })
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        loadAdminSystem()
      } catch (err) {
        googleDisconnectBtn.disabled = false
        googleDisconnectBtn.textContent = 'Disconnect'
        alert(`Failed to disconnect Google: ${err.message}`)
      }
    })
  }

  // Wire up reload app button
  const reloadAppBtn = el.querySelector('#reload-app-btn')
  if (reloadAppBtn) {
    reloadAppBtn.addEventListener('click', () => {
      // Cache-busting reload: append a timestamp to force the browser to fetch fresh resources
      const url = new URL(window.location.href)
      url.searchParams.set('_cb', Date.now())
      window.location.replace(url.toString())
    })
  }

  // Wire up session reset
  el.querySelector('#session-reset-btn').addEventListener('click', async () => {
    const btn = el.querySelector('#session-reset-btn')
    const status = el.querySelector('#session-reset-status')
    btn.disabled = true
    status.textContent = 'Resetting…'
    try {
      const res = await apiFetch('/admin/session/reset', { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      status.textContent = 'Done — next message starts fresh'
      el.querySelector('#session-info').innerHTML = '<span class="admin-kv-key" style="color:var(--text-dimmer)">No active session</span>'
    } catch (err) {
      status.textContent = `Error: ${err.message}`
      btn.disabled = false
    }
  })

  // Wire up connection reset
  el.querySelector('#reset-connection-btn').addEventListener('click', () => {
    if (confirm('Reset connection settings? You will need to reconnect.')) {
      config = null
      window.ki.saveConfig(null)
      document.getElementById('setup-secret').value = ''
      document.getElementById('admin-screen').classList.add('hidden')
      showSetup()
    }
  })

  // Wire up device permissions — enable/disable notifications
  const enableNotifBtn = el.querySelector('#enable-notif-btn')
  const disableNotifBtn = el.querySelector('#disable-notif-btn')
  const notifStatusEl = el.querySelector('#notif-card-status')
  const notifStatusValEl = el.querySelector('#notif-status-val')

  if (enableNotifBtn) {
    enableNotifBtn.addEventListener('click', async () => {
      if (enableNotifBtn.disabled) return
      enableNotifBtn.disabled = true
      notifStatusEl.textContent = 'Requesting permission…'
      try {
        const perm = await Notification.requestPermission()
        if (perm !== 'granted') {
          notifStatusEl.textContent = 'Permission denied — enable in browser or OS settings'
          notifStatusValEl.textContent = 'Blocked'
          notifStatusValEl.style.color = 'var(--danger,#c0392b)'
          enableNotifBtn.textContent = 'Blocked — enable in browser settings'
          return
        }
        notifStatusEl.textContent = 'Subscribing…'
        const swReg = await navigator.serviceWorker.ready
        const vapidRes = await apiFetch('/push/vapid-public-key')
        if (!vapidRes.ok) throw new Error('VAPID not configured on server — add VAPID_PUBLIC_KEY to .env')
        const { publicKey } = await vapidRes.json()
        const sub = await swReg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(publicKey),
        })
        const subJson = sub.toJSON()
        const saveRes = await apiFetch('/push/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: subJson.endpoint, keys: subJson.keys }),
        })
        if (!saveRes.ok) throw new Error(`Server error: HTTP ${saveRes.status}`)
        localStorage.setItem('ki-vapid-key', publicKey)
        notifStatusValEl.textContent = '✓ Enabled'
        notifStatusValEl.style.color = 'var(--success,#27ae60)'
        enableNotifBtn.textContent = 'Notifications enabled ✓'
        enableNotifBtn.style.display = 'none'
        disableNotifBtn.style.display = ''
        notifStatusEl.textContent = 'Enabled — you will receive push notifications when the app is in the background'
        setTimeout(() => { notifStatusEl.textContent = '' }, 4000)
      } catch (err) {
        notifStatusEl.textContent = `Error: ${err.message}`
        enableNotifBtn.disabled = false
      }
    })
  }

  if (disableNotifBtn) {
    disableNotifBtn.addEventListener('click', async () => {
      disableNotifBtn.disabled = true
      notifStatusEl.textContent = 'Unsubscribing…'
      try {
        const swReg = await navigator.serviceWorker.ready
        const sub = await swReg.pushManager.getSubscription()
        if (sub) {
          await apiFetch('/push/unsubscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ endpoint: sub.endpoint }),
          })
          await sub.unsubscribe()
        }
        notifStatusValEl.textContent = 'Disabled'
        notifStatusValEl.style.color = ''
        disableNotifBtn.style.display = 'none'
        enableNotifBtn.textContent = 'Enable Notifications'
        enableNotifBtn.disabled = false
        enableNotifBtn.style.display = ''
        notifStatusEl.textContent = 'Notifications disabled'
        setTimeout(() => { notifStatusEl.textContent = '' }, 3000)
      } catch (err) {
        notifStatusEl.textContent = `Error: ${err.message}`
        disableNotifBtn.disabled = false
      }
    })
  }

  // Auto-resubscribe if VAPID key has rotated or was never tracked (web push only)
  if ('Notification' in window && Notification.permission === 'granted' && 'serviceWorker' in navigator && notifStatusEl) {
    ;(async () => {
      try {
        const swReg = await navigator.serviceWorker.ready
        const existingSub = await swReg.pushManager.getSubscription()
        if (!existingSub) return  // not subscribed — nothing to check
        const storedKey = localStorage.getItem('ki-vapid-key')
        const vapidRes = await apiFetch('/push/vapid-public-key')
        if (!vapidRes.ok) return
        const { publicKey } = await vapidRes.json()
        if (storedKey === publicKey) return  // keys match — nothing to do
        // VAPID key rotated (or was never tracked): re-subscribe with current key
        notifStatusEl.textContent = 'Refreshing subscription (key changed)…'
        const oldEndpoint = existingSub.endpoint
        await existingSub.unsubscribe()
        // Tell the server to remove the old (now-invalid) endpoint before saving the new one
        await apiFetch('/push/unsubscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: oldEndpoint }),
        })
        const newSub = await swReg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(publicKey),
        })
        const subJson = newSub.toJSON()
        await apiFetch('/push/subscribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: subJson.endpoint, keys: subJson.keys }),
        })
        localStorage.setItem('ki-vapid-key', publicKey)
        notifStatusEl.textContent = 'Subscription refreshed'
        setTimeout(() => { notifStatusEl.textContent = '' }, 3000)
      } catch { /* best-effort — don't surface errors for this background check */ }
    })()
  }

  // Wire up image gen model save
  el.querySelector('#image-gen-model-save').addEventListener('click', async () => {
    const input = el.querySelector('#image-gen-model-input')
    const status = el.querySelector('#image-gen-model-status')
    const val = input.value.trim()
    if (!val) { status.textContent = 'Model name cannot be empty.'; return }
    status.textContent = 'Saving…'
    try {
      const res = await apiFetch('/admin/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image_gen_model: val }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      status.textContent = `Saved — next image will use "${val}"`
    } catch (err) {
      status.textContent = `Error: ${err.message}`
    }
  })

  // Wire up identity / instructions editors
  for (const name of ['identity', 'instructions']) {
    el.querySelector(`#${name}-edit`).addEventListener('click', () => {
      const editor = el.querySelector(`#${name}-editor`)
      const isOpen = editor.style.display !== 'none'
      editor.style.display = isOpen ? 'none' : 'block'
      el.querySelector(`#${name}-edit`).textContent = isOpen ? 'Edit' : 'Done'
    })
    el.querySelector(`#${name}-save`).addEventListener('click', async () => {
      const textarea = el.querySelector(`#${name}-textarea`)
      const status = el.querySelector(`#${name}-status`)
      const val = textarea.value
      if (!val.trim()) { status.textContent = 'Content cannot be empty.'; return }
      status.textContent = 'Saving…'
      try {
        const res = await apiFetch(`/admin/prompts/${name}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: val }),
        })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        status.textContent = 'Saved'
        setTimeout(() => { status.textContent = '' }, 2000)
      } catch (err) {
        status.textContent = `Error: ${err.message}`
      }
    })
  }

  // Wire up heartbeat prompt toggle
  el.querySelector('#heartbeat-prompt-toggle').addEventListener('click', () => {
    const viewer = el.querySelector('#heartbeat-prompt-viewer')
    const btn = el.querySelector('#heartbeat-prompt-toggle')
    const isOpen = viewer.style.display !== 'none'
    viewer.style.display = isOpen ? 'none' : 'block'
    btn.textContent = isOpen ? 'View' : 'Hide'
  })
}

function renderJobCard(primaryJobId, label, tools, schedules, envVars, runs, jobs) {
  // Collect runs across all schedule ids for this integration
  const allRuns = []
  const ids = schedules.length ? schedules : [primaryJobId]
  for (const id of ids) {
    if (runs[id]) allRuns.push(...runs[id].map(r => ({ ...r, jobId: id })))
  }
  allRuns.sort((a, b) => new Date(b.started_at) - new Date(a.started_at))
  const recent = allRuns.slice(0, 10)

  // Next run time: use first schedule id that has a known next run
  let nextRunIso = null
  for (const id of ids) {
    if (jobs && jobs[id]) { nextRunIso = jobs[id]; break }
  }

  let html = `<div class="admin-card">
    <div class="admin-card-label">${escHtml(label)}</div>`

  if (tools.length) {
    html += `<div class="admin-kv">
      <span class="admin-kv-key">Tools</span>
      <span class="admin-kv-val">${tools.map(escHtml).join(', ')}</span>
    </div>`
  }

  if (schedules.length) {
    html += `<div class="admin-kv">
      <span class="admin-kv-key">Schedules</span>
      <span class="admin-kv-val">${schedules.map(escHtml).join(', ')}</span>
    </div>`
  }

  if (envVars.length) {
    html += `<div class="admin-kv">
      <span class="admin-kv-key">Env vars</span>
      <span class="admin-kv-val">${envVars.map(escHtml).join(', ')}</span>
    </div>`
  }

  if (nextRunIso) {
    html += `<div class="admin-kv">
      <span class="admin-kv-key">Next run</span>
      <span class="admin-kv-val">${formatNextRun(nextRunIso)}</span>
    </div>`
  }

  if (recent.length === 0) {
    html += `<div class="admin-runs-empty">No runs recorded yet</div>`
  } else {
    const lastRun = recent[0]
    const lastRunAge = formatAge(lastRun.started_at)
    const statusCounts = recent.reduce((acc, r) => { acc[r.status] = (acc[r.status] || 0) + 1; return acc }, {})
    const avgDuration = recent.filter(r => r.duration_ms != null).reduce((s, r, _, a) => s + r.duration_ms / a.length, 0)

    html += `<div class="admin-runs-header">
      <span class="admin-runs-label">Last run: ${lastRunAge}</span>
      ${avgDuration ? `<span class="admin-runs-label">${Math.round(avgDuration / 1000)}s avg</span>` : ''}
      ${statusCounts.error ? `<span class="admin-runs-label admin-runs-errors">${statusCounts.error} error${statusCounts.error > 1 ? 's' : ''}</span>` : ''}
    </div>
    <div class="admin-run-dots">${recent.map(r => `<span class="admin-run-dot ${r.status}" title="${formatRunTooltip(r)}"></span>`).join('')}</div>`

    // Show error details if the most recent run failed
    if (lastRun.status === 'error' && lastRun.error) {
      html += `<div class="admin-run-error-text">${escHtml(lastRun.error)}</div>`
    }
  }

  html += '</div>'
  return html
}

function formatNextRun(isoStr) {
  const d = new Date(isoStr)
  const diff = d - Date.now()
  if (diff <= 0) return 'imminent'
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `in ${mins}m`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `in ${hrs}h ${mins % 60}m`
  return `in ${Math.floor(hrs / 24)}d`
}

function formatAge(isoStr) {
  const diff = Date.now() - new Date(isoStr)
  const mins = Math.floor(diff / 60000)
  if (mins < 60)  return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24)   return `${hrs}h ago`
  return `${Math.floor(hrs / 24)}d ago`
}

function formatRunTooltip(run) {
  const t = new Date(run.started_at).toLocaleString('en-NZ', { dateStyle: 'short', timeStyle: 'short' })
  const dur = run.duration_ms != null ? ` · ${Math.round(run.duration_ms / 1000)}s` : ''
  const err = run.error ? ` · ${run.error.slice(0, 80)}` : ''
  return `${t}${dur}${err}`
}



// ── Image Lightbox ────────────────────────────────────────────────────────────
let _lbSrc = null
let _lbScale = 1
let _lbPanX = 0
let _lbPanY = 0
let _lbMediaId = null
let _lbCellEl = null
let _lbDragging = false
let _lbDragStartX = 0
let _lbDragStartY = 0
let _lbDragStartPanX = 0
let _lbDragStartPanY = 0

function openLightbox(src, prompt, mediaId, cellEl) {
  _lbSrc = src
  _lbMediaId = mediaId || null
  _lbCellEl = cellEl || null
  _lbScale = 1
  _lbPanX = 0
  _lbPanY = 0
  document.getElementById('lightbox-img').src = src
  const promptEl = document.getElementById('lightbox-prompt')
  if (promptEl) {
    promptEl.textContent = prompt || ''
    promptEl.style.display = prompt ? '' : 'none'
  }
  const lbDelBtn = document.getElementById('lightbox-delete-btn')
  if (lbDelBtn) lbDelBtn.style.display = mediaId ? '' : 'none'
  document.getElementById('lightbox-overlay').classList.remove('hidden')
  _lbUpdateTransform()
}

function closeLightbox() {
  document.getElementById('lightbox-overlay').classList.add('hidden')
  _lbSrc = null
}

function _lbUpdateTransform() {
  document.getElementById('lightbox-img').style.transform =
    `translate(${_lbPanX}px, ${_lbPanY}px) scale(${_lbScale})`
  const wrap = document.getElementById('lightbox-img-wrap')
  if (wrap) wrap.classList.toggle('zoomed', _lbScale > 1)
}

function initLightbox() {
  const overlay = document.createElement('div')
  overlay.id = 'lightbox-overlay'
  overlay.className = 'lightbox-overlay hidden'

  const toolbar = document.createElement('div')
  toolbar.className = 'lightbox-toolbar'

  const downloadBtn = document.createElement('button')
  downloadBtn.className = 'lightbox-btn'
  downloadBtn.textContent = '↓  Download'
  downloadBtn.addEventListener('click', e => {
    e.stopPropagation()
    const a = document.createElement('a')
    a.href = _lbSrc
    a.download = 'ki-image.png'
    a.click()
  })

  const lbDeleteBtn = document.createElement('button')
  lbDeleteBtn.id = 'lightbox-delete-btn'
  lbDeleteBtn.className = 'lightbox-btn lightbox-btn-danger'
  lbDeleteBtn.textContent = '🗑  Delete'
  lbDeleteBtn.style.display = 'none'
  lbDeleteBtn.addEventListener('click', async e => {
    e.stopPropagation()
    if (!_lbMediaId) return
    lbDeleteBtn.disabled = true
    try {
      const res = await apiFetch(`/desktop/media/${_lbMediaId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(`Delete failed: ${res.status}`)
      const grid = document.getElementById('media-grid')
      if (_lbCellEl) {
        _lbCellEl.remove()
        if (grid && !grid.querySelector('.media-cell')) {
          grid.innerHTML = '<div class="media-status">No media yet</div>'
        }
      }
      closeLightbox()
    } catch {
      lbDeleteBtn.disabled = false
    }
  })

  const closeBtn = document.createElement('button')
  closeBtn.className = 'lightbox-btn'
  closeBtn.textContent = '✕  Close'
  closeBtn.addEventListener('click', e => {
    e.stopPropagation()
    closeLightbox()
  })

  toolbar.appendChild(lbDeleteBtn)
  toolbar.appendChild(downloadBtn)
  toolbar.appendChild(closeBtn)

  const imgWrap = document.createElement('div')
  imgWrap.id = 'lightbox-img-wrap'
  imgWrap.className = 'lightbox-img-wrap'

  const img = document.createElement('img')
  img.id = 'lightbox-img'
  img.className = 'lightbox-img'
  imgWrap.appendChild(img)

  const promptEl = document.createElement('div')
  promptEl.id = 'lightbox-prompt'
  promptEl.className = 'lightbox-prompt'
  promptEl.style.display = 'none'

  const hint = document.createElement('div')
  hint.className = 'lightbox-hint'
  hint.textContent = 'Scroll to zoom · Drag to pan · Esc to close'

  overlay.appendChild(toolbar)
  overlay.appendChild(imgWrap)
  overlay.appendChild(promptEl)
  overlay.appendChild(hint)
  document.body.appendChild(overlay)

  // Escape key
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLightbox() })

  // Wheel zoom
  overlay.addEventListener('wheel', e => {
    e.preventDefault()
    const factor = e.deltaY < 0 ? 1.12 : 0.89
    _lbScale = Math.max(0.25, Math.min(12, _lbScale * factor))
    if (_lbScale <= 1) { _lbPanX = 0; _lbPanY = 0 }
    _lbUpdateTransform()
  }, { passive: false })

  // Drag to pan
  imgWrap.addEventListener('mousedown', e => {
    if (_lbScale <= 1) return
    _lbDragging = true
    _lbDragStartX = e.clientX
    _lbDragStartY = e.clientY
    _lbDragStartPanX = _lbPanX
    _lbDragStartPanY = _lbPanY
    imgWrap.classList.add('dragging')
    e.preventDefault()
  })

  document.addEventListener('mousemove', e => {
    if (!_lbDragging) return
    _lbPanX = _lbDragStartPanX + (e.clientX - _lbDragStartX)
    _lbPanY = _lbDragStartPanY + (e.clientY - _lbDragStartY)
    _lbUpdateTransform()
  })

  document.addEventListener('mouseup', () => {
    if (_lbDragging) {
      _lbDragging = false
      document.getElementById('lightbox-img-wrap')?.classList.remove('dragging')
    }
  })
}

// ── Today note modal ──────────────────────────────────────────────────────────
async function openTodayModal() {
  const modal = document.getElementById('today-modal')
  const content = document.getElementById('today-modal-content')
  content.innerHTML = '<p style="color:var(--text-dim)">Loading…</p>'
  modal.classList.remove('hidden')
  try {
    const res = await apiFetch('/desktop/today-note')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    if (typeof marked !== 'undefined') {
      content.innerHTML = marked.parse(data.content, { gfm: true, breaks: false })
    } else {
      content.textContent = data.content
    }
    await renderTodayTasksBlocks(content, data.date)
  } catch (err) {
    content.innerHTML = `<p style="color:var(--text-dim)">Failed to load: ${err.message}</p>`
  }
}

async function renderTodayTasksBlocks(container, date) {
  const blocks = container.querySelectorAll('pre > code.language-tasks')
  if (!blocks.length) return

  let tasks
  try {
    const res = await apiFetch('/desktop/vault/tasks')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    tasks = (await res.json()).tasks || []
  } catch (err) {
    console.error('Failed to load vault tasks:', err)
    return
  }

  const dailyNotePath = date ? `daily/${date}.md` : null

  for (const codeEl of blocks) {
    const pre = codeEl.parentElement
    const lines = codeEl.textContent.trim().split('\n').map(l => l.trim()).filter(Boolean)

    const rules = { doneFilter: null, pathIncludes: [], pathExcludes: [], tagIncludes: [], tagExcludes: [] }
    for (const rawLine of lines) {
      const line = rawLine.toLowerCase()
      if (line === 'not done') {
        rules.doneFilter = false
      } else if (line === 'done') {
        rules.doneFilter = true
      } else if (line.startsWith('path does not include ')) {
        let val = line.slice('path does not include '.length).trim()
        if (val === '{{query.file.path}}' && dailyNotePath) val = dailyNotePath
        rules.pathExcludes.push(val)
      } else if (line.startsWith('path includes ')) {
        rules.pathIncludes.push(line.slice('path includes '.length).trim())
      } else if (line.startsWith('tags include ')) {
        rules.tagIncludes.push(line.slice('tags include '.length).trim())
      } else if (line.startsWith('tags do not include ')) {
        rules.tagExcludes.push(line.slice('tags do not include '.length).trim())
      }
      // ignore: hide toolbar, hide task count, sort by ...
    }

    let filtered = tasks
    if (rules.doneFilter === false) filtered = filtered.filter(t => !t.done)
    if (rules.doneFilter === true) filtered = filtered.filter(t => t.done)
    for (const p of rules.pathIncludes) filtered = filtered.filter(t => t.path.toLowerCase().includes(p))
    for (const p of rules.pathExcludes) filtered = filtered.filter(t => !t.path.toLowerCase().includes(p))
    for (const tag of rules.tagIncludes) filtered = filtered.filter(t => t.tags.some(tg => tg.toLowerCase() === tag))
    for (const tag of rules.tagExcludes) filtered = filtered.filter(t => !t.tags.some(tg => tg.toLowerCase() === tag))

    const wrapper = document.createElement('div')
    wrapper.className = 'tasks-block'

    if (!filtered.length) {
      const empty = document.createElement('p')
      empty.className = 'tasks-empty'
      empty.textContent = 'No tasks.'
      wrapper.appendChild(empty)
    } else {
      const ul = document.createElement('ul')
      ul.className = 'tasks-list'
      for (const task of filtered) {
        const li = document.createElement('li')
        li.className = 'tasks-item'

        const row = document.createElement('div')
        row.className = 'tasks-item-row'

        const cb = document.createElement('input')
        cb.type = 'checkbox'
        cb.checked = task.done
        cb.addEventListener('change', async () => {
          const newDone = cb.checked
          try {
            const res = await apiFetch('/desktop/vault/task-update', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ file_path: task.path, task_text: task.text, done: newDone }),
            })
            if (!res.ok) {
              cb.checked = !newDone
              console.error('Task update failed:', await res.text())
            }
          } catch (err) {
            cb.checked = !newDone
            console.error('Task update error:', err)
          }
        })

        const textSpan = document.createElement('span')
        textSpan.className = 'tasks-text'
        textSpan.innerHTML = escHtml(task.text).replace(/#([\w/-]+)/g, '<span class="tasks-tag">#$1</span>')

        row.appendChild(cb)
        row.appendChild(textSpan)
        li.appendChild(row)

        const pathSpan = document.createElement('span')
        pathSpan.className = 'tasks-path'
        pathSpan.textContent = task.path
        li.appendChild(pathSpan)

        ul.appendChild(li)
      }
      wrapper.appendChild(ul)
    }

    pre.replaceWith(wrapper)
  }
}

function closeTodayModal() {
  document.getElementById('today-modal').classList.add('hidden')
}

document.getElementById('today-modal-close').addEventListener('click', closeTodayModal)
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !document.getElementById('today-modal').classList.contains('hidden')) {
    closeTodayModal()
  }
})

// ── Journal PDF download ──────────────────────────────────────────────────────
async function triggerJournalDownload() {
  const inner = document.getElementById('messages-inner')
  const notice = document.createElement('div')
  notice.style.cssText = 'padding: 6px 16px; color: var(--text-dim); font-style: italic; font-size: 0.9em;'
  notice.textContent = 'Generating journal PDF…'
  inner.appendChild(notice)
  scrollToBottom()

  try {
    const res = await apiFetch('/desktop/journal-pdf')
    if (!res.ok) {
      const msg = await res.text().catch(() => `HTTP ${res.status}`)
      throw new Error(msg)
    }
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const today = new Date().toISOString().slice(0, 10)
    a.href = url
    a.download = `journal-${today}.pdf`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 60000)
    notice.textContent = 'Journal PDF downloaded.'
    setTimeout(() => notice.remove(), 4000)
  } catch (err) {
    notice.textContent = `Failed to generate PDF: ${err.message}`
    notice.style.color = 'var(--error-color, #c04040)'
  }
}

// ── Utility ───────────────────────────────────────────────────────────────────
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
  const rawData = atob(base64)
  return Uint8Array.from([...rawData].map(c => c.charCodeAt(0)))
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}
