# 腹腔镜手术视频 5 Hz 标注仓库

逐张读取按时间排列的真实视频帧，标注“哪把器械、对什么目标、做了什么动作、出现什么可见变化”。默认模型为现有 OpenAI 兼容接口中的 **`gemini-4-argon`（Gemini 4）**，默认基地址为 `https://api3.xhub.chat/v1`，可按实际服务修改。

运行环境：macOS / Linux、Python 3.10+、ffmpeg / ffprobe。`start.sh` 首次创建本地 `.venv` 并安装依赖，后续复用。仓库不包含视频或 API key。

## 立即启动

在这个工作区中，可直接运行下面的命令，无需密钥，也不会调用模型：

```bash
cd /Users/bytedance/code/npj/laparoscopy_5hz
bash start.sh \
  --video ../laparoscopy_annotation/assets/video.mp4 \
  --start 314 --end 324 \
  --output outputs/gemini4_preview \
  --dry-run
```

这会准备原视频 314–324 秒的核心区间和前后各 1 秒上下文。输出里包含 60 张 5 Hz 图像；其中 50 张位于核心区间。每张图像单独保存，标明原视频全片帧号和时间。

准备实际标注时，将 `.env.example` 复制为 `.env`，用编辑器在本机填入 `XHUB_API_KEY`：

```bash
cp .env.example .env
bash start.sh doctor
```

`doctor` 只显示是否已配置密钥，不显示密钥、前缀或长度。环境变量优先于 `.env`；可使用 `ANNOTATION_API_KEY`、`XHUB_API_KEY` 或 `OPENAI_API_KEY`，按这个顺序取第一个非空值。`.env` 不提交、不打包。

去掉 `--dry-run`，相同配置和目录即可继续：

```bash
bash start.sh \
  --video ../laparoscopy_annotation/assets/video.mp4 \
  --start 314 --end 324 \
  --output outputs/gemini4_preview
```

整片标注：

```bash
bash start.sh \
  --video /绝对路径/video.mp4 \
  --output outputs/full_video
```

更换电脑时先安装 ffmpeg：macOS 使用 `brew install ffmpeg`，Ubuntu 使用 `sudo apt-get install ffmpeg`。可用 `PYTHON_BIN=/path/to/python3 bash start.sh ...` 指定 Python。pip 镜像按本机配置或 `PIP_INDEX_URL` 设置。

## Prompt 与本次标注规范

原文保存在 `laparoscopy_5hz/resources/prompt_zh.txt`，作为每次请求的 system prompt。它保留用户提供的判读规则、输出前复核要求、词表及 JSON 示例。程序只在 user 消息中补充本次 core、图像帧号、实际时间及 ontology。

本版与之前的 2 fps 标注不同：

- 采样固定为 **5 Hz**。逐张发送图像，不拼成联系表，不先做全片术式推断。
- 一条 event 表示 **一个器械 × 一个动作 × 一个目标的连续交互**，允许多器械同时交互。
- `grasp`、`retract`、`release` 分段；接近、单纯定位、静止及镜头活动不作为 event。
- 工作端或交互证据不足时使用 `uncertain_ranges`，不补造动作。
- 采用给定的代码词表，明确区分纱布目标与纱布中介。
- `events`、`uncertain_ranges`、`key_events` 可以为空；成功的空事件结果也会缓存。
- 此版输出不包含器官 bbox。它实现本次指定的交互事件 JSON 格式；已有 2 fps 结果不会被覆盖或重用。

程序校验的是结构、词表、时间和帧号约束，不能替代工作端、目标响应、烟雾来源或临床解剖判断。`confidence` 是模型自评，所有导出事件仍标记待复核。

## 帧号和时间的精确定义

**frame 是从 0 开始的原视频全片显示顺序帧号，不是 5 Hz 样本序号，不是窗口内序号。** 使用 ffprobe 的视频帧时间戳建立映射，再使用 ffmpeg 的 `n` 选择对应源帧。不能使用 `coded_picture_number` 代替显示顺序帧号。

对于 25 fps 视频：

| 原视频时间 | 原视频全片帧号 |
|---|---:|
| 314.0 s | 7850 |
| 314.2 s | 7855 |
| 314.4 s | 7860 |
| 324.0 s | 8100 |

314–324 秒的 core 为 `[7850,8100)`；8100 不属于核心区间。事件 `end_frame` 也不包含。相邻样本相差 0.2 秒，在该视频里相差 5 个原视频帧。

原视频时间定义为源帧 PTS 减去首个视频帧 PTS。源视频为变帧率或帧率与 5 不整除时，按全片 0.2 秒网格选择其后的首个真实源帧，保存真实 PTS；时间可能略偏离网格，不复制图像伪造精度。重复或逆序时间戳会报错，低于 5 Hz 的源视频会被拒绝。

也可以直接指定全片帧范围：

```bash
bash start.sh \
  --video /绝对路径/video.mp4 \
  --start-frame 7850 --end-frame 8100 \
  --output outputs/frame_range \
  --dry-run
```

帧范围和秒数范围不能混用。`end-frame` 可以等于源视频总帧数，表示全片末尾的独占边界。

## 窗口、上下文与边界

默认核心窗口为 10 秒，前后上下文各 1 秒，顺序请求，每个窗口最多 100 张图。请求中明确告知核心范围、已提供的全片帧号及实际时间。模型可借助上下文判读，但输出只允许覆盖 core。

```bash
bash start.sh --video /绝对路径/video.mp4 \
  --window-seconds 8 --context-seconds 1 \
  --width 720 --max-images 100 \
  --max-tokens 8000 --attempts 3 --timeout 300 \
  --output outputs/custom
```

图片不放大源图，顶端时间标签位于手术画面外。core 划分保持半开区间；输出边界必须使用已提供的样本帧或 core 起止帧。关键事件必须定位到 core 内实际提供的图像。

同一持续动作若跨多个 core，其在各 core 内的片段分别保留。**默认不自动跨 core 拼接**：指定输出格式没有稳定的器械实例 ID，不能仅凭“左侧抓钳”或相似目标安全判定为同一把器械。原始窗口结果不改动，汇总通过 window_id 和 event_id 保留出处，供人工连接边界片段。

## 词表与 Schema

默认词表在 `laparoscopy_5hz/resources/ontology.json`。自定义 ontology 只覆盖提供的字段，其他字段沿用默认词表；代码必须是非空、不重复的字符串列表。禁止模型自行构造代码。

```bash
bash start.sh annotate --video /绝对路径/video.mp4 \
  --ontology /绝对路径/ontology.json \
  --output outputs/custom_ontology

bash start.sh schema --output /tmp/annotation.schema.json
```

输出 Schema 根据实际 ontology 加上枚举约束。程序还会检查：

- reviewed 起止帧必须等于本次 core；
- event 与 uncertain range 必须满足 `core_start <= start < end <= core_end`；
- 关键事件必须在 core 中且对应真实输入样本；
- 边界帧必须来自输入图像或 core 独占边界；
- 完全重复的交互拒绝；不同器械的重叠交互保留；
- 纱布为目标时 `mediator=none`；`wipe` 不能将纱布自身当作被擦组织；
- 额外字段、未知代码、NaN、重复 JSON 键、Markdown 代码围栏均拒绝。

单窗口文件校验：

```bash
bash start.sh validate outputs/gemini4_preview/windows/window_00000.json \
  --input-manifest outputs/gemini4_preview/window_inputs/window_00000.json
```

使用自定义词表时，校验命令也需要传入同一份 `--ontology`。不指定 input manifest 时只校验 JSON 内声明的 core，不独立检查该声明是否对应某次实际请求。

## 输出与续跑

```text
outputs/<run>/
├── run_config.json            # 无密钥配置、视频/Prompt/Schema指纹
├── video_timeline.json        # 每个源帧的原视频时间
├── sampled_frames.json        # 本次实际抽取的全片帧号
├── frames/                    # 带标签的逐张JPEG
├── window_inputs/             # 每个core的输入范围与图像清单
├── windows/                   # 原始单core JSON，严格使用指定输出格式
├── call_metadata/             # 模型名称、允许列出的token统计、重试次数
├── failures/                  # 安全的失败类别，不保存响应正文
├── output.schema.json         # 本次实际词表的Schema
├── annotations.json           # 汇总封套：coverage、窗口摘要、events等
└── events.csv                 # 帧号、秒数、代码、证据、结果和出处
```

`windows/window_*.json` 与用户指定格式一致。`annotations.json` 是带出处的汇总封套，不是单次模型回复；它区分 requested、reviewed 和 failed cores，避免把失败窗口伪装为“已读但无动作”。dry-run 不制造模型事件，状态为 `dry_run`。

重复运行相同命令会校验并复用已完成窗口，仅请求尚未完成的窗口。视频内容、prompt、词表、模型、API 基地址、窗口、图像宽度或关键请求配置变化时，必须使用新的输出目录。输出目录带运行锁，避免两进程同时写同一缓存。

HTTP 408/429/5xx、暂时网络故障和不合格式输出有限重试；认证／权限错误和模型明确过滤拒绝不自动重试。需要自己处理的缺失区间会明确保留。格式失败时重发相同图像及安全的校验提示，不保存失败正文。接口不支持 JSON mode 时保持默认配置；只有确认支持时才使用 `--json-mode`。

## 密钥处理

密钥仅从本机 `.env` 或环境变量读取，仅用于请求 Authorization header。没有 `--api-key` 命令参数，不把密钥放入命令行历史。`.env`、`.venv`、outputs、缓存与打包目录均已忽略。

不记录请求头、完整请求体、原始 API 响应、异常网络正文或环境变量。成功结果先脱敏再校验；日志及导出再次脱敏。token 元数据使用允许字段列表。接口 URL 禁止携带账号、密码、查询参数或密钥；HTTPS 为默认，HTTP 仅允许 localhost 测试；请求不自动跟随重定向。

## 已执行验证

```bash
bash start.sh doctor
.venv/bin/python -m unittest discover -s tests -v
```

已通过 14 项离线测试，覆盖原视频帧号、5 Hz 网格、半开区间、词表、自定义词表、并发器械、纱布语义、JSON 严格性、认证与拒绝处理、密钥脱敏、模拟接口和续跑。

另用已有真实手术视频执行 314–324 秒 dry-run：原视频 17,921 帧、25 fps；core 为 `[7850,8100)`；核心 50 张、含上下文 60 张；相邻源样本相差 5 帧／0.2 秒。没有调用付费模型，没有将离线测试样例用作手术标注。

真实 Gemini API 端到端调用尚未执行：启动时未配置 API key。填写本机配置后即可按上方命令启动。
