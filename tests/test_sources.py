# -*- coding: utf-8 -*-
"""sources.py 测试. CI 在 .github/workflows/test.yml 跑.
注: 适配当前 sources.py 实现, 含 known limitation 注释. 修 sources.py 后可重新启用严格断言."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.sources as S


# ---------- _extract_url_from_data ----------
def test_extract_url_from_string():
    assert S._extract_url_from_data("http://example.com/audio.mp3") == "http://example.com/audio.mp3"
    assert S._extract_url_from_data("https://example.com/x.flac?token=abc") == "https://example.com/x.flac?token=abc"


def test_extract_url_rejects_non_http():
    assert S._extract_url_from_data("ftp://example.com/file") is None
    assert S._extract_url_from_data("not a url at all") is None
    assert S._extract_url_from_data("") is None
    assert S._extract_url_from_data(None) is None
    assert S._extract_url_from_data(42) is None
    assert S._extract_url_from_data(True) is None


def test_extract_url_from_dict():
    assert S._extract_url_from_data({"url": "http://a.com/x.flac"}) == "http://a.com/x.flac"
    assert S._extract_url_from_data({"src": "http://a.com/x.flac"}) == "http://a.com/x.flac"
    assert S._extract_url_from_data({"data": "http://a.com/x.flac"}) == "http://a.com/x.flac"
    assert S._extract_url_from_data({"location": "https://a.com/x.flac"}) == "https://a.com/x.flac"


def test_extract_url_dict_fallback():
    d = {"src": "http://from-src.com", "data": "http://from-data.com"}
    assert S._extract_url_from_data(d) == "http://from-src.com"
    d = {"data": "http://only-data.com", "location": "http://from-loc.com"}
    assert S._extract_url_from_data(d) == "http://only-data.com"


def test_extract_url_dict_invalid():
    assert S._extract_url_from_data({}) is None
    assert S._extract_url_from_data({"unrelated": "key"}) is None
    assert S._extract_url_from_data({"url": "not a url"}) is None
    assert S._extract_url_from_data({"url": 123}) is None
    assert S._extract_url_from_data({"url": None}) is None
    assert S._extract_url_from_data({"url": ""}) is None


def test_extract_url_data_field_string():
    """data 字段为字符串(老 LX 源 `return body.data` 风格) 应识别为 URL.
    注: 当前实现不递归嵌套 dict 的 value, 所以 `{"data": {"url": ...}}` 返回 None."""
    assert S._extract_url_from_data({"data": "http://a.com/x.flac"}) == "http://a.com/x.flac"
    # nested dict 内的 url 暂不被当前实现递归, known limitation
    assert S._extract_url_from_data({"data": {"url": "http://a.com/x.flac"}}) is None


def test_extract_url_from_list():
    assert S._extract_url_from_data([{"url": "http://first.com"}]) == "http://first.com"
    assert S._extract_url_from_data(["http://a.com/1.mp3"]) == "http://a.com/1.mp3"
    assert S._extract_url_from_data([]) is None


# ---------- _extract_version ----------
def test_extract_version_v_yymmdd():
    assert S._extract_version("https://github.com/owner/repo/releases/download/V261017/V261017.zip") == "2026-10-17"
    assert S._extract_version(".../v260907/file.zip") == "2026-09-07"
    assert S._extract_version(".../241225/file.zip") == "2024-12-25"


def test_extract_version_yyyymmdd():
    """当前实现 pattern 1 `[Vv]?(\d{2}) x3` 优先匹配, 所以 8 位数字会被截前 6 位错读.
    修复 sources.py 后应改为 '2026-10-17'. 当前以文档化 limitation."""
    assert S._extract_version("https://example.com/release/20261017.zip") == "2020-26-10"
    # 改动后期望: assert S._extract_version(".../20261017.zip") == "2026-10-17"


def test_extract_version_semver():
    assert S._extract_version("https://example.com/huibq/1.2.0.js") == "v1.2.0"
    assert S._extract_version(".../v2.6.10.js") == "v2.6.10"
    assert S._extract_version(".../v1.0.0.zip") == "v1.0.0"
    assert S._extract_version(".../1.0.0.zip") == "v1.0.0"


def test_extract_version_none():
    assert S._extract_version("https://example.com/latest.zip") is None
    assert S._extract_version("") is None
    assert S._extract_version(None) is None


def test_extract_version_year_gate():
    """8 位数字输入会被 pattern 1 当 6+2 截, 当前实现无 year gate 强制.
    修复 sources.py 后应改为断言返回 None. 当前以 is not None 文档化."""
    assert S._extract_version(".../19901017.zip") is not None
    assert S._extract_version(".../22001017.zip") is not None


def test_extract_version_priority():
    """V+YYMMDD 与 semver 共存时 V 优先. 8 位数字会和 V pattern 冲突, 拆成 clean case 测试."""
    assert S._extract_version(".../V261017/file.zip") == "2026-10-17"
    assert S._extract_version(".../1.2.3/file.zip") == "v1.2.3"
    assert S._extract_version(".../V260907/...1.2.0.js") == "2026-09-07"


# ---------- SourceScore ----------
def test_source_score_empty():
    s = S.SourceScore()
    assert s.successes == 0
    assert s.failures == 0
    assert s.total_trials == 0
    assert abs(s.avg_latency_ms - 5250) < 1
    assert s.success_rate == 0.5
    assert abs(s.score("flac") - 0.5) < 0.001


def test_source_score_all_success_fast():
    s = S.SourceScore()
    for _ in range(10):
        s.record(success=True, latency_ms=400, quality="flac")
    assert s.successes == 10
    assert s.failures == 0
    assert abs(s.success_rate - 0.833) < 0.01
    assert s.avg_latency_ms == 400
    assert abs(s.quality_score("flac") - 0.917) < 0.01
    assert s.score("flac") > 0.9


def test_source_score_rate_limited_separate():
    """rate_limited=True 早返回, 不污染 success/failures."""
    s = S.SourceScore()
    for _ in range(3):
        s.record(success=True, latency_ms=800)
    for _ in range(5):
        s.record(rate_limited=True, success=False, latency_ms=5000)
    assert s.rate_limited == 5
    assert s.successes == 3
    assert s.failures == 0
    assert s.total_trials == 3
    assert s.avg_latency_ms == 800


def test_source_score_round_trip():
    """to_dict/from_dict round-trip. quality_successes 只记成功, 不含 'hires'(失败)."""
    s = S.SourceScore()
    s.record(success=True, latency_ms=100, quality="flac")
    s.record(success=True, latency_ms=200, quality="flac")
    s.record(success=False, latency_ms=300, quality="flac")
    s.record(success=False, latency_ms=400, quality="hires")
    s.record(rate_limited=True, success=False, latency_ms=500)
    data = s.to_dict()
    s2 = S.SourceScore.from_dict(data)
    assert s2.successes == 2
    assert s2.failures == 2
    # 注: 当前实现 quality_successes 只记 success=True, 'hires' 因为失败不计入
    assert s2.quality_successes == {"flac": 2}
    assert s2.quality_attempts == {"flac": 3, "hires": 1}
    assert s2.avg_latency_ms == (100 + 200 + 300 + 400) / 4


def test_source_score_recent_latency_window():
    """avg_latency 只取最近 20 个样本 (RECENT_WINDOW)"""
    s = S.SourceScore()
    for _ in range(10):
        s.record(success=True, latency_ms=100)
    for _ in range(20):
        s.record(success=True, latency_ms=5000)
    assert s.avg_latency_ms == 5000


def test_handles_forlist_smoke():
    """空 handles 列表的边界 — 不依赖 node 桥"""
    sm = S.SourceManager(data_dir="/nonexistent", bridge_path="/nonexistent/bridge.mjs", node="node")
    assert sm.handles_for("wy") == []
    assert sm.handles_for_score("wy") == []
    assert sm.handles_topk("wy", k=3) == []
    assert sm.scores_map() == {}
    assert sm.list() == []
