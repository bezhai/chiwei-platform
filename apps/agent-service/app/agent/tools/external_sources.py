"""外部查询工具：天气 / 日出日落 / 节气 / 节假日 / 番剧 / 本市活动。

赤尾世界的"外部干预"信息源。每个工具内部是**写定的代码**——调对应官方 API、按已知
响应结构解析、返回一份**结构化数据**（@tool 返回 dict，框架会 JSON 序列化喂给
agent）。不让 LLM 去解析原始 JSON，也不在这层把数据拼成人话——拼人话、组织底料是
抓取 agent 的事，工具只负责返回**准的结构化事实**。

成功返回带字段的结构（天气：温度/体感/天气/湿度/风；番剧：今天周几/番剧名列表；
节假日：日期/周几/类型/节日名；本市活动：城市/窗口天数/活动列表），并带
``"ok": True``。

失败降级契约（六个工具一致）：网络失败 / 坏 key / 解析失败 / API 返回错误码时，返回
``{"ok": False, "reason": "..."}``（绝不返回空、绝不返回半截或脏数据、绝不冒充成功）。
``reason`` 里**绝不含 key 明文、不含带 key 的完整 url 或 header**——异常对象只暴露类型
名、不拼进可能含敏感信息的 url（key 永远走 header / 不入 url，见 ``query_weather``）。

**"读不懂"和"上游说没有"是两件事，不许压成一件。** 这是这个模块最容易犯、也已经犯过的
错：``data.get("x") or []`` 拿不到就当空、逐项解析失败就 ``continue``，于是"字段名换了"
在返回值里跟"这两周城里真没事" / "今天真没番"长得一模一样，没有日志、没有报错、单测因为
替身跟代码一致而永远绿——这个仓库有两条源为此从上线起恒不生效了一年。所以每个源都把这
三种情况分开：上游在我们认识的结构里说"没有"（``ok=True`` + 空列表）、有东西但被我们自
己的条件过滤掉了（``ok=True`` + 空列表 + 一个数得出来的计数）、**响应不是我们认识的样子
（``ok=False``）**。空列表只在前两种情况下出现。

agent 别瞎编：某个工具返回 ``ok=False`` 就如实说那项今天没拿到，绝不编一个顶上——这
靠抓取 agent 的 prompt 管，工具这层只保证数据准。

**这个世界坐落在哪座真实城市，不在这个模块里，也不在配置里——它是调用方传进来的一个
参数。** 三只跟地点有关的手（天气、日出日落、本市活动）各收一个 ``city``，谁在调它谁
就得知道这家人住在哪；那件事写在世界自己的设定集里，由 :mod:`app.living.world` 那一轮
读出来再传下来。这个模块**绝不**替调用方补一个默认城市——补了就是拿别处的天和别处的
日落冒充这里的，那是撒谎。没给城市就 ``ok=False``。

和风的数据接口不吃中文市名（只吃 LocationID 或坐标），所以天气和日出日落先打一次
GeoAPI ``/geo/v2/city/lookup`` 把市名换成 LocationID（:func:`_resolve_location`）。
查不到城市、GeoAPI 出错一律走既有的降级契约，不退回任何写死的坐标。

**这一跳是模糊搜索，所以它认下来的是谁必须交回给调用方。** 官方文档写明 ``location``
只给一个汉字也能匹配，结果按相关性和 rank 排序（文档自己举的例：``location=西安`` 同时
匹配陕西省西安市和辽源市西安区）。名字有歧义时上游可能认到另一座城市去，而调用方无从
发现——**返回里把传进来的市名原样贴回去不构成验证**，那只是自己的输入换了个字段名。所以
这两只手成功时带一个 ``matched``（上游那条记录自己的 ``name`` / ``adm2`` / ``adm1`` /
``country``），同一次搜索还匹配到别处时另带 ``also_matched``。这一层**不替调用方消歧**：
``adm`` / ``range`` 要的是它并没有交给我们的信息，替它猜就是在这里替世界拿主意。

**这一跳没有实跑过**：prod 的和风 key 不能落到开发机上，所以它是照官方文档的响应结构
写的，测试里那几份 geo fixture 也是照文档写的、不是抓包抓下来的（本文件其余每一份
fixture 都是真响应的切片）。

**一处跟现行文档对不上，留着没删：** 代码里 ``code == "404"`` 那条分支（"查不到这座
城市"）来自旧版和风的约定——HTTP 恒 200、状态写在 body 的 ``code`` 里。现行文档的错误
体已经换成 RFC 7807：真的 HTTP 状态码 + 一个 ``error`` 对象，"这个地名匹配不到"是
**HTTP 400 + ``error.title == "NO SUCH LOCATION"``**，那张表里根本没有 404 这一条
（它的 404 是"路径或路径参数不对"）。因为验不了这个账号的 host 到底按哪一版答，两条都
接：先看 ``error.title``，再看 body 里的 ``code``。哪天真跑过一次，死掉的那条应该删。

时间一律用 :func:`now_cst`（CST 北京时间），不用 UTC——"今天"对赤尾是北京的今天。
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Any

import cnlunar
import httpx
from pydantic import Field

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.infra.config import settings
from app.infra.cst_time import now_cst

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 三只跟地点有关的手收的那个参数，说明只有这一份。
#
# **一个市名样本都不给。** 举例就是词表：写在这儿的市名会被模型逐字抄走，而且世界改到
# 别处去之后这几个字还在。这个仓库已经为"举例里的地名被抄走"踩过两次线上事故（原委写在
# ``tests/living/conftest.py`` 那一节），这里原来也有同一个病——报错文案写着"填中文市名
# 如「广州」"。说清楚要什么就够了，不需要样本。
CITY_ARG = "这个世界所在的那座真实城市的中文市名"

CityArg = Annotated[str, Field(description=CITY_ARG)]

# 和风 GeoAPI：中文市名 → LocationID。数据接口只吃 LocationID 或坐标，所以每一只跟
# 地点有关的和风查询都先过这一跳。
_GEO_LOOKUP_PATH = "/geo/v2/city/lookup"

# GeoAPI ``number`` 的取值范围是 1-20（默认 10）。**不填 1**：这是一个模糊搜索，只要一
# 行回来，"上游是不是在两座同名的地方之间替我们挑了一座"就完全看不出来。要 5 行不是为了
# 挑得更准（挑哪一个仍然听上游按相关性和 rank 排的序），是为了能把落选的那几个一并交回
# 给调用方，让它自己看出这次的名字有歧义。
_GEO_CANDIDATES = 5

# 新版和风的错误体是 RFC 7807：HTTP 状态码 + ``error`` 对象（``status`` / ``type`` /
# ``title`` / ``detail``）。文档里"这个地名匹配不到"就是 400 + 这个 title。
_GEO_NO_SUCH_LOCATION = "NO SUCH LOCATION"

# 交回给调用方的"上游认下来的这是谁"：只收这几项，从细到粗。
_PLACE_IDENTITY_FIELDS = ("name", "adm2", "adm1", "country")

# 和风天气的 API Host 不写死：2024 改版后每个账号有专属 host（统一 devapi/api 域名
# 对新 key 返 Invalid Host 403），host 从 settings.qweather_api_host 走 env 注入。
_BANGUMI_CALENDAR_URL = "https://api.bgm.tv/calendar"
_TIMOR_HOLIDAY_BASE = "https://timor.tech/api/holiday/info"

# Bilibili 会员购：漫展 / Only 同人展 / livehouse / 主题餐厅 / IP 展览的公开票务接口，
# 无 key、无签名、无 cookie，域名在国内（所以不走 forward_proxy，和 timor / 和风一样）。
_BILI_CITY_LIST_URL = "https://show.bilibili.com/api/ticket/city/list"
_BILI_PROJECT_LIST_URL = "https://show.bilibili.com/api/ticket/project/listV2"
# city/list 的 channel 是个枚举，只有 1 / 3 会返城市表，别的值被判参数非法。
_BILI_CITY_CHANNEL = "3"
# listV2 的必填参数：少 platform 直接报 param missing；pagesize 超过 20 会被风控
# 拒掉（code 81102084），所以 20 就是上游给的一页上限。
_BILI_LIST_VERSION = "134"
_BILI_PAGE_SIZE = 20
# 会员购按 User-Agent 拦：httpx 默认的 ``python-httpx/x.y.z`` 直接吃 412（实测同一
# 秒同一个 url，换成浏览器 UA 就是 200）。没有签名、没有 cookie、没有 referer 要求，
# 这个 header 就是全部——所以它必须写在这里并被测试钉住，否则哪天 httpx 升版这条
# 源会无声无息地关掉。
BILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# "这几天"有多长。两周：够一场要提前买票的漫展 / livehouse 落进来，又不会把三个月
# 后的东西堆到她眼前。
EVENT_WINDOW_DAYS = 14

_HTTP_TIMEOUT = 10.0

# timor type.type → 节假日类型人话标签（结构化字段 ``kind``，不是拼好的整句）。
_HOLIDAY_TYPE_LABEL = {
    0: "工作日",
    1: "周末休息",
    2: "法定节假日",
    3: "周末调休补班",
}


def _failed(reason: str) -> dict[str, Any]:
    """统一的失败结构。``reason`` 已由调用处保证不含 key 明文 / 带 key 的 url。"""
    return {"ok": False, "reason": reason}


# ---------------------------------------------------------------------------
# 市名 → 和风 LocationID —— 每次现查，代码里不存对照表
# ---------------------------------------------------------------------------


def _qweather_error_title(resp: httpx.Response) -> str:
    """新版和风错误体（RFC 7807）里的 ``error.title``；读不出来就是空串。

    ``title`` 是文档列死的一张枚举表（``NO SUCH LOCATION`` / ``INVALID HOST`` /
    ``UNAUTHORIZED`` / ``OVER MONTHLY LIMIT`` …），所以它进 ``reason`` 是安全的。同一个
    ``error`` 里的 ``detail`` 是上游自由文本，**不往外贴**——降级契约管的是"reason 里
    只出现我们自己写得出的东西"。
    """
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, dict):
        return ""
    title = error.get("title")
    return title.strip() if isinstance(title, str) else ""


def _qweather_http_reason(who: str, resp: httpx.Response) -> str:
    """非 200 时的人话原因。上游给了 ``error.title`` 就带上，没给就只说状态码。

    光一个 ``HTTP 403`` 读起来像网络坏了；``HTTP 403 INVALID HOST`` 说的是配置错了。
    """
    title = _qweather_error_title(resp)
    return f"{who}返回 HTTP {resp.status_code} {title}".rstrip()


@dataclass(frozen=True, slots=True)
class _MatchedPlace:
    """GeoAPI 认下来的那座城市：拿去查数据的 ``id``，加上上游自己说的它是谁。

    ``identity`` 只装上游真写了的字段（``name`` / ``adm2`` / ``adm1`` / ``country``），
    ``others`` 是同一次搜索里落选的那几个候选，各压成一行 "名字（上级行政区）"。
    """

    id: str
    identity: dict[str, str]
    others: tuple[str, ...]


def _place_identity(entry: dict[str, Any]) -> dict[str, str]:
    """上游给这条记录写的名字和行政区。没给的不补位，更不拿调用方的输入顶上。"""
    identity: dict[str, str] = {}
    for field in _PLACE_IDENTITY_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            identity[field] = value.strip()
    return identity


def _place_label(identity: dict[str, str]) -> str:
    """把一条身份压成一行：落选候选按这个交回去，够分辨两个同名的地方就行。"""
    name = identity.get("name")
    if not name:
        return ""
    scope = identity.get("adm1") or identity.get("country")
    if not scope or scope == name:
        return name
    return f"{name}（{scope}）"


async def _resolve_location(
    client: httpx.AsyncClient, *, api_host: str, headers: dict[str, str], city: str
) -> tuple[_MatchedPlace | None, str]:
    """把中文市名换成和风认下来的那座城市；返回 ``(城市, 失败原因)``，恰有一个有值。

    **代码里不存市名→id 的对照表**，理由跟 :func:`_pick_area_id` 同一条：那种表会过期，
    而过期的后果是静默查错城市。

    **这是一个模糊搜索**：官方文档写明只给一个汉字也能匹配，结果按相关性和 rank 排序，
    ``location=西安`` 会同时返回陕西省西安市和辽源市西安区。所以"上游到底认下了哪儿"
    是一件调用方必须能看见的事——这里把选中那条的 ``name`` / ``adm2`` / ``adm1`` /
    ``country`` 原样交回去，**绝不把传进来的市名贴回去当验证**（那是拿自己的输入给自己
    打分）。落选的候选也一并交回，同名的时候调用方才知道这次的名字有歧义。

    这里**不做消歧**：``adm`` / ``range`` 要的是调用方并没有的信息（上级行政区、国家
    代码），替它猜一个就是在这一层替世界拿主意。能做的是把歧义暴露出去。

    **这一跳没有实跑过**（见模块 docstring）：prod 的和风 key 不能落到开发机上，所以它
    按官方文档的响应结构写——成功是 HTTP 200 + ``code: "200"``，``location`` 是一个
    数组、每项带 ``id`` / ``name`` / ``adm1`` / ``adm2``；匹配不到按现行文档是 HTTP 400
    + ``error.title = "NO SUCH LOCATION"``（旧版那个 ``code: "404"`` 一并留着接，见
    模块 docstring 里那条"跟文档对不上"的记号）。

    网络异常不在这里接，由调用方那一层的 ``except httpx.HTTPError`` 统一接住——两跳
    共用一个 client，失败的措辞也该是同一句。
    """
    resp = await client.get(
        f"https://{api_host}{_GEO_LOOKUP_PATH}",
        params={"location": city, "number": _GEO_CANDIDATES},
        headers=headers,
    )
    if resp.status_code != 200:
        if _qweather_error_title(resp) == _GEO_NO_SUCH_LOCATION:
            # 跟"这座城市今天没天气"是两件事：这是根本没问对城市。
            logger.warning("geo lookup 查不到 %r", city)
            return None, f"和风 GeoAPI 查不到「{city}」这座城市"
        logger.warning("geo lookup http %d", resp.status_code)
        return None, _qweather_http_reason("和风 GeoAPI", resp)
    try:
        data = resp.json()
    except ValueError:
        logger.warning("geo lookup body not json")
        return None, "响应解析失败"
    if not isinstance(data, dict):
        # 合法 JSON 但不是一个对象。少了这一条就是 ``AttributeError`` 抛出去、被
        # ``tool_error`` 包成另一种返回，而降级契约说好了失败一律是 ``ok=False``。
        return None, "响应结构异常"
    code = data.get("code")
    if code == "404":
        logger.warning("geo lookup 查不到 %r", city)
        return None, f"和风 GeoAPI 查不到「{city}」这座城市"
    if code != "200":
        logger.warning("geo lookup api code=%s", code)
        return None, f"和风 GeoAPI 返回错误码 {code}"
    found = data.get("location")
    if not isinstance(found, list):
        return None, "响应缺少城市列表"
    candidates = [entry for entry in found if isinstance(entry, dict)]
    if not candidates:
        if found:
            # 有个列表，但里面没有一条是我们认识的记录。
            return None, "响应结构异常"
        # 空列表 = 这个名字没匹配上任何地方。跟"读不懂这份响应"是两件事。
        logger.warning("geo lookup 查不到 %r", city)
        return None, f"和风 GeoAPI 查不到「{city}」这座城市"

    best = candidates[0]
    location_id = best.get("id")
    if not location_id:
        return None, "响应缺少 LocationID"
    identity = _place_identity(best)
    if "name" not in identity:
        # 拿得到 id 却说不出这是哪儿——那调用方就验不了这次有没有查错地方，而这一跳
        # 存在的意义就是让它验得了。
        return None, "响应缺少城市名"
    others = tuple(
        label
        for other in candidates[1:]
        if (label := _place_label(_place_identity(other)))
    )
    return _MatchedPlace(id=str(location_id), identity=identity, others=others), ""


# ===========================================================================
# 天气 —— 和风 QWeather
# ===========================================================================


@tool
@tool_error("天气查询失败")
async def query_weather(city: CityArg) -> dict[str, Any]:
    """查一座城市此刻的实时天气，返回结构化天气数据。

    查的是**这一刻**的实况：想知道后来变了没有，过一会儿再查一次就是了。

    Returns:
        成功时返回 ``{"ok": True, "matched": {...}, "temp": "24", "feels_like":
        "26", "weather": "小雨", "humidity": "80", "wind": "南风2级"}``（除
        ``matched`` 外的字段值是从和风响应里取出的原始字符串，不拼成人话）。

        ``matched`` 是**上游认下来的那座城市自己说的身份**（``name`` / ``adm2`` /
        ``adm1`` / ``country``，上游没给的就没有）。市名换 LocationID 那一跳是模糊
        搜索，名字有歧义时可能认到另一座城市去，所以这份读数是谁的天得由上游讲——
        把传进来的市名原样贴回去不构成验证，那只是自己的输入换了个字段名。同一次
        搜索还匹配到别的地方时，多一个 ``also_matched``（落选候选，各是一行"名字
        （上级行政区）"），据此可以换一个更完整的名字再问一次。

        查询失败时返回 ``{"ok": False, "reason": "..."}``（reason 不含任何密钥）。
    """
    api_key = settings.qweather_api_key
    if not api_key:
        # 不把 key（None）拼进文本，只给人话原因。
        return _failed("未配置和风天气 API Key")
    api_host = settings.qweather_api_host
    if not api_host:
        # host 没配就直接降级，不去打统一域名（必被 Invalid Host 403 拒）。
        return _failed("未配置和风天气 API Host")
    wanted = city.strip()
    if not wanted:
        # 没有城市就没有"这里"，不拿写死的地方顶上。
        return _failed("没说是哪座城市")

    url = f"https://{api_host}/v7/weather/now"
    # key 只走 header，绝不进 url query —— reason 里贴 url 也不会泄露。
    headers = {"X-QW-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            place, why = await _resolve_location(
                client, api_host=api_host, headers=headers, city=wanted
            )
            if place is None:
                return _failed(why)
            resp = await client.get(
                url, params={"location": place.id}, headers=headers
            )
    except httpx.HTTPError as exc:
        logger.warning("query_weather connect error: %s", type(exc).__name__)
        return _failed(f"无法连接和风天气({type(exc).__name__})")

    if resp.status_code != 200:
        logger.warning("query_weather http %d", resp.status_code)
        return _failed(_qweather_http_reason("和风天气", resp))

    try:
        data = resp.json()
    except ValueError:
        logger.warning("query_weather body not json")
        return _failed("响应解析失败")

    if not isinstance(data, dict):
        # 合法 JSON 但不是对象——``data.get`` 会 ``AttributeError`` 抛出去，被
        # ``tool_error`` 包成另一种返回，而降级契约说好了失败一律是 ``ok=False``。
        return _failed("响应结构异常")

    if data.get("code") != "200":
        logger.warning("query_weather api code=%s", data.get("code"))
        return _failed(f"和风天气返回错误码 {data.get('code')}")

    now = data.get("now")
    if not isinstance(now, dict):
        # ``or {}`` 只兜得住缺失，兜不住"``now`` 是个别的东西"——那一样是 AttributeError。
        return _failed("响应缺少天气字段")
    text = now.get("text")
    temp = now.get("temp")
    if not text or temp is None:
        return _failed("响应缺少天气字段")

    result: dict[str, Any] = {
        "ok": True,
        # 这份读数是谁的天，由上游讲，不由传进来的市名讲。
        "matched": place.identity,
        "temp": temp,
        "weather": text,
    }
    feels = now.get("feelsLike")
    if feels is not None:
        result["feels_like"] = feels
    if (humidity := now.get("humidity")) is not None:
        result["humidity"] = humidity
    wind_dir = now.get("windDir")
    wind_scale = now.get("windScale")
    if wind_dir and wind_scale is not None:
        result["wind"] = f"{wind_dir}{wind_scale}级"
    if place.others:
        result["also_matched"] = list(place.others)
    return result


# ===========================================================================
# 番剧 —— Bangumi 放送日历（必走 forward_proxy）
# ===========================================================================


def _weekday_field(day: dict[str, Any], key: str) -> Any:
    """``/calendar`` 每一格里的 ``weekday`` 子对象取一项；不是对象就当没有。

    ``(d.get("weekday") or {}).get(key)`` 兜得住缺失，兜不住"``weekday`` 是个字符串"
    ——那会在生成器里 ``AttributeError``，绕过降级契约。
    """
    block = day.get("weekday")
    return block.get(key) if isinstance(block, dict) else None


@tool
@tool_error("番剧查询失败")
async def query_anime_calendar() -> dict[str, Any]:
    """查询今天正在更新的番剧，返回结构化番剧列表。

    Returns:
        成功时返回 ``{"ok": True, "weekday": "星期日", "anime": ["Re:Zero 第三
        季", ...]}``（``anime`` 是今天在更新的番剧名列表，今天没番时是空列表
        ``[]`` —— 没番仍算查询成功）；查询失败时返回 ``{"ok": False, "reason":
        "..."}``。

        "今天没番"只在**日历里有今天这一格、而那一格的 items 是空的**时候成立。日历
        里根本没有今天，或者那一格读不出任何番名，都是查询失败——那是响应不是我们认
        识的样子，不是一句关于今天的肯定句。
    """
    proxy = settings.forward_proxy_url
    client_kwargs: dict[str, object] = {"timeout": _HTTP_TIMEOUT}
    if proxy:
        client_kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            resp = await client.get(_BANGUMI_CALENDAR_URL)
    except httpx.HTTPError as exc:
        logger.warning("query_anime_calendar connect error: %s", type(exc).__name__)
        return _failed(f"无法连接 Bangumi({type(exc).__name__})")

    if resp.status_code != 200:
        logger.warning("query_anime_calendar http %d", resp.status_code)
        return _failed(f"Bangumi 返回 HTTP {resp.status_code}")

    try:
        week = resp.json()
    except ValueError:
        logger.warning("query_anime_calendar body not json")
        return _failed("响应解析失败")

    if not isinstance(week, list):
        return _failed("响应结构异常")

    # Bangumi weekday.id：周一=1 ... 周日=7，与 datetime.isoweekday() 同口径。
    today_id = now_cst().isoweekday()
    today_block = next(
        (
            d
            for d in week
            if isinstance(d, dict) and _weekday_field(d, "id") == today_id
        ),
        None,
    )
    if today_block is None:
        # ``/calendar`` 一周七格，今天那一格必须在里面。它不在，说明这份日历不是我们
        # 认识的样子（``weekday.id`` 改了名、日历缩了水），**不是"今天没番"**——后者
        # 是一句关于世界的肯定句，而且没人分得出它是真的还是这里读空了。这个仓库正是
        # 这么让两条源静默死了一年。
        logger.warning("query_anime_calendar 日历里没有 weekday.id=%d", today_id)
        return _failed("响应里没有今天这一天")

    items = today_block.get("items")
    if not isinstance(items, list):
        # ``or []`` 会把这里也变成"今天没番"。
        logger.warning("query_anime_calendar 今天这一格没有番剧列表")
        return _failed("响应缺少今天的番剧列表")

    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # name_cn 是 HTML 转义的，要 unescape；没有中文名退回原名。
        raw = item.get("name_cn") or item.get("name") or ""
        if not isinstance(raw, str):
            continue
        name = html.unescape(raw).strip()
        if name:
            names.append(name)

    if items and not names:
        # 有条目却一个番名都取不出来：字段名换了，不是今天没番。
        logger.warning("query_anime_calendar %d 条番剧一条都解析不出来", len(items))
        return _failed("番剧列表解析失败")

    weekday_cn = _weekday_field(today_block, "cn")
    if not isinstance(weekday_cn, str) or not weekday_cn.strip():
        weekday_cn = "今天"

    # 今天没有在更新的番剧仍是一次**成功**查询（空列表，不是失败）——agent 据此如实说
    # 今天没番，而不是把它当查询失败。
    return {"ok": True, "weekday": weekday_cn.strip(), "anime": names}


# ===========================================================================
# 节假日 —— timor
# ===========================================================================


@tool
@tool_error("节假日查询失败")
async def query_holiday() -> dict[str, Any]:
    """查询今天的节假日状态，返回结构化节假日数据。

    Returns:
        成功时返回 ``{"ok": True, "date": "2026-06-08", "weekday": "周日",
        "kind": "工作日"|"周末休息"|"法定节假日"|"周末调休补班", "holiday_name":
        "端午节"|None}``（``kind`` 是节假日类型标签，``holiday_name`` 仅法定节假
        日 / 调休补班时有值，否则为 ``None``）；查询失败时返回 ``{"ok": False,
        "reason": "..."}``。
    """
    today = now_cst().strftime("%Y-%m-%d")
    url = f"{_TIMOR_HOLIDAY_BASE}/{today}"

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("query_holiday connect error: %s", type(exc).__name__)
        return _failed(f"无法连接 timor({type(exc).__name__})")

    if resp.status_code != 200:
        logger.warning("query_holiday http %d", resp.status_code)
        return _failed(f"timor 返回 HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        logger.warning("query_holiday body not json")
        return _failed("响应解析失败")

    if not isinstance(data, dict):
        return _failed("响应结构异常")

    if data.get("code") != 0:
        logger.warning("query_holiday api code=%s", data.get("code"))
        return _failed(f"timor 返回错误码 {data.get('code')}")

    type_block = data.get("type")
    if not isinstance(type_block, dict):
        logger.warning("query_holiday 响应缺少 type 体")
        return _failed("响应缺少节假日类型")
    type_code = type_block.get("type")
    label = (
        _HOLIDAY_TYPE_LABEL.get(type_code) if isinstance(type_code, int) else None
    )
    if label is None:
        return _failed("响应缺少节假日类型")

    holiday = data.get("holiday")
    holiday_name = holiday.get("name") if isinstance(holiday, dict) else None

    result: dict[str, Any] = {
        "ok": True,
        "date": today,
        "kind": label,
        # 仅法定节假日 / 调休补班时 timor 才给 holiday.name；其余为 None。
        "holiday_name": holiday_name,
    }
    # 周几这一项拿不到就不给——``or ""`` 交出去的空串不是关于今天的事实，是一个洞
    # 披着事实的皮，而契约说好了绝不返回半截数据。
    weekday_name = type_block.get("name")
    if isinstance(weekday_name, str) and weekday_name.strip():
        result["weekday"] = weekday_name.strip()
    return result


# ===========================================================================
# 日出日落 —— 和风 QWeather astronomy
# ===========================================================================


def _qweather_local_hm(iso: str | None) -> str | None:
    """和风日出/日落形如 ``2026-06-08T05:41+08:00``，取本地 ``HH:MM``。

    和风返回的时刻自带 ``+08:00`` 偏移、就是当地时间，``T`` 后 5 个字符即
    ``HH:MM``。拿不到合法格式就返 ``None``，由调用处判为字段缺失降级。
    """
    if not isinstance(iso, str) or "T" not in iso:
        return None
    hm = iso.split("T", 1)[1][:5]
    # 形如 "05:41"——两位数:两位数才算合法。
    if len(hm) == 5 and hm[2] == ":":
        return hm
    return None


@tool
@tool_error("日出日落查询失败")
async def query_sun_times(city: CityArg) -> dict[str, Any]:
    """查一座城市今天的日出 / 日落时刻，返回结构化数据。

    Returns:
        成功时返回 ``{"ok": True, "matched": {...}, "sunrise": "05:41",
        "sunset": "19:12"}``（``sunrise`` / ``sunset`` 是当地 ``HH:MM``，从和风
        astronomy 返回的带 ``+08:00`` 偏移时刻里取出）。

        ``matched`` 是上游认下来的那座城市自己说的身份，含义和理由同
        :func:`query_weather`；歧义时另有 ``also_matched``。日落时刻在相邻两个县
        之间差几分钟、跨省差一小时，所以认错了地方交回来的仍是一个看着完全合理的
        数字——这份读数是谁的，必须由上游讲。

        查询失败时返回 ``{"ok": False, "reason": "..."}``（reason 不含任何密钥）。
    """
    api_key = settings.qweather_api_key
    if not api_key:
        return _failed("未配置和风天气 API Key")
    api_host = settings.qweather_api_host
    if not api_host:
        return _failed("未配置和风天气 API Host")
    wanted = city.strip()
    if not wanted:
        # 编一个日落时刻就是撒谎（见模块 docstring）。宁可这项今天没有。
        return _failed("没说是哪座城市")

    # date 是 CST 当天 YYYYMMDD（"今天"对赤尾是北京的今天）。
    today = now_cst().strftime("%Y%m%d")
    url = f"https://{api_host}/v7/astronomy/sun"
    # key 只走 header，绝不进 url query。
    headers = {"X-QW-Api-Key": api_key}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            place, why = await _resolve_location(
                client, api_host=api_host, headers=headers, city=wanted
            )
            if place is None:
                return _failed(why)
            resp = await client.get(
                url,
                params={"location": place.id, "date": today},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("query_sun_times connect error: %s", type(exc).__name__)
        return _failed(f"无法连接和风天气({type(exc).__name__})")

    if resp.status_code != 200:
        logger.warning("query_sun_times http %d", resp.status_code)
        return _failed(_qweather_http_reason("和风天气", resp))

    try:
        data = resp.json()
    except ValueError:
        logger.warning("query_sun_times body not json")
        return _failed("响应解析失败")

    if not isinstance(data, dict):
        return _failed("响应结构异常")

    if data.get("code") != "200":
        logger.warning("query_sun_times api code=%s", data.get("code"))
        return _failed(f"和风天气返回错误码 {data.get('code')}")

    sunrise = _qweather_local_hm(data.get("sunrise"))
    sunset = _qweather_local_hm(data.get("sunset"))
    if not sunrise or not sunset:
        return _failed("响应缺少日出日落字段")

    result: dict[str, Any] = {
        "ok": True,
        # 这两个时刻是谁的，由上游讲。
        "matched": place.identity,
        "sunrise": sunrise,
        "sunset": sunset,
    }
    if place.others:
        result["also_matched"] = list(place.others)
    return result


# ===========================================================================
# 节气农历 —— cnlunar 本地天文计算（无网络）
# ===========================================================================


@tool
@tool_error("节气农历查询失败")
async def query_lunar_term() -> dict[str, Any]:
    """计算今天的农历日期 + 节气，返回结构化时令数据（本地算，不走网络）。

    用 :mod:`cnlunar` 对今天（CST）做确定性农历 / 节气天文计算——给世界引擎一份
    "现在是农历几月几、生肖年、今天是否某节气、临近哪个节气"的时令底料。

    Returns:
        成功时返回 ``{"ok": True, "lunar_date": "四月廿三", "zodiac_year":
        "丙午马年", "solar_term": "夏至"|None, "next_solar_term": "夏至",
        "days_to_next_term": 13}``——``solar_term`` 仅当**今天正好是**某个节气时
        有值（否则 ``None``），``next_solar_term`` / ``days_to_next_term`` 是临近
        的下一个节气名和还差几天；本地计算失败时返回
        ``{"ok": False, "reason": "..."}``。
    """
    today = now_cst()
    # now_cst() 是 CST aware datetime；cnlunar 内部对 date 做 naive 减法、喂 aware
    # 会 TypeError。剥掉 tzinfo 拿"当地的那一天"——civil 日期正是农历 / 节气
    # 计算要的口径。
    civil = today.replace(tzinfo=None)
    lunar = cnlunar.Lunar(civil, godType="8char")

    # 农历月日去掉"大/小"月标记，留干净的"四月廿三"。
    lunar_month = (lunar.lunarMonthCn or "").rstrip("大小")
    lunar_day = lunar.lunarDayCn or ""
    if not lunar_month or not lunar_day:
        # 两个 ``or ""`` 拼得出一个空的"农历日期"，而它会顶着 ``ok=True`` 出去——跟那
        # 两条静默死了一年的源同一个形状，只是这次在本地算。
        logger.warning("query_lunar_term 农历日期算空了")
        return _failed("农历日期算不出来")
    lunar_date = f"{lunar_month}{lunar_day}"

    # 生肖年：干支 + 生肖（丙午马年）。
    zodiac_year = f"{lunar.year8Char}{lunar.chineseYearZodiac}年"

    # todaySolarTerms 为 "无" 表示今天不是节气日；否则就是今天的节气名。
    today_term = lunar.todaySolarTerms
    solar_term = today_term if today_term and today_term != "无" else None

    # 临近的下一个节气 + 还差几天（nextSolarTermDate 是 (月, 日)）。
    next_term = lunar.nextSolarTerm
    next_date = date(lunar.nextSolarTermYear, *lunar.nextSolarTermDate)
    days_to_next = (next_date - civil.date()).days

    return {
        "ok": True,
        "lunar_date": lunar_date,
        "zodiac_year": zodiac_year,
        "solar_term": solar_term,
        "next_solar_term": next_term,
        "days_to_next_term": days_to_next,
    }


# ===========================================================================
# 本市活动 —— Bilibili 会员购票务列表
# ===========================================================================


def _bili_data(resp: httpx.Response, what: str) -> tuple[dict[str, Any] | None, str]:
    """把会员购的一次响应剥成 ``data`` 体；不对劲就给人话原因。"""
    if resp.status_code != 200:
        logger.warning("query_city_events %s http %d", what, resp.status_code)
        return None, f"Bilibili 会员购返回 HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except ValueError:
        logger.warning("query_city_events %s body not json", what)
        return None, "响应解析失败"
    if not isinstance(payload, dict):
        return None, "响应结构异常"
    # 业务码 0 才算成功。参数不合上游口味时它回 200 + code 81102084。
    if payload.get("code") != 0:
        logger.warning("query_city_events %s api code=%s", what, payload.get("code"))
        return None, f"Bilibili 会员购返回错误码 {payload.get('code')}"
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, "响应缺少数据体"
    return data, ""


def _city_candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    """把会员购当场返的城市表摊平成一串候选。

    读不出任何一条就是空列表，而**空列表是结构问题、不是"这座城市不在表里"**：调用处
    要靠这个区分开来，否则一份没读懂的响应会变成一句关于上游覆盖范围的肯定句。
    """
    candidates: list[dict[str, Any]] = []
    hot = data.get("hot")
    if isinstance(hot, list):
        candidates.extend(c for c in hot if isinstance(c, dict))
    blocks = data.get("list")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            city_list = block.get("city_list")
            if isinstance(city_list, list):
                candidates.extend(c for c in city_list if isinstance(c, dict))
    return candidates


def _pick_area_id(candidates: list[dict[str, Any]], city: str) -> int | None:
    """在城市表的候选里按名字找 ``area`` 编码（GB/T 2260 行政区划码）。

    表是**当场拉的**，代码里不存市名→编码的对照表：那种表会过期，而过期的后果是
    静默查错城市。``type`` 2 是市、1 是省，同名先认市——"吉林"既是吉林省也是吉林市，
    落到省级会把整省的东西都端过来。

    这里比的是**完全相等**（``name`` 或 ``fullname``），不是模糊搜索，所以匹配上就是
    匹配上了，没有和风那一跳的"可能认到别处去"的问题。
    """
    for city_only in (True, False):
        for cand in candidates:
            if city_only and cand.get("type") != 2:
                continue
            if city in (cand.get("name"), cand.get("fullname")):
                area_id = cand.get("id")
                if isinstance(area_id, int):
                    return area_id
    return None


# 上游的 project_name 真的会带零宽字符（实测「广州·阴阳师十周年…」两头各裹一个
# U+200B）。``str.strip()`` 管不了它们——Python 不把零宽算空白——于是这些看不见的
# 东西会原样进她的上下文，也会让任何按名字比对的地方对不上。
# 写成转义而不是字面量：字面量在编辑器和 diff 里都是空的，改坏了没人看得出来。
_INVISIBLE = "\u200b\u200c\u200d\ufeff"


def _clean_name(raw: object) -> str:
    return str(raw or "").strip().strip(_INVISIBLE).strip()


def _event_day(raw: object) -> date | None:
    """会员购的 ``start_time`` / ``end_time`` 是 ``YYYY-MM-DD``（偶尔带时刻）。"""
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _parse_event(item: object) -> dict[str, Any] | None:
    """把上游一条记录读成一件活动；读不成返回 ``None``，由调用处数个数。

    "读不成"要能被数出来才有用：一条读不出来是脏数据，**整页都读不出来是字段名换了**，
    而那两件事不能返回同一个结果。日期在这里还是 :class:`date`，时间窗过滤完才转字符串。
    """
    if not isinstance(item, dict):
        return None
    name = _clean_name(item.get("project_name"))
    if not name:
        return None
    start = _event_day(item.get("start_time"))
    if start is None:
        return None
    return {
        "name": name,
        "kind": item.get("third_category_name") or None,
        "venue": item.get("venue_name") or None,
        "district": item.get("district_name") or None,
        "start": start,
        # 单日活动的 end_time 跟 start_time 相同；缺了就按单日算。
        "end": _event_day(item.get("end_time")) or start,
    }


@tool
@tool_error("本市活动查询失败")
async def query_city_events(city: CityArg) -> dict[str, Any]:
    """查一座城市接下来两周的漫展 / 演出 / 展览 / 主题店，返回结构化活动列表。

    数据来自 Bilibili 会员购的公开票务列表：真实的、有票在卖的、在那座城市的活动
    ——所以"约人一起去"才有东西可约。市名由会员购当场返回的城市表换成它的 ``area``
    编码，代码里不存对照表。

    **这不是全城活动的全集**：上游那一页是推荐位，只收它自己在卖的票，展馆自办的
    免费展览、影院排片都不在里面。可以说"看到有个 X"，不能说"这两周就这些"。

    Returns:
        成功时返回 ``{"ok": True, "city": <问的那座城市>, "days": 14, "listed":
        6, "events": [{"name": ..., "kind": ..., "venue": ..., "district": ...,
        "start": "2026-10-02", "end": "2026-10-05"}, ...]}``（字段值原样来自上游，
        ``events`` 只留跟今天起 14 天有交集的）；查询失败时返回
        ``{"ok": False, "reason": "..."}``。

        ``listed`` 是上游那一页一共列了几件（**不看时间窗**），用来把两件不同的事分
        开说：``listed`` 为 0 是"这座城市现在一件在卖的都没有"，``listed`` 不为 0 而
        ``events`` 是空的是"有，但都不在这两周"。两者都是查询成功。

        读不懂响应是第三件事，走 ``ok=False``：没有 ``result`` 这一项、``result``
        不是列表、整页活动一条都解析不出来，都不会伪装成"城里没事"。
    """
    city = city.strip()
    if not city:
        return _failed("没说是哪座城市")

    today = now_cst().date()
    until = today + timedelta(days=EVENT_WINDOW_DAYS)

    try:
        # 国内域名，不走 forward_proxy（只有 Bangumi 需要，见 query_anime_calendar）。
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT, headers={"User-Agent": BILI_USER_AGENT}
        ) as client:
            city_resp = await client.get(
                _BILI_CITY_LIST_URL, params={"channel": _BILI_CITY_CHANNEL}
            )
            city_data, why = _bili_data(city_resp, "city/list")
            if city_data is None:
                return _failed(why)

            candidates = _city_candidates(city_data)
            if not candidates:
                # 表里一条城市都读不出来 ≠ 表里没有这座城市。后者是一句关于上游覆盖
                # 范围的肯定句，不能拿一份没读懂的响应去下。
                logger.warning("query_city_events 城市表读不出任何候选")
                return _failed("响应缺少城市表")

            area_id = _pick_area_id(candidates, city)
            if area_id is None:
                # 跟"这两周没活动"是两件事：这是根本没问对城市，不能让她当成城里没事。
                logger.warning("query_city_events 城市表里没有 %r", city)
                return _failed(f"Bilibili 会员购没有「{city}」这座城市")

            list_resp = await client.get(
                _BILI_PROJECT_LIST_URL,
                params={
                    "version": _BILI_LIST_VERSION,
                    "page": "1",
                    "pagesize": str(_BILI_PAGE_SIZE),
                    "area": str(area_id),
                    "platform": "web",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("query_city_events connect error: %s", type(exc).__name__)
        return _failed(f"无法连接 Bilibili 会员购({type(exc).__name__})")

    list_data, why = _bili_data(list_resp, "project/listV2")
    if list_data is None:
        return _failed(why)

    listing = list_data.get("result")
    if not isinstance(listing, list):
        # ``get("result") or []`` 会把这里变成"这两周城里没事"——一句关于世界的肯定
        # 句，而它其实是"这份响应我们读不懂"。两者在返回值里长得一模一样，谁也分不
        # 出来：这个仓库正是这个形状让两条源静默死了一年。
        logger.warning("query_city_events 响应里没有活动列表")
        return _failed("响应缺少活动列表")

    parsed = [
        event for event in (_parse_event(item) for item in listing) if event
    ]
    if listing and not parsed:
        # 有一整页却一条都读不出来：字段名换了，不是城里没事。
        logger.warning(
            "query_city_events %d 条活动一条都解析不出来", len(listing)
        )
        return _failed("活动列表解析失败")
    if len(parsed) < len(listing):
        # 部分读不出来仍算成功（脏数据是常态），但要留下痕迹——不然"上游悄悄改了一半
        # 字段"和"上游这页本来就杂"永远分不开。
        logger.warning(
            "query_city_events 丢掉 %d/%d 条读不出来的活动",
            len(listing) - len(parsed),
            len(listing),
        )

    events = [
        {**event, "start": event["start"].isoformat(), "end": event["end"].isoformat()}
        for event in parsed
        # 跟 [今天, 今天+窗口] 有交集才算"这几天有"：已经开场还没结束的要留住，
        # 结束了的和还早得很的都不要。
        if not (event["start"] > until or event["end"] < today)
    ]

    # 这两周城里没事仍是一次**成功**查询（空列表，不是失败）。``listed`` 让调用方分得
    # 清"上游这页一件都没有"和"有，但都不在这两周"。
    return {
        "ok": True,
        "city": city,
        "days": EVENT_WINDOW_DAYS,
        "listed": len(parsed),
        "events": events,
    }
