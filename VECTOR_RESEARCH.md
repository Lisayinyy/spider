# 矢量绘制 / 动漫化实时滤镜 —— 技术调研与实现笔记

本文是为复刻小红书笔记「AI小白手搓4小时复刻矢量绘制」做的技术调研沉淀。
调研目标是回答两个问题：**参考视频到底是怎么做的**，以及**实时实现该用哪套算法**。

---

## 一、先搞清参考视频到底是什么（这一步最容易跳过，也最贵）

我最初两轮实现全部朝错方向精修，根因是**没有先把参考素材看清楚**。
测试从 9 条做到 14 条全绿，但形态从一开始就不对。

### 正确的第一步：抽帧做 contact sheet

```bash
yt-dlp -o ref/xhs_vector.mp4 <视频链接>
ffmpeg -i ref/xhs_vector.mp4 -vf "fps=1,scale=540:-1" ref/vecframes/f%02d.jpg
ffmpeg -i ref/xhs_vector.mp4 -vf "select='not(mod(n\,30))',scale=430:-1,tile=4x4" \
       -frames:v 1 ref/vecframes/contact.png
```

**单帧看不出形态，一张拼图 30 秒就看出来了。** 我从 contact sheet 读出四件事：

| 维度 | 我原本以为 | 实际 |
|---|---|---|
| 窗形 | 方框取景窗 | **贯穿画面的横向斜带** |
| 画风 | toon filter（细描边） | **日漫赛璐璐**（大色块平涂、几乎无描边） |
| 范围 | 只替换人物 | **带内连背景一起换**（窗框、吊灯都变矢量插画） |
| 实时性 | 实时滤镜 | 头发穿过带边界**连续** → 整帧预转绘 + 手势遮罩擦除 |

最后一条是判断作者做法的决定性证据：如果是实时逐像素滤镜，
带边界处的头发不可能与带外的头发严丝合缝地接上。

### 读几何：不要写统计判据，放大 + 画网格用眼睛读

我为了自动量斜带边界，写了三个统计判据，**全部失败**：

| 判据 | 失败原因 |
|---|---|
| 高饱和 + 低梯度 | 被真人衣服/背景污染，一路延伸到画面底部 |
| 3×3 局部标准差找纯色块 | 抓到的是天花板（本身就是平的大白面） |
| 「暗细线」找矢量描边 | 没有斜带的纯真人帧也报出一段（假阳性） |

**真人照片和矢量插画在任何单一统计量上都不可分**（饱和度/平坦度/暗线全部重叠）。

有效做法是 `resize(3x, NEAREST)` + 画 10% 网格，逐格目视读出：

```
左手食指尖 (27%, 30%)   右手食指尖 (77%, 32%)   ← 斜带上边界
左手拇指尖 (28%, 58%)   右手拇指尖 (76%, 57%)   ← 斜带下边界
带厚 ≈ 28% 画面高；左右延伸出画面（x 覆盖 0..1）
```

网格目视 2 分钟，比写 3 个探针又快又对。

---

## 二、算法调研：权威源在哪

### 检索经验

- **中文搜索基本无效**：搜「矢量绘制 教程」「cel shading shader」返回的是
  AI 绘图工具、Canva、剪映教程，以及大量无关内容。两轮尝试都是如此。
- **GitHub 仓库/代码搜索 API 才是有效入口**：

```bash
curl -s "https://api.github.com/search/repositories?q=<关键词>&sort=stars&order=desc&per_page=5" \
     -H "Accept: application/vnd.github+json"
```

用 `anime4k+glsl`、`kuwahara+anisotropic+shader` 这类关键词，一次就命中源头。

### 找到的两个权威源

#### 1. GPUImage2（4941★, BSD）— 保边平滑 + 色阶量化

- `KuwaharaFilter.swift` / `Kuwahara_GLES.fsh`（radius=3）
- `ToonFilter.swift`（threshold=0.2, quantizationLevels=10）
- `SmoothToonFilter.swift`（GaussianBlur → ToonFilter）
- `Posterize.swift`（colorLevels=10）

论文出处：Kyprianidis, Kang, Doellner,
*"Anisotropic Kuwahara Filtering on the GPU"*, GPU Pro p.247, 2010。

**Kuwahara 原理**：把邻域分成四个象限，各算均值和方差，取方差最小者的均值。
效果是「保边平滑」—— 平坦区被抹平成色块，边界不被跨越。

#### 2. bloc97/Anime4K（21235★, MIT）— 动漫线条处理

专门做动漫画质的 GLSL shader 集，有 WebGL 移植（`monyone/Anime4K.js`, MIT）。
对本项目最有价值的是 `glsl/Experimental-Effects/` 两个文件：

**`Anime4K_Darken_HQ.glsl` — DoG 单侧线条加深**

```glsl
// 两趟可分离高斯（sigma 按分辨率缩放）
// 关键这一行：只保留负的一侧
return vec4(min(LINELUMA_tex(pos).x - comp_gaussian_y(), 0.0), ...);

// 最后直接加回 RGB（负值 = 变暗）
#define STRENGTH 1.5
return HOOKED_tex(HOOKED_pos) + (comp_gaussian_y() * STRENGTH);
```

**`Anime4K_Thin_HQ.glsl` — 线条细化**（本项目未采用，留作备选）

```glsl
#define STRENGTH 0.6
vec2 dn = LINESOBEL_tex(pos).xy;
vec2 dd = (dn / (length(dn) + 0.01)) * d * relstr;  // 拟归一化，避免除零
pos -= dd;      // 沿梯度方向位移，把粗线往中心收细
return HOOKED_tex(pos);
```

---

## 三、DoG 单侧 vs Sobel 双侧：这是本项目最关键的一个认知

我原来用 Sobel `length(vec2(gx, gy))` 做描边，真人脸上 `darkFrac` 冲到 **0.77**
（77% 画面变暗），满脸黑斑。

**根因**：`length(gradient)` 是**无符号**的。一条边有亮侧和暗侧，
两侧的梯度幅值一样大，于是**两侧都被上墨**。
肤色的自然明暗过渡也有两侧 —— 全被描成脏斑。

**DoG 的 `min(diff, 0.0)` 只保留「比周围暗」的一侧**，亮侧完全不动。
它加深的是**本来就存在的暗线**，不会凭空造线。

实测对照（真人 ROI，`dogStr` 从 0 拉到 4.0 = 默认值的 5 倍）：

| dogStr | mean | darkFrac | rough | uniq |
|---|---|---|---|---|
| 0.0 | 95.1 | 0.422 | 0.134 | 106 |
| 0.8（默认） | 91.6 | 0.423 | 0.162 | 162 |
| 1.5 | 88.9 | 0.431 | 0.214 | 173 |
| 2.5 | 85.9 | 0.440 | 0.269 | 181 |
| 4.0 | 82.4 | 0.460 | 0.320 | 186 |

即使拉到 5 倍强度，`darkFrac` 也只从 0.422 到 0.460 —— **不会泛滥**。

破坏对照（把 `min(diff,0)` 改成 `-abs(diff)` 即双侧）在 `dogStr=4.0` 下：

| | darkFrac | rough |
|---|---|---|
| 单侧 | 0.460 | 0.320 |
| 双侧 | 0.521 | **0.491** |

`darkFrac` 只差 13%，`rough` 差 **53%** —— 所以测试断言必须用 `rough` 判，
用 `darkFrac` 判是**永真的**（两版都撞不穿阈值）。这一条已在测试注释里如实记录。

### DoG 的模糊基准必须用真高斯，不能用 Kuwahara

我一开始图省事，直接拿 Kuwahara 已经算好的 `flat3` 当「模糊后的亮度」。
实测 `rough` 从 0.134 涨到 0.170；换真高斯后是 0.162。

**原因**：Kuwahara 是**保边**的 —— 边界两侧各自被抹平成两个平台，
中心亮度与平台的差在边界处会突然翻转，产生一圈硬阶跃。
高斯不保边，差值随距离连续衰减，得到的线才是渐隐的。
这也正是 Anime4K 原实现用两趟可分离高斯的原因。

---

## 四、posterize 的三个坑（都是实测归因出来的）

### 坑 1：不能在线性 RGB 上均匀量化

真人的肤色、头发亮度大量落在低段，`floor(x*L)` 把整片暗部砸进同一档 → 死黑一坨。
实测 `vecMix` 0→1 时 mean 从 64.4 掉到 **23.3**、darkFrac 从 0.45 涨到 0.85。

**正解**：先把亮度重映射进「插画区间」（抬黑位、压白位、调 gamma），
再**只量化明度**，色相/饱和度保持连续。

### 坑 2：档内偏移会被 `/L` 放大成 L 相关的亮度抬升

```glsl
float lq = (floor(lp) + 0.62 + soft) / L;   // 0.62 是给 L=6 调出来的
```

那 `0.12` 的偏移被 `/L` 放大成 `0.12/L` 的亮度抬升：
L=6 抬 0.020，**L=3 抬 0.040**。
从 toon（L=6）切到赛璐璐（L=3）时画面整体过亮，我误判为「色阶太少」。

**判据：mean 随 L 单调变化 = 有 L 相关的常数项在作祟，跟色阶数本身无关。**
改成档中心 `0.5` 后偏移归零。

### 坑 3：色度必须按明度变化比例同步放大

亮度从 `lin` 抬到 `lq` 之后，原样搬过来的色度相对新亮度就被稀释了，
画面读作灰绿而不是插画的明亮色。修法：

```glsl
float lift = lq / max(lin, 0.08);
vec3 chroma = (flat3 - vec3(lin)) * clamp(lift, 0.6, 2.6) * satGain;
```

### 锯齿归因（相邻像素亮度差 > 14 的占比）

```
Kuwahara 单独          rough 0.082
+ posterize            rough 0.205   <- 主凶，+0.123
+ Sobel 描边           rough 0.287   <- 次之，+0.082
```

主凶是 posterize 的**硬台阶**。解法是**软量化**：档位边界用 smoothstep
做一小段过渡（宽度 0.11 档），把 posterize 的贡献压到 +0.011。

注意：软量化对**赛璐璐档几乎无效**（它的过渡宽度本来就只有 0.035，
接近硬台阶，那正是刀切色块想要的）。这条的牙齿只在 toon 档测得到。

---

## 五、赛璐璐 vs toon filter：两档参数与判据都不能共用

### 参数

| | 赛璐璐（默认） | toon filter |
|---|---|---|
| `vecStyle` | 1 | 0 |
| 色阶 `vecLevels` | 3 | 6 |
| 量化过渡 `qSoft` | 0.035（刀切） | 0.11（柔和） |
| 描边 `inkGain` | 0.22（几乎无） | 0.85 |
| DoG `dogStr` | 0.8 | 0（不叠加） |
| 饱和 `satGain` | 1.70 | 1.55 |
| 黑位 / gamma | 0.05 / 1.00 | 0.16 / 0.78 |

**DoG 和 Sobel 描边必须二选一，不能叠加**：
实测 toon 档叠 DoG 后 ink 从 0.236 涨到 0.325、mean 从 68.9 掉到 58.2，
正是「描边泛滥」复发。所以 DoG 那一行乘了 `uVecStyle` 做门控。

### 赛璐璐「不暗」的真正原因（三次猜错的归因过程）

赛璐璐第一版 mean 冲到 139，读作过曝橙红块。我先猜「顶档提亮太狠」，
再猜「饱和度太高」，逐项关掉实测（style=1, L=4）：

```
关顶档提亮   mean 128.7  (无变化)
关硬高光     mean 127.8  (-0.9)
关饱和差异   mean 128.8  (+0.1)
关描边差异   mean  89.3  (-39.4)  <- 唯一主因
```

**「不暗」纯粹因为我把描边关了，而赛璐璐的阴影档根本没生成。**
正解不是把描边加回来，而是给赛璐璐**独立暗档**
（黑位 0.16→0.05、gamma 0.78→1.00），让暗面真的落进最低档形成阴影色块。
这才是赛璐璐该有的暗部来源 —— 阴影是画上去的色块，不是描出来的线。

### 判据

赛璐璐的暗像素 `dark ≈ ink ≈ 0.422`，即所有暗像素都低于 48 —— **是色块不是线**。
所以：

- 赛璐璐用 `rough`（色块平整度）和 `uniq`（色数）判，**不能用 ink 判**
- toon filter 用 `ink` 的存在与不泛滥判

---

## 六、斜带几何的实现

手势规则本身（`[A.index, B.index, B.thumb, A.thumb]`，
即食指连线 = 上边、拇指连线 = 下边）**原来就对**，只缺外推。

```js
const BAND_EXTEND = 1.35;
const ext = (p, q) => {
  const dx = q[0]-p[0], dy = q[1]-p[1];
  const len = Math.hypot(dx, dy) || 1e-5;
  const ux = dx/len, uy = dy/len;
  return [[p[0]-ux*len*BAND_EXTEND, p[1]-uy*len*BAND_EXTEND],
          [q[0]+ux*len*BAND_EXTEND, q[1]+uy*len*BAND_EXTEND]];
};
```

**必须沿每条边自己的方向向量外推**，不能统一水平外推 ——
否则会丢掉「带跟着手的姿态倾斜」这个特征。

实测（喂参考帧 f05 的指尖坐标走真实 `onGesture` 管线）：
`x 范围 -0.41..1.45`（贯穿）、厚度 `0.266H`（参考 0.28）、倾斜 `+0.074`。

### 一个假破坏的教训

验证「倾斜跟手」这条断言时，我只把 `ext()` 里的 `dy` 置零 —— 测试**照样全绿**。
因为食指连线本身左右手 y 不同，端点仍保留原 y，倾斜并没有消失。
真破坏必须**强制两端取中点**把边拉平，才能让断言转红。

> **做破坏对照时必须确认被破坏的量真的被破坏了**，不能只看代码改了。

---

## 七、真人像才是测试台，合成纹理证明不了效果

上一版测试只在自己画的渐变方块上跑，9 条断言全绿，
但同一份代码喂真人照片时 `darkFrac` 0.77、mean 从 64 掉到 23，满脸黑斑。

**合成渐变方块没有真实结构边界**：Sobel 阈值从 0.02 扫到 0.90，
`darkFrac` 恒为 0.017 —— 那条「描边有牙齿」的断言是**纯永真**的。

→ 合成纹理只能证明「算法在跑」，证明不了「真人看起来像矢量插画」。

### 当前真人像基线

ROI `[.30,.22,.70,.78]`，`shots/testface.png`：

| 配置 | mean | darkFrac | ink | rough | uniq |
|---|---|---|---|---|---|
| 原图（vecMix=0） | 64.2 | 0.451 | 0.429 | 0.183 | 139 |
| 赛璐璐默认 | 91.6 | 0.423 | 0.422 | 0.162 | 162 |
| 赛璐璐关 DoG | 95.1 | 0.422 | 0.422 | 0.134 | 105 |
| toon 默认 | 68.9 | 0.244 | 0.236 | 0.180 | 114 |

赛璐璐成品：比原图**亮 43%**、色数少 **24%**（关 DoG 时少 24%）、
边缘比原图**更平整**，且保留 0.42 的阴影色块。

---

## 八、我们的实现 vs 参考视频

| | 参考视频 | 本项目 |
|---|---|---|
| 转绘 | 后期整帧预转绘（K 帧 / AI） | **实时逐像素**（WebGL2 单 pass） |
| 姿势 | 固定，不跟人动 | 跟着人动 |
| 场景 | 只有那一段视频 | 任意场景、任意人 |
| 带边界的头发 | 严丝合缝（同一张预渲染图） | 有细微差异（实时算的） |

参考视频不是实时的，这既是它画质更整的原因，也是我们的差异化优势。

---

## 九、踩坑清单（工程侧）

- **GLSL 注释里绝对不能出现反引号**。shader 源码内嵌在 JS 模板字符串里，
  一个反引号会提前终止模板字符串，报 `Unexpected identifier` 这种
  与 GLSL 毫不相干的 JS 解析错误。这轮引用 Anime4K 文件路径时又踩了一次。
  自查脚本：定位 shader 模板段，断言段内 `` ` `` 和 `${` 计数为 0。
- **Playwright `page.evaluate` 读不到 ES module 作用域变量**，
  必须通过 `window.__XXX__` 钩子暴露。
- **不要把整幅像素数组回传 Python**，统计全在页面内做、只回传数字。
- 本机没有 `timeout` 命令，长测试用 bash 工具自带的 timeout 参数。

---

## 参考资料

- bloc97/Anime4K — https://github.com/bloc97/Anime4K （MIT, 21235★）
  - `glsl/Experimental-Effects/Anime4K_Darken_HQ.glsl`
  - `glsl/Experimental-Effects/Anime4K_Thin_HQ.glsl`
- monyone/Anime4K.js — WebGL 移植（MIT）
- BradLarson/GPUImage2 — https://github.com/BradLarson/GPUImage2 （BSD, 4941★）
  - `KuwaharaFilter.swift` / `ToonFilter.swift` / `Posterize.swift`
- Kyprianidis, Kang, Doellner, *"Anisotropic Kuwahara Filtering on the GPU"*,
  GPU Pro, p.247, 2010
- sophiamyang/finger-frame-effect — 手指取景框追踪管线（本项目手势部分的来源）
