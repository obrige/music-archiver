# -*- coding: utf-8 -*-
# 冒烟测试: python test/smoke.py (需已安装 requirements)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import search_kg, search_wy  # noqa: E402


def main():
    print('== 1. 网易云搜索: 晴天 ==')
    r = search_wy('晴天 周杰伦', 5)
    print('结果:', len(r['list']), r['list'][0]['name'], r['list'][0]['singer'])
    print()
    print('== 2. 酷狗搜索 ==')
    kg = search_kg('晴天', 5)
    print('结果:', len(kg['list']), kg['list'][0]['name'])
    print()
    print('== 3. 音源沙箱(需已导入音源) ==')
    from app.main import sm
    sm.init()
    hs = sm.list()
    print('已导入音源:', len(hs), [h['name'] for h in hs])
    if hs:
        h = sm.get(hs[0]['id'])
        try:
            res = h.resolve(hs[0]['sources'].keys()[0], r['list'][0], 'flac')
            print('URL:', (res.get('url') or '')[:60])
        except Exception as e:
            print('解析失败(预期):', e)
    print()
    print('冒烟测试完成')


if __name__ == '__main__':
    main()
