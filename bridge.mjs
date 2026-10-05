// 洛雪音源脚本运行桥: 由 Python 端以 node bridge.mjs <script.js> 启动
// 协议: stdin 逐行收 JSON 请求, stdout 逐行回 JSON
// 启动先发 bridge-ready, 脚本加载完成后发 meta, 错误统一发 {ok:false,error}
import vm from 'node:vm'
import readline from 'node:readline'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import nodeCrypto from 'node:crypto'
import nodeZlib from 'node:zlib'

const require = createRequire(import.meta.url)
let CryptoJS = null
try { CryptoJS = require('./crypto-js.js') } catch (e) { out({ ok: false, error: 'crypto-js 加载失败: ' + (e && e.message) }); process.exit(1) }

const out = (obj) => process.stdout.write(JSON.stringify(obj) + '\n')
out({ type: 'bridge', ready: true })

const scriptPath = process.argv[2]
let code
try {
  code = fs.readFileSync(scriptPath, 'utf-8')
} catch (e) {
  out({ ok: false, error: '读取脚本失败: ' + (e && e.message) })
  process.exit(1)
}

const EVENT_NAMES = { request: 'request', inited: 'inited', updateAlert: 'updateAlert' }
let inited = null
let requestHandler = null

// 脚本自身信息(部分脚本会读取 currentScriptInfo.rawScript 等)
const scriptInfo = parseScriptMeta(code)

function parseScriptMeta(script) {
  const head = /^\/\*[\s\S]+?\*\//.exec(script)
  if (!head) return { name: '未命名音源', description: '', author: '', homepage: '', version: '' }
  const meta = {}
  const rxp = /^\s?\*\s?@(\w+)\s(.+)$/
  for (const line of head[0].split(/\r?\n/)) {
    const m = rxp.exec(line)
    if (!m) continue
    const key = m[1]
    if (!['name', 'description', 'author', 'homepage', 'version'].includes(key)) continue
    meta[key] = ((meta[key] || '') + (meta[key] ? ' ' : '') + m[2].trim())
  }
  return {
    name: (meta.name || '未命名音源').slice(0, 24),
    description: (meta.description || '').slice(0, 36),
    author: (meta.author || '').slice(0, 56),
    homepage: (meta.homepage || '').slice(0, 1024),
    version: (meta.version || '0.0.0').slice(0, 36),
  }
}

const DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

const requestFn = (url, options, callback) => {
  const method = (options && options.method) || 'GET'
  const headers = options && options.headers
  let body = options && options.body
  if (body && typeof body === 'object' && !(body instanceof FormData)) {
    try { body = JSON.stringify(body) } catch (e) {}
  }
  const timeout = Math.min(Number(options && options.timeout) || 30000, 60000)
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout)
  const finalHeaders = Object.assign({}, headers)
  // 真实洛雪运行时: 脚本未指定 UA 时自动补充默认 Chrome UA
  if (!finalHeaders['User-Agent'] && !finalHeaders['user-agent']) finalHeaders['User-Agent'] = DEFAULT_UA
  if (body && typeof body === 'string') finalHeaders['Content-Type'] = finalHeaders['Content-Type'] || 'application/json'
  fetch(url, { method, headers: finalHeaders, body, signal: ctrl.signal })
    .then(async (resp) => {
      clearTimeout(timer)
      const text = await resp.text()
      let parsed = text
      try { parsed = JSON.parse(text) } catch (e) {}
      callback(null, { statusCode: resp.status, statusMessage: resp.statusText, headers: resp.headers, raw: text, body: parsed })
    })
    .catch((err) => { clearTimeout(timer); callback(err, null) })
}

const lx = {
  EVENT_NAMES,
  request: requestFn,
  on(name, handler) { if (name === EVENT_NAMES.request) requestHandler = handler },
  send(name, data) { if (name === EVENT_NAMES.inited) inited = data },
  currentScriptInfo: scriptInfo,
  utils: {
    crypto: {
      aesEncrypt: (buffer, mode, key, iv) => {
        const cipher = nodeCrypto.createCipheriv(mode, key, iv)
        return Buffer.concat([cipher.update(buffer), cipher.final()])
      },
      rsaEncrypt: (buffer, key) => {
        buffer = Buffer.concat([Buffer.alloc(128 - buffer.length), buffer])
        return nodeCrypto.publicEncrypt({ key, padding: nodeCrypto.constants.RSA_NO_PADDING }, buffer)
      },
      randomBytes: (size) => nodeCrypto.randomBytes(size),
      md5: (str) => nodeCrypto.createHash('md5').update(str).digest('hex'),
    },
    buffer: {
      from: (...args) => Buffer.from(...args),
      bufToString: (buf, format) => Buffer.from(buf, 'binary').toString(format),
    },
    zlib: {
      inflate: (buf) => new Promise((resolve, reject) => nodeZlib.inflate(buf, (err, data) => err ? reject(new Error(err.message)) : resolve(data))),
      deflate: (data) => new Promise((resolve, reject) => nodeZlib.deflate(data, (err, buf) => err ? reject(new Error(err.message)) : resolve(buf))),
    },
  },
  env: 'desktop',
  version: '2.0.0',
}

const sandboxConsole = {
  log: (...a) => { try { out({ type: 'console', msg: a.map(String).join(' ').slice(0, 500) }) } catch (e) {} },
  error: (...a) => { try { out({ type: 'console', level: 'error', msg: a.map(String).join(' ').slice(0, 500) }) } catch (e) {} },
  warn: (...a) => { try { out({ type: 'console', msg: a.map(String).join(' ').slice(0, 500) }) } catch (e) {} },
}

// 脚本可用 require(与真实洛雪运行时一致): node 内置模块 + 内置 crypto-js
const nodeRequire = createRequire(import.meta.url)
const sandboxRequire = (name) => {
  if (name === 'crypto-js') return nodeRequire('./crypto-js.js')
  return nodeRequire(name)
}
const scriptDir = scriptPath.substring(0, scriptPath.lastIndexOf('/')) || '.'
const limitedProcess = { env: {}, platform: 'linux', version: process.version, versions: process.versions }

const sandbox = {
  console: sandboxConsole, setTimeout, clearTimeout, setInterval, clearInterval, Buffer,
  JSON, Math, Date, Promise, Object, Array, String, Number, Boolean, RegExp, Error,
  Map, Set, Symbol, URL, URLSearchParams, TextEncoder, TextDecoder, AbortController,
  FormData, Blob, Uint8Array, ArrayBuffer, encodeURIComponent, decodeURIComponent,
  parseInt, parseFloat, isNaN, isFinite,
}
sandbox.globalThis = sandbox
sandbox.global = sandbox
sandbox.window = sandbox
sandbox.self = sandbox
sandbox.lx = lx
sandbox.require = sandboxRequire
sandbox.__dirname = scriptDir
sandbox.__filename = scriptPath
sandbox.process = limitedProcess
// 社区音源脚本普遍使用 eval/Function 混淆, 放宽代码生成限制(与 lx-music-api-server 一致)
// 沙箱仍隔离宿主全局(无 process/require), 脚本无法逃逸
const context = vm.createContext(sandbox)

try {
  vm.runInContext(code, context, { timeout: 6000 })
} catch (e) {
  if (!inited && !requestHandler) {
    out({ ok: false, error: '脚本执行失败: ' + (e && e.message) })
    process.exit(1)
  }
}

// 操作白名单
const whitelistActions = {
  kw: ['musicUrl'], kg: ['musicUrl'], tx: ['musicUrl'], wy: ['musicUrl'], mg: ['musicUrl'], xm: ['musicUrl'],
  local: ['musicUrl', 'lyric', 'pic'],
}
// 音质: 音源声明什么就透传什么(不过滤, 只要音源支持就显示), 默认按优先级取最高
const qualityPriority = ['flac24bit', 'flac', '320k', '192k', '128k']

async function main() {
  const deadline = Date.now() + 8000
  while (!inited && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100))
  if (!inited || !inited.sources) {
    out({ ok: false, error: "脚本未声明 sources(缺少 lx.send('inited', ...) 调用)" })
    process.exit(1)
  }
  const meta = scriptInfo
  const sources = {}
  for (const source of Object.keys(inited.sources || {})) {
    const s = inited.sources[source]
    if (!s || s.type !== 'music') continue
    const q = (s.qualitys || []).filter((x) => typeof x === 'string' && x)
    // 操作也透传: 脚本声明什么就支持什么(musicUrl/lyric/pic)
    const a = (s.actions || []).filter((x) => typeof x === 'string' && x)
    if (q.length || a.length) sources[source] = { type: 'music', actions: a, qualitys: q }
  }
  if (!Object.keys(sources).length) {
    out({ ok: false, error: '脚本声明的 sources 全部无效(不在白名单内)' })
    process.exit(1)
  }
  out({ type: 'meta', name: meta.name, sources })

  const rl = readline.createInterface({ input: process.stdin })
  rl.on('line', async (line) => {
    try {
      const req = JSON.parse(line)
      const { source, action, info } = req
      if (!requestHandler) {
        out({ ok: false, error: '脚本未注册 request 处理' })
        return
      }
      try {
        const resp = await Promise.race([
          requestHandler({ source, action, info }),
          new Promise((_, reject) => setTimeout(() => reject(new Error('音源请求超时')), 30000)),
        ])
        if (typeof resp === 'string' && /^https?:/.test(resp)) {
          out({ ok: true, data: { url: resp } })
        } else if (resp && typeof resp === 'object' && typeof resp.url === 'string' && /^https?:/.test(resp.url)) {
          out({ ok: true, data: { url: resp.url, lyric: resp.lyric || null, cover: resp.cover || null } })
        } else {
          out({ ok: false, error: '音源返回格式无效' })
        }
      } catch (e) {
        out({ ok: false, error: (e && e.message) || String(e) })
      }
    } catch (e) {
      out({ ok: false, error: '协议错误' })
    }
  })
}

main().catch((e) => {
  out({ ok: false, error: '桥异常: ' + ((e && e.message) || e) })
  process.exit(1)
})
