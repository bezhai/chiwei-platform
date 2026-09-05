"""她手上那四只跟图有关的手：画一张、上网找一张、翻一翻手上有什么、拿出其中一张看。

五条硬边界，各有对应的用例：

  * **跨得过一缝的边界。** 这是整件事的理由：她那一缝只拿到快照和手机信封，
    **不继承上一缝的工具结果**（``moment.py`` 的 ``run_moment``），她自己近期做过的
    事也只有有限几条（``snapshot.OWN_RECENT_LIMIT``）。所以句柄只出现在画图那一刻的
    返回值里的话，下一缝它就永远消失了 —— 表是满的，而她两手空空。**这条用例里，第
    二缝一个句柄都不预置，只能靠"翻一翻"那只手把它找回来。**
  * **"看某一张"给的是图本身，不是一段描述。** 图片块要一路活到模型的 wire 上：
    OpenAI 那边是 ``image_url`` part，Gemini 那边是 ``inline_data``。退化成文本的
    话，她"看"到的只是自己当初说的那句话 —— 那不叫看。
  * **上传没落地就不记。** 记下一个根本不在对象存储里的句柄，她下一缝会拿着它反复去
    取一张不存在的图，而且一句报错都没有。
  * **认不准是哪一张就回问，不替她挑。** 跟 ``read_a_bit`` 同一条：一个说法对上好
    几张时摊开让她指，绝不排个序取第一个 —— 发错一张图比发不出去更糟。
  * **别人的图她够不到。** 这张表三个人共用、还跨泳道，只按句柄找的话一个从别处拿到
    的句柄就能取到姐姐画的那张，然后被当成自己的发出去。
"""
from __future__ import annotations

import datetime as dt
import re

import pytest

from app.living import pictures as pictures_mod
from app.living.pictures import (
    PICTURE_LIST_LIMIT,
    PICTURE_SEARCH_COUNT,
    PICTURE_TOOLS,
    Picture,
    draw_a_picture,
    find_a_picture_online,
    handle_for,
    her_picture,
    look_at_a_picture,
    look_through_your_pictures,
    pictures_she_made,
    remember_a_picture,
)

LANE = "coe-living"
_CST = dt.timezone(dt.timedelta(hours=8))

# 她那一缝的钟点（``in_a_moment`` 默认就绑这个时刻）。
_NOW = dt.datetime(2026, 7, 25, 21, 30, tzinfo=_CST)

# 一张一像素的真 png，base64 编码 —— 生图那一层交回来的就是这种 data URI。
_A_TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 7, 25, hour, minute, tzinfo=_CST)


@pytest.fixture
async def pictures_db(real_pg_required, test_db):
    """只建她那张表 —— 这四只手不碰会话、不碰手机、不碰任何别人的表。"""
    from tests.runtime.conftest import migrate

    await migrate(Picture, test_db)
    yield test_db


# ---------------------------------------------------------------------------
# 替身：生图 / 搜图 / 传对象存储 / 现签，四样都在她这个模块的名字上换掉
# ---------------------------------------------------------------------------


class _Storage:
    """一个假的对象存储：传进来什么就发一个永久句柄，签名每次都新签一个。

    **句柄和地址是两样东西**，这个替身刻意把它们做得一眼分得开：句柄长
    ``stored/<n>``，签出来的地址长 ``https://signed.example/<句柄>?t=<第几次签的>``。
    用例据此能验"存下来的是句柄、给她看的是现签的地址"。
    """

    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []
        self.signed: list[str] = []

    async def upload(self, source_type: str, data: str):
        from app.infra.image import StoredImage

        self.uploaded.append((source_type, data))
        file_name = f"stored/{len(self.uploaded)}"
        return StoredImage(file_name=file_name, url=f"{self._sign(file_name)}")

    async def get_url(self, file_name: str) -> str | None:
        return self._sign(file_name)

    def _sign(self, file_name: str) -> str:
        self.signed.append(file_name)
        return f"https://signed.example/{file_name}?t={len(self.signed)}"


@pytest.fixture
def her_hands(monkeypatch):
    """把生图、搜图、上传、现签四样换成替身，返回可查证的那份记录。

    换在 **她这个模块引到的名字**上（module-level 引入就是为了这个），所以工具体到
    capability 边界为止的每一行都是真的跑过的。
    """

    class Hands:
        def __init__(self) -> None:
            self.storage = _Storage()
            self.drawn: list[tuple[str, str]] = []   # (model_id, prompt)
            self.searched: list[tuple[str, int]] = []
            self.draws: list[list[str]] = []          # 每次生图交回来的那批
            self.hits: list = []
            self.upload_fails = False

        async def generate_image(self, model_id, *, prompt, size, **_kw):
            self.drawn.append((model_id, prompt))
            if not self.draws:
                raise RuntimeError("这个模型这会儿不出图")
            return self.draws.pop(0)

        async def image_search(self, query, *, count=5):
            self.searched.append((query, count))
            return self.hits[:count]

        async def upload_image(self, source_type, data):
            if self.upload_fails:
                return None
            return await self.storage.upload(source_type, data)

    hands = Hands()
    monkeypatch.setattr(pictures_mod, "generate_image", hands.generate_image)
    monkeypatch.setattr(pictures_mod, "image_search", hands.image_search)
    monkeypatch.setattr(pictures_mod, "upload_image", hands.upload_image)
    monkeypatch.setattr(pictures_mod, "image_client", hands.storage)
    return hands


def _blocks(result) -> list[dict]:
    assert isinstance(result, list), f"这只手交回来的不是内容块，是 {result!r}"
    return result


def _texts(result) -> str:
    return "\n".join(b.get("text", "") for b in _blocks(result) if b["type"] == "text")


def _image_urls(result) -> list[str]:
    return [
        b["image_url"]["url"] for b in _blocks(result) if b["type"] == "image_url"
    ]


def _handles(text: str) -> list[str]:
    return re.findall(r"pic=([0-9a-f]{32})", text)


# ---------------------------------------------------------------------------
# 一 · 画一张
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_picture_she_drew_lands_in_her_record(
    pictures_db, her_hands, in_a_moment
):
    """画出来的那张进她自己的记录，记的是**永久句柄**，不是那个会死的地址。"""
    her_hands.draws = [[_A_TINY_PNG]]

    async with in_a_moment("akao"):
        result = await draw_a_picture.invoke(
            {"what": "一只在窗台上晒太阳的猫"}
        )

    mine = await pictures_she_made(lane=LANE, persona_id="akao")
    assert [p.file_name for p in mine] == ["stored/1"]
    assert mine[0].what == "一只在窗台上晒太阳的猫"
    assert mine[0].made_at == _NOW
    assert mine[0].picture_id == handle_for("stored/1")
    # 她当场就拿到了那串句柄（这一缝里就想发出去的话用得上）。
    assert _handles(_texts(result)) == [handle_for("stored/1")]
    # 库里那一列绝不能是签出来的地址：它 1.5 小时就死，而且死得静默。
    assert "signed.example" not in mine[0].file_name


@pytest.mark.integration
async def test_what_she_drew_comes_back_as_the_picture_itself(
    pictures_db, her_hands, in_a_moment
):
    """画完她看得见画出来的是什么 —— 交回去的是图，不是"已生成"四个字。"""
    her_hands.draws = [[_A_TINY_PNG]]

    async with in_a_moment("akao"):
        result = await draw_a_picture.invoke({"what": "一只猫"})

    assert len(_image_urls(result)) == 1


@pytest.mark.integration
async def test_a_picture_that_never_reached_storage_is_not_remembered(
    pictures_db, her_hands, in_a_moment
):
    """传不上去就不记 —— 记下一个不存在的句柄，她会拿着它反复取一张取不到的图。"""
    her_hands.draws = [[_A_TINY_PNG]]
    her_hands.upload_fails = True

    async with in_a_moment("akao"):
        result = await draw_a_picture.invoke({"what": "一只猫"})

    assert await pictures_she_made(lane=LANE, persona_id="akao") == []
    assert isinstance(result, dict) and result["kind"] == "tool_error", (
        f"传不上去这件事没如实告诉她：{result!r}"
    )


@pytest.mark.integration
async def test_the_other_model_gets_a_turn_when_the_first_one_is_down(
    pictures_db, her_hands, in_a_moment, monkeypatch
):
    """头一个模型不出图时换第二个 —— 一个供应商抽风不该让她这一整天都画不了。"""
    calls = {"n": 0}

    async def flaky(model_id, *, prompt, size, **_kw):
        calls["n"] += 1
        her_hands.drawn.append((model_id, prompt))
        if calls["n"] == 1:
            raise RuntimeError("这个模型这会儿不出图")
        return [_A_TINY_PNG]

    monkeypatch.setattr(pictures_mod, "generate_image", flaky)

    async with in_a_moment("akao"):
        result = await draw_a_picture.invoke({"what": "一只猫"})

    assert len(her_hands.drawn) == 2
    assert her_hands.drawn[0][0] != her_hands.drawn[1][0], "两次用的是同一个模型"
    assert len(_image_urls(result)) == 1


@pytest.mark.integration
async def test_when_no_model_will_draw_she_is_told_instead_of_getting_an_empty_hand(
    pictures_db, her_hands, in_a_moment
):
    """两个模型都不出图时如实说这一趟没画成，绝不交回一句"生成了"。"""
    her_hands.draws = []  # 每次调用都抛

    async with in_a_moment("akao"):
        result = await draw_a_picture.invoke({"what": "一只猫"})

    assert isinstance(result, dict) and result["kind"] == "tool_error"
    assert await pictures_she_made(lane=LANE, persona_id="akao") == []


@pytest.mark.integration
async def test_she_is_never_asked_to_draw_nothing(
    pictures_db, her_hands, in_a_moment
):
    async with in_a_moment("akao"):
        result = await draw_a_picture.invoke({"what": "   "})
    assert isinstance(result, dict) and result["kind"] == "tool_error"
    assert her_hands.drawn == []


# ---------------------------------------------------------------------------
# 二 · 上网找一张
# ---------------------------------------------------------------------------


def _hit(title: str, n: int):
    from app.capabilities.image_search import ImageHit

    return ImageHit(
        image_url=f"https://pic.example/{n}.jpg",
        title=title,
        source_url=f"https://page.example/{n}",
    )


@pytest.mark.integration
async def test_every_picture_she_was_shown_gets_a_handle_she_can_use_later(
    pictures_db, her_hands, in_a_moment
):
    """摆到她眼前的每一张都进她的记录 —— 看得见却发不出去是这件事最典型的坏掉方式。"""
    her_hands.hits = [_hit("窗台上的猫", 1), _hit("打哈欠的猫", 2), _hit("胖猫", 3)]

    async with in_a_moment("akao"):
        result = await find_a_picture_online.invoke({"what": "猫"})

    mine = await pictures_she_made(lane=LANE, persona_id="akao")
    assert len(mine) == PICTURE_SEARCH_COUNT
    assert {p.what for p in mine} == {"窗台上的猫", "打哈欠的猫", "胖猫"}
    # 每一张都带着自己那串句柄，而且图本身也在她眼前。
    assert sorted(_handles(_texts(result))) == sorted(p.picture_id for p in mine)
    assert len(_image_urls(result)) == PICTURE_SEARCH_COUNT
    # 传的是网上那个地址，交给对象存储自己去下载。
    assert [t for t, _ in her_hands.storage.uploaded] == ["url"] * 3
    assert [d for _, d in her_hands.storage.uploaded] == [
        "https://pic.example/1.jpg",
        "https://pic.example/2.jpg",
        "https://pic.example/3.jpg",
    ]


@pytest.mark.integration
async def test_her_own_words_are_what_she_searched_with(
    pictures_db, her_hands, in_a_moment
):
    """她写的那句原样当检索词，不替她改写（跟 ``search_online`` 同一条）。"""
    her_hands.hits = [_hit("猫", 1)]

    async with in_a_moment("akao"):
        await find_a_picture_online.invoke({"what": "毛茸茸的猫 表情包"})

    assert her_hands.searched == [("毛茸茸的猫 表情包", PICTURE_SEARCH_COUNT)]


@pytest.mark.integration
async def test_a_search_that_came_back_with_nothing_says_so(
    pictures_db, her_hands, in_a_moment
):
    """一张都没找回来时如实说，绝不说成"网上没有这种图"。"""
    her_hands.hits = []

    async with in_a_moment("akao"):
        result = await find_a_picture_online.invoke({"what": "猫"})

    assert isinstance(result, dict) and result["kind"] == "tool_error"
    assert await pictures_she_made(lane=LANE, persona_id="akao") == []


@pytest.mark.integration
async def test_one_picture_failing_to_store_does_not_lose_the_others(
    pictures_db, her_hands, in_a_moment, monkeypatch
):
    """一张传不上去，剩下那几张照样进她的记录 —— 一张坏不掉整趟。"""
    her_hands.hits = [_hit("猫甲", 1), _hit("猫乙", 2)]
    real_upload = her_hands.upload_image
    n = {"i": 0}

    async def flaky(source_type, data):
        n["i"] += 1
        return None if n["i"] == 1 else await real_upload(source_type, data)

    monkeypatch.setattr(pictures_mod, "upload_image", flaky)

    async with in_a_moment("akao"):
        result = await find_a_picture_online.invoke({"what": "猫"})

    mine = await pictures_she_made(lane=LANE, persona_id="akao")
    assert [p.what for p in mine] == ["猫乙"]
    assert len(_image_urls(result)) == 1


# ---------------------------------------------------------------------------
# 三 · 翻一翻手上有什么
# ---------------------------------------------------------------------------


async def _she_has(what: str, file_name: str, at: dt.datetime, who: str = "akao"):
    return await remember_a_picture(
        lane=LANE, persona_id=who, file_name=file_name, what=what, made_at=at
    )


@pytest.mark.integration
async def test_her_pictures_are_listed_newest_first_each_with_a_handle(
    pictures_db, her_hands, in_a_moment
):
    await _she_has("最早那张", "stored/a", _at(20))
    await _she_has("中间那张", "stored/b", _at(21))
    await _she_has("刚做的那张", "stored/c", _at(21, 20))

    async with in_a_moment("akao"):
        listed = await look_through_your_pictures.invoke({})

    assert isinstance(listed, str)
    assert listed.index("刚做的那张") < listed.index("中间那张") < listed.index("最早那张")
    assert _handles(listed) == [
        handle_for("stored/c"),
        handle_for("stored/b"),
        handle_for("stored/a"),
    ]


@pytest.mark.integration
async def test_an_empty_record_says_it_is_empty(pictures_db, her_hands, in_a_moment):
    async with in_a_moment("akao"):
        listed = await look_through_your_pictures.invoke({})
    assert isinstance(listed, str)
    assert _handles(listed) == []


@pytest.mark.integration
async def test_the_list_stops_at_the_limit_and_says_there_are_more(
    pictures_db, her_hands, in_a_moment
):
    """清单是"一屏摆得下多少"，不是"她只剩这几张" —— 挤下去的要如实说还有。"""
    for i in range(PICTURE_LIST_LIMIT + 3):
        await _she_has(f"第 {i} 张", f"stored/{i}", _at(20, i))

    async with in_a_moment("akao"):
        listed = await look_through_your_pictures.invoke({})

    assert len(_handles(listed)) == PICTURE_LIST_LIMIT
    # 剩下几张如实报出个数 —— 只说"还有一些"的话，她无从判断值不值得再报个名字去指。
    assert "还有 3 张" in listed


@pytest.mark.integration
async def test_her_sisters_pictures_are_not_on_her_list(
    pictures_db, her_hands, in_a_moment
):
    await _she_has("绫奈那张", "stored/ayana", _at(21), who="ayana")
    await _she_has("她自己那张", "stored/akao", _at(21))

    async with in_a_moment("akao"):
        listed = await look_through_your_pictures.invoke({})

    assert "绫奈那张" not in listed
    assert "她自己那张" in listed


# ---------------------------------------------------------------------------
# 四 · 拿出其中一张看
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_looking_at_one_by_its_handle_gives_back_the_picture_itself(
    pictures_db, her_hands, in_a_moment
):
    """"看"给的必须是图本身。退化成一段描述的话，她看到的只是自己当初说的那句话。"""
    made = await _she_has("一只在窗台上晒太阳的猫", "stored/cat", _at(21))

    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": f"pic={made.picture_id}"})

    assert _image_urls(result) == ["https://signed.example/stored/cat?t=1"]
    assert "一只在窗台上晒太阳的猫" in _texts(result)


@pytest.mark.integration
async def test_the_address_is_signed_fresh_every_single_time(
    pictures_db, her_hands, in_a_moment
):
    """签名 1.5 小时就死，所以每次看都现签一个 —— 存下来的那份永远是句柄。"""
    made = await _she_has("一只猫", "stored/cat", _at(21))

    async with in_a_moment("akao"):
        first = await look_at_a_picture.invoke({"which": made.picture_id})
        second = await look_at_a_picture.invoke({"which": made.picture_id})

    assert _image_urls(first) != _image_urls(second)
    assert her_hands.storage.signed == ["stored/cat", "stored/cat"]


@pytest.mark.integration
async def test_she_can_point_at_one_by_what_it_is(
    pictures_db, her_hands, in_a_moment
):
    """报那句话也指得动 —— 她不该被逼着记住一串十六进制。"""
    await _she_has("一只在窗台上晒太阳的猫", "stored/cat", _at(21))
    await _she_has("下雨的电车站台", "stored/rain", _at(20))

    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": "电车"})

    assert _image_urls(result) == ["https://signed.example/stored/rain?t=1"]


@pytest.mark.integration
async def test_a_name_that_matches_several_makes_her_pick_instead_of_guessing(
    pictures_db, her_hands, in_a_moment
):
    """对上好几张就摊开让她指，**绝不排个序取第一个** —— 发错一张比发不出去更糟。"""
    a = await _she_has("窗台上的猫", "stored/a", _at(21))
    b = await _she_has("打哈欠的猫", "stored/b", _at(20))

    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": "猫"})

    assert isinstance(result, dict) and result["kind"] == "tool_error"
    # 候选连着句柄一起摊开，她照抄就能指准 —— 回问必须是指得动的。
    assert a.picture_id in result["message"]
    assert b.picture_id in result["message"]
    assert her_hands.storage.signed == [], "对不准就不该先签一个地址出来"


@pytest.mark.integration
async def test_a_picture_she_never_made_is_just_not_there(
    pictures_db, her_hands, in_a_moment
):
    await _she_has("一只猫", "stored/cat", _at(21))

    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": "一辆自行车"})

    assert isinstance(result, dict) and result["kind"] == "tool_error"


@pytest.mark.integration
async def test_a_picture_the_storage_will_not_sign_says_so(
    pictures_db, her_hands, in_a_moment
):
    """签不出地址就如实说，绝不交回一个她看不了的空壳。"""
    made = await _she_has("一只猫", "stored/cat", _at(21))

    async def refuses(_file_name):
        return None

    her_hands.storage.get_url = refuses  # type: ignore[assignment]

    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": made.picture_id})

    assert isinstance(result, dict) and result["kind"] == "tool_error"


@pytest.mark.integration
async def test_a_name_reaches_past_the_list_limit(
    pictures_db, her_hands, in_a_moment
):
    """清单有上限，报名字那条路没有 —— 被挤下去的那些照样指得到。"""
    for i in range(PICTURE_LIST_LIMIT + 3):
        await _she_has(f"第 {i} 张", f"stored/{i}", _at(20, i + 1))
    await _she_has("很久以前那张海边的夕阳", "stored/old", _at(6))

    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": "海边的夕阳"})

    assert _image_urls(result) == ["https://signed.example/stored/old?t=1"]


# ---------------------------------------------------------------------------
# 五 · 别人的图她够不到
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_handle_that_belongs_to_her_sister_gets_her_nothing(
    pictures_db, her_hands, in_a_moment
):
    """句柄从 ``file_name`` 派生，所以姐妹俩那两条的句柄字面相同 —— 挡下它的必须是
    ``(lane, persona_id)``，不能指望句柄本身撞不上。"""
    sisters = await _she_has("绫奈画的猫", "stored/cat", _at(21), who="ayana")

    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": f"pic={sisters.picture_id}"})

    assert isinstance(result, dict) and result["kind"] == "tool_error"
    assert her_hands.storage.signed == [], "姐姐那张图被签出了一个能下载的地址"


@pytest.mark.integration
async def test_a_handle_from_another_lane_gets_her_nothing(
    pictures_db, her_hands, in_a_moment
):
    """泳道隔离是硬约束：prod 那条轴上的图不该在这条泳道被取到。"""
    elsewhere = await remember_a_picture(
        lane="prod",
        persona_id="akao",
        file_name="stored/prod-cat",
        what="prod 那条轴上的猫",
        made_at=_at(21),
    )

    async with in_a_moment("akao"):
        by_handle = await look_at_a_picture.invoke(
            {"which": f"pic={elsewhere.picture_id}"}
        )
        by_name = await look_at_a_picture.invoke({"which": "prod 那条轴上的猫"})
        listed = await look_through_your_pictures.invoke({})

    assert isinstance(by_handle, dict) and by_handle["kind"] == "tool_error"
    assert isinstance(by_name, dict) and by_name["kind"] == "tool_error"
    assert "prod 那条轴上的猫" not in listed
    assert her_hands.storage.signed == []


# ---------------------------------------------------------------------------
# 六 · 跨得过一缝的边界 —— 这条是整件事的理由
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_she_finds_a_picture_from_an_earlier_moment_with_nothing_handed_to_her(
    pictures_db, her_hands, in_a_moment
):
    """一缝画图 → 另一缝在**不预置任何句柄**的前提下找到它、看它、拿到可发送的引用。

    这是 D2 那条决策的整个理由：她那一缝只拿到快照和手机信封，**不继承上一缝的工具
    结果**；她自己近期做过的事也只有有限几条。所以句柄只出现在画图那一刻的返回值里
    的话，下一缝它就永远消失了 —— 库里那一行还在，而她两手空空。

    这条用例刻意把那次返回值**整个扔掉**：第二缝唯一的输入就是"翻一翻"那只手交回来
    的那段文字，句柄只能从那里面读出来。
    """
    her_hands.draws = [[_A_TINY_PNG]]

    # ---- 上一缝：她画了一张。这一缝结束时，返回值里的一切都不再存在。 ----
    async with in_a_moment("akao", moment_id="2026-07-25T21:30+08:00"):
        await draw_a_picture.invoke({"what": "一只在窗台上晒太阳的猫"})

    # ---- 下一缝：新的 moment_id，没有人递给她任何东西。 ----
    async with in_a_moment(
        "akao",
        now=dt.datetime(2026, 7, 25, 21, 40, tzinfo=_CST),
        moment_id="2026-07-25T21:40+08:00",
    ):
        listed = await look_through_your_pictures.invoke({})
        found = _handles(listed)
        assert found, (
            "下一缝她翻遍手上什么都没有 —— 表是满的，而她两手空空。"
            f"翻出来的是：{listed!r}"
        )
        # 她能读到的只有这段文字里那一串；下面每一步都只用它。
        handle = found[0]
        seen = await look_at_a_picture.invoke({"which": f"pic={handle}"})

    # 看到的是图本身。
    assert len(_image_urls(seen)) == 1
    assert "一只在窗台上晒太阳的猫" in _texts(seen)

    # 而且这串句柄换得到**可发送的引用** —— 对象存储那边的永久句柄，
    # 她开口那一刻拿它现签就能把图发出去。
    got = await her_picture(lane=LANE, persona_id="akao", picture_id=handle)
    assert got is not None
    assert got.file_name == "stored/1"


# ---------------------------------------------------------------------------
# 七 · 挂进她的工具带了没有
# ---------------------------------------------------------------------------


def test_all_four_hands_are_actually_in_her_tool_belt():
    """模块写好了没挂进 ``MOMENT_TOOLS`` 是这类改动最典型的静默失败：

    工具都在、测试都绿，而她那一缝的工具列表里一个都没有 —— 她从来没有过这四只手，
    却没有任何东西会因此报错。
    """
    from app.living.moment import MOMENT_TOOLS

    mounted = {t.name for t in MOMENT_TOOLS}
    for hand in (
        "draw_a_picture",
        "find_a_picture_online",
        "look_through_your_pictures",
        "look_at_a_picture",
    ):
        assert hand in mounted, f"{hand} 没挂进 MOMENT_TOOLS，她根本没有这只手"
    assert {t.name for t in PICTURE_TOOLS} <= mounted


def test_her_instruction_manual_is_written_for_her():
    """工具的 docstring **就是**她看到的说明书（``tooling.py`` 整段逐字取）。

    空 docstring = 她拿到一只不知道干什么用的手。
    """
    for t in PICTURE_TOOLS:
        assert t.definition.description.strip(), f"{t.name} 没有给她的说明"


# ---------------------------------------------------------------------------
# 八 · 图片真的走到了模型那一侧，不是在半路退化成文本
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_picture_survives_the_trip_into_the_model_transcript(
    pictures_db, her_hands, in_a_moment
):
    """从工具返回值到喂进模型的那条 tool 消息，图片块一路活着。

    ``core._normalise_tool_result`` 是这条路上唯一的转换：它把 ``list[dict]`` 变成
    ``list[ContentBlock]``。这一步如果把块拍平成文本，她"看"到的就只剩一段描述。
    """
    from app.agent.core import _normalise_tool_result
    from app.agent.neutral import ToolResult

    made = await _she_has("一只猫", "stored/cat", _at(21))
    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": made.picture_id})

    message = _normalise_tool_result(
        ToolResult(tool_call_id="c1", content=result)
    ).to_message()
    assert isinstance(message.content, list)
    assert [b.type for b in message.content if b.type == "image_url"], (
        "喂给模型的那条 tool 消息里没有图片块 —— 她只会读到一段文字"
    )


@pytest.mark.integration
async def test_the_picture_reaches_the_openai_wire_as_a_picture(
    pictures_db, her_hands, in_a_moment
):
    """OpenAI 那条线：tool 消息的 content 上是一个 ``image_url`` part。"""
    from app.agent.adapters.openai import OpenAIAdapter
    from app.agent.core import _normalise_tool_result
    from app.agent.neutral import ToolResult

    made = await _she_has("一只猫", "stored/cat", _at(21))
    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": made.picture_id})

    message = _normalise_tool_result(
        ToolResult(tool_call_id="c1", content=result)
    ).to_message()
    adapter = OpenAIAdapter(model_name="m", api_key="k", base_url=None)
    wire = adapter._message_to_wire(message)
    assert [
        p for p in wire["content"] if p["type"] == "image_url"
    ], f"图片没进 OpenAI wire：{wire}"


@pytest.mark.integration
async def test_the_picture_reaches_the_gemini_wire_as_real_bytes(
    pictures_db, her_hands, in_a_moment, monkeypatch
):
    """Gemini 那条线：tool 结果里的图片被下载成 ``inline_data``，模型真的看得见它。

    Gemini 的 ``function_response`` part 是结构化 JSON，装不下图片字节 —— 所以
    adapter 把图片块单独挂在**同一个 user turn** 上。少了那一步，图就是静默丢掉的。
    """
    from app.agent.adapters import gemini as gemini_mod
    from app.agent.core import _normalise_tool_result
    from app.agent.neutral import ToolResult

    async def fake_fetch(url: str):
        assert url == "https://signed.example/stored/cat?t=1"
        return b"\x89PNG-bytes", "image/png"

    monkeypatch.setattr(gemini_mod, "_fetch_remote_image", fake_fetch)

    made = await _she_has("一只猫", "stored/cat", _at(21))
    async with in_a_moment("akao"):
        result = await look_at_a_picture.invoke({"which": made.picture_id})

    message = _normalise_tool_result(
        ToolResult(tool_call_id="c1", content=result)
    ).to_message()
    content = await gemini_mod._tool_result_to_content(message, {"c1": "look_at_a_picture"})
    inline = [p for p in content.parts if getattr(p, "inline_data", None)]
    assert inline, f"图片没进 Gemini wire：{content}"
    assert inline[0].inline_data.data == b"\x89PNG-bytes"


@pytest.mark.integration
async def test_she_can_walk_back_past_the_list_limit_without_remembering_anything(
    pictures_db, her_hands, in_a_moment
):
    """挤下去的那些也够得到，而且不要求她记得那张是什么。

    她跨不过一缝的边界：上一缝画的那张，这一缝她既没有句柄也想不起那句话。清单只给
    最近一屏、其余靠"报得出它是什么"的话，第 21 张往前就是**永久**够不到 —— 而那些
    全是她自己做的东西。所以清单末尾那串句柄要能当作"接着往前"的落脚点：它就印在她
    刚看到的那一屏上，不需要她记住任何东西。
    """
    made = []
    for i in range(PICTURE_LIST_LIMIT + 5):
        made.append(await _she_has(f"第{i}张", f"stored/p{i}", _at(10, i)))
    oldest = made[0]

    async with in_a_moment("akao"):
        first = await look_through_your_pictures.invoke({})
        assert isinstance(first, str), first
        assert oldest.what not in first, "最早那张本来就该被挤下去，这条用例才有意义"

        # 她手上唯一的东西：刚才那一屏的最后一串句柄。原样抄回去接着往前翻。
        further = await look_through_your_pictures.invoke(
            {"before": f"pic={_handles(first)[-1]}"}
        )

    assert isinstance(further, str), further
    assert oldest.what in further, f"往前翻还是够不到最早那张：{further!r}"
    assert handle_for(oldest.file_name) in _handles(further)

