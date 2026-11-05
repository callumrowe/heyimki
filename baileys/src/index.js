const http = require('http')
const {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  Browsers,
  downloadMediaMessage,
} = require('@whiskeysockets/baileys')
const pino = require('pino')
const qrcode = require('qrcode-terminal')

const fs = require('fs')

const AGENT_API_URL = process.env.AGENT_API_URL || 'http://api:8000'
// The single WhatsApp chat Ki talks to. Leave it unset on first run: Baileys
// then answers nobody and logs the JID of every chat that messages it, so you
// can copy yours into YOUR_WHATSAPP_JID in .env.
const ALLOWED_JID = (process.env.ALLOWED_JID || '').trim()
const INTERNAL_SECRET = process.env.INTERNAL_SECRET || ''

if (!INTERNAL_SECRET) {
  console.error('INTERNAL_SECRET env var is required')
  process.exit(1)
}
if (!ALLOWED_JID) {
  console.log('[setup] YOUR_WHATSAPP_JID is not set — Ki will not reply to anyone yet.')
  console.log('[setup] Scan the QR code, then message this WhatsApp account from your own phone.')
}

// Sent on every call to the api. The api rejects unauthenticated callers.
const API_HEADERS = {
  'Content-Type': 'application/json',
  'Authorization': `Bearer ${INTERNAL_SECRET}`,
}

// Module-level socket reference so the HTTP server can reach it
let activeSock = null

// Internal HTTP server — accepts POST /send, /send-image, /send-document, /send-voice-note from the api
http.createServer((req, res) => {
  const urlObj = new URL(req.url, 'http://localhost')
  const pathname = urlObj.pathname

  if (req.method !== 'POST' || (pathname !== '/send' && pathname !== '/send-image' && pathname !== '/send-document' && pathname !== '/send-voice-note')) {
    res.writeHead(404); res.end(); return
  }
  if (!ALLOWED_JID) {
    res.writeHead(503); res.end('YOUR_WHATSAPP_JID is not configured'); return
  }

  const chunks = []
  req.on('data', chunk => chunks.push(chunk))
  req.on('end', async () => {
    try {
      // /send-voice-note accepts raw binary body; secret + to come from query params
      if (pathname === '/send-voice-note') {
        const secret = urlObj.searchParams.get('secret')
        if (secret !== INTERNAL_SECRET) {
          res.writeHead(403); res.end('Forbidden'); return
        }
        if (!activeSock) {
          res.writeHead(503); res.end('Not connected'); return
        }
        const to = urlObj.searchParams.get('to')
        const targetJid = to || ALLOWED_JID
        const audioBuffer = Buffer.concat(chunks)
        await activeSock.sendMessage(targetJid, {
          audio: audioBuffer,
          mimetype: 'audio/mpeg',
          ptt: true,
        })
        console.log(`[send-voice-note] sent ${audioBuffer.length} bytes to ${targetJid}`)
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ ok: true }))
        return
      }

      // All other routes use JSON body with secret field
      const parsed = JSON.parse(Buffer.concat(chunks).toString())
      const { secret } = parsed
      if (secret !== INTERNAL_SECRET) {
        res.writeHead(403); res.end('Forbidden'); return
      }
      if (!activeSock) {
        res.writeHead(503); res.end('Not connected'); return
      }

      if (pathname === '/send') {
        const { text } = parsed
        await activeSock.sendMessage(ALLOWED_JID, { text })

      } else if (pathname === '/send-image') {
        const { jid, image_b64, caption } = parsed
        const targetJid = jid || ALLOWED_JID
        const imageBuffer = Buffer.from(image_b64, 'base64')
        await activeSock.sendMessage(targetJid, {
          image: imageBuffer,
          caption: caption || '',
        })
        console.log(`[send-image] sent ${imageBuffer.length} bytes to ${targetJid}`)

      } else if (pathname === '/send-document') {
        const { data, filename, mimetype } = parsed
        const buffer = Buffer.from(data, 'base64')
        await activeSock.sendMessage(ALLOWED_JID, {
          document: buffer,
          mimetype: mimetype || 'text/plain',
          fileName: filename || 'file.txt',
        })
        console.log(`[send-document] sent ${buffer.length} bytes as "${filename}"`)
      }

      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: true }))
    } catch (err) {
      console.error('[send] failed:', err.message)
      res.writeHead(500); res.end('Error')
    }
  })
}).listen(3000, () => console.log('Internal HTTP server listening on :3000'))

// Suppress Baileys' internal logs — it's very noisy at default level
const logger = pino({ level: 'silent' })

// Track recent sent message IDs → text so we can look them up on reaction
// Persisted to auth_info/sent_messages.json so reactions survive restarts
const SENT_MESSAGES_FILE = './auth_info/sent_messages.json'
const SENT_MESSAGES_MAX = 200

function loadSentMessages() {
  try {
    const raw = fs.readFileSync(SENT_MESSAGES_FILE, 'utf8')
    return new Map(Object.entries(JSON.parse(raw)))
  } catch {
    return new Map()
  }
}

function saveSentMessages(map) {
  try {
    fs.writeFileSync(SENT_MESSAGES_FILE, JSON.stringify(Object.fromEntries(map)), 'utf8')
  } catch (err) {
    console.error('[sentMessages] failed to persist:', err.message)
  }
}

const sentMessages = loadSentMessages()
console.log(`[sentMessages] loaded ${sentMessages.size} entries from disk`)

// Dedup incoming messages — prevents double-processing on reconnect re-delivery
// Persisted to disk so container restarts don't re-process messages re-delivered by WhatsApp
const PROCESSED_IDS_FILE = './auth_info/processed_ids.json'
const PROCESSED_IDS_MAX = 200

function loadProcessedIds() {
  try {
    const raw = fs.readFileSync(PROCESSED_IDS_FILE, 'utf8')
    return new Set(JSON.parse(raw))
  } catch {
    return new Set()
  }
}

function saveProcessedIds(set) {
  try {
    fs.writeFileSync(PROCESSED_IDS_FILE, JSON.stringify([...set]), 'utf8')
  } catch (err) {
    console.error('[processedIds] failed to persist:', err.message)
  }
}

const processedMessageIds = loadProcessedIds()
console.log(`[processedIds] loaded ${processedMessageIds.size} entries from disk`)

// Serialize message processing — only one callAgent at a time
let messageQueue = Promise.resolve()
function enqueueMessage(fn) {
  messageQueue = messageQueue.then(fn).catch(err => console.error('[queue] unhandled error:', err.message, err.stack))
  return messageQueue
}

async function sendReply(sock, text) {
  const result = await sock.sendMessage(ALLOWED_JID, { text })
  if (result?.key?.id) {
    sentMessages.set(result.key.id, text)
    // Trim to max, evicting oldest entries
    while (sentMessages.size > SENT_MESSAGES_MAX) {
      sentMessages.delete(sentMessages.keys().next().value)
    }
    saveSentMessages(sentMessages)
  }
}

async function callAgent(text, image = null, audio = null, document = null, attempt = 0) {
  const body = { text, jid: ALLOWED_JID, channel: 'whatsapp' }
  if (image) body.image = image
  if (audio) body.audio = audio
  if (document) body.document = document
  try {
    const res = await fetch(`${AGENT_API_URL}/message`, {
      method: 'POST',
      headers: API_HEADERS,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(300_000), // 5 min — browser tasks need it
    })
    if (!res.ok) throw new Error(`API responded ${res.status}`)
    const { reply } = await res.json()
    return reply
  } catch (err) {
    // Retry on connection errors only (not timeouts or HTTP errors — those are intentional)
    const isConnectionError = err.code === 'ECONNREFUSED' || err.code === 'ENOTFOUND' || err.code === 'ECONNRESET'
    if (isConnectionError && attempt < 2) {
      const wait = (attempt + 1) * 5000
      console.log(`[agent] connection error, retrying in ${wait / 1000}s (attempt ${attempt + 1}/3): ${err.message}`)
      await new Promise(r => setTimeout(r, wait))
      return callAgent(text, image, audio, document, attempt + 1)
    }
    throw err
  }
}

async function callReaction(messageText) {
  const res = await fetch(`${AGENT_API_URL}/reaction`, {
    method: 'POST',
    headers: API_HEADERS,
    body: JSON.stringify({ message: messageText }),
    signal: AbortSignal.timeout(10_000),
  })
  if (!res.ok) throw new Error(`API responded ${res.status}`)
}

async function callReactionNegative(messageText, fallback = false) {
  const res = await fetch(`${AGENT_API_URL}/reaction-negative`, {
    method: 'POST',
    headers: API_HEADERS,
    body: JSON.stringify(fallback ? { message: null, fallback: true } : { message: messageText }),
    signal: AbortSignal.timeout(10_000),
  })
  if (!res.ok) throw new Error(`API responded ${res.status}`)
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState('./auth_info')

  const { version } = await fetchLatestBaileysVersion()

  const sock = makeWASocket({
    version,
    auth: state,
    logger,
    browser: Browsers.ubuntu('Chrome'),
  })
  activeSock = sock

  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      qrcode.generate(qr, { small: true })
    }
    if (connection === 'open') {
      console.log('WhatsApp connected')
    }
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      console.log(`Connection closed (code ${code})`)
      if (code === DisconnectReason.loggedOut) {
        console.log('Logged out — delete auth_info volume and restart to re-scan QR')
        process.exit(1)
      }
      start()
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const msg of messages) {
      // Only the allowlisted chat is handled. Until one is configured, log the
      // JID of anyone who messages so the owner can find theirs.
      if (!ALLOWED_JID || msg.key.remoteJid !== ALLOWED_JID) {
        if (!ALLOWED_JID && type === 'notify' && !msg.key.fromMe && msg.key.remoteJid) {
          console.log(`[setup] message from ${msg.key.remoteJid} — if that's you, set YOUR_WHATSAPP_JID=${msg.key.remoteJid} in .env and restart baileys + api`)
        }
        continue
      }

      // Reactions can arrive with any type and fromMe=true — check first
      const reaction = msg.message?.reactionMessage
      if (reaction) {
        if (reaction.text === '👍' || reaction.text?.startsWith('👍')) {
          const original = sentMessages.get(reaction.key?.id)
          if (original) {
            console.log(`[reaction] 👍 closed: ${original.slice(0, 60)}`)
            callReaction(original).catch(err => console.error('Reaction call failed:', err.message))
          } else {
            console.log(`[reaction] 👍 but ID not in cache (size=${sentMessages.size})`)
          }
        } else if (reaction.text === '👎' || reaction.text?.startsWith('👎')) {
          const original = sentMessages.get(reaction.key?.id)
          if (original) {
            console.log(`[reaction] 👎 on: ${original.slice(0, 60)}`)
            callReactionNegative(original).catch(err => console.error('Reaction-negative call failed:', err.message))
          } else {
            console.log(`[reaction] 👎 ID not in cache (size=${sentMessages.size}) — using fallback`)
            callReactionNegative(null, true).catch(err => console.error('Reaction-negative fallback failed:', err.message))
          }
        }
        continue
      }

      if (type !== 'notify') continue

      // Ignore messages sent by us
      if (msg.key.fromMe) continue

      // Dedup — skip messages we've already processed (e.g. re-delivered after reconnect)
      const msgId = msg.key.id
      if (msgId && processedMessageIds.has(msgId)) {
        console.log(`[dedup] skipping already-processed message ${msgId}`)
        continue
      }
      if (msgId) {
        processedMessageIds.add(msgId)
        if (processedMessageIds.size > PROCESSED_IDS_MAX) {
          processedMessageIds.delete(processedMessageIds.values().next().value)
        }
        saveProcessedIds(processedMessageIds)
      }

      // Mark as read (blue ticks)
      await sock.sendReceipt(msg.key.remoteJid, msg.key.participant, [msg.key.id], 'read')

      const text =
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text

      const imageMsg = msg.message?.imageMessage
      const audioMsg = msg.message?.audioMessage
      const docMsg = msg.message?.documentMessage

      if (!text && !imageMsg && !audioMsg && !docMsg) {
        await sendReply(sock, "I can only handle text, images, voice messages, and files for now.")
        continue
      }

      const contextInfo = msg.message?.extendedTextMessage?.contextInfo
      const quotedMsg = contextInfo?.quotedMessage
      const quotedText =
        quotedMsg?.conversation ||
        quotedMsg?.extendedTextMessage?.text

      const fullText = quotedText
        ? `[Replying to: "${quotedText}"]\n${text || ''}`
        : (text || '')

      // Intercept /kill — immediately cancel any running agent loop.
      // Don't forward as a normal message; the running callAgent() will receive
      // "Stopped." from the API and sendReply it automatically.
      if (fullText.trim().toLowerCase() === '/kill') {
        let killed = false
        try {
          const killRes = await fetch(`${AGENT_API_URL}/kill`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ secret: INTERNAL_SECRET, channel: 'whatsapp' }),
            signal: AbortSignal.timeout(5_000),
          })
          if (killRes.ok) {
            const data = await killRes.json()
            killed = data.killed
            console.log(`[kill] killed=${killed}`)
          }
        } catch (err) {
          console.error('[kill] request failed:', err.message)
        }
        if (!killed) {
          await sendReply(sock, "Nothing running to stop.")
        }
        // If killed=true: the in-flight callAgent() will return "Stopped." from
        // the API and sendReply it — no extra send needed here.
        continue
      }

      // Extract image if present
      let image = null
      if (imageMsg) {
        try {
          const buffer = await downloadMediaMessage(
            msg, 'buffer', {},
            { logger, reuploadRequest: sock.updateMediaMessage }
          )
          image = {
            data: buffer.toString('base64'),
            media_type: imageMsg.mimetype || 'image/jpeg',
          }
          console.log(`[image] received ${Math.round(buffer.length / 1024)}KB ${image.media_type}`)
        } catch (err) {
          console.error('[image] download failed:', err.message)
          await sendReply(sock, "I couldn't download that image. Try again?")
          continue
        }
      }

      // Extract document if present
      let document = null
      if (docMsg) {
        try {
          const buffer = await downloadMediaMessage(
            msg, 'buffer', {},
            { logger, reuploadRequest: sock.updateMediaMessage }
          )
          document = {
            data: buffer.toString('base64'),
            media_type: docMsg.mimetype || 'application/octet-stream',
            filename: docMsg.fileName || 'document',
          }
          console.log(`[document] received ${Math.round(buffer.length / 1024)}KB ${document.media_type} "${document.filename}"`)
        } catch (err) {
          console.error('[document] download failed:', err.message)
          await sendReply(sock, "I couldn't download that document. Try again?")
          continue
        }
      }

      // Extract audio if present
      let audio = null
      if (audioMsg) {
        try {
          const buffer = await downloadMediaMessage(
            msg, 'buffer', {},
            { logger, reuploadRequest: sock.updateMediaMessage }
          )
          audio = {
            data: buffer.toString('base64'),
            mime_type: audioMsg.mimetype || 'audio/ogg; codecs=opus',
          }
          console.log(`[audio] received ${Math.round(buffer.length / 1024)}KB`)
          console.log('[audio] calling agent with audio payload')
        } catch (err) {
          console.error('[audio] download failed:', err.message)
          await sendReply(sock, "I couldn't download that voice message. Try again?")
          continue
        }
      }

      enqueueMessage(async () => {
        const msgType = audio ? 'audio' : image ? 'image' : 'text'
        console.log(`[agent] starting callAgent for message type: ${msgType}`)
        try {
          await sock.sendPresenceUpdate('composing', ALLOWED_JID)
          const reply = await callAgent(fullText, image, audio, document)
          await sock.sendPresenceUpdate('paused', ALLOWED_JID)
          if (reply) await sendReply(sock, reply)
        } catch (err) {
          console.error('Agent call failed:', err.message)
          await sock.sendPresenceUpdate('paused', ALLOWED_JID)
          const reply = err.name === 'TimeoutError' || err.name === 'AbortError'
            ? "That took too long and timed out. If it was a browser task it may still be running — try again or rephrase?"
            : `Something went wrong reaching the agent: ${err.message}`
          await sendReply(sock, reply)
        }
      })
    }
  })
}

start()
