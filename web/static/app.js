/* MusicArchiver 前端(原生 JS, 无构建) */
(function () {
  'use strict'

  var state = {
    tab: 'search', platform: 'wy', query: '', sources: [], extraOk: {}, activeSourceId: '',
    searchList: [], tasks: [], library: [], sseState: '未连接', now: null,
    subEditor: null,
    subBusy: null,
    libPath: '',
  }

  // 平台显示名与可搜索平台
  var PLATFORM_NAMES = { wy: '网易云', kg: '酷狗', tx: 'QQ音乐', kw: '酷我', mg: '咪咕', local: '本地', qsvip: '汽水', xm: '小爱' }
  var SEARCHABLE = ['wy', 'kg', 'tx', 'kw', 'mg']
  var QUALITY_NAMES = {
    '128k': '128k 标准', '192k': '192k 较高', '320k': '320k 高品',
    'flac': 'FLAC 无损', 'flac24bit': '24bit 母带', 'flac32bit': '32bit 母带', 'flac64bit': '64bit 母带',
    'hires': 'Hi-Res 高解析', 'hires24': 'Hi-Res 24bit', 'hiRes': 'Hi-Res 高解析', 'hi-res': 'Hi-Res 高解析',
    'surround': '环绕立体声', 'surround5.1': '5.1环绕', 'surround7.1': '7.1环绕', '5.1': '5.1环绕', '7.1': '7.1环绕',
    'dolby': '杜比全景声', 'dolby5.1': '杜比5.1', 'atmos': '杜比全景声', 'atmos_plus': '杜比全景声+',
    'sky': '臻品全景声', 'jyeffect': '臻品音效', 'jymaster': '臻品母带', 'master': '母带', 'vip': 'VIP 专属',
  }
  function qname(q) {
    if (QUALITY_NAMES[q]) return QUALITY_NAMES[q]
    var s = String(q).toLowerCase()
    if (s.indexOf('hires') >= 0 || s.indexOf('hi-res') >= 0) return 'Hi-Res 高解析'
    if (s.indexOf('surround') >= 0 || s.indexOf('环绕') >= 0) return '环绕立体声'
    if (s.indexOf('dolby') >= 0 || s.indexOf('杜比') >= 0) return '杜比全景声'
    if (s.indexOf('5.1') >= 0) return '5.1环绕'
    if (s.indexOf('7.1') >= 0) return '7.1环绕'
    if (s.indexOf('master') >= 0 || s.indexOf('母带') >= 0) return '母带'
    if (s === 'sky') return '臻品全景声'
    if (s.indexOf('jyeffect') >= 0) return '臻品音效'
    if (s.indexOf('jymaster') >= 0) return '臻品母带'
    return q
  }
  var QUALITY_PRIORITY = ['flac64bit', 'flac32bit', 'flac24bit', 'jymaster', 'hires', 'hires24', 'hiRes', 'hi-res', 'atmos_plus', 'atmos', 'dolby', 'sky', 'jyeffect', 'surround', 'surround7.1', '7.1', 'surround5.1', '5.1', 'master', 'flac', '320k', '192k', '128k']
  var DEFAULT_QUALITY = 'flac'

  var $ = function (id) { return document.getElementById(id) }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;')
  }

  function timeAgo(ms) {
    if (!ms) return '从未'
    var diff = Math.floor(Date.now() / 1000 - ms / 1000)
    if (diff < 5) return '刚刚'
    if (diff < 60) return diff + ' 秒前'
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前'
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前'
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + ' 天前'
    var d = new Date(ms)
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0')
  }

  // 综合分: 0-1, 对齐后端 SourceScore.score(). 用 0.5 占位质量分(无 per-quality 数据)
  function compositeScore(s) {
    var sc = s.score || {}
    var n = (sc.successes || 0) + (sc.failures || 0)
    if (n === 0) return 0.5
    var sr = sc.successRate || 0
    var avg = sc.avgLatencyMs || 5000
    var spd = Math.max(0, Math.min(1, (10000 - avg) / 9500))
    return 0.4 * 0.5 + 0.3 * spd + 0.3 * sr
  }

  function api(path, opts) {
    opts = opts || {}
    return fetch(path, {
      method: opts.method || 'GET',
      headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (r) { return r.json() })
  }

  function durText(d) {
    if (!d) return '-'
    var s = Math.round(d / 1000)
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0')
  }
  function sizeText(n) { return (n / 1048576).toFixed(1) + 'MB' }
  function stText(s) { return ({ queued: '排队', running: '运行中', success: '成功', failed: '失败', skipped: '跳过' }[s] || s) }
  function stClass(s) { return ({ queued: 'queued', running: 'running', success: 'success', failed: 'failed', skipped: 'skipped' }[s] || 'queued') }
  function lastSuccessHtml(ts) {
    if (!ts) return '<span class="badge failed">从未成功</span>'
    var diff = Date.now() / 1000 - ts
    var txt
    if (diff < 60) txt = '刚刚'
    else if (diff < 3600) txt = Math.floor(diff / 60) + '分钟前'
    else if (diff < 86400) txt = Math.floor(diff / 3600) + '小时前'
    else txt = Math.floor(diff / 86400) + '天前'
    var cls = diff < 86400 ? 'success' : 'skipped'
    return '<span class="badge ' + cls + '">' + txt + '</span>'
  }

  function scoreHtml(s) {
    var sc = s.score || {}
    var n = (sc.successes || 0) + (sc.failures || 0)
    if (n === 0) return '<span class="hint">未使用</span>'
    var sr = sc.successRate || 0
    var avg = sc.avgLatencyMs || 0
    var srPct = Math.round(sr * 100)
    var srCls = sr >= 0.7 ? 'success' : (sr >= 0.4 ? 'running' : 'failed')
    var lat = avg < 1000 ? Math.round(avg) + 'ms' : (avg < 5000 ? (avg / 1000).toFixed(1) + 's' : Math.round(avg / 1000) + 's')
    var comp = compositeScore(s).toFixed(2)
    return '<span class="badge ' + srCls + '">' + srPct + '%</span> ' +
           '<span style="color:#64748b;font-size:12px">' + lat + ' · 分 ' + comp + ' · n=' + n + '</span>'
  }

  function toast(msg, type) {
    var box = document.getElementById('toast-box')
    if (!box) {
      box = document.createElement('div')
      box.id = 'toast-box'
      box.style.cssText = 'position:fixed;top:16px;right:16px;z-index:99999;display:flex;flex-direction:column;gap:8px;max-width:320px'
      document.body.appendChild(box)
    }
    var el = document.createElement('div')
    el.style.cssText = 'padding:10px 16px;border-radius:10px;font-size:13px;color:#fff;box-shadow:0 6px 20px rgba(0,0,0,.18);animation:toastIn .2s ease;background:' + (type === 'success' ? '#16a34a' : type === 'warn' ? '#d97706' : '#dc2626') + ';word-break:break-all'
    el.textContent = msg
    box.appendChild(el)
    setTimeout(function () { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; setTimeout(function () { el.remove() }, 320) }, 3200)
  }

  function toggleEmpty(id, list) {
    var el = $(id)
    if (el) el.style.display = list && list.length ? 'none' : ''
  }

  function activeSource() {
    return state.sources.find(function (s) { return s.id === state.activeSourceId })
  }

  function switchTab(tab) {
    state.tab = tab
    ;['search', 'tasks', 'library', 'settings'].forEach(function (t) {
      $('view-' + t).style.display = t === tab ? '' : 'none'
      $('tab-' + t).className = t === tab ? 'on' : ''
    })
    if (tab === 'tasks') loadTasks()
    if (tab === 'library') loadLib()
    if (tab === 'settings') { loadSettings(); renderSubs() }
  }

  var OFFICIAL = ['wy', 'kg', 'tx', 'kw', 'mg']
  function renderPlatforms() {
    var sel = $('platform')
    var available = []
    var seen = {}
    OFFICIAL.forEach(function (k) { if (!seen[k]) { seen[k] = true; available.push(k) } })
    Object.keys(state.extraOk || {}).forEach(function (k) {
      if (state.extraOk[k] && !seen[k]) { seen[k] = true; available.push(k) }
    })
    if (!available.length) {
      sel.innerHTML = '<option value="">（请先到设置导入音源）</option>'
      sel.disabled = true
      return
    }
    sel.disabled = false
    sel.innerHTML = available.map(function (k) {
      return '<option value="' + k + '">' + (PLATFORM_NAMES[k] || k) + '</option>'
    }).join('')
    if (available.indexOf(state.platform) < 0) state.platform = available[0]
    sel.value = state.platform
  }

  function bestEntry(list) {
    var best = null
    ;(list || []).forEach(function (e) {
      if (!best) { best = e; return }
      var a = (e.sr || 0) * 1000 + (e.bits || 0)
      var b = (best.sr || 0) * 1000 + (best.bits || 0)
      if (a > b) best = e
    })
    return best
  }

  function renderSources() {
    $('btn-search').disabled = !state.sources.length
    var tbody = $('source-tbody')
    toggleEmpty('source-empty', state.sources)
    if (!state.sources.length) { tbody.innerHTML = ''; return }
    var sorted = state.sources.slice().sort(function (a, b) { return compositeScore(b) - compositeScore(a) })
    tbody.innerHTML = sorted.map(function (s) {
      var detail = Object.keys(s.sources || {}).map(function (k) {
        return (PLATFORM_NAMES[k] || k) + ' ' + (s.sources[k].qualitys || []).join('/')
      }).join('、')
      return '<tr><td><strong>' + esc(s.name) + '</strong></td><td>' + esc(Object.keys(s.sources || {}).join(', ')) + '</td>' +
        '<td>' + esc(detail) + '</td>' +
        '<td>' + scoreHtml(s) + '</td>' +
        '<td>' + lastSuccessHtml(s.lastSuccess) + '</td><td><button data-src-del="' + esc(s.id) + '">删除</button></td></tr>'
    }).join('')
    renderPlatforms()
    if (state.sources.length) $('btn-search').disabled = false
  }

  function refreshSources() {
    return api('/api/sources').then(function (rs) {
      state.sources = rs || []
      renderSources()
      testExtraPlatforms()
    }).catch(function () {})
  }

  function testExtraPlatforms() {
    var extras = {}
    state.sources.forEach(function (s) {
      Object.keys(s.sources || {}).forEach(function (k) {
        if (OFFICIAL.indexOf(k) >= 0) return
        var acts = ((s.sources[k] || {}).actions) || []
        if (acts.indexOf('search') >= 0 || acts.indexOf('musicSearch') >= 0) extras[k] = true
      })
    })
    Object.keys(extras).forEach(function (k) {
      api('/api/search', { method: 'POST', body: { query: '晴天', platform: k, limit: 1 } })
        .then(function (r) { state.extraOk[k] = !!(r.list && r.list.length); renderPlatforms() })
        .catch(function () { state.extraOk[k] = false; renderPlatforms() })
    })
  }

  function importByUrl() {
    var url = $('import-url').value.trim()
    if (!url) return
    api('/api/sources/import-url', { method: 'POST', body: { url: url } })
      .then(function () { return refreshSources() })
      .then(function () { toast('已导入', 'success') })
      .catch(function (e) { toast('导入失败: ' + e.message) })
  }

  function importByFile(e) {
    var f = e.target.files && e.target.files[0]
    if (!f) return
    f.text().then(function (text) {
      return api('/api/sources/import-file', { method: 'POST', body: { script: text } })
    }).then(function () { return refreshSources() })
      .then(function () { toast('已导入 ' + f.name, 'success') })
      .catch(function (err) { toast('导入失败: ' + err.message) })
    e.target.value = ''
  }

  function importJsFilesMulti(files, tag) {
    var jsFiles = (files || []).filter(function (f) { return f.name.toLowerCase().endsWith('.js') })
    if (!jsFiles.length) return toast(tag + ' 内没有 .js 文件', 'warn')
    var fd = new FormData()
    jsFiles.forEach(function (f) { fd.append('files', f) })
    fetch('/api/sources/import-files', { method: 'POST', body: fd })
      .then(function (r) { return r.json() })
      .then(function (r) {
        refreshSources()
        var added = (r.added || []).length, failed = (r.failed || []).length
        toast(tag + ': 成功 ' + added + ' / 失败 ' + failed, added ? 'success' : 'warn')
      })
      .catch(function (e) { toast('批量导入失败: ' + e.message) })
  }

  function importByFolder(e) {
    importJsFilesMulti(Array.from(e.target.files || []), '文件夹')
    e.target.value = ''
  }
  function importByMulti(e) {
    importJsFilesMulti(Array.from(e.target.files || []), '多选')
    e.target.value = ''
  }
  function importByZip(e) {
    var f = e.target.files && e.target.files[0]
    if (!f) return
    var fd = new FormData()
    fd.append('file', f)
    fetch('/api/sources/import-zip', { method: 'POST', body: fd })
      .then(function (r) { return r.json() })
      .then(function (r) {
        refreshSources()
        var added = (r.added || []).length, failed = (r.failed || []).length
        if (r && r.detail && /过大|413/.test(r.detail)) {
          toast('ZIP 太大 (上限 200MB, 可调 MAX_ZIP_UPLOAD_MB 环境变量)', 'warn')
        } else {
          toast('ZIP ' + f.name + ': 成功 ' + added + ' / 失败 ' + failed, added ? 'success' : 'warn')
        }
      })
      .catch(function (e) { toast('ZIP 导入失败: ' + e.message) })
    e.target.value = ''
  }
  function importByZipUrl() {
    var url = $('import-zip-url').value.trim()
    if (!url) return
    api('/api/sources/import-zip-url', { method: 'POST', body: { url } })
      .then(function (r) {
        refreshSources()
        var added = (r.added || []).length
        toast('ZIP URL: 成功 ' + added, added ? 'success' : 'warn')
        $('import-zip-url').value = ''
      })
      .catch(function (e) { toast('ZIP URL 导入失败: ' + e.message) })
  }

  function removeSource(id) {
    api('/api/sources/' + encodeURIComponent(id), { method: 'DELETE' })
      .then(function () {
        if (state.activeSourceId === id) state.activeSourceId = ''
        return refreshSources()
      })
      .catch(function (e) { toast('删除失败: ' + e.message) })
  }

  function renderSubs() {
    api('/api/subscriptions').then(function (rs) {
      var list = rs || []
      var tbody = $('sub-tbody')
      toggleEmpty('sub-empty', list)
      tbody.innerHTML = list.map(function (g) {
        var busy = state.subBusy === g.id
        var last = timeAgo(g.lastUpdate)
        var errBadge = (g.lastErrorCount > 0)
          ? '<span class="badge failed" title="上一轮 ' + g.lastErrorCount + ' 个错误">⚠ ' + g.lastErrorCount + ' 错</span> '
          : (g.lastUpdate ? '<span class="badge success">✓</span> ' : '')
        return '<tr>' +
          '<td><strong>' + esc(g.name) + '</strong></td>' +
          '<td>' + (g.urls || []).length + '</td>' +
          '<td>每 ' + (g.intervalMin || 60) + ' 分</td>' +
          '<td>' + errBadge + last + '</td>' +
          '<td>' +
            '<button data-sub-refresh="' + esc(g.id) + '"' + (busy ? ' disabled' : '') + '>' + (busy ? '刷新中…' : '立即刷新') + '</button> ' +
            '<button data-sub-del="' + esc(g.id) + '">删除</button>' +
          '</td>' +
        '</tr>'
      }).join('')
    }).catch(function () {})
  }

  function renderSubUrls() {
    var box = $('sub-urls')
    if (!box) return
    var urls = (state.subEditor && state.subEditor.urls) || []
    if (!urls.length) { box.innerHTML = '<span class="hint">尚未加入 URL</span>'; return }
    box.innerHTML = urls.map(function (u, i) {
      return '<div style="display:flex;gap:6px;align-items:center;padding:2px 0">' +
        '<span class="badge ' + (u.kind === 'zip' ? 'running' : 'success') + '">' + esc(u.kind) + '</span>' +
        '<code style="flex:1;word-break:break-all;font-size:12px">' + esc(u.url) + '</code>' +
        '<button data-sub-url-del="' + i + '">×</button>' +
      '</div>'
    }).join('')
  }

  function subAddUrl() {
    var url = $('sub-url').value.trim()
    if (!url) return
    var kind = $('sub-url-kind').value
    if (!state.subEditor) state.subEditor = { urls: [] }
    state.subEditor.urls.push({ url: url, kind: kind })
    $('sub-url').value = ''
    renderSubUrls()
  }
  function subDelUrl(i) {
    if (!state.subEditor) return
    state.subEditor.urls.splice(i, 1)
    renderSubUrls()
  }
  function clearSubEditor() {
    state.subEditor = null
    $('sub-editor').style.display = 'none'
    $('sub-url').value = ''
    renderSubUrls()
  }

  // 2 步式 addSub: 第一次点击仅打开 sub-editor, 二次点击才真创建
  function addSub() {
    if (state.subEditor) {
      var ed = state.subEditor
      if (!ed.urls || !ed.urls.length) return toast('至少加入一个 URL', 'warn')
      api('/api/subscriptions', { method: 'POST', body: { name: ed.name || $('sub-name').value.trim(), urls: ed.urls, intervalMin: ed.intervalMin || 60 } })
        .then(function (r) {
          state.subEditor = null
          $('sub-editor').style.display = 'none'
          $('sub-name').value = ''
          $('sub-interval').value = 60
          $('sub-url').value = ''
          renderSubUrls()
          renderSubs()
          refreshSources()
          if (r && r.initialFetchError) toast('已创建, 首次拉取失败: ' + r.initialFetchError, 'warn')
          else toast('已创建订阅组, ' + (ed.urls.length) + ' 个 URL 已拉取', 'success')
        })
        .catch(function (e) { toast('创建失败: ' + e.message) })
      return
    }
    var name = $('sub-name').value.trim()
    if (!name) return toast('需要订阅组名称', 'warn')
    state.subEditor = {
      name: name,
      intervalMin: parseInt($('sub-interval').value) || 60,
      urls: []
    }
    $('sub-editor').style.display = ''
    $('sub-editor-title').textContent = '新建订阅组: ' + name + ' — 添加 URL 后再次点击"创建"'
    renderSubUrls()
    toast('请在下方添加 URL, 然后再次点击"创建"', 'success')
  }

  function refreshSub(gid) {
    if (state.subBusy === gid) return
    state.subBusy = gid
    renderSubs()
    api('/api/subscriptions/' + encodeURIComponent(gid) + '/refresh', { method: 'POST' })
      .then(function (r) {
        var n = (r && r.added) ? r.added.length : 0
        var e = (r && r.errors) ? r.errors.length : 0
        toast('已刷新: 导入 ' + n + ' / 错误 ' + e, e ? 'warn' : 'success')
        renderSubs(); refreshSources()
      })
      .catch(function (e) { toast('刷新失败: ' + e.message) })
      .then(function () { state.subBusy = null; renderSubs() }, function () { state.subBusy = null; renderSubs() })
  }
  function removeSub(gid) {
    if (!confirm('删除订阅组? 该组导入的音源会被清理')) return
    api('/api/subscriptions/' + encodeURIComponent(gid), { method: 'DELETE' })
      .then(function () { renderSubs(); refreshSources(); toast('已删除', 'success') })
      .catch(function (e) { toast('删除失败: ' + e.message) })
  }

  function renderSearchRows() {
    var tbody = $('search-tbody')
    if (!state.searchList.length) {
      tbody.innerHTML = ''
      toggleEmpty('search-empty', state.searchList)
      $('btn-batch').textContent = '批量上传所选(0)'
      return
    }
    toggleEmpty('search-empty', state.searchList)
    tbody.innerHTML = state.searchList.map(function (s, i) {
      var checks = '<button data-act="play" data-i="' + i + '">▶ 试听</button>' +
        '<button data-act="dl" data-i="' + i + '">下载</button>' +
        '<button data-act="up" data-i="' + i + '">上传</button>'
      var chips = ''
      if (s._verified) {
        chips = s._verified.map(function (q) { return '<span class="qchip ok">' + esc(q) + '</span>' }).join('')
        if (!s._verified.length) chips = '<span class="hint">无FLAC</span>'
      } else if (s._verifying) {
        chips = '<span class="hint">校验中…</span>'
      } else {
        chips = '<span class="hint">待校验</span>'
      }
      return '<tr>' +
        '<td><input type="checkbox" data-i="' + i + '" class="row-check"' + (s._sel ? ' checked' : '') + ' /></td>' +
        '<td><strong>' + esc(s.name) + '</strong></td><td>' + esc(s.singer) + '</td><td>' + esc(s.albumName) + '</td>' +
        '<td>' + (chips || '<span class="hint">-</span>') + '</td>' +
        '<td>' + durText(s.duration) + '</td><td>' + checks + '</td></tr>'
    }).join('')
    $('btn-batch').textContent = '批量上传所选(' + selected().length + ')'
  }

  function selected() {
    return state.searchList.filter(function (s) { return s._sel })
  }

  var tipTimer = null
  function loadHot() {
    api('/api/hot?platform=' + encodeURIComponent(state.platform)).then(function (r) {
      var box = $('hot-box')
      var row = $('hot-row')
      var list = (r && r.list) || []
      if (!list.length) { row.style.display = 'none'; return }
      row.style.display = ''
      box.innerHTML = list.map(function (k) {
        return '<button class="hot-tag" data-k="' + esc(k) + '">' + esc(k) + '</button>'
      }).join('')
    }).catch(function () {})
  }

  function loadTips() {
    var q = $('query').value.trim()
    var dl = $('tip-list')
    if (!q) { dl.innerHTML = ''; return }
    api('/api/search/tip?platform=' + encodeURIComponent(state.platform) + '&keyword=' + encodeURIComponent(q)).then(function (r) {
      dl.innerHTML = ((r && r.list) || []).map(function (k) { return '<option value="' + esc(k) + '">' }).join('')
    }).catch(function () {})
  }

  function declaredQualitys() {
    var qset = {}
    var black = { '128k': 1, '192k': 1, '320k': 1 }
    state.sources.forEach(function (s) {
      var qs = (s.sources && s.sources[state.platform] && s.sources[state.platform].qualitys) || []
      qs.forEach(function (q) { if (!black[q]) qset[q] = true })
    })
    return Object.keys(qset)
  }

  function mergeVerified(song, list) {
    ;(list || []).forEach(function (e) {
      var key = e.size || e.url
      var old = song._vsize[key]
      if (!old) { song._vsize[key] = e; return }
      if ((e.sr || 0) * 1000 + (e.bits || 0) > (old.sr || 0) * 1000 + (old.bits || 0)) song._vsize[key] = e
    })
    song._verifiedQualities = Object.values(song._vsize)
    song._verified = song._verifiedQualities.map(function (x) { return x.spec || x.quality })
  }

  function verifySong(song) {
    song._verifying = true
    song._verified = []
    song._verifiedQualities = []
    song._vsize = {}
    renderSearchRows()
    var sources = state.sources.filter(function (s) { return s.sources && s.sources[state.platform] })
    if (!sources.length) { song._verifying = false; renderSearchRows(); return Promise.resolve() }
    return fetch('/api/resolve-all/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform: state.platform, song: pickSong(song), qualities: [] }),
    }).then(function (resp) {
      var reader = resp.body.getReader()
      var decoder = new TextDecoder()
      var buf = ''
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { song._verifying = false; renderSearchRows(); return }
          buf += decoder.decode(res.value, { stream: true })
          var parts = buf.split('\n\n')
          buf = parts.pop()
          parts.forEach(function (p) {
            var m = /^data: (.*)$/m.exec(p)
            if (!m) return
            try { var d = JSON.parse(m[1]) } catch (e) { return }
            if (d.done) { song._verifying = false; renderSearchRows(); return }
            mergeVerified(song, d.list || [])
            renderSearchRows()
          })
          return pump()
        })
      }
      return pump()
    }).catch(function () { song._verifying = false; renderSearchRows() })
  }

  function verifySongs(list) {
    var n = Math.min(list.length, 12)
    var i = 0
    function next() {
      if (i >= n) return
      verifySong(list[i]).then(function () { i++; next() }).catch(function () { i++; next() })
    }
    next()
  }

  function doSearch() {
    var q = $('query').value.trim()
    if (!q) return
    state.platform = $('platform').value
    if (!state.platform) return toast('当前音源没有可搜索的平台', 'warn')
    api('/api/search', { method: 'POST', body: { query: q, platform: state.platform, limit: 30 } })
      .then(function (r) {
        state.searchList = (r.list || []).map(function (s) { s._sel = false; return s })
        renderSearchRows()
        verifySongs(state.searchList)
      })
      .catch(function (e) { toast('搜索失败: ' + e.message) })
  }

  function resolveSong(song) {
    var best = bestEntry(song._verifiedQualities)
    if (best) return Promise.resolve({ url: best.url, spec: best.spec })
    return verifySong(song).then(function () {
      var b = bestEntry(song._verifiedQualities)
      if (b) return { url: b.url, spec: b.spec }
      return api('/api/resolve', { method: 'POST', body: { platform: state.platform, song: pickSong(song), quality: 'flac' } })
    })
  }

  function play(i) {
    var song = state.searchList[i]
    resolveSong(song).then(function (r) {
      if (r && r.url) {
        state.now = { name: song.name, singer: song.singer, url: '/api/stream?url=' + encodeURIComponent(r.url) }
        $('player').style.display = ''
        $('player-info').textContent = song.name + ' - ' + song.singer
        $('audio').src = state.now.url
      } else {
        toast('解析失败: ' + ((r && r.error) || '无链接'))
      }
    }).catch(function (e) { toast('解析失败: ' + e.message) })
  }

  function download(i) {
    var song = state.searchList[i]
    resolveSong(song).then(function (r) {
      if (r && r.url) {
        var url = '/api/download?url=' + encodeURIComponent(r.url) + '&name=' + encodeURIComponent(song.name + ' - ' + song.singer + '.flac')
        window.open(url, '_blank')
      } else toast('解析失败: ' + ((r && r.error) || '无链接'))
    }).catch(function (e) { toast('解析失败: ' + e.message) })
  }

  function createTask(songs) {
    return api('/api/tasks', {
      method: 'POST',
      body: {
        platform: state.platform, songs: songs, qualities: ['flac24bit', 'hires', 'jymaster', 'flac', '320k', '192k', '128k'],
        withLyric: $('with-lyric').checked, withCover: $('with-cover').checked,
      },
    })
  }

  function pickSong(s) {
    return { id: s.id, name: s.name, singer: s.singer, albumName: s.albumName, albumId: s.albumId, duration: s.duration, interval: s.interval, songmid: s.songmid, songId: s.songId, strMediaMid: s.strMediaMid, hash: s.hash, rid: s.rid, copyrightId: s.copyrightId, contentId: s.contentId, picUrl: s.picUrl }
  }

  function uploadOne(i) {
    var song = state.searchList[i]
    createTask([pickSong(song)]).then(function (r) {
      toast('已创建任务: ' + r.id, 'success')
      switchTab('tasks')
    }).catch(function (e) { toast(e.message) })
  }

  function batchUpload() {
    var list = selected()
    if (!list.length) return toast('请先勾选歌曲', 'warn')
    createTask(list.map(pickSong)).then(function (r) {
      toast('已创建批量任务: ' + r.id, 'success')
      switchTab('tasks')
    }).catch(function (e) { toast(e.message) })
  }

  function renderTasks() {
    var wrap = $('task-list')
    toggleEmpty('task-empty', state.tasks)
    if (!state.tasks.length) { wrap.innerHTML = ''; return }
    wrap.innerHTML = state.tasks.map(function (t) {
      var logs = (t.log || []).map(function (l) { return '<div class="log">' + esc(l) + '</div>' }).join('')
      return '<div class="task">' +
        '<div class="head"><span>' + esc(t.title) + '</span>' +
        '<span class="badge ' + stClass(t.status) + '">' + stText(t.status) + ' ' + t.progress + '%</span></div>' +
        '<div class="bar"><div class="fill" style="width:' + t.progress + '%"></div></div>' + logs + '</div>'
    }).join('')
  }

  function loadTasks() {
    api('/api/tasks').then(function (r) {
      state.tasks = (r && r.list) || []
      renderTasks()
    }).catch(function () {})
  }

  var es = null
  function startSSE() {
    if (es) es.close()
    state.sseState = '连接中…'
    $('sse-state').textContent = state.sseState
    es = new EventSource('/api/tasks/events')
    es.onopen = function () { state.sseState = '实时进度已开启'; $('sse-state').textContent = state.sseState }
    es.onerror = function () { state.sseState = '重连中…'; $('sse-state').textContent = state.sseState }
    es.onmessage = function (ev) {
      var d = JSON.parse(ev.data)
      if (d.type !== 'task') return
      var i = state.tasks.findIndex(function (t) { return t.id === d.task.id })
      if (i >= 0) state.tasks[i] = Object.assign({}, state.tasks[i], d.task)
      else state.tasks.unshift(d.task)
      renderTasks()
    }
  }

  function renderLib() {
    var tbody = $('lib-tbody')
    toggleEmpty('lib-empty', state.library)
    var back = state.libPath ? '<button id="lib-up">⬆ 返回上级</button> ' : ''
    var current = state.libPath ? decodeURIComponent(state.libPath) : '/'
    $('lib-nav').innerHTML = back + '<span class="hint">' + esc(current) + '</span>'
    toggleEmpty('lib-empty', state.library)
    if (!state.library.length) { tbody.innerHTML = ''; return }
    tbody.innerHTML = state.library.map(function (it, i) {
      var del = it.isDir ? '' : '<button data-act="del" data-i="' + i + '">删除</button>'
      var shown = it.displayname || decodeURIComponent(it.href || '')
      var open = it.isDir ? '<button data-act="open" data-i="' + i + '">打开</button>' : ''
      return '<tr><td>' + esc(shown) + '</td><td>' + (it.isDir ? '📁 目录' : '🎵 文件') + '</td>' +
        '<td>' + (it.size ? sizeText(it.size) : '-') + '</td><td>' + open + del + '</td></tr>'
    }).join('')
  }

  function loadLib(path) {
    if (typeof path === 'string') state.libPath = path
    api('/api/webdav/list?path=' + encodeURIComponent(state.libPath)).then(function (r) {
      state.library = Array.isArray(r) ? r : []
      renderLib()
    }).catch(function (e) { toast('加载失败: ' + e.message) })
  }
  function enterLib(it) {
    if (!it.isDir) return
    loadLib(it.href)
  }
  function upLib() {
    var p = state.libPath
    if (!p) return
    var trimmed = p.replace(/\/$/, '')
    var idx = trimmed.lastIndexOf('/')
    state.libPath = idx > 0 ? trimmed.slice(0, idx + 1) : ''
    loadLib(state.libPath)
  }

  function delItem(i) {
    var it = state.library[i]
    if (!confirm('删除 ' + it.href + ' ?')) return
    api('/api/webdav/delete', { method: 'DELETE', body: { href: it.href } })
      .then(function () { loadLib() })
      .catch(function (e) { toast('删除失败: ' + e.message) })
  }

  function loadSettings() {
    api('/api/config').then(function (c) {
      if (c && c.webdav) {
        $('dav-url').value = c.webdav.url || ''
        $('dav-user').value = c.webdav.user || ''
        $('dav-pass').value = ''
      }
    }).catch(function () {})
  }

  function saveCfg() {
    api('/api/config', {
      method: 'POST',
      body: { url: $('dav-url').value, user: $('dav-user').value, pass: $('dav-pass').value },
    }).then(function () { toast('已保存', 'success') }).catch(function (e) { toast('保存失败: ' + e.message) })
  }

  function bind() {
    ;['search', 'tasks', 'library', 'settings'].forEach(function (t) {
      $('tab-' + t).addEventListener('click', function () { switchTab(t) })
    })

    $('btn-search').addEventListener('click', doSearch)
    $('query').addEventListener('keyup', function (e) {
      if (e.key === 'Enter') return doSearch()
      clearTimeout(tipTimer)
      tipTimer = setTimeout(loadTips, 300)
    })
    $('platform').addEventListener('change', function () { state.platform = $('platform').value; loadHot() })
    $('hot-box').addEventListener('click', function (e) {
      var k = e.target.getAttribute('data-k')
      if (k) { $('query').value = k; doSearch() }
    })
    $('check-all').addEventListener('change', function (e) {
      var on = e.target.checked
      state.searchList.forEach(function (s) { s._sel = on })
      renderSearchRows()
    })
    $('btn-batch').addEventListener('click', batchUpload)
    $('search-tbody').addEventListener('click', function (e) {
      var t = e.target
      var i = t.getAttribute('data-i')
      if (!i) return
      if (t.getAttribute('data-act') === 'play') play(+i)
      else if (t.getAttribute('data-act') === 'dl') download(+i)
      else if (t.getAttribute('data-act') === 'up') uploadOne(+i)
    })
    $('search-tbody').addEventListener('change', function (e) {
      if (!e.target.classList.contains('row-check')) return
      var i = +e.target.getAttribute('data-i')
      if (state.searchList[i]) state.searchList[i]._sel = e.target.checked
      renderSearchRows()
    })

    $('btn-sse').addEventListener('click', startSSE)
    $('btn-refresh-lib').addEventListener('click', function () { loadLib(state.libPath) })
    $('btn-backfill').addEventListener('click', function () {
      api('/api/backfill', { method: 'POST', body: {} }).then(function (r) {
        toast('补全任务已创建: ' + r.id, 'success')
        switchTab('tasks')
      }).catch(function (e) { toast('创建失败: ' + e.message) })
    })
    $('lib-tbody').addEventListener('click', function (e) {
      var t = e.target
      if (!t) return
      var i = t.getAttribute('data-i')
      if (!i) return
      if (t.getAttribute('data-act') === 'del') delItem(+i)
      else if (t.getAttribute('data-act') === 'open') enterLib(state.library[+i])
    })
    $('lib-nav').addEventListener('click', function (e) {
      if (e.target.id === 'lib-up') upLib()
    })

    $('btn-save-cfg').addEventListener('click', saveCfg)
    $('btn-import-url').addEventListener('click', importByUrl)
    $('import-file').addEventListener('change', importByFile)
    $('source-tbody').addEventListener('click', function (e) {
      var id = e.target.getAttribute('data-src-del')
      if (id) removeSource(id)
    })
    if ($('import-folder')) $('import-folder').addEventListener('change', importByFolder)
    if ($('import-multi')) $('import-multi').addEventListener('change', importByMulti)
    if ($('import-zip')) $('import-zip').addEventListener('change', importByZip)
    if ($('btn-import-zip-url')) $('btn-import-zip-url').addEventListener('click', importByZipUrl)

    if ($('btn-add-sub')) {
      $('btn-add-sub').addEventListener('click', addSub)
      $('btn-sub-add-url').addEventListener('click', subAddUrl)
      $('sub-tbody').addEventListener('click', function (e) {
        var t = e.target
        var rid = t.getAttribute('data-sub-refresh')
        var did = t.getAttribute('data-sub-del')
        if (rid) refreshSub(rid)
        else if (did) removeSub(did)
      })
      $('sub-urls').addEventListener('click', function (e) {
        var i = e.target.getAttribute('data-sub-url-del')
        if (i !== null) subDelUrl(Number(i))
      })
    }
  }

  bind()
  refreshSources()
  loadTasks()
  loadSettings()
  loadHot()
})()
