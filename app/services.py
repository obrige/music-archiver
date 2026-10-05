# -*- coding: utf-8 -*-
"""搜索 / FLAC 解析 / 元数据 / WebDAV 客户端(全部基于标准库 urllib, 零额外依赖)"""
import base64
import email.utils
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'
PROPFIND_BODY = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>'


# ---------- HTTP 基础 ----------
def http_get(url, headers=None, timeout=20, referer=None):
    h = {'User-Agent': UA}
    if referer:
        h['Referer'] = referer
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        raise RuntimeError('HTTP %s' % e.code)


def fetch_json(url, headers=None, timeout=20, referer=None):
    text = http_get(url, headers, timeout, referer)
    try:
        return json.loads(text)
    except Exception:
        return text


# ---------- 搜索 ----------
def _eapi_params(url, obj):
    """网易云 eapi 加密(与官方 lx-music 一致)"""
    import hashlib
    text = json.dumps(obj, separators=(',', ':'))
    message = 'nobody%suse%smd5forencrypt' % (url, text)
    digest = hashlib.md5(message.encode()).hexdigest()
    data = '%s-36cd479b6b5-%s-36cd479b6b5-%s' % (url, text, digest)
    from Crypto.Cipher import AES
    cipher = AES.new(b'e82ckenh8dichen8', AES.MODE_ECB)
    raw = data.encode()
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad
    return cipher.encrypt(raw).hex().upper()


def eapi_post(url, obj):
    body = urllib.parse.urlencode({'params': _eapi_params(url, obj)})
    req = urllib.request.Request('http://interface.music.163.com/eapi/batch', method='POST', headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36',
        'Origin': 'https://music.163.com',
        'Content-Type': 'application/x-www-form-urlencoded',
    }, data=body.encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        raise RuntimeError('eapi HTTP %s' % e.code)
    except urllib.error.URLError:
        req2 = urllib.request.Request('https://interface.music.163.com/eapi/batch', method='POST', headers=req.headers, data=req.data)
        with urllib.request.urlopen(req2, timeout=20) as r2:
            return json.loads(r2.read().decode('utf-8', 'replace'))


# ================= 搜索: 官方 lx-music-desktop 完整移植 =================
import hashlib
import base64


def _eapi_params(url, obj):
    """网易云 eapi 加密(与官方一致)"""
    text = json.dumps(obj, separators=(',', ':'))
    message = 'nobody%suse%smd5forencrypt' % (url, text)
    digest = hashlib.md5(message.encode()).hexdigest()
    data = '%s-36cd479b6b5-%s-36cd479b6b5-%s' % (url, text, digest)
    from Crypto.Cipher import AES
    cipher = AES.new(b'e82ckenh8dichen8', AES.MODE_ECB)
    raw = data.encode()
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad
    return cipher.encrypt(raw).hex().upper()


def eapi_post(url, obj):
    """官方通过 /eapi/batch 批量端点转发, url 为不带 /eapi 前缀的路径"""
    body = urllib.parse.urlencode({'params': _eapi_params(url, obj)})
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36',
        'Origin': 'https://music.163.com',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    try:
        req = urllib.request.Request('http://interface.music.163.com/eapi/batch', method='POST', headers=headers, data=body.encode())
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        raise RuntimeError('eapi HTTP %s' % e.code)
    except urllib.error.URLError:
        req2 = urllib.request.Request('https://interface.music.163.com/eapi/batch', method='POST', headers=headers, data=body.encode())
        with urllib.request.urlopen(req2, timeout=20) as r2:
            return json.loads(r2.read().decode('utf-8', 'replace'))


def _fmt_size(n):
    """sizeFormate 的近似实现: 字节 -> 可读"""
    try:
        n = float(n)
    except Exception:
        return ''
    if n >= 1048576:
        return '%.1fMB' % (n / 1048576)
    if n >= 1024:
        return '%.1fKB' % (n / 1024)
    return '%dB' % int(n)


def _fmt_time(sec):
    """formatPlayTime: 秒 -> m:ss"""
    try:
        sec = int(sec)
    except Exception:
        return ''
    if sec < 0:
        return ''
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return '%d:%02d:%02d' % (h, m, s)
    return '%d:%02d' % (m, s)


# ---------- 网易云 ----------
def search_wy(query, limit=30):
    j = eapi_post('/api/search/song/list/page', {
        'keyword': query, 'needCorrect': '1', 'channel': 'typing',
        'offset': 0, 'scene': 'normal', 'total': 'true', 'limit': limit,
    })
    resources = ((j.get('data') or {}).get('resources')) if isinstance(j, dict) else None
    resources = resources or []
    out = []
    for item in resources:
        si = (item.get('baseInfo') or {}).get('simpleSongData') or {}
        al = si.get('al') or {}
        ar = si.get('ar') or []
        mid = str(si.get('id') or '')
        pic = al.get('picUrl') or ''
        out.append({
            'id': mid, 'name': si.get('name'),
            'singer': '/'.join([a.get('name', '') for a in ar]),
            'albumName': al.get('name'), 'albumId': al.get('id'),
            'duration': si.get('dt'), 'interval': _fmt_time((si.get('dt') or 0) / 1000),
            'songmid': mid, 'picUrl': pic, 'img': pic, 'source': 'wy',
        })
    return {'list': out, 'total': len(out), 'source': 'wy'}


# ---------- 酷狗 ----------
def search_kg(query, limit=30):
    url = ('https://songsearch.kugou.com/song_search_v2?keyword=%s&page=1&pagesize=%d'
           '&userid=0&clientver=&platform=WebFilter&filter=2&iscorrection=1&privilege_filter=0&area_code=1'
           % (urllib.parse.quote(query), limit))
    j = fetch_json(url, referer='https://www.kugou.com/', timeout=20)
    lists = ((j.get('data') or {}).get('lists')) if isinstance(j, dict) else None
    lists = lists or []

    def to_song(raw):
        types, _types = [], {}
        if raw.get('FileSize'):
            _types['128k'] = {'size': _fmt_size(raw.get('FileSize')), 'hash': raw.get('FileHash')}
        if raw.get('HQFileSize'):
            _types['320k'] = {'size': _fmt_size(raw.get('HQFileSize')), 'hash': raw.get('HQFileHash')}
        if raw.get('SQFileSize'):
            _types['flac'] = {'size': _fmt_size(raw.get('SQFileSize')), 'hash': raw.get('SQFileHash')}
        if raw.get('ResFileSize'):
            _types['flac24bit'] = {'size': _fmt_size(raw.get('ResFileSize')), 'hash': raw.get('ResFileHash')}
        types = list(_types.keys())
        dur = raw.get('Duration') or 0
        h = raw.get('FileHash') or ''
        return {
            'id': h, 'name': raw.get('SongName'),
            'singer': '/'.join([s.get('name', '') for s in (raw.get('Singers') or [])]),
            'albumName': raw.get('AlbumName'), 'albumId': raw.get('AlbumID'),
            'duration': dur, 'interval': _fmt_time(dur), '_interval': dur,
            'songmid': str(raw.get('Audioid') or ''), 'hash': h,
            'types': types, '_types': _types, 'picUrl': raw.get('AlbumImage') or '', 'source': 'kg',
        }

    out, seen = [], set()
    for it in lists:
        key = str(it.get('Audioid')) + str(it.get('FileHash'))
        if key in seen:
            continue
        seen.add(key)
        out.append(to_song(it))
        for g in (it.get('Grp') or []):
            gk = str(g.get('Audioid')) + str(g.get('FileHash'))
            if gk in seen:
                continue
            seen.add(gk)
            out.append(to_song(g))
        if len(out) >= limit:
            break
    return {'list': out, 'total': len(out), 'source': 'kg'}


# ---------- QQ音乐(zzcSign 签名) ----------
TX_PART1 = [23, 14, 6, 36, 16, 40, 7, 19]
TX_PART2 = [16, 1, 32, 12, 19, 27, 8, 5]
TX_SCRAMBLE = [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64, 242, 133, 143, 161, 121, 179]


def zzc_sign(text):
    h = hashlib.sha1(text.encode()).hexdigest()
    part1 = ''.join(h[i] if i < len(h) else '' for i in TX_PART1)
    part2 = ''.join(h[i] if i < len(h) else '' for i in TX_PART2)
    part3 = bytes(TX_SCRAMBLE[i] ^ int(h[i * 2:i * 2 + 2], 16) for i in range(20))
    b64 = base64.b64encode(part3).decode().replace('\\', '').replace('/', '').replace('+', '').replace('=', '')
    return ('zzc' + part1 + b64 + part2).lower()


def search_tx(query, limit=30):
    data = {
        'comm': {'ct': '11', 'cv': '14090508', 'v': '14090508', 'tmeAppID': 'qqmusic',
                 'phonetype': 'EBG-AN10', 'deviceScore': '553.47', 'devicelevel': '50',
                 'newdevicelevel': '20', 'rom': 'HuaWei/EMOTION/EmotionUI_14.2.0', 'os_ver': '12',
                 'OpenUDID': '0', 'OpenUDID2': '0', 'QIMEI36': '0', 'udid': '0', 'chid': '0',
                 'aid': '0', 'oaid': '0', 'taid': '0', 'tid': '0', 'wid': '0', 'uid': '0',
                 'sid': '0', 'modeSwitch': '6', 'teenMode': '0', 'ui_mode': '2', 'nettype': '1020', 'v4ip': ''},
        'req': {'module': 'music.search.SearchCgiService', 'method': 'DoSearchForQQMusicMobile',
                'param': {'search_type': 0, 'searchid': str(__import__('random').random())[2:],
                          'query': query, 'page_num': 1, 'num_per_page': limit, 'highlight': 0,
                          'nqc_flag': 0, 'multi_zhida': 0, 'cat': 2, 'grp': 1, 'sin': 0, 'sem': 0}},
    }
    sign = zzc_sign(json.dumps(data, separators=(',', ':')))
    body = json.dumps(data, separators=(',', ':'))
    req = urllib.request.Request('https://u.y.qq.com/cgi-bin/musics.fcg?sign=' + sign, method='POST', headers={
        'User-Agent': 'QQMusic 14090508(android 12)',
        'Content-Type': 'application/json',
    }, data=body.encode())
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode('utf-8', 'replace'))
    item_song = (((j.get('req') or {}).get('data') or {}).get('body') or {}).get('item_song') or []
    out = []
    for item in item_song:
        file = item.get('file') or {}
        mid = file.get('media_mid') or ''
        if not mid:
            continue
        album = item.get('album') or {}
        album_id = album.get('mid') or ''
        _types = {}
        for q, k in (('128k', 'size_128mp3'), ('320k', 'size_320mp3'), ('flac', 'size_flac'), ('flac24bit', 'size_hires')):
            if file.get(k):
                _types[q] = {'size': _fmt_size(file.get(k))}
        pic = ''
        if album_id and album_id != '空':
            pic = 'https://y.gtimg.cn/music/photo_new/T002R500x500M000%s.jpg' % album_id
        out.append({
            'id': str(item.get('mid') or ''), 'name': item.get('title'),
            'singer': '/'.join([s.get('name', '') for s in (item.get('singer') or [])]),
            'albumName': album.get('name') or '', 'albumId': album_id,
            'duration': int(item.get('interval') or 0) * 1000, 'interval': _fmt_time(item.get('interval') or 0),
            'songmid': item.get('mid') or '', 'songId': item.get('id'), 'strMediaMid': mid,
            'types': list(_types.keys()), '_types': _types, 'picUrl': pic, 'source': 'tx',
        })
    return {'list': out, 'total': len(out), 'source': 'tx'}


# ---------- 酷我 ----------
KW_MINFO = __import__('re').compile(r'level:(\w+),bitrate:(\d+),format:(\w+),size:([\w.]+)')


def search_kw(query, limit=30):
    url = ('http://search.kuwo.cn/r.s?client=kt&all=%s&pn=0&rn=%d&uid=794762570&ver=kwplayer_ar_9.2.2.1'
           '&vipver=1&show_copyright_off=1&newver=1&ft=music&cluster=0&strategy=2012&encoding=utf8'
           '&rformat=json&vermerge=1&mobi=1&issubtitle=1' % (urllib.parse.quote(query), limit))
    j = fetch_json(url, timeout=20)
    items = j.get('abslist') if isinstance(j, dict) else None
    items = items or []
    out = []
    for info in items:
        nminfo = info.get('N_MINFO') or ''
        _types = {}
        for m in KW_MINFO.finditer(nminfo):
            bitrate = m.group(2)
            q = {'4000': 'flac24bit', '2000': 'flac', '320': '320k', '128': '128k'}.get(bitrate)
            if q:
                _types[q] = {'size': m.group(4)}
        rid = str(info.get('MUSICRID') or '')
        song_id = rid.replace('MUSIC_', '')
        dur = 0
        try:
            dur = int(info.get('DURATION') or 0)
        except ValueError:
            dur = 0
        out.append({
            'id': song_id, 'name': info.get('SONGNAME'),
            'singer': info.get('ARTIST') or '',
            'albumName': info.get('ALBUM') or '', 'albumId': info.get('ALBUMID') or '',
            'duration': dur * 1000, 'interval': _fmt_time(dur),
            'songmid': song_id, 'rid': rid, 'types': list(_types.keys()), '_types': _types,
            'picUrl': info.get('web_albumpic_short') or '', 'source': 'kw',
        })
    return {'list': out, 'total': len(out), 'source': 'kw'}


# ---------- 咪咕 ----------
MG_DEVICE = '963B7AA0D21511ED807EE5846EC87D20'
MG_SIGN_MD5 = '6cdc72a439cef99a3418d2a78aa28c73'


def mg_sign(time, text):
    return hashlib.md5(('%s%syyapp2d16148780a1dcc7408e06336b98cfd50%s%s' % (text, MG_SIGN_MD5, MG_DEVICE, time)).encode()).hexdigest()


def search_mg(query, limit=30):
    import time as _time
    ts = str(int(_time.time() * 1000))
    search_switch = urllib.parse.quote(json.dumps({"song": 1, "album": 0, "singer": 0, "tagSong": 1, "mvSong": 0, "bestShow": 1, "songlist": 0, "lyricSong": 0}))
    url = ('https://jadeite.migu.cn/music_search/v3/search/searchAll?isCorrect=0&isCopyright=1&searchSwitch=%s'
           '&pageSize=%d&text=%s&pageNo=1&sort=0&sid=USS' % (search_switch, limit, urllib.parse.quote(query)))
    j = fetch_json(url, headers={
        'uiVersion': 'A_music_3.6.1', 'deviceId': MG_DEVICE, 'timestamp': ts,
        'sign': mg_sign(ts, query), 'channel': '0146921',
        'User-Agent': 'Mozilla/5.0 (Linux; U; Android 11.0.0; zh-cn; MI 11 Build/OPR1.170623.032) AppleWebKit/534.30 (KHTML, like Gecko) Version/4.0 Mobile Safari/534.30',
    }, timeout=20)
    out, seen = [], set()
    groups = ((j.get('songResultData') or {}).get('resultList')) if isinstance(j, dict) else None
    groups = groups or (j if isinstance(j, list) else [])
    for grp in groups:
        for data in (grp if isinstance(grp, list) else []):
            if not isinstance(data, dict):
                continue
            cid = data.get('copyrightId')
            sid = data.get('songId')
            if not sid or not cid or cid in seen:
                continue
            seen.add(cid)
            _types = {}
            for t in (data.get('audioFormats') or []):
                fmt = t.get('formatType')
                q = {'PQ': '128k', 'HQ': '320k', 'SQ': 'flac', 'ZQ24': 'flac24bit'}.get(fmt)
                if q:
                    _types[q] = {'size': _fmt_size(t.get('asize') or t.get('isize'))}
            img = data.get('img3') or data.get('img2') or data.get('img1') or ''
            if img and not img.startswith('http'):
                img = 'http://d.musicapp.migu.cn' + img
            dur = int(data.get('duration') or 0)
            out.append({
                'id': str(sid), 'name': data.get('name'),
                'singer': '/'.join([s.get('name', '') for s in (data.get('singerList') or [])]),
                'albumName': data.get('album') or '', 'albumId': data.get('albumId'),
                'duration': dur * 1000, 'interval': _fmt_time(dur),
                'songmid': str(sid), 'copyrightId': cid, 'contentId': data.get('contentId'),
                'types': list(_types.keys()), '_types': _types, 'picUrl': img, 'source': 'mg',
            })
    return {'list': out, 'total': len(out), 'source': 'mg'}


SEARCH_IMPL = {'wy': search_wy, 'kg': search_kg, 'tx': search_tx, 'kw': search_kw, 'mg': search_mg}


# ---------- 热搜 ----------
def hot_search(platform):
    if platform == 'kg':
        url = ('http://gateway.kugou.com/api/v3/search/hot_tab'
               '?signature=ee44edb9d7155821412d220bcaf509dd&appid=1005&clientver=10026&plat=0')
        j = fetch_json(url, headers={
            'dfid': '1ssiv93oVqMp27cirf2CvoF1',
            'mid': '156798703528610303473757548878786007104',
            'clienttime': '1584257267',
            'x-router': 'msearch.kugou.com',
            'User-Agent': 'Android9-AndroidPhone-10020-130-0-searchrecommendprotocol-wifi',
            'KG-RC': '1',
        }, timeout=15)
        out = []
        if isinstance(j, dict):
            for item in (j.get('data') or {}).get('list') or []:
                for k in (item.get('keywords') or []):
                    out.append(k.get('keyword'))
        return {'source': 'kg', 'list': out[:20]}
    if platform == 'tx':
        url = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
        body = json.dumps({
            'comm': {'ct': '19', 'cv': '1803', 'guid': '0', 'patch': '118',
                     'psrf_access_token_expiresAt': 0, 'psrf_qqaccess_token': '',
                     'psrf_qqopenid': '', 'psrf_qqunionid': '', 'tmeAppID': 'qqmusic',
                     'tmeLoginType': 0, 'uin': '0', 'wid': '0'},
            'hotkey': {'method': 'GetHotkeyForQQMusicPC', 'module': 'tencent_musicsoso_hotkey.HotkeyService',
                       'param': {'search_id': '', 'uin': 0}},
        })
        j = fetch_json(url, headers={'Content-Type': 'application/json', 'Referer': 'https://y.qq.com/'}, timeout=15)
        out = []
        if isinstance(j, dict):
            out = (j.get('hotkey') or {}).get('data') or {}
            out = out.get('hotkey') if isinstance(out, dict) else []
        return {'source': 'tx', 'list': [x.get('k') for x in out][:20] if isinstance(out, list) else []}
    if platform == 'wy':
        j = eapi_post('/api/search/chart/detail', {'id': 'HOT_SEARCH_SONG#@#'})
        out = []
        if isinstance(j, dict):
            for x in ((j.get('data') or {}).get('itemList') or []):
                out.append(x.get('searchWord'))
        return {'source': 'wy', 'list': out[:20]}
    return {'source': platform, 'list': []}


# ---------- 搜索建议 ----------
def search_tip(platform, keyword):
    if platform == 'tx':
        url = 'https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg?format=json&key=%s' % urllib.parse.quote(keyword)
        j = fetch_json(url, referer='https://y.qq.com/', timeout=15)
        songs = ((j.get('data') or {}).get('song') or {}).get('itemlist') if isinstance(j, dict) else None
        return {'source': 'tx', 'list': [x.get('name') for x in (songs or [])][:10]}
    if platform == 'kg':
        url = 'https://suggestions.kugou.com/get?keyword=%s' % urllib.parse.quote(keyword)
        j = fetch_json(url, timeout=15)
        info = ((j.get('data') or {}).get('info')) if isinstance(j, dict) else None
        return {'source': 'kg', 'list': [x.get('SongName') for x in (info or [])][:10]}
    if platform == 'wy':
        url = 'https://music.163.com/api/search/suggest/web?s=%s' % urllib.parse.quote(keyword)
        j = fetch_json(url, referer='https://music.163.com/', timeout=15)
        songs = ((j.get('result') or {}).get('songs')) if isinstance(j, dict) else None
        return {'source': 'wy', 'list': [x.get('name') for x in (songs or [])][:10]}
    return {'source': platform, 'list': []}


# ---------- 歌词(全平台) ----------
def _krc_to_lrc(krc_bytes):
    """酷狗 KRC -> LRC: krc1头 + XOR 0x40 + zlib 解压"""
    import zlib
    if krc_bytes[:4] != b'krc1':
        return None
    data = bytes([b ^ 0x40 for b in krc_bytes[4:]])
    try:
        text = zlib.decompress(data).decode('utf-8', 'replace')
    except Exception:
        return None
    lines = []
    for line in text.split('\n'):
        line = line.strip()
        if not line.startswith('[') or ']' not in line:
            continue
        head, rest = line[1:].split(']', 1)
        parts = head.split(',')
        if len(parts) != 2:
            continue
        try:
            ms = int(parts[0]) + int(parts[1])
        except ValueError:
            continue
        m, s = divmod(ms / 1000, 60)
        lines.append('[%02d:%05.2f]%s' % (int(m), s, rest))
    return '\n'.join(lines)


def get_platform_lyric(platform, song):
    if platform == 'wy':
        return get_wy_lyric(str(song.get('id') or ''))
    if platform == 'tx':
        mid = song.get('songmid') or song.get('id')
        if not mid:
            return {'lyric': '', 'tlyric': ''}
        url = ('https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid=%s&format=json&nobase64=0&g_tk=5381'
               % urllib.parse.quote(str(mid)))
        j = fetch_json(url, referer='https://y.qq.com/', timeout=15)
        if isinstance(j, dict) and j.get('retcode') == 0:
            import base64
            lyric = base64.b64decode(j.get('lyric') or '').decode('utf-8', 'replace')
            tlyric = base64.b64decode(j.get('trans') or '').decode('utf-8', 'replace') if j.get('trans') else ''
            return {'lyric': lyric, 'tlyric': tlyric}
        return {'lyric': '', 'tlyric': ''}
    if platform == 'kg':
        h = song.get('hash') or song.get('id')
        if not h:
            return {'lyric': '', 'tlyric': ''}
        url = ('http://m.kugou.com/app/i/krc.php?cmd=100&keyword=%s&hash=%s&timelength=%d&d=0.38664927426725626'
               % (urllib.parse.quote(str(song.get('name') or '')), urllib.parse.quote(str(h)),
                  int(song.get('duration') or 0) // 1000))
        try:
            req = urllib.request.Request(url, headers={
                'KG-RC': '1', 'KG-THash': 'expand_search_manager.cpp:852736169:451',
                'User-Agent': 'KuGou2012-9020-ExpandSearchManager',
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            lrc = _krc_to_lrc(data) if data else None
            return {'lyric': lrc or '', 'tlyric': ''}
        except Exception:
            return {'lyric': '', 'tlyric': ''}
    if platform == 'kw':
        rid = song.get('rid') or song.get('id')
        if not rid:
            return {'lyric': '', 'tlyric': ''}
        url = 'https://m.kuwo.cn/newh5app/api/mobile/v1/lyric?musicId=%s' % urllib.parse.quote(str(rid))
        j = fetch_json(url, referer='https://www.kuwo.cn/', timeout=15)
        lrclist = ((j.get('data') or {}).get('lrclist')) if isinstance(j, dict) else None
        if isinstance(lrclist, list):
            return {'lyric': '\n'.join('[%s]%s' % (x.get('time'), x.get('line')) for x in lrclist if x.get('line')), 'tlyric': ''}
        return {'lyric': '', 'tlyric': ''}
    if platform == 'mg':
        cid = song.get('copyrightId') or song.get('contentId') or song.get('id')
        if not cid:
            return {'lyric': '', 'tlyric': ''}
        url = 'https://music.migu.cn/v3/api/music/audioPlayer/getLyric?copyrightId=%s' % urllib.parse.quote(str(cid))
        j = fetch_json(url, headers={'User-Agent': UA, 'Referer': 'https://music.migu.cn/'}, timeout=15)
        if isinstance(j, dict):
            return {'lyric': j.get('lyric') or '', 'tlyric': ''}
        return {'lyric': '', 'tlyric': ''}
    return {'lyric': '', 'tlyric': ''}


# ---------- FLAC ----------
def parse_flac_info(data):
    if len(data) < 42:
        return None
    if data[0:4] != b'fLaC':
        return None
    block_len = (data[5] << 16) | (data[6] << 8) | data[7]
    si = data[8:8 + block_len]
    if len(si) < 18:
        return None
    sr = (si[10] << 12) | (si[11] << 4) | (si[12] >> 4)
    ch = ((si[12] >> 1) & 0x07) + 1
    bits = (((si[12] & 0x01) << 4) | (si[13] >> 4)) + 1
    return {'sr': sr, 'ch': ch, 'bits': bits}


def spec_from_flac(data, level=''):
    info = parse_flac_info(data)
    if not info:
        return '44.1kHz 16bit'
    feat = ''
    if info['ch'] == 6:
        feat = ' 5.1环绕'
    elif info['ch'] == 8:
        feat = ' 7.1环绕'
    elif info['ch'] > 2:
        feat = ' 环绕'
    elif 'sky' in str(level):
        feat = ' 5.1环绕'
    elif info['sr'] >= 176400:
        feat = ' 臻品母带'
    elif info['sr'] >= 96000:
        feat = ' Hi-Res'
    return '%gkHz %dbit%s' % (info['sr'] / 1000, info['bits'], feat)


def safe_name(s):
    s = str(s or '')
    s = s.replace('/', '&').replace('\\', '&')
    for ch, rep in [('?', '？'), ('#', '＃'), ('*', '＊'), ('%', '％'), (':', '：'), ('"', '＂')]:
        s = s.replace(ch, rep)
    return s


# ---------- 元数据 ----------
def get_wy_lyric(song_id):
    url = 'https://music.163.com/api/song/lyric?id=%s&lv=1&kv=1&tv=-1' % urllib.parse.quote(song_id)
    j = fetch_json(url, referer='https://music.163.com/')
    if isinstance(j, dict):
        return {
            'lyric': (j.get('lrc') or {}).get('lyric') or '',
            'tlyric': (j.get('tlyric') or {}).get('lyric') or '',
        }
    return {'lyric': '', 'tlyric': ''}


def download_bytes(url, timeout=30, referer='https://music.163.com/'):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': referer})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read()
    except Exception:
        return None


def download_head(url, n=16384, timeout=20):
    """只下载文件头部若干字节(用 Range), 返回 (数据, 文件总大小); 大小未知返回 None"""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://music.163.com/', 'Range': 'bytes=0-%d' % (n - 1)})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(n + 64)
            total = None
            cr = r.headers.get('Content-Range')
            if cr and '/' in cr:
                total = cr.rsplit('/', 1)[1]
                try:
                    total = int(total)
                except ValueError:
                    total = None
            if total is None:
                cl = r.headers.get('Content-Length')
                if cl:
                    try:
                        total = int(cl)
                    except ValueError:
                        total = None
            return data, total
    except Exception:
        return None, None


def upgrade_pic(pic_url):
    if not pic_url:
        return ''
    if '?param=' in pic_url:
        return re.sub(r'param=\d+y\d+', 'param=500y500', pic_url)
    return re.sub(r'/\d{3}y/\d{3}x/\d{3}w_\d{3}h/\d{3}c', '/500y/500x/500w_500h/500c', pic_url)


def measure_mp3(data):
    """从 MP3 帧头实测码率/采样率"""
    pos = 0
    if data[:3] == b'ID3':
        if len(data) < 10:
            return None
        size = ((data[6] & 0x7f) << 21) | ((data[7] & 0x7f) << 14) | ((data[8] & 0x7f) << 7) | (data[9] & 0x7f)
        pos = 10 + size
    if pos + 4 > len(data):
        return None
    b0, b1 = data[pos], data[pos + 1]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    ver = (b1 >> 3) & 0x03
    layer = (b1 >> 1) & 0x03
    br_idx = (data[pos + 2] >> 4) & 0x0F
    sr_idx = (data[pos + 2] >> 2) & 0x03
    if ver == 3 and layer == 1:
        table = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
        srs = [44100, 48000, 32000]
    elif ver == 2 and layer == 1:
        table = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
        srs = [22050, 24000, 16000]
    else:
        return None
    if br_idx >= 15 or sr_idx >= 3:
        return None
    kbps = table[br_idx]
    if kbps:
        return '%dkHz %dkbps' % (srs[sr_idx] / 1000, kbps)
    return None


LEVEL_SPEC = {
    'lossless': '44.1kHz 16bit', 'hires': '96kHz 24bit Hi-Res',
    'jyeffect': '96kHz 24bit Hi-Res', 'sky': '44.1kHz 16bit 5.1环绕',
    'jymaster': '192kHz 24bit 臻品母带', 'flac': '44.1kHz 16bit',
    '128k': '128kbps', '192k': '192kbps', '320k': '320kbps', 'flac24bit': '96kHz 24bit',
}


def measure_spec(data, lv=''):
    """直接测量文件头; 非 FLAC 时回退 LEVEL_SPEC 期望值(忽略 MP3 实测)"""
    fallback = LEVEL_SPEC.get(lv, '44.1kHz 16bit')
    info = parse_flac_info(data) if data else None
    if not info:
        return fallback
    sr, ch, bits = info['sr'], info['ch'], info['bits']
    if ch == 6:
        feat = ' 5.1环绕'
    elif ch == 8:
        feat = ' 7.1环绕'
    elif ch > 2:
        feat = ' 环绕'
    elif lv == 'sky':
        feat = ' 5.1环绕'
    elif sr >= 176400:
        feat = ' 臻品母带'
    elif sr >= 96000:
        feat = ' Hi-Res'
    else:
        feat = ''
    return '%gkHz %dbit%s' % (sr / 1000, bits, feat)


# ---------- WebDAV ----------
_dav_th_lock = threading.Lock()
_dav_th_last = [0.0]


def _dav_throttle(interval=0.3):
    """WebDAV 请求全局限速(约 3 请求/秒), 避免高并发触发 OpenList 429"""
    import time as _tm
    with _dav_th_lock:
        now = _tm.time()
        wait = interval - (now - _dav_th_last[0])
        if wait > 0:
            _tm.sleep(wait)
        _dav_th_last[0] = _tm.time()


def _dav_req(cfg, method, path, headers=None, body=None, timeout=60):
    _dav_throttle()
    url = cfg['url'].rstrip('/') + '/' + path
    h = {'Authorization': 'Basic ' + base64.b64encode(('%s:%s' % (cfg.get('user', ''), cfg.get('pass', ''))).encode()).decode()}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, method=method, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode('utf-8', 'replace') if e.fp else '')
    except urllib.error.URLError as e:
        raise RuntimeError('WebDAV 连接失败: %s' % e.reason)


def _unescape_xml(s):
    return s.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'").replace('&amp;', '&')


def dav_propfind(cfg, path, depth='1'):
    status, xml = _dav_req(cfg, 'PROPFIND', path,
                           {'Depth': depth, 'Content-Type': 'application/xml'},
                           PROPFIND_BODY.encode('utf-8'))
    if status == 404:
        return []  # 集合不存在(去重/创建目录场景视为空)
    if status not in (207, 200):
        raise RuntimeError('WebDAV PROPFIND 返回 HTTP %s(检查地址/账号密码)' % status)
    items = []
    blocks = re.split(r'<(?:[\w-]+:)?response\b[^>]*>', xml, flags=re.I)[1:]
    for blk in blocks:
        m = re.search(r'<(?:[\w-]+:)?href\b[^>]*>([^<]+)<', blk, re.I)
        if not m:
            continue
        dn = re.search(r'<(?:[\w-]+:)?displayname\b[^>]*>([^<]*)<', blk, re.I)
        sz = re.search(r'<(?:[\w-]+:)?getcontentlength\b[^>]*>([^<]+)<', blk, re.I)
        mt = re.search(r'<(?:[\w-]+:)?getlastmodified\b[^>]*>([^<]+)<', blk, re.I)
        mtime = 0
        if mt:
            try:
                mtime = email.utils.parsedate_to_datetime(mt.group(1).strip()).timestamp()
            except Exception:
                mtime = 0
        is_dir = bool(re.search(r'<(?:[\w-]+:)?collection\b', blk, re.I))
        items.append({
            'href': _unescape_xml(m.group(1)),
            'displayname': _unescape_xml(dn.group(1)) if dn else '',
            'isDir': is_dir,
            'size': int(sz.group(1)) if sz else 0,
            'mtime': mtime,
        })
    return items


def dav_mkcol(cfg, path):
    status, _ = _dav_req(cfg, 'MKCOL', path)
    return status


def dav_put(cfg, path, data, ctype='application/octet-stream'):
    status, _ = _dav_req(cfg, 'PUT', path, {'Content-Type': ctype}, data)
    return status


def dav_delete(cfg, path):
    status, _ = _dav_req(cfg, 'DELETE', path)
    return status
