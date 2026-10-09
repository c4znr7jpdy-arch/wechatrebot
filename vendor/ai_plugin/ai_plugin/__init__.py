"""
Reusable business modules for AstrBot plugins.

The old NoneBot2 entrypoint and AI routing stack were archived under
``ai_plugin/legacy_nonebot2``. Keep this package initializer side-effect free:
AstrBot plugins import concrete modules such as ``ai_plugin.news`` and
``ai_plugin.image_generator`` directly.
"""

__all__: list[str] = []
