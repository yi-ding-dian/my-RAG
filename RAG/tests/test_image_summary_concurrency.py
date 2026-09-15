"""图片摘要并发改造：并发生效 / 回填保序 / 进度单调 / 单图失败隔离

改造前是串行（见 record 2026-09-14 压测），这里锁住"并发不改变正确性"：
并发只该影响**耗时**，产物与串行必须一致。

注：项目未装 pytest-asyncio，异步逻辑用 asyncio.run 在同步测试里跑
（同一测试内的多次调用放在同一个 async 函数里，共用同一个 loop）。
"""
import asyncio
import time

import pytest

from backend.config import ImageSummaryConfig, VisionModelConfig
from backend.services import image_summary as img_summ

N_IMGS = 12
ONE_DELAY = 0.12  # mock 单张耗时：串行 1.44s vs 并发 4 约 0.36s，差距足够明显


def _model_cfg():
    return VisionModelConfig(name='t', base_url='http://x/v1', api_key='k',
                             model='m', timeout=10)


def _summary_cfg():
    return ImageSummaryConfig(output_format='brief', prompt='描述这张图')


def _markdown():
    return '\n\n'.join(f'![](images/img{i}.jpg)' for i in range(N_IMGS))


def _images():
    return [{'name': f'img{i}.jpg', 'data': b'\xff' * 64} for i in range(N_IMGS)]


@pytest.fixture(autouse=True)
def _reset_pool():
    """每个测试用干净的并发池（池是模块级全局，跨测试 loop 串用会报错）"""
    img_summ._sem = None
    img_summ._sem_limit = 0
    yield
    img_summ._sem = None
    img_summ._sem_limit = 0


@pytest.fixture(autouse=True)
def _fake_pixel_area(monkeypatch):
    """假图字节解不出尺寸会被判"小图跳过"，这里统一当成大图"""
    monkeypatch.setattr(img_summ, '_pixel_area', lambda data: 500 * 500)


def _mock_describe(monkeypatch, calls, fail_on=()):
    async def fake(client, model_cfg, prompt, data, name):
        await asyncio.sleep(ONE_DELAY)
        calls.append(name)
        if name in fail_on:
            raise RuntimeError(f'模拟失败 {name}')
        return f'摘要-{name}'
    monkeypatch.setattr(img_summ, '_describe_one', fake)


def test_serial_and_concurrent_produce_identical_output(monkeypatch):
    """并发 4 与串行的产物必须逐字节一致（并发只该改耗时）"""
    calls = []
    _mock_describe(monkeypatch, calls)

    async def _run():
        serial = await img_summ.summarize_images(
            _markdown(), _images(), model_cfg=_model_cfg(),
            summary_cfg=_summary_cfg(), client=object(), concurrency=1)
        conc = await img_summ.summarize_images(
            _markdown(), _images(), model_cfg=_model_cfg(),
            summary_cfg=_summary_cfg(), client=object(), concurrency=4)
        return serial, conc

    (s_md, s_stats), (c_md, c_stats) = asyncio.run(_run())

    assert c_md == s_md, '并发改变了产物内容'
    assert c_stats['done'] == s_stats['done'] == N_IMGS
    for i in range(N_IMGS):  # 回填保序：每张图后面跟的是它自己的摘要
        assert f'![](images/img{i}.jpg)\n> 图片说明：摘要-img{i}.jpg' in c_md


def test_concurrency_actually_speeds_up(monkeypatch):
    """并发 4 的耗时应显著低于串行（验证真的并发，而不是换个写法继续串行）"""
    _mock_describe(monkeypatch, [])

    async def _run():
        t0 = time.monotonic()
        await img_summ.summarize_images(
            _markdown(), _images(), model_cfg=_model_cfg(),
            summary_cfg=_summary_cfg(), client=object(), concurrency=1)
        serial = time.monotonic() - t0
        t0 = time.monotonic()
        await img_summ.summarize_images(
            _markdown(), _images(), model_cfg=_model_cfg(),
            summary_cfg=_summary_cfg(), client=object(), concurrency=4)
        return serial, time.monotonic() - t0

    serial, conc = asyncio.run(_run())
    assert conc < serial / 2, f'并发没生效：串行 {serial:.2f}s vs 并发 {conc:.2f}s'


def test_progress_counter_is_monotonic(monkeypatch):
    """进度按"已完成数"上报：单调递增、终值 = 待处理张数（并发下不乱序）"""
    _mock_describe(monkeypatch, [])
    seen = []

    async def _run():
        await img_summ.summarize_images(
            _markdown(), _images(), model_cfg=_model_cfg(),
            summary_cfg=_summary_cfg(), client=object(),
            on_progress=lambda d, t: seen.append((d, t)), concurrency=4)

    asyncio.run(_run())
    assert [d for d, _ in seen] == list(range(1, N_IMGS + 1)), f'进度非单调: {seen}'
    assert all(t == N_IMGS for _, t in seen), f'分母不是待处理数: {seen}'


def test_single_failure_does_not_affect_others(monkeypatch):
    """单图异常不冒泡、不影响同批其他图，只记进 failed"""
    _mock_describe(monkeypatch, [], fail_on={'img3.jpg', 'img7.jpg'})

    async def _run():
        return await img_summ.summarize_images(
            _markdown(), _images(), model_cfg=_model_cfg(),
            summary_cfg=_summary_cfg(), client=object(), concurrency=4)

    md, stats = asyncio.run(_run())
    assert stats['done'] == N_IMGS - 2
    assert sorted(stats['failed']) == ['img3.jpg', 'img7.jpg']
    assert '![](images/img3.jpg)' in md           # 引用还在
    assert '![](images/img3.jpg)\n>' not in md    # 只是没摘要
    assert '摘要-img5.jpg' in md                  # 同批其他图不受影响


def test_stats_pending_excludes_skipped(monkeypatch):
    """stats.pending = 实际送模型的张数（进度分母用它，与 total 差在被跳过的图）

    收尾文案若用 total（引用总数），会出现"进度走 n/6、结束显示 12"的错位。
    """
    _mock_describe(monkeypatch, [])
    monkeypatch.setattr(img_summ, '_pixel_area',
                        lambda data: 500 * 500 if data[:1] == b'\xff' else 10 * 10)

    md = '\n\n'.join(f'![](images/img{i}.jpg)' for i in range(12))
    imgs = [{'name': f'img{i}.jpg', 'data': b'\xff' * 64} for i in range(6)]
    imgs += [{'name': f'img{i}.jpg', 'data': b'\x00' * 64} for i in range(6, 12)]

    async def _run():
        return await img_summ.summarize_images(
            md, imgs, model_cfg=_model_cfg(), summary_cfg=_summary_cfg(),
            client=object(), concurrency=4)

    _, stats = asyncio.run(_run())
    assert stats['total'] == 12      # 引用总数
    assert stats['pending'] == 6     # 实际送模型数（6 张小的被跳过）
    assert stats['skipped_small'] == 6
    assert stats['done'] == 6


def test_stats_pending_present_when_no_refs():
    """无图片引用时提前返回，pending 也必须存在（上层 .get 取值不能 KeyError）"""
    async def _run():
        return await img_summ.summarize_images(
            '没有图片引用的正文', [], model_cfg=_model_cfg(),
            summary_cfg=_summary_cfg(), client=object())

    _, stats = asyncio.run(_run())
    assert stats['total'] == 0
    assert stats['pending'] == 0


def test_concurrency_1_is_serial(monkeypatch):
    """并发 1 = 改造前的串行行为（作为回退档位）"""
    order = []

    async def fake(client, model_cfg, prompt, data, name):
        order.append(name)
        await asyncio.sleep(0)
        return f'摘要-{name}'

    monkeypatch.setattr(img_summ, '_describe_one', fake)

    async def _run():
        await img_summ.summarize_images(
            _markdown(), _images(), model_cfg=_model_cfg(),
            summary_cfg=_summary_cfg(), client=object(), concurrency=1)

    asyncio.run(_run())
    assert order == [f'img{i}.jpg' for i in range(N_IMGS)], '并发 1 应严格按序执行'
