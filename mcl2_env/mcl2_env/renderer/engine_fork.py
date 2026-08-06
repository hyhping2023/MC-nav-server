"""engine_fork 渲染器：从 fork 客户端共享内存环形缓冲读取帧。

契约：docs/m1_protocol.md §2（C 侧 Mcl2FrameHeader 紧凑布局 = 44 字节）。

共享内存布局：
    header 44B: magic(u32) width(u32) height(u32) stride(u32) depth(u32)
                write_idx(u64) read_idx(u64) server_tick(i64)
    data: depth 个帧槽，每槽 stride*height 字节（BGRA，stride = width*4）
    最新完整帧 = write_idx-1 槽（C 侧在帧写完后最后递增 write_idx）

命名约定（M1 决策）：C 侧用普通文件 open()+ftruncate()+mmap，Python 侧
open()+mmap —— 默认路径 /tmp/mcl2_frames（macOS 无 /dev/shm，shm_open 名在
Python 侧拿不到 fd）。兼容 POSIX shm 名（/mcl2_frames / env MCL2_FRAME_SHM），
需要 posix_ipc（未安装则跳过，不影响文件路径方案）。

无共享内存时优雅降级：start() 不抛错，available=False，get_frame() 返回 None。
"""

from __future__ import annotations

import logging
import mmap
import os
import struct
import time
from typing import Optional

import numpy as np

from .base import Frame, Renderer

log = logging.getLogger("mcl2_env.renderer.engine_fork")

_SHM_NAME_ENV = "MCL2_FRAME_SHM"     # POSIX shm 名（客户端 client.conf mcl2_frame_shm）
_SHM_FILE_ENV = "MCL2_FRAME_FILE"    # 普通文件路径覆盖
_DEFAULT_SHM_FILE = "/tmp/mcl2_frames"
_DEFAULT_SHM_NAME = "/mcl2_frames"

# 环形缓冲协议头（与 m1_protocol §2 Mcl2FrameHeader 对齐，紧凑布局 44 字节）
_HEADER_FMT = "<IIIIIQQq"  # magic,width,height,stride,depth,write_idx,read_idx,server_tick
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_MAGIC = 0x4D434C32
_MAX_STRIDE = 1 << 20  # 防御：单行 stride 上限 1 MiB
_MAX_RACE_RETRIES = 3  # 读到半帧（write_idx 在读取期间变化）时的重试次数


class EngineForkRenderer(Renderer):
    """共享内存环形缓冲消费者（第一人称 RGB 帧）。"""

    def __init__(
        self,
        width: int = 224,
        height: int = 224,
        fps: int = 5,
        shm_name: Optional[str] = None,
        frame_depth: int = 32,
        shm_path: Optional[str] = None,
    ):
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_depth = frame_depth
        self.shm_name = shm_name or os.environ.get(_SHM_NAME_ENV, _DEFAULT_SHM_NAME)
        self.shm_path = shm_path or os.environ.get(_SHM_FILE_ENV, _DEFAULT_SHM_FILE)
        self._mm: Optional[mmap.mmap] = None
        self._fd: Optional[int] = None
        self._running = False
        self._available = False
        self._shm_source: Optional[str] = None
        self._last_frame: Optional[Frame] = None
        self._last_poll = 0.0
        self._interval = 1.0 / max(1, fps)

    @property
    def available(self) -> bool:
        """共享内存是否已打开（无 C 侧抓帧时 False，get_frame() 返回 None）。"""
        return self._available and self._mm is not None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._running = True
        self._last_poll = 0.0
        self._last_frame = None
        if not self._open_shm():
            log.warning(
                "EngineForkRenderer: 共享内存不可用 (file=%s, posix=%s)，降级为无帧",
                self.shm_path,
                self.shm_name,
            )
        else:
            log.info("EngineForkRenderer: 已打开 %s", self._shm_source)

    def stop(self) -> None:
        self._running = False
        self._close_shm()

    # ------------------------------------------------------------ frames

    def get_frame(self) -> Optional[Frame]:
        """按 fps 降采样返回最新帧；无可用帧返回 None（或复用缓存帧）。"""
        now = time.time()
        if not self._running or now - self._last_poll < self._interval:
            return self._last_frame
        self._last_poll = now
        frame = self._read_latest_frame()
        if frame is not None:
            self._last_frame = frame
        return self._last_frame

    def _read_latest_frame(self) -> Optional[Frame]:
        """读最新完整帧；读到半帧（write_idx 读取期间变化）则重试。

        竞态场景：读者以 write_idx=W 取槽 (W-1)%depth，期间写者回绕后开始
        覆写同一槽位 → 数据可能撕裂。通过读后复查 write_idx 判别并重试。
        """
        raw = None
        for _ in range(_MAX_RACE_RETRIES):
            raw = self._read_frame_raw()
            if raw is None:
                # shm 可能尚未创建（客户端晚于渲染器启动）或已被重建：重试打开
                self._open_shm()
                raw = self._read_frame_raw()
            if raw is None:
                return None
            # 复查 write_idx：与读数据前的快照一致才认定帧完整
            h2 = self._parse_header(self._mm[:_HEADER_SIZE])
            if h2 is not None and h2["write_idx"] == raw["header"]["write_idx"]:
                break
            raw = None  # 半帧，丢弃重试
        if raw is None:
            return None
        header = raw["header"]
        rgb = self._to_rgb(raw["data"], header)
        return Frame(
            image=rgb,
            server_tick=header["server_tick"],
            wall_time=time.time(),
            width=header["width"],
            height=header["height"],
        )

    # ------------------------------------------------------------ shm

    def _open_shm(self) -> bool:
        self._close_shm()
        fd = self._try_open_file(self.shm_path)
        source = f"file:{self.shm_path}"
        if fd is None:
            fd = self._try_open_posix(self.shm_name)
            source = f"posix:{self.shm_name}"
        if fd is None:
            return False
        try:
            self._fd = fd
            # size=0 => 映射整个文件（覆盖 header + 全部帧槽）
            self._mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
            self._available = True
            self._shm_source = source
            return True
        except (OSError, ValueError) as e:
            log.warning("EngineForkRenderer: mmap 失败: %s", e)
            self._close_shm()
            return False

    def _close_shm(self) -> None:
        if self._mm is not None:
            try:
                self._mm.close()
            except Exception:  # noqa: BLE001
                pass
            self._mm = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._available = False
        self._shm_source = None

    @staticmethod
    def _try_open_file(path: str) -> Optional[int]:
        try:
            return os.open(path, os.O_RDONLY)
        except OSError as e:
            log.debug("EngineForkRenderer: open %s 失败: %s", path, e)
            return None

    @staticmethod
    def _try_open_posix(name: str) -> Optional[int]:
        try:
            import posix_ipc  # 可选依赖
        except ImportError:
            log.debug("EngineForkRenderer: posix_ipc 未安装，跳过 POSIX shm")
            return None
        try:
            shm = posix_ipc.SharedMemory(name, flags=posix_ipc.O_RDONLY)
        except OSError as e:
            log.debug("EngineForkRenderer: shm_open %s 失败: %s", name, e)
            return None
        try:
            return os.dup(shm.fd)
        finally:
            shm.close_fd()

    # ------------------------------------------------------------ 解析（自检可复用）

    def _read_frame_raw(self) -> Optional[dict]:
        """读头并返回最新完整帧的原始 BGRA 字节（自检入口，测试无需 C 侧）。

        返回 {"header": dict, "frame_index": int, "data": bytes}；
        无 mmap / magic 不符 / 尺寸非法 / 数据未写满时返回 None。
        """
        if self._mm is None:
            return None
        header = self._parse_header(self._mm[:_HEADER_SIZE])
        if header is None:
            return None
        write_idx = header["write_idx"]
        if write_idx == 0:
            return None  # 尚无帧写入
        frame_index = write_idx - 1
        slot = frame_index % header["depth"]
        frame_size = header["stride"] * header["height"]
        start = _HEADER_SIZE + slot * frame_size
        end = start + frame_size
        if end > self._mm.size():
            log.debug(
                "EngineForkRenderer: 环形缓冲数据不完整 (需要 %d, 映射 %d)", end, self._mm.size()
            )
            return None
        return {
            "header": header,
            "frame_index": frame_index,
            "data": self._mm[start:end],
        }

    @classmethod
    def _parse_header(cls, raw: bytes) -> Optional[dict]:
        """解析 44 字节头；magic/尺寸校验失败返回 None。"""
        if raw is None or len(raw) < _HEADER_SIZE:
            return None
        magic, width, height, stride, depth, write_idx, read_idx, server_tick = struct.unpack(
            _HEADER_FMT, raw[:_HEADER_SIZE]
        )
        if magic != _MAGIC:
            log.debug("EngineForkRenderer: magic 不符 0x%08X", magic)
            return None
        if width <= 0 or height <= 0 or depth <= 0 or stride < width * 3 or stride > _MAX_STRIDE:
            log.debug(
                "EngineForkRenderer: 非法尺寸 w=%d h=%d stride=%d depth=%d",
                width, height, stride, depth,
            )
            return None
        return {
            "magic": magic,
            "width": width,
            "height": height,
            "stride": stride,
            "depth": depth,
            "write_idx": write_idx,
            "read_idx": read_idx,
            "server_tick": server_tick,
        }

    @staticmethod
    def _to_rgb(data: bytes, header: dict) -> np.ndarray:
        """BGRA（stride=width*4）→ (H,W,3) uint8 RGB；兼容 stride==width*3 的 RGB。"""
        w, h, stride = header["width"], header["height"], header["stride"]
        row = np.frombuffer(data, dtype=np.uint8).reshape(h, stride)
        if stride == w * 3:
            return np.ascontiguousarray(row[:, : w * 3].reshape(h, w, 3))
        bgra = row[:, : w * 4].reshape(h, w, 4)
        return bgra[:, :, [2, 1, 0]].copy()
