-- mcl2_agent / api/vision.lua
-- 视觉同步：相机位姿上报 + 渲染器握手。
-- 图像帧由 fork 客户端抓取后经共享内存直接进 Python；本模块只负责：
--   1) 上报渲染器所需的相机参数（pos / look / fov / fps）
--   2) 强制确定性渲染环境（固定时间、天气、亮度）
-- 骨架实现：相机上报接口 + 渲染配置应用。

local S = minetest.get_translator("mcl2_agent")

mcl2agent.vision = {}

-- 采集 bot 的相机参数（供渲染器每帧对齐使用）
-- @param player ObjectRef
-- @return table {pos, look={yaw,pitch,dir}, fov}
function mcl2agent.vision.get_camera(player)
    if not player then return nil end
    local pos = player:get_pos()
    local look_h = player:get_look_horizontal()
    local look_v = player:get_look_vertical()
    local dir = player:get_look_dir()
    return {
        pos = {x = pos.x, y = pos.y, z = pos.z},
        look = {
            yaw = look_h,
            pitch = look_v,
            dir = {x = dir.x, y = dir.y, z = dir.z},
        },
        fov = mcl2agent.config.vision.fov,
    }
end

-- 进入确定性渲染模式（数据采集前调用）
-- @param player ObjectRef
function mcl2agent.vision.enter_deterministic(player)
    if mcl2agent.config.determinism.freeze_time then
        player:set_physics_override({speed = 1, jump = 1, gravity = 1})
        -- TODO: 冻结 time_speed（需要设置 time_speed 环境值，见 reset.lua）
    end
    if mcl2agent.config.determinism.weather then
        -- TODO: 固定天气（Mineclonia 的 weather mod 提供 API）
    end
end

-- 渲染器握手：由 bridge 调用，把帧信息回传供记录对齐
-- @param msg table {renderer, width, height, fps, shm_key}
function mcl2agent.vision.on_renderer_ready(msg)
    mcl2agent.vision.renderer = msg
    minetest.log("action", "[mcl2_agent] renderer ready: "
        .. tostring(msg and msg.renderer))
end

-- 给 Python 的相机上报（由 state 采样调用，见 state.lua）
mcl2agent.vision.get_camera_for_state = mcl2agent.vision.get_camera
