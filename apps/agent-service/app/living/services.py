"""她这边插件之间的契约：服务的接口（``Protocol``）和接口里出现的数据类型。

她的每个功能是一个插件，一个目录 ``app/living/<插件>/``。插件之间不互相 import
（``tests/living/test_plugin_boundaries.py`` 守着）：一个插件要另一个插件的东西，就由提供方在
setup 里 ``ctx.provide(名字, 对象)`` 交出服务，使用方在 :class:`~app.host.Plugin` 的 ``requires``
里写上名字、setup 里 ``ctx.service(名字)`` 取到，作为构造参数传给自己的插件对象。测试直接传替身。

这里只放签名和数据类型，不放实现；不读库、不调模型的共用纯函数在 :mod:`app.living.common`。
"""

from __future__ import annotations
