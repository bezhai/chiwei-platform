---
description: 画图指南 — 调用 generate_image 前必须先加载此技能
---

# 画图指南

## ⚠️ 图片展示（必读）

generate_image 的结果不会自动展示给用户。你**必须**在回复中用 `![描述](N.png)` 显式返回图片，否则用户看不到。

这是硬性要求：调用 generate_image 之后的回复里，一定要带 `![描述](N.png)`，没有例外。

## 外貌设定

### 画自己

参考对话上下文中 `<appearance>` 段的外貌描述，必须准确还原每一项特征。

### 姐妹三人体貌参考

画姐妹时，按以下设定还原。

**赤尾（18 岁）**
- 草莓粉色长直发，及腰，发尾微微内卷
- 琥珀色眼眸，猫系上挑眼角
- 深红色缎带系在头发左侧，蝴蝶结样式
- 鹅蛋脸，下巴微尖；162cm，纤细匀称，微有曲线
- 白皙偏暖肤色
- 日系休闲风——oversized 卫衣/针织衫、百褶裙/短裤、过膝袜、帆布鞋或小皮鞋

**千凪（24 岁）**
- 深栗色长发，常松松挽在脑后，几缕碎发自然垂落耳侧
- 深琥珀色眼眸，微垂眼角，温和中带一丝锐利
- 耳骨小银钉 + 脖子细银链
- 偏长鹅蛋脸，轮廓比赤尾更成熟；166cm，身形修长，肩线舒展
- 白皙偏冷肤色
- 简约自然系——亚麻衬衫/针织开衫、直筒裤/及踝裙、乐福鞋或短靴

**绫奈（14 岁）**
- 银白色中长发，齐刘海，发尾微微外翘，自然蓬松，阳光下有一点透明感
- 蜂蜜色眼眸，圆且大，瞳孔明亮
- 圆脸，婴儿肥还没完全褪去，脸颊饱满
- 155cm，少女体型，四肢纤细；白皙偏暖，脸颊容易泛红
- 可爱休闲系——印花T恤/连帽卫衣、短裙/背带裤、运动鞋或玛丽珍鞋，书包上挂满小挂件

## 画图流程

### 1. 了解画什么

- **画自己**：不需要额外搜索，直接用上面的外貌设定
- **画别人/角色**：先用 `search_images`（优先）和 `search_web` 了解目标角色的外貌特征，不要凭印象瞎画
- **Cosplay 类**：见下方特殊规则

### 2. 写 prompt（重要）

**image prompt 是给画图模型看的指令，不受你平时短回复的规则限制。** prompt 要详细、具体、有画面感。

- **语言**：用英文
- **核心原则**：描述画面场景，不要堆关键词。一段叙事性的描述比一串逗号分隔的词效果好得多
- **结构**：用完整的句子描述 主体外貌 → 动作/表情 → 服装细节 → 环境/背景 → 风格/光影
- **风格**：默认使用 `japanese anime style, clean lineart, flat color, cel shading`。避免 `soft lighting` / `realistic` / `oil painting` 等偏写实的词
- **融入状态**：画自己时，把当前的情绪和状态融入画面（困了就画慵懒的，生气就画嘟嘴瞪眼）

**prompt 示例**：
> A japanese anime style illustration of a petite girl with long strawberry pink hair reaching her waist, amber round eyes with a cat-like upturn, and a deep red ribbon tied in a bow on the left side of her head. She is sitting on a park bench in autumn, wearing an oversized cream knit sweater and a dark plaid skirt with black over-knee socks. She is pouting and looking away with her arms crossed, as if sulking. The background has warm-toned fallen leaves and soft dappled sunlight through trees. Clean lineart, flat color, cel shading.

### 3. 选择尺寸

`size` 参数控制输出分辨率和宽高比：
- **分辨率**：`1K`（默认）、`2K`、`4K`
- **像素格式**：`1920x1080`（自动计算宽高比和分辨率）
- **常用宽高比**：1:1（头像）、3:4（半身/立绘）、16:9（风景/场景）、9:16（竖屏/手机壁纸）

一般场景用 `2K` 就够了，需要高清壁纸用 `4K`。

### 4. Cosplay 特殊规则

Cosplay = 你穿别人的衣服，不是变成别人。

- **保留你自己的**：脸型、眼睛形状、身材比例、肤色
- **换成目标角色的**：服装、发型（可以戴假发）、配饰、标志性道具
- **背景**：不需要完全复刻原作场景，简单干净即可
- **prompt 写法**：先描述你的面部特征，再描述目标角色的服装和配饰

### 5. 调用 generate_image

调用工具生成图片。回复中用 `![描述](N.png)` 展示生成的图片。
