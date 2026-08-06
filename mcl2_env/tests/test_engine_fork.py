#!/usr/bin/env python3
"""EngineForkRenderer 单元测试：用临时文件模拟共享内存头 + 帧数据（无 C 侧）。

覆盖（docs/m1_protocol.md §2）：
  - 头解析 / magic 校验（错误 magic 返回 None）
  - 最新帧逻辑（write_idx-1 槽 + 环形回绕）
  - BGRA → RGB 转换
  - 无共享内存时优雅降级（get_frame() -> None）
  - get_frame() fps 降采样缓存

运行方式（任选其一）：
    python3 -m pytest mcl2_env/tests/
    python3 mcl2_env/tests/test_engine_fork.py
"""

from __future__ import annotations

import mmap
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

# ---- 包导入引导：mcl2_env 仅需 numpy（__init__ 已把 pydantic/gymnasium 设为可选）----
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.renderer.engine_fork import (  # noqa: E402
    EngineForkRenderer,
    _HEADER_FMT,
    _HEADER_SIZE,
    _MAGIC,
)


# ---------------------------------------------------------------- helpers

def make_shm_bytes(width: int, height: int, depth: int, write_idx: int,
                   magic: int = _MAGIC, fill=None) -> bytes:
    """构造 C 侧共享内存镜像：header + depth 个 BGRA 帧槽。"""
    stride = width * 4
    frame_size = stride * height
    buf = bytearray(_HEADER_SIZE + frame_size * depth)
    if fill is None:
        # 每帧 R 分量不同（f*33），便于区分槽位
        fill = lambda f, y, x: (0, 0, (f * 33) & 0xFF, 255)  # BGRA
    for f in range(depth):
        base = _HEADER_SIZE + f * frame_size
        for y in range(height):
            for x in range(width):
                b, g, r, a = fill(f, y, x)
                idx = base + y * stride + x * 4
                buf[idx:idx + 4] = bytes((b, g, r, a))
    buf[:_HEADER_SIZE] = struct.pack(
        _HEADER_FMT, magic, width, height, stride, depth, write_idx, 0, 0
    )
    return bytes(buf)


def make_renderer_with_shm(data: bytes, fps: int = 5) -> tuple[EngineForkRenderer, int, str]:
    """把模拟 shm 数据写入临时文件并 mmap 注入 renderer；返回 (r, fd, path)。"""
    fd, path = tempfile.mkstemp(prefix="mcl2_frames_test_")
    os.write(fd, data)
    r = EngineForkRenderer(fps=fps)
    r._running = True
    r._mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
    return r, fd, path


def rewrite_fd(fd: int, data: bytes) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)


def _cleanup(r: EngineForkRenderer, fd: int, path: str) -> None:
    """停渲染器、关 fd、删临时文件。"""
    r.stop()
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------- tests

def test_header_size_is_44() -> None:
    assert _HEADER_SIZE == 44, f"header must be 44 bytes, got {_HEADER_SIZE}"
    assert struct.calcsize("<IIIIIQQq") == 44


def test_parse_header_ok() -> None:
    data = make_shm_bytes(width=8, height=6, depth=4, write_idx=2)
    r, fd, path = make_renderer_with_shm(data)
    try:
        h = r._parse_header(r._mm[:_HEADER_SIZE])
        assert h is not None
        assert h["magic"] == _MAGIC
        assert h["width"] == 8 and h["height"] == 6
        assert h["stride"] == 32 and h["depth"] == 4
        assert h["write_idx"] == 2 and h["read_idx"] == 0
        assert h["server_tick"] == 0
    finally:
        _cleanup(r, fd, path)


def test_bad_magic_returns_none() -> None:
    data = make_shm_bytes(width=8, height=6, depth=4, write_idx=2, magic=0xDEADBEEF)
    r, fd, path = make_renderer_with_shm(data)
    try:
        assert r._parse_header(r._mm[:_HEADER_SIZE]) is None
        assert r._read_frame_raw() is None, "错误 magic 应返回 None"
    finally:
        _cleanup(r, fd, path)


def test_invalid_dims_returns_none() -> None:
    # width=0 -> 头校验失败
    data = bytearray(make_shm_bytes(width=4, height=4, depth=2, write_idx=1))
    data[: _HEADER_SIZE] = struct.pack(
        _HEADER_FMT, _MAGIC, 0, 4, 16, 2, 1, 0, 0
    )
    r, fd, path = make_renderer_with_shm(bytes(data))
    try:
        assert r._read_frame_raw() is None
    finally:
        _cleanup(r, fd, path)


def test_no_frames_yet_returns_none() -> None:
    data = make_shm_bytes(width=4, height=4, depth=4, write_idx=0)
    r, fd, path = make_renderer_with_shm(data)
    try:
        assert r._read_frame_raw() is None, "write_idx=0 尚无帧"
    finally:
        _cleanup(r, fd, path)


def test_latest_frame_slot_and_index() -> None:
    """write_idx=2 -> 最新帧 frame_index=1, slot=1。"""
    data = make_shm_bytes(width=8, height=6, depth=4, write_idx=2)
    r, fd, path = make_renderer_with_shm(data)
    try:
        raw = r._read_frame_raw()
        assert raw is not None
        assert raw["frame_index"] == 1
        assert raw["header"]["write_idx"] == 2
        # slot 1 的数据：R 分量 = 1*33 = 33
        bgra = np.frombuffer(raw["data"], dtype=np.uint8).reshape(6, 32)
        assert bgra[0, 2] == 33, f"expected frame1 R=33, got {bgra[0, 2]}"
    finally:
        _cleanup(r, fd, path)


def test_latest_frame_wraps_ring() -> None:
    """write_idx = depth+1 -> frame_index=depth, slot=0（回绕）。"""
    depth = 4
    data = make_shm_bytes(width=8, height=6, depth=depth, write_idx=depth + 1)
    r, fd, path = make_renderer_with_shm(data)
    try:
        raw = r._read_frame_raw()
        assert raw is not None
        assert raw["frame_index"] == depth
        # slot 0 的数据：R = 0
        bgra = np.frombuffer(raw["data"], dtype=np.uint8).reshape(6, 32)
        assert bgra[0, 2] == 0
    finally:
        _cleanup(r, fd, path)


def test_rgb_conversion() -> None:
    """BGRA -> RGB：纯红像素应转为 [255, 0, 0]。"""
    def fill(f, y, x):
        # pixel0 纯红 (B=0,G=0,R=255)，pixel1 纯蓝 (B=255,G=0,R=0)
        if x == 0:
            return (0, 0, 255, 255)
        return (255, 0, 0, 255)

    data = make_shm_bytes(width=2, height=1, depth=2, write_idx=1, fill=fill)
    r, fd, path = make_renderer_with_shm(data)
    try:
        frame = r._read_latest_frame()
        assert frame is not None
        img = frame.image
        assert img.shape == (1, 2, 3), f"shape={img.shape}"
        assert img.dtype == np.uint8
        np.testing.assert_array_equal(img[0, 0], [255, 0, 0])  # 红
        np.testing.assert_array_equal(img[0, 1], [0, 0, 255])  # 蓝
        assert frame.server_tick == 0
        assert frame.wall_time > 0
        assert frame.width == 2 and frame.height == 1
    finally:
        _cleanup(r, fd, path)


def test_get_frame_fps_downsample() -> None:
    """interval 内复用缓存帧；超过 interval 后重新读取最新帧。"""
    r, fd, path = make_renderer_with_shm(
        make_shm_bytes(width=4, height=4, depth=8, write_idx=1), fps=5
    )
    try:
        f1 = r.get_frame()
        assert f1 is not None

        # 底层写入新帧（write_idx=2 -> slot 1），但仍在 interval 内 -> 缓存
        rewrite_fd(fd, make_shm_bytes(width=4, height=4, depth=8, write_idx=2))
        f2 = r.get_frame()
        assert f2 is f1, "interval 内应返回缓存帧"

        time.sleep(0.25)  # fps=5 -> interval=0.2s
        f3 = r.get_frame()
        assert f3 is not None and f3 is not f1, "超过 interval 应读取新帧"
    finally:
        _cleanup(r, fd, path)


def test_degraded_without_shm() -> None:
    """无共享内存文件时优雅降级：start 不抛错，available=False，get_frame() None。"""
    with tempfile.TemporaryDirectory() as td:
        r = EngineForkRenderer(shm_path=str(Path(td) / "missing_frames"))
        r.start()
        assert r.available is False
        assert r.get_frame() is None
        r.stop()
        assert r.available is False


def test_race_retry_reads_stable_frame() -> None:
    """读到半帧（复查 write_idx 变化）→ 重试后拿到稳定帧。"""
    data = make_shm_bytes(width=4, height=4, depth=8, write_idx=5)
    r, fd, path = make_renderer_with_shm(data)
    try:
        state = {"calls": 0}
        real = r._read_frame_raw

        def flaky():
            state["calls"] += 1
            raw = real()
            if state["calls"] == 1 and raw is not None:
                # 模拟半帧：复查时 write_idx 不一致（写者刚推进）
                raw = dict(raw)
                h = dict(raw["header"])
                h["write_idx"] = h["write_idx"] + 1
                raw["header"] = h
            return raw

        r._read_frame_raw = flaky
        frame = r._read_latest_frame()
        assert frame is not None
        assert frame.width == 4 and frame.height == 4
        assert state["calls"] >= 2, "半帧应触发至少一次重试"
    finally:
        _cleanup(r, fd, path)


def test_race_retry_exhausted_returns_none() -> None:
    """持续半帧（每次复查 write_idx 都变化）→ 重试耗尽返回 None。"""
    data = make_shm_bytes(width=4, height=4, depth=8, write_idx=3)
    r, fd, path = make_renderer_with_shm(data)
    try:
        real = r._read_frame_raw

        def always_flaky():
            raw = real()
            if raw is not None:
                raw = dict(raw)
                h = dict(raw["header"])
                h["write_idx"] = h["write_idx"] + 1
                raw["header"] = h
            return raw

        r._read_frame_raw = always_flaky
        assert r._read_latest_frame() is None, "重试耗尽应返回 None"
    finally:
        _cleanup(r, fd, path)


def test_rgb_compat_stride_width3() -> None:
    """兼容 stride == width*3（已是 RGB 布局，不做通道交换）。"""
    width, height, depth = 4, 2, 2
    stride = width * 3
    buf = bytearray(_HEADER_SIZE + stride * height * depth)
    for y in range(height):
        for x in range(width):
            idx = _HEADER_SIZE + y * stride + x * 3
            buf[idx:idx + 3] = bytes((x * 10, 128, 255))
    buf[:_HEADER_SIZE] = struct.pack(_HEADER_FMT, _MAGIC, width, height, stride, depth, 1, 0, 0)
    r, fd, path = make_renderer_with_shm(bytes(buf))
    try:
        img = r._read_latest_frame().image
        assert img.shape == (height, width, 3)
        np.testing.assert_array_equal(img[0, 0], [0, 128, 255])
    finally:
        _cleanup(r, fd, path)


# ---------------------------------------------------------------- runner

def _main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback

            print(f"  FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    print(f"{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    _main()
