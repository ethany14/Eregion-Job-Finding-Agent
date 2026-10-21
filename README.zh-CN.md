<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/readme/banner-light.webp">
    <img src="frontend/site/assets/readme/banner-dark.webp" alt="Eregion：你的求职，一次对话搞定。" width="100%">
  </picture>
</p>

<p align="center">
  <a href="#get-started"><img src="https://img.shields.io/badge/python-3.12-3d6a4a?style=flat-square&logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="docs/configuration.md"><img src="https://img.shields.io/badge/models-OpenAI--compatible-3d6a4a?style=flat-square" alt="任何兼容 OpenAI 的模型"></a>
  <a href="#where-jobs-come-from"><img src="https://img.shields.io/badge/sources-company%20boards%20%C2%B7%20LinkedIn-9a7428?style=flat-square" alt="公司招聘页和 LinkedIn"></a>
  <a href="docs/evaluation-report.md"><img src="https://img.shields.io/badge/evaluation-report-c0532a?style=flat-square" alt="评估报告"></a>
  <a href="#jev"><img src="https://img.shields.io/badge/fit%20scoring-Jev%20optional-d9b86a?style=flat-square" alt="可选的 Jev 匹配评分"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6c776f?style=flat-square" alt="MIT 许可证"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <b>简体中文</b>
</p>

<p align="center">
  <b><a href="#get-started">快速开始</a></b> ·
  <a href="https://ethany14.github.io/Eregion-Job-Finding-Agent/guide.html">用户指南</a> ·
  <a href="docs/configuration.md">配置</a> ·
  <a href="docs/evaluation-report.md">评估</a> ·
  <b><a href="https://ethany14.github.io/Eregion-Job-Finding-Agent/">网站</a></b>
</p>

---

Eregion 是一个运行在你自己电脑上的求职助手。告诉它你在找什么，它会搜索公司招聘页和
LinkedIn，完整阅读每一则职位描述，再按与你简历的匹配程度把职位分好类交给你。每个职位都会列出
差距，以及还需要你确认的事项。它还能为你定制简历和求职信，在 Chrome 里把申请表一路填到审核页，
并陪你练习面试。

所有数据都保存在本地数据库中。模型调用使用你自己提供的 API 密钥，并且**没有你的确认，什么都不会被提交**。

界面支持**英文和简体中文**：在侧边栏底部点 **中 / EN** 即可切换，选择会保存在这个浏览器里；
没有选择时，中文浏览器默认显示中文。

## 一个工作区，覆盖整个求职过程

<table>
  <tr>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/screens/chat-light.webp">
        <img src="frontend/site/assets/screens/chat-night.webp" alt="议事厅：助手的搜索总结，以及一张产品分析师职位的优先匹配卡片，附理由、差距和下一步。">
      </picture>
      <h3>💬 议事厅（Council）</h3>
      你和助手的对话。结果以卡片呈现，每张卡片带有分级、理由、差距和下一步。
    </td>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/screens/home-light.webp">
        <img src="frontend/site/assets/screens/home-night.webp" alt="纪事首页：今天的动态、需要你处理的烽火，以及征途中的申请。">
      </picture>
      <h3>📖 纪事（Chronicle）</h3>
      首页：今天发生了什么，哪些对话在等你。
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/screens/road-light.webp">
        <img src="frontend/site/assets/screens/road-night.webp" alt="征途：申请总数、一条蜿蜒的站点地图，以及进度表。">
      </picture>
      <h3>🛤️ 征途（The Road）</h3>
      所有申请都在一张地图、看板或表格上，还能看到回复率。
    </td>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/screens/forge-light.webp">
        <img src="frontend/site/assets/screens/forge-night.webp" alt="锻炉：定制简历和求职信，带有起草、淬炼、封缄的进度和已核实的预览。">
      </picture>
      <h3>⚒️ 锻炉（The Forge）</h3>
      材料会经过<i>起草</i>、<i>淬炼</i>（每一条表述都对照简历核实），在你批准后<i>封缄</i>。
    </td>
  </tr>
</table>

职位提醒会定期搜索并给你发送摘要；**瞭望台（The Lookout）** 显示助手正在使用的浏览器，你可以随时接管。
在侧边栏可以切换羊皮纸色的**日间**主题和冬青绿的**夜间**主题，以及中英文界面。

## 一次职位搜索是怎么进行的

用平常的话提出请求，例如 *“帮我找美国远程的高级后端职位，按我的简历来。”* 助手会：

1. **搜索**经过验证的公司招聘页目录和 LinkedIn，并把不同雇主的结果穿插排列。
2. **完整阅读每一则职位描述**，对照你的要求检查：职位、地点、级别，以及职位是否仍在招聘。
   只会剔除明显不符的，例如完全不同的职业、你要高级职位却是 Staff 级、或你排除的城市。
   接近但不完全符合的会保留，并写明差别。
3. **评估匹配度**：对照你的简历打分，使用你的主模型或可选的 [Jev](#jev)。
   有预算限制的评分会分小批保存，即使因预算停止，已完成的评分也都会保留。
4. **把结果分成三级**，每一级都附有从职位描述中引用的证据：

| 分级 | 含义 |
| :-- | :-- |
| **优先匹配** | 职位、地点和级别都符合，而且你的简历体现了核心要求。 |
| **值得一试** | 相关，但简历中没有体现某项核心技能或经验。每个差距都会列出，并说明它有多重要。 |
| **需要你确认** | 只有你能回答的问题：GPA、工作许可、入职日期，或职位描述没有说清的级别或地点。 |

没有数量指标，也不会用勉强匹配的职位凑数。只有助手保存为推荐的职位才会出现在分级中；
没有评分、或评分后被搁置的职位会单独列出并注明。回复会说明搜了什么、哪里失败了、还剩什么，
你可以回复 *“继续搜”* 让它接着找。

## 一条征途，从初见到录用

每个申请都会经过同样的五个站点。工作由助手完成，决定由你来做。

```mermaid
flowchart LR
    A(["<b>初见</b><br/>搜索招聘页<br/>和 LinkedIn"]) --> B(["<b>备妥</b><br/>分级匹配 · 定制简历、<br/>求职信和问答"])
    B --> C(["<b>已投递</b><br/>在 Chrome 中填表，<br/>其余的问你"])
    C --> D(["<b>面试中</b><br/>针对职位的练习<br/>和反馈"])
    D --> E(["<b>录用</b>"])
    classDef st fill:#16211d,stroke:#7fb08e,color:#e4ece7,stroke-width:1.5px;
    classDef gold fill:#16211d,stroke:#d9b86a,color:#d9b86a,stroke-width:1.5px;
    class A,B,C,D st;
    class E gold;
```

| 站点 | Eregion 做什么 |
| :-- | :-- |
| **初见** | 按你的请求或定时计划搜索招聘页和 LinkedIn，返回带差距和待确认事项的分级推荐。 |
| **备妥** | 展示带证据的匹配分。起草定制简历、求职信和申请问答，并对照简历核实每一条表述。 |
| **已投递** | 在 Chrome 里逐页处理申请，直到审核页：填写已知信息，已批准的文档只上传一次，缺少的信息在对话中问你。 |
| **面试中** | 针对具体职位练习面试问题，提供反馈和最终报告。 |

你可以随时更正、暂停或接管。**批准材料和最终提交永远由你来做。**

### 在真实网站上申请

申请功能在真实的雇主网站上测试过，使用的是开发者本人的资料和登录，每次都停在审核页：

- **Workday**（Adobe 和 Intel）的申请到达了审核页，所有答案都与申请人的信息一致。
  登录通过安全的账号表单完成，你的密码永远不会进入对话或模型。
- 在早期试点中，**Figma、Palantir、Notion、Ampersand 和 Veeva** 的职位也都到达了审核页。
- 简历只上传一次，并在页面上核对；页面上已有的文件、或不是助手上传的文件，会留给你决定。
- 当网站要求 **CAPTCHA、一次性验证码或访问检查**时，助手会停下来告诉你，等你在同一个浏览器里处理。
  只有申请表恢复后它才会继续，也从不读取或保存你的验证码。Eregion 不包含自动破解 CAPTCHA 的功能。

<a id="where-jobs-come-from"></a>
## 职位从哪里来

| 来源 | 设置 | 说明 |
| :-- | :-- | :-- |
| **公司招聘页**（Greenhouse、Lever、Ashby、Workday） | 无需设置 | 一个包含 118 个招聘页的目录，每个都通过其公开 API 验证过。 |
| **LinkedIn**，通过本地 MCP 服务器 | 在单独的浏览器配置中登录一次 | 配置后默认开启。调用有预算限制，遇到登录或限流问题时会停止使用这个来源。见[默认来源](docs/configuration.md#default-job-sources)。 |
| **JobSpy**（可选） | 另一个装有 `python-jobspy` 的 Python，设为 `JOB_AGENT_JOBSPY_PYTHON` | 带完整描述的 Indeed 和 LinkedIn 卡片；Indeed 一次请求就能返回搜索结果。默认关闭，每个任务的请求数有上限。在一次单样本对比中，它速度快、文本完整，但在相同预算下推荐更少，因此默认仍使用招聘页和 LinkedIn（[详情](docs/evaluation-report.md#2--job-sources)）。 |
| **你粘贴或打开的职位** | 无需设置 | 在对话里分享链接或职位文本，或在任何职位页面上使用 **Eregion Lens** Chrome 扩展。 |

Indeed 的 MCP 服务器只接受白名单客户端，因此 Eregion 不使用它。

<a id="jev"></a>
## ⚡ 用 Jev 更快地评估匹配度

[Jev](docs/jev-benchmark.md) 是 Typesafe 推出的快速是/否判断模型。开启后，匹配评分由 Jev 完成，
对话、规划和写作仍由你的主模型负责。不开启 Jev，Eregion 也能完整运行。

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/readme/jev-speed-light.webp">
    <img src="frontend/site/assets/readme/jev-speed-dark.webp" alt="评估匹配度所需时间（每个职位的秒数，同一刻度）：仅主模型 32–49 秒；Jev 处理真实职位平均 0.39 秒；最慢的一次实时搜索中 Jev 每个职位 0.59 秒。" width="70%">
  </picture>
</p>

在 10 月 14 日的场景测试中，Jev 在 **13 次实时搜索中为 150 个真实职位评分**：

| 指标 | 结果 |
| :-- | --: |
| 每个职位的 Jev 耗时（平均） | **0.39 秒** |
| 每个职位的 Jev 耗时（按搜索） | 0.28–0.59 秒 |
| 每个职位的 Jev 请求数 | 2.1 |
| 失败的 Jev 请求 | 317 次中 0 次 |
| 仅用主模型评一个职位（较早的合成测试） | 32–49 秒 |

Jev 耗时是其各次请求时间之和，职位是分批共享评分的。主模型的数据来自另一次冷启动测试，
因此这两行只用于比较量级，并非完全相同的条件。要开启 Jev，把下面几行加入 `.env` 并重启：

```dotenv
TYPESAFE_API_KEY=your-typesafe-key
TYPESAFE_BASE_URL=https://api.typesafe.ai
TYPESAFE_DEFAULT_MODEL=jev-latest
TYPESAFE_ENABLED=true
```

- 每批最多为 16 个职位评分，判断结果在本地缓存，因此中途被打断的评分批次不会为同一个判断付两次费。
- Jev 无法判断的要求会保持*未确定*，分数会以区间显示。
- 不会回退到主模型：如果 Typesafe 无法访问，评分会暂停，直到你修好密钥或关闭 Jev。

匹配请求只发送提取出的简历文本和职位描述；简历文件、截图和密码永远不会离开你的电脑。
所有设置见[配置文档](docs/configuration.md#optional-jev-acceleration)。

## 效果如何

搜索功能用四位合成求职者测试（纽约的初级数据分析师、美国远程的高级后端工程师、加州的机器学习实习生、
奥斯汀的初级 UX 设计师）；申请功能在真实的雇主网站上测试。每次实时测试都有固定的调用和 token 预算，
并且不会提交任何申请。

- **搜索结果：**在最近的四个场景测试中，共展示 123 个职位，结构性错误为 0。人工审核中，
  展示的 23 个职位里错误为 0；29 个标注过的职位中，有 28 个的展示或隐藏符合审核者的判断。
- **分级：**约三分之二与审核者一致；大多数不一致只差一级。
- **申请：**Adobe 和 Intel 的 Workday 申请都以正确的答案到达了审核页，每个申请用了 53–59 次模型调用。
- **如实回复：**回复会说明哪里失败、还剩什么；预算紧张时，没有评分的职位会如实列为未评分，绝不会当作推荐。

已知限制：

- 助手常常较早停止搜索，并会说明这一点、提出可以继续。
- 不同运行之间的判断可能略有差异。
- 评分准确度还没有用人工标注来衡量。
- 较长的 Workday 页面可能用完单页的调用额度；此时运行会暂停，已填写的内容都会保留。

完整的总结（包括过程中失败的部分）见 **[评估报告](docs/evaluation-report.md)**（英文）。

<a id="get-started"></a>
## 快速开始

> [!NOTE]
> 你需要 **Python 3.12**、**Google Chrome**，以及一个支持工具调用和结构化输出的
> **兼容 OpenAI 的**模型 API 密钥。浏览器视觉备援还需要模型支持图片输入。

### 1 · 安装

```sh
git clone https://github.com/ethany14/Eregion-Job-Finding-Agent.git
cd Eregion-Job-Finding-Agent
```

<details open>
<summary><b>macOS 或 Linux</b></summary>

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

</details>

<details>
<summary><b>Windows PowerShell</b></summary>

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

</details>

### 2 · 连接你的模型

在仓库根目录创建 `.env`：

```dotenv
LLM_MODEL_ID=your-model-id
LLM_API_KEY=your-api-key
# 仅当你的服务商使用自定义的兼容接口时需要。
LLM_BASE_URL=https://your-provider.example/v1
```

> [!IMPORTANT]
> 请妥善保管 `.env`；Git 会忽略它。

要添加 LinkedIn 作为来源，请参考[默认来源](docs/configuration.md#default-job-sources)。
只需在单独的浏览器配置中登录一次；不设置它，公司招聘页也能正常使用。

### 3 · 启动 Eregion

```sh
python -m alembic upgrade head
python -m uvicorn eregion.api.main:app --host 127.0.0.1 --port 8000
```

打开 **[localhost:8000/apply](http://localhost:8000/apply)**，上传一份文字可选中的 PDF 简历，
检查**你的资料**，然后说出你的需求：

> 🔎 *帮我按简历找纽约的初级数据分析师职位，并解释差距。*

> ✍️ *为这个职位定制简历并写一封求职信。每一条表述都要有我的经历作为依据。*

> ⏰ *每个工作日纽约时间上午 9 点，推荐类似的新职位。*

助手工作期间、或有定时搜索要运行时，请保持服务器运行。

### 可选开关

默认全部关闭；加入 `.env` 并重启即可。

| 设置 | 作用 |
| :-- | :-- |
| `JOB_AGENT_JOBSPY_PYTHON=path\to\python` | 启用 JobSpy，作为助手可选用的额外来源。`JOB_AGENT_JOBSPY_SITES=indeed,linkedin` 选择网站（默认 `indeed`）。也可以在应用的 **模型与 API 密钥 → 功能** 中开关。 |
| `JOB_AGENT_BROWSER_DRIVER=patchright` | 用 Patchright（已包含在 `requirements.txt` 中）代替默认的 Playwright 来处理申请。 |
| `JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS`、`JOB_AGENT_TASK_MAX_INPUT_TOKENS` | 每个对话的硬性上限，在每次请求发送前计算。 |
| `JOB_AGENT_TASK_MEMORY_MODE=on` | 实验性功能：为较长的申请对话保存恢复摘要。 |

## 需要了解的事

| | |
| :-- | :-- |
| 🔒 **本地、单用户** | 服务器只在你的电脑上监听，不是托管服务。 |
| 🧩 **有些步骤需要你来** | 登录、多因素认证、CAPTCHA 和一次性验证码会让助手暂停，直到你在同一个浏览器里处理完；之后它会确认表单已恢复再继续。 |
| 📊 **分数描述的是证据** | 分数反映的是你的简历体现了什么，而不是雇主的决定。 |
| ✅ **发送前先审核** | 材料会对照你的简历核实，但使用前请你亲自读一遍。 |
| 📄 **文字版 PDF** | 简历需要能选中文字；扫描件需要先做文字识别。 |

## 文档

文档目前为英文。

| 指南 | 内容 |
| :-- | :-- |
| 📘 [使用 Eregion](docs/usage.md) | 日常流程、审批、保存的答案、提醒和恢复 |
| ⚙️ [配置](docs/configuration.md) | 模型、职位来源、LinkedIn MCP、Jev、浏览器和本地数据 |
| 🧭 [架构](docs/agent-loop.md) | 单一的智能体循环、证据、上下文和浏览器工作进程 |
| 🔬 [内部原理](docs/internals/README.md) | 决策循环、上下文工程、提示词、记忆、预算与安全的详细说明 |
| 📈 [评估报告](docs/evaluation-report.md) | Eregion 如何测试，以及结果如何 |
| ⚡ [Jev 与性能](docs/jev-benchmark.md) | 快速评分、记录的测量数据，以及如何复现 |
| 🛠️ [开发](docs/development.md) | 仓库结构、检查、实时评估和参与贡献 |
| 🌍 [网站](https://ethany14.github.io/Eregion-Job-Finding-Agent/) | 概览、用户指南和文档 · [网站源码](frontend/site/README.md) |

<details>
<summary><b>给开发者</b></summary>

代码位于 `backend/`（一个 Python 命名空间：`eregion.runtime`、`eregion.domain`、`eregion.api`）、
`frontend/` 和 `tools/`。可以从[运行时地图](backend/eregion/runtime/README.md)和
[文档索引](docs/README.md)开始。在仓库根目录运行检查：

```sh
python -m pytest -q
node --test "tests/*.mjs"
```

真实 Chrome 测试需要手动开启：设置 `JOB_AGENT_BROWSER_TESTS=1`（再设置
`JOB_AGENT_BROWSER_DRIVER=patchright` 可以用 Patchright 重复测试）。加载 CAPTCHA 服务商官方测试组件的测试
还需要 `JOB_AGENT_OFFICIAL_WIDGETS=1`。

界面文字的英文原文写在 `frontend/web_app` 的代码中，中文翻译在
[`frontend/web_app/i18n-zh.js`](frontend/web_app/i18n-zh.js)；新增界面文字时请用 `t()` 包裹并补上中文。

运行中的服务器还提供 [API 文档](http://localhost:8000/docs) 和[健康检查](http://localhost:8000/health)。

</details>

## 许可证

Eregion 以 [MIT 许可证](LICENSE) 发布。

<p align="center">
  <br>
  <img src="frontend/site/assets/holly.svg" width="36" alt=""><br>
  <sub><i>少一点填表，近一步机会。</i></sub>
</p>
