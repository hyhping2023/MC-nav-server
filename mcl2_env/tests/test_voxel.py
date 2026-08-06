#!/usr/bin/env python3
"""VoxelRenderer 单元测试：服务端体素 DDA 合成帧（无客户端/无共享内存）。

覆盖：
  - 输出 (H,W,3) uint8、非纯色（std > 阈值）
  - 相机朝向变化 → 画面变化
  - 前方墙体命中 → 中央像素为墙体色（灰）
  - voxels=None（无体素网格）→ 程序化地面回退，仍非纯色
  - 非法体素网格 → 优雅回退，不抛错
  - pydantic 风格对象输入（.x/.y/.z / .dir）兼容
  - set_camera 前 get_frame() 返回 None

运行方式（任选其一）：
    python3 -m pytest mcl2_env/tests/
    python3 mcl2_env/tests/test_voxel.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if _PROJECT_ROOT not in map(Path, sys.path):
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcl2_env.renderer.voxel import VoxelRenderer  # noqa: E402

W, H = 64, 64
POS = {"x": 10.5, "y": 64.5, "z": 20.5}
LOOK_N = {"yaw": 0.0, "pitch": 0.0, "dir": {"x": 0.0, "y": 0.0, "z": -1.0}}
LOOK_E = {"yaw": 90.0, "pitch": 0.0, "dir": {"x": 1.0, "y": 0.0, "z": 0.0}}


def make_grid(half: int = 2, fill: str = "air") -> list:
    """(2*half+1)^3 网格，全 fill。"""
    s = 2 * half + 1
    return [[[fill] * s for _ in range(s)] for _ in range(s)]


def make_ground_grid() -> list:
    """半空半地网格：eye 行以下为 dirt/stone，以上为 air（含眼位空气）。"""
    half = 2
    g = make_grid(half, "air")
    for i in range(2 * half + 1):
        for k in range(2 * half + 1):
            for j in range(2 * half + 1):
                # 中心格（j=2）为脚部方块 y=64，其下为方块
                if j < 2:
                    g[i][j][k] = "mcl_core:stone" if j == 0 else "mcl_core:dirt"
    return g


def render(look=LOOK_N, voxels=None) -> np.ndarray:
    r = VoxelRenderer(width=W, height=H, fov=72)
    r.start()
    r.set_camera(POS, look, voxels)
    f = r.get_frame()
    r.stop()
    assert f is not None
    return f.image


# ---------------------------------------------------------------- tests

def test_frame_shape_dtype() -> None:
    img = render(voxels=make_ground_grid())
    assert img.shape == (H, W, 3)
    assert img.dtype == np.uint8
    assert img.min() >= 0 and img.max() <= 255


def test_frame_non_solid() -> None:
    img = render(voxels=make_ground_grid())
    assert float(img.std()) > 10.0, "体素合成帧不应是纯色"


def test_frame_changes_with_look() -> None:
    a = render(look=LOOK_N, voxels=None)
    b = render(look=LOOK_E, voxels=None)
    diff = float(np.abs(a.astype(int) - b.astype(int)).mean())
    assert diff > 1.0, f"相机朝向变化应改变画面 (mean diff={diff:.2f})"


def test_wall_hit_colors_center() -> None:
    """正前方一面石墙：画面中央像素应为灰色（mcl_core:stone 色）。"""
    half = 2
    g = make_grid(half, "air")
    # dz=-1 层（k=1，玩家前方 1 格）整层石头
    for i in range(2 * half + 1):
        for j in range(2 * half + 1):
            g[i][j][1] = "mcl_core:stone"
    img = render(look=LOOK_N, voxels=g)

    center = img[H // 2, W // 2].astype(int)
    spread = int(center.max() - center.min())
    assert spread <= 30, f"中央像素应为灰色系 (pixel={center})"
    assert 40 <= center.mean() <= 160, f"中央像素应为 stone 色系 (pixel={center})"
    # 画面里应有相当比例的灰色像素（墙占了大半）
    spread_arr = img.max(axis=2).astype(int) - img.min(axis=2).astype(int)
    gray_frac = float((spread_arr <= 30).mean())
    assert gray_frac > 0.2, f"墙体应占据画面相当比例 (gray_frac={gray_frac:.2f})"


def test_no_voxels_ground_fallback() -> None:
    img = render(voxels=None)
    assert img.shape == (H, W, 3)
    assert float(img.std()) > 10.0


def test_invalid_voxels_falls_back() -> None:
    # 非立方体（2 层）→ 应回退且不抛错
    bad = [[["air"] * 3 for _ in range(3)]]
    img = render(voxels=bad)
    assert img.shape == (H, W, 3)
    assert float(img.std()) > 10.0


def test_empty_voxels_list_ok() -> None:
    img = render(voxels=[])
    assert img.shape == (H, W, 3)


def test_model_like_inputs() -> None:
    """pydantic 风格对象（.x/.y/.z / .dir）与 dict 等效。"""

    class V:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z

    class L:
        def __init__(self, dirv):
            self.dir = dirv

    r = VoxelRenderer(width=W, height=H, fov=72)
    r.start()
    r.set_camera(V(10.5, 64.5, 20.5), L(V(0.0, 0.0, -1.0)), None)
    f = r.get_frame()
    r.stop()
    assert f is not None and f.image.shape == (H, W, 3)
    assert float(f.image.std()) > 10.0


def test_get_frame_none_before_camera() -> None:
    r = VoxelRenderer(width=W, height=H)
    r.start()
    assert r.get_frame() is None
    r.stop()


def test_frame_meta() -> None:
    r = VoxelRenderer(width=W, height=H)
    r.start()
    r.set_camera(POS, LOOK_N, None)
    f = r.get_frame()
    r.stop()
    assert f is not None
    assert f.server_tick == 0
    assert f.wall_time > 0
    assert f.width == W and f.height == H


def test_available_true_after_start() -> None:
    r = VoxelRenderer()
    r.start()
    assert r.available is True
    r.stop()


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
