"""vla-env: VLA control hub for the Minecraft research environment.

包采用**惰性导入**策略：``import vla_env`` 只暴露 ``__version__``，
不拉取任何重依赖（grpcio / websockets / numpy / gymnasium / pillow /
pyyaml），因此即使部分依赖缺失，顶层导入也不会崩溃（M0 验收硬性项）。

子模块（env / action_space / obs / client_ws / server_grpc / lockstep）
为 M0 里程碑桩，通过 ``__getattr__`` 在首次访问时才真正导入；桩模块自身
对依赖做了防御性处理，保证 ``import vla_env`` 永不因依赖缺失而失败。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

# M0 桩子模块（均可独立 import；功能性方法抛 NotImplementedError）。
_LAZY_SUBMODULES = (
    "env",
    "action_space",
    "obs",
    "client_ws",
    "server_grpc",
    "lockstep",
)


def __getattr__(name):
    """惰性导入子模块：首次访问 ``vla_env.<name>`` 时才真正 import。

    依赖缺失时吞掉 ImportError 并给出提示（不向调用方抛异常），
    保持顶层 ``import vla_env`` 始终成功。
    """
    if name in _LAZY_SUBMODULES:
        import importlib

        try:
            module = importlib.import_module(f".{name}", __name__)
        except ImportError:
            raise AttributeError(
                f"vla_env.{name} 依赖缺失，无法导入（M0 桩依赖未安装）"
            ) from None
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
