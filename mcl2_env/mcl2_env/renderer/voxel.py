"""voxel 渲染器：服务端体素合成第一人称视图（fallback / debug）。

不依赖客户端，从状态接口的 voxels + 相机参数做 DDA 射线投射。画面真实感差，
仅用于：
  - 客户端未运行 / 共享内存无帧时的视觉回退（M1 验收用 VoxelRenderer 在线跑通）；
  - 开发调试。

输入（env._observe / random_agent 每步注入 set_camera）：
  pos    player.pos，玩家脚部坐标 {x,y,z}（dict 或 Vec3）
  look   player.look，含 dir {x,y,z}（dict 或 CameraState）
  voxels world.voxels，(2*half+1)^3 的节点名字符串三维数组（Lua get_voxel_grid，
        中心格 = 玩家所在方块）；voxels 为 None 时退化为程序化地面 + 天空。

输出：get_frame() → (H, W, 3) uint8 RGB 第一人称帧。天空渐变 + 距离雾保证
画面非纯色，且随相机朝向变化。
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

import numpy as np

from .base import Frame, Renderer

log = logging.getLogger("mcl2_env.renderer.voxel")

# ---------------------------------------------------------------- 色表

# 方块名子串 → RGB（靠前的规则优先；匹配规则顺序敏感）
_COLOR_RULES: list[tuple[tuple[str, ...], tuple[int, int, int]]] = [
    (("leaves", "foliage", "grass_block", "grass"), (62, 132, 46)),
    (("log", "tree", "wood", "plank", "bark"), (124, 88, 45)),
    (("dirt", "mud", "farmland"), (101, 67, 33)),
    (("stone", "cobble", "mossy", "obsidian", "deepslate"), (118, 120, 123)),
    (("sand", "sandstone"), (216, 201, 160)),
    (("water", "river"), (63, 118, 228)),
    (("brick",), (146, 90, 76)),
    (("coal",), (46, 46, 50)),
    (("iron",), (196, 192, 184)),
    (("gold",), (232, 200, 80)),
    (("wool", "concrete", "terracotta"), (205, 205, 205)),
    (("glass",), (150, 200, 228)),
    (("snow", "ice"), (235, 240, 248)),
    (("clay",), (152, 141, 132)),
    (("torch", "flower", "mushroom", "tallgrass", "fern", "bush", "cactus"),
     (160, 190, 70)),
]
_DEFAULT_COLOR = (168, 100, 200)  # 未匹配方块（调试可见）
_AIR_NAMES = {"air", "ignore", "unknown"}

# 天空（垂直渐变：地平线 → 天顶）
_SKY_HORIZON = np.array([178, 206, 238], dtype=np.float64)
_SKY_ZENITH = np.array([82, 142, 222], dtype=np.float64)
# 无命中时下方远景（峡谷底部 / 网格外地面）
_DEEP_GROUND = np.array([74, 88, 60], dtype=np.float64)

_EYE_HEIGHT = 1.6  # 玩家视线高度（Luanti 眼高约 1.5，取 1.6）

_GROUND_CHECKER_A = np.array([88, 138, 52], dtype=np.float64)  # 草地亮格
_GROUND_CHECKER_B = np.array([70, 116, 42], dtype=np.float64)  # 草地暗格
_GROUND_SIDE = np.array([96, 64, 34], dtype=np.float64)        # 地面侧壁（远视）


def _color_for(name: str) -> tuple[int, int, int]:
    """节点名 → 色表 RGB；子串匹配（Mineclonia 节点带 mod 前缀）。"""
    for needles, rgb in _COLOR_RULES:
        for n in needles:
            if n in name:
                return rgb
    return _DEFAULT_COLOR


class VoxelRenderer(Renderer):
    """朴素体素合成器：DDA 射线投射 + 色表 + 天空/距离雾。

    - set_camera(pos, look, voxels) 注入相机与局部体素网格（每步调用）。
    - get_frame() 对当前相机重新渲染（fallback 帧率低，无缓存降采样）。
    """

    def __init__(self, width: int = 224, height: int = 224, fov: int = 72,
                 voxel_half: int = 3, eye_height: float = _EYE_HEIGHT):
        self.width = width
        self.height = height
        self.fov = fov
        self.voxel_half = voxel_half
        self.eye_height = eye_height
        self._cam: Optional[tuple[dict, dict, Optional[list]]] = None
        self._last: Optional[Frame] = None
        self._warned_bad_grid = False

    # ------------------------------------------------------------ lifecycle

    @property
    def available(self) -> bool:
        """voxel 合成不依赖外部资源，start 后即可用。"""
        return True

    def start(self) -> None:
        self._cam = None
        self._last = None

    def stop(self) -> None:
        pass

    # ------------------------------------------------------------ camera

    def set_camera(self, pos, look, voxels) -> None:
        """注入相机与体素数据；随后 get_frame() 输出当前视角合成帧。

        pos/look 兼容 dict（bridge 原始 JSON）与 pydantic 模型（GymnasiumEnv）。
        """
        self._cam = (pos, look, voxels)
        self._last = None  # 新相机 → 强制重渲染

    # ------------------------------------------------------------ frames

    def get_frame(self) -> Optional[Frame]:
        if self._cam is None:
            return None
        pos, look, voxels = self._cam
        img = self._render(pos, look, voxels)
        self._last = Frame(
            image=img,
            server_tick=0,
            wall_time=time.time(),
            width=self.width,
            height=self.height,
        )
        return self._last

    # ------------------------------------------------------------ 渲染

    def _render(self, pos, look, voxels) -> np.ndarray:
        eye = self._eye_pos(pos)
        fwd = self._forward(look)
        right, up = self._camera_basis(fwd)
        dirs = self._pixel_dirs(fwd, right, up)

        occ, colors = self._build_grid(voxels)

        if occ is None:
            rgb = self._render_ground_fallback(eye, dirs, pos)
        else:
            half = occ.shape[0] // 2
            origin_block = tuple(math.floor(c) for c in self._vec3(pos))
            grid_min = np.array([origin_block[0] - half, origin_block[1] - half,
                                 origin_block[2] - half], dtype=np.float64)
            rgb = self._render_grid(eye, dirs, occ, colors, grid_min)
        return np.clip(rgb, 0, 255).astype(np.uint8)

    def _eye_pos(self, pos) -> np.ndarray:
        x, y, z = self._vec3(pos)
        return np.array([x, y + self.eye_height, z], dtype=np.float64)

    def _forward(self, look) -> np.ndarray:
        d = None
        if isinstance(look, dict):
            d = look.get("dir") or {}
        elif look is not None:
            d = getattr(look, "dir", None)
        if d is None:
            return np.array([0.0, 0.0, -1.0])  # 无朝向时退化：朝 -Z
        x, y, z = self._vec3(d)
        f = np.array([x, y, z], dtype=np.float64)
        n = np.linalg.norm(f)
        if n < 1e-9:
            return np.array([0.0, 0.0, -1.0])
        return f / n

    @staticmethod
    def _camera_basis(fwd: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """右手系相机基：right / up（y 向上，fwd 为视线）。"""
        world_up = np.array([0.0, 1.0, 0.0])
        right = np.cross(fwd, world_up)
        n = np.linalg.norm(right)
        if n < 1e-9:  # 视线与 y 轴平行（正上/正下）
            right = np.array([1.0, 0.0, 0.0])
        else:
            right /= n
        up = np.cross(right, fwd)
        up /= np.linalg.norm(up)
        return right, up

    def _pixel_dirs(self, fwd: np.ndarray, right: np.ndarray,
                    up: np.ndarray) -> np.ndarray:
        """(H, W, 3) 每像素单位射线方向。"""
        h, w = self.height, self.width
        aspect = w / h
        tan = math.tan(math.radians(self.fov) / 2.0)
        xs = (np.arange(w) + 0.5) / w * 2.0 - 1.0      # [-1, 1)
        ys = 1.0 - (np.arange(h) + 0.5) / h * 2.0      # 顶部 +1
        u, v = np.meshgrid(xs * aspect * tan, ys * tan)
        dirs = (fwd[None, None, :]
                + u[..., None] * right[None, None, :]
                + v[..., None] * up[None, None, :])
        return dirs / np.linalg.norm(dirs, axis=2, keepdims=True)

    # ------------------------------------------------------------ 体素网格

    @staticmethod
    def _vec3(v) -> tuple[float, float, float]:
        if isinstance(v, dict):
            return float(v["x"]), float(v["y"]), float(v["z"])
        return float(v.x), float(v.y), float(v.z)

    def _build_grid(self, voxels):
        """Lua 体素网格 → (occ bool(S³), colors uint8(S³,3))。

        网格为 (2*half+1)^3 字符串数组，中心格 = 玩家所在方块。
        voxels 为 None / 尺寸非法时返回 (None, None) → 程序化地面。
        """
        if voxels is None:
            return None, None
        try:
            arr = np.asarray(voxels)
        except (ValueError, TypeError):
            self._warn_grid("无法转换为数组")
            return None, None
        if arr.ndim != 3 or len(set(arr.shape)) != 1:
            self._warn_grid(f"非立方体素网格 shape={arr.shape}")
            return None, None

        names = arr.astype(str)
        flat = names.reshape(-1)
        occ = np.zeros(flat.size, dtype=bool)
        colors = np.zeros((flat.size, 3), dtype=np.uint8)
        for i, name in enumerate(flat):
            occ[i] = name not in _AIR_NAMES
            colors[i] = _color_for(name)
        return occ.reshape(arr.shape), colors.reshape(arr.shape + (3,))

    def _warn_grid(self, why: str) -> None:
        if not self._warned_bad_grid:
            log.warning("VoxelRenderer: 体素网格无效 (%s)，退化为程序化地面", why)
            self._warned_bad_grid = True

    # ------------------------------------------------------------ 网格射线投射

    def _render_grid(self, eye, dirs, occ, colors, grid_min) -> np.ndarray:
        """对所有像素批量 DDA；返回 (H, W, 3) 颜色（float）。"""
        h, w, _ = dirs.shape
        flat = dirs.reshape(-1, 3)
        origin = np.broadcast_to(eye - grid_min, (flat.shape[0], 3))  # grid 局部坐标

        t_hit, is_hit, hit_cells = self._dda(origin, flat, occ)

        n = flat.shape[0]
        rgb = np.zeros((n, 3), dtype=np.float64)
        if is_hit.any():
            hc = hit_cells[is_hit]
            hit_rgb = colors[hc[:, 0], hc[:, 1], hc[:, 2]]
            shade = self._distance_shade(t_hit[is_hit])
            rgb[is_hit] = (hit_rgb.astype(np.float64) * shade[:, None]
                           + _SKY_HORIZON * (1.0 - shade[:, None]))

        miss = ~is_hit
        if miss.any():
            rgb[miss] = self._sky_for_pixels(miss, h, w)

        return rgb.reshape(h, w, 3)

    @staticmethod
    def _sky_for_pixels(miss: np.ndarray, height: int, width: int) -> np.ndarray:
        """未命中像素按纵向位置填天空渐变 / 深色远景。

        miss 为 (H*W,) 布尔；按像素行号恢复纵向位置。
        """
        idx = np.where(miss)[0]
        v = 1.0 - (idx // width + 0.5) / height * 2.0  # 顶部 +1
        t = np.clip((v + 0.25) / 1.25, 0.0, 1.0)       # 地平线略低于画面中线
        sky = _SKY_HORIZON[None, :] * (1.0 - t)[:, None] + _SKY_ZENITH[None, :] * t[:, None]
        return np.where(v[:, None] >= -0.05, sky, _DEEP_GROUND)

    @staticmethod
    def _dda(origin: np.ndarray, dirs: np.ndarray, occ: np.ndarray):
        """向量化 3D DDA：从 origin（grid 局部坐标）沿 dirs 步进 occ 立方体。

        返回 (t_hit[N], is_hit[N], hit_cells[N,3])：命中距离 / 是否命中 /
        命中所在格下标（未命中格为 inf / False / 零填充）。
        """
        n = dirs.shape[0]
        s = occ.shape[0]

        cell = np.floor(origin).astype(np.int64)          # (N,3)
        step = np.zeros((n, 3), dtype=np.int64)
        step[dirs > 0] = 1
        step[dirs < 0] = -1
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = np.abs(1.0 / dirs)
        tdelta = np.where(np.isinf(inv), np.inf, inv)
        boundary = np.where(step > 0, cell.astype(np.float64) + 1.0,
                            cell.astype(np.float64))
        tmax = np.where(np.isinf(tdelta), np.inf,
                        (boundary - origin) * tdelta)

        t_hit = np.full(n, np.inf)
        hit_cells = np.zeros((n, 3), dtype=np.int64)
        is_hit = np.zeros(n, dtype=bool)
        active = np.ones(n, dtype=bool)

        def in_grid(c) -> np.ndarray:
            return ((c >= 0) & (c < s)).all(axis=1)

        def occ_at(c) -> np.ndarray:
            ok = in_grid(c)
            out = np.zeros(len(c), dtype=bool)
            if ok.any():
                sub = c[ok]
                out[ok] = occ[sub[:, 0], sub[:, 1], sub[:, 2]]
            return out

        # 起点即实心（眼睛卡进方块）→ t=0 命中
        init = occ_at(cell)
        if init.any():
            ids = np.where(init)[0]
            is_hit[ids] = True
            t_hit[ids] = 0.0
            hit_cells[ids] = cell[ids]
            active[ids] = False

        for _ in range(s * 3 + 2):
            idx = np.where(active)[0]
            if len(idx) == 0:
                break
            sub_tmax = tmax[idx]
            axis = np.argmin(sub_tmax, axis=1)            # (M,)
            rows = np.arange(len(idx))
            t_cross = sub_tmax[rows, axis]                # 进入下一格的距离

            cell[idx, axis] += step[idx, axis]
            tmax[idx, axis] += tdelta[idx, axis]

            cur = cell[idx]
            out = ~in_grid(cur)
            if out.any():
                active[idx[out]] = False
            if not out.all():
                keep = ~out
                hit_sub = occ_at(cur[keep])
                if hit_sub.any():
                    hit_ids = idx[keep][hit_sub]
                    is_hit[hit_ids] = True
                    t_hit[hit_ids] = t_cross[keep][hit_sub]
                    hit_cells[hit_ids] = cell[hit_ids]
                    active[hit_ids] = False
        return t_hit, is_hit, hit_cells

    @staticmethod
    def _distance_shade(t: np.ndarray) -> np.ndarray:
        """距离雾系数：近处 1.0 → 远处 0.35（向雾色过渡）。"""
        return 0.35 + 0.65 * np.exp(-0.12 * t)

    # ------------------------------------------------------------ 程序化地面回退

    def _render_ground_fallback(self, eye, dirs, pos) -> np.ndarray:
        """无体素网格时：程序化棋盘地面 + 天空渐变，保证非纯色且随朝向变化。"""
        h, w, _ = dirs.shape
        flat = dirs.reshape(-1, 3)
        n = flat.shape[0]
        ground_y = math.floor(self._vec3(pos)[1])  # 脚部方块 y

        rgb = np.zeros((n, 3), dtype=np.float64)
        miss = np.ones(n, dtype=bool)

        down = flat[:, 1] < -1e-9
        if down.any():
            t = (ground_y - eye[1]) / flat[down, 1]
            t = np.maximum(t, 0.0)
            valid = t > 0
            if valid.any():
                down_valid = np.zeros(n, dtype=bool)
                down_valid[down] = valid
                sub = flat[down][valid]
                p = eye[None, :] + t[valid, None] * sub
                checker = ((np.floor(p[:, 0] + p[:, 2]) % 2) == 0)
                base = np.where(checker[:, None], _GROUND_CHECKER_A,
                                _GROUND_CHECKER_B)
                shade = self._distance_shade(t[valid])
                rgb[down_valid] = (base * shade[:, None]
                                   + _SKY_HORIZON * (1.0 - shade[:, None]))
                miss[down_valid] = False

        rgb[miss] = self._sky_for_pixels(miss, h, w)
        return rgb.reshape(h, w, 3)
