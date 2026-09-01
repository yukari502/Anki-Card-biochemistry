# Markdown 转 Anki 生成器

这是一个本地运行的 Markdown → Anki 工具。它既可以转换单个 Markdown 文件，也可以递归读取一个完整牌组文件夹；文件夹层级会映射成 Anki 的主牌组与 subdeck，图片、MathJax、Mermaid、表格和原生 HTML 会一并处理。

面向日常使用的详细步骤见 [使用说明.md](使用说明.md)；本文主要记录当前实现、设计约束、整体工作流和后续维护计划。

## 当前状态

目前已经实现并验证：

- 基于 `tkinter` 的图形界面，可选择单个 Markdown 或完整牌组文件夹。
- 按独占一行的 `---` 分割卡片，提取 `### Front` 与 `### Back`。
- 递归查找 `.md` / `.markdown` 文件，将子文件夹映射为多层 subdeck。
- 标准 Markdown 转 HTML，包括加粗、列表、表格、普通代码块。
- 保留原生 HTML，例如 `<mark>`，并提供对应高亮样式。
- 保护 Anki MathJax 定界符 `\(...\)` 与 `\[...\]`，避免 Markdown 转换破坏反斜杠。
- 将 Mermaid 围栏代码块转换为 `<div class="mermaid">`，在正反面模板中加载 Mermaid 10。
- 将 `![说明](media/图片.png)` 和 `![说明](/media/图片.png)` 转换为 Anki 图片标签并打包媒体。
- 图片不存在时只记录警告并跳过，不中断整个牌组。
- 不同 Chapter 出现同名图片时，自动给冲突图片添加稳定摘要，避免 Anki 的扁平媒体目录发生覆盖。
- 使用后台线程生成牌组，GUI 在打包期间仍可刷新日志。
- 使用参考项目 `Ref/anki_generator` 的 Apple 风格卡片布局，并补充黄色标记、图片限制、表格、代码和夜间模式。
- 模型 ID、牌组 ID 和卡片 GUID 均采用固定或确定性生成策略，支持重复生成后导入更新。

已做过端到端验证：多 Chapter subdeck、两个 Chapter 的同名图片、MathJax、Mermaid、Markdown 表格、缺失图片、单文件模式，以及修改 Back 后卡片 GUID 保持不变。

## 整体使用计划

完整闭环分为五步：

1. **让 NotebookLM 整理资料**：使用 [Plan.md](Plan.md) 中的严格提示词生成 Front/Back 卡片，要求每张卡以 `---` 结束。
2. **积累 Markdown**：按课程、章节保存到主牌组文件夹中的不同 Chapter 子目录。
3. **准备媒体**：截图或保存教材图片，放在对应 Markdown 同目录的 `media` 文件夹，并使用标准 Markdown 图片语法引用。
4. **生成牌组**：在本程序中选择单个 Markdown 或主牌组文件夹，生成 `.apkg`。
5. **导入和增量更新**：导入 Anki；以后补充答案或添加卡片后重新生成并再次导入，稳定 GUID 会让已有卡片得到更新。

推荐对长期维护的卡片使用显式 `anki-id`，这样即使以后修改 Front 或移动文件，Anki 仍能识别为同一张卡。

## 安装与运行

需要 Python 3.10 或更高版本。

```powershell
python -m pip install -r requirements.txt
python markdown_to_anki.py
```

Windows 也可以双击 [run.bat](run.bat)。依赖目前只有：

- `genanki`：创建 Anki 模型、牌组与 `.apkg`。
- `Markdown`：把普通 Markdown、表格和围栏代码转换为 HTML。

## 从 `.apkg` 反向导出并往返编辑

正向和反向功能已经合并在同一个程序中。运行：

```powershell
python markdown_to_anki.py
```

也可以双击 `run.bat`，在同一窗口选择“Markdown → Anki”或“Anki → Markdown”。反向导出会在包的同目录创建同名文件夹。命令行用法：

```powershell
python markdown_to_anki.py export "细胞生物学.apkg"
python markdown_to_anki.py export "细胞生物学.apkg" -o "自定义输出文件夹"
python markdown_to_anki.py build "细胞生物学" -o "重新生成.apkg"
```

导出结果包含：

- 按原 subdeck 分目录的 `cards.md`；
- 每个 Markdown 同目录下实际引用的 `media` 文件夹；
- `unreferenced_media` 中原包携带但当前卡片未引用的媒体；
- 隐藏元数据 `.anki-roundtrip.json`，用于保留原模型、模板、牌组 ID 和未引用媒体；
- 每张卡片中的 `anki-guid`、`anki-model-id`、`anki-tags` 和 `anki-raw-html` 注释。

名称为 `Ch.1` 至 `Ch.9` 的 Anki 子牌组会导出成自然排序友好的目录 `Ch.01` 至 `Ch.09`；`Ch.10` 及以后保持原数字。`.anki-roundtrip.json` 仍保存原始子牌组名，所以重新打包后 Anki 内的牌组名称和 ID 不会改变。

编辑时可以修改 `### Front`、`### Back`、`### OriginalMaterial` 下面的内容及其媒体文件，但不要删除或修改上述 `anki-*` 注释，也不要删除 `.anki-roundtrip.json`。完成后，在 `markdown_to_anki.py` 中选择整个导出文件夹重新打包；程序会直接使用原始 GUID，而不是重新计算 GUID。

为保证首次“导出 → 原样重打包”时字段逐字节一致，反向导出的正文保留为 Markdown 允许的原生 HTML，并带有 `anki-raw-html`。修改这类卡片时请继续写 HTML；如果希望改用 `**粗体**` 等普通 Markdown 语法，可以删除该卡的 `anki-raw-html` 注释，GUID 仍会保留，但重新渲染后的 HTML 字段不再与原包逐字节相同。

导出器默认不覆盖已存在的同名文件夹，避免误删已经编辑的内容。Anki 数据库中没有生成任何卡片的孤立笔记不会导出，因为它们无法用当前 Front/Back 卡片格式表示。当前支持含 `collection.anki2` 或未压缩 `collection.anki21` 的 `.apkg`；若包只含压缩的 `collection.anki21b`，请先在 Anki 中导出为兼容旧版的包。

## 推荐目录结构

```text
Biochemistry/
├─ Chapter01_蛋白质/
│  ├─ cards.md
│  └─ media/
│     └─ peptide_bond.png
├─ Chapter02_酶/
│  ├─ kinetics.md
│  └─ media/
│     └─ kinetics.png
└─ Chapter03_代谢/
   └─ Section01_糖酵解/
      ├─ glycolysis.md
      └─ media/
         └─ glycolysis.png
```

选择 `Biochemistry` 后会生成：

```text
Biochemistry
├─ Chapter01_蛋白质
├─ Chapter02_酶
└─ Chapter03_代谢
   └─ Section01_糖酵解
```

Anki 内部名称分别类似 `Biochemistry::Chapter01_蛋白质` 和 `Biochemistry::Chapter03_代谢::Section01_糖酵解`。文件夹模式默认输出到 `Biochemistry/Biochemistry.apkg`；单文件模式输出到 Markdown 同目录下的同名 `.apkg`。

## 卡片格式

````markdown
<!-- anki-id: biochem-urea-cycle-001 -->
### Front
尿素循环的主要作用是什么？

### Back
将有毒的氨转化为相对无毒的<mark>尿素</mark>。

关键反应：\(NH_3 \rightarrow CO(NH_2)_2\)。

![尿素循环](media/urea_cycle.png)

```mermaid
flowchart LR
    A[氨] --> B[尿素]
```

---
````

文件统一使用 UTF-8；UTF-8 BOM 也受支持。

## 稳定 ID 设计

这里的“ID 相同”指同一张卡在不同生成批次中保持相同 GUID。不同卡片仍必须有不同 GUID，否则 Anki 无法区分它们。

### 显式 ID（推荐）

```markdown
<!-- anki-id: biochem-urea-cycle-001 -->
```

- 可使用英文字母、数字、点、下划线、冒号和连字符。
- 必须在整个牌组中唯一。
- 修改 Front、Back，或者移动和重命名 Markdown 文件，GUID 都保持不变。
- 已经使用的 `anki-id` 不应修改或复用。

反向导出的卡片会改用 URL 编码的精确 GUID：

```markdown
<!-- anki-guid: f%2FuxQ%26TV%3Cy -->
```

`anki-guid` 的优先级高于 `anki-id`，重新打包时会原样解码并写回。它是 Anki 用于识别同一条笔记的 GUID；数据库中的数字 note/card ID 属于具体用户资料和导入批次，不是可移植身份，不应当作 UUID 使用。

### 自动 ID

不写 `anki-id` 时程序会自动生成：

- 文件夹模式：根据“Markdown 相对路径 + Front 内容”生成。
- 单文件模式：继续使用旧版的“牌组名 + Front 内容”算法，以兼容此前已经生成的卡片。

自动模式下可以安全修改 Back。修改 Front、移动文件或重命名文件可能产生新的 GUID，因此长期维护时建议补充显式 `anki-id`。

### 维护时绝对不要随意修改

- `MODEL_ID = 2_026_082_901`：修改后 Anki 会把模板视为另一种笔记类型。
- GUID 命名空间 `markdown-to-anki-v1`：修改会让所有使用新算法的卡片变成新卡。
- `stable_deck_id()` 的摘要算法：修改会改变牌组 ID。
- 已发布卡片的 `anki-id`。

如果未来确实需要变更这些内容，应先设计迁移方案并用一个复制的 Anki 用户资料测试，不能直接替换。

## 媒体处理规则

图片路径相对于当前 Markdown 文件，而不是主牌组根目录：

```text
Chapter02_酶/
├─ kinetics.md
└─ media/
   └─ curve.png
```

在 `kinetics.md` 中引用：

```markdown
![动力学曲线](media/curve.png)
```

程序会验证解析后的物理路径仍在该 `media` 文件夹内，拒绝 `..` 等越界路径。Anki 的媒体目录没有子文件夹，因此打包前会在临时目录中暂存图片；同名冲突文件使用稳定摘要重命名，并同步更新卡片中的 `<img src>`。

## 代码结构

主要逻辑集中在 [markdown_to_anki.py](markdown_to_anki.py)：

- `parse_cards()`：分割并验证卡片，读取可选 `anki-id`。
- `_convert_images()`：定位图片、处理缺失和同名冲突、生成 HTML。
- `_convert_mermaid()`：把 Mermaid 围栏转换成可渲染容器。
- `markdown_to_html()`：保护 MathJax 后执行 Markdown 转换。
- `_find_markdown_files()`：发现文件夹模式下的 Markdown 文件。
- `build_anki_package()`：建立模型、主牌组/subdeck、稳定 GUID、媒体暂存并写出 `.apkg`。
- `export_anki_package()`：从 `.apkg` 导出 Markdown、媒体及往返元数据。
- `AnkiMarkdownApp`：在同一窗口提供两个转换方向，并在后台执行和记录日志。

其他文件：

- [使用说明.md](使用说明.md)：面向使用者的完整操作说明。
- [Plan.md](Plan.md)：NotebookLM 提示词和资料整理工作流。
- [example_cards.md](example_cards.md)：包含显式 ID、图片、MathJax、Mermaid 和表格的示例。
- [requirements.txt](requirements.txt)：Python 依赖版本范围。
- [run.bat](run.bat)：Windows 快速启动入口。
- `Ref/anki_generator/`：旧版 AI 生成项目，仅作为 CSS、Markdown、MathJax 和模板实现参考；当前工具不依赖其中的 API 配置或虚拟环境。

## 已知限制

- Mermaid 当前从 jsDelivr CDN 加载，离线或网络受限时流程图可能不显示。
- 重新导入 `.apkg` 可以更新相同 GUID 的卡片，但不会自动删除 Anki 中已经存在、后来从 Markdown 删除的旧卡片。
- 没有显式 `anki-id` 时，修改 Front 或移动文件可能生成新卡。
- 当前没有在 GUI 中预览最终 HTML，也没有生成前的差异报告。
- 当前验证以端到端脚本检查为主，尚未建立持续运行的自动化测试套件。

## 后续维护计划

按优先级建议推进：

### 第一阶段：可靠性

- 为卡片解析、稳定 GUID、目录到 subdeck 映射、图片冲突和 MathJax 保护建立正式自动化测试。
- 增加“生成前检查”模式：只扫描格式、重复 `anki-id`、缺失媒体和空卡片，不写 `.apkg`。
- 对重复显式 `anki-id` 给出更醒目的汇总报告，并允许直接定位来源文件。
- 生成一份可保存的构建报告，记录牌组、卡片数、媒体数、警告和输出位置。

### 第二阶段：维护体验

- 在 GUI 中显示 Markdown 文件树、subdeck 映射和卡片数量预览。
- 支持自定义主牌组名称和输出位置，同时保持内部 ID 策略稳定。
- 提供可选的 `anki-id` 辅助生成工具，为没有 ID 的旧 Markdown 批量写入唯一 ID；执行前必须备份并展示变更。
- 增加“仅验证”和“生成牌组”两个明确按钮。

### 第三阶段：离线与可移植性

- 研究把 Mermaid 运行资源随牌组打包，减少对 CDN 的依赖；需要先验证 Anki Desktop、AnkiMobile 和 AnkiDroid 的脚本兼容性。
- 为 Windows 提供更友好的独立可执行文件或安装包，减少 Python 环境配置。
- 增加配置文件，但只保存界面和输出偏好，不把卡片身份依赖于易变配置。

### 可选扩展

- 增加 HTML 预览与夜间模式预览。
- 支持标签、来源、章节编号等额外字段，但需保持当前模型的迁移兼容。
- 增加构建版本信息，便于排查某个 `.apkg` 由哪个工具版本生成。

## 修改后的回归检查清单

未来每次修改核心逻辑后，至少确认：

1. 同一份输入连续生成两次，所有 note GUID 完全一致。
2. 只修改 Back 后 GUID 不变，重新导入能更新原卡。
3. 使用显式 `anki-id` 时，修改 Front 或移动文件后 GUID 仍不变。
4. 多层目录正确生成 `主牌组::Chapter::Section`。
5. 两个 Chapter 中的同名图片都能显示且不会覆盖。
6. 缺失图片只产生警告，不阻止其他卡片生成。
7. `<mark>`、表格、MathJax、Mermaid、夜间模式正常。
8. 单文件模式仍兼容旧版自动 GUID。
9. 输出 `.apkg` 可以被目标 Anki 版本正常导入。

维护原则：优先保证已发布卡片的 GUID 与模型 ID 稳定，再考虑界面或格式功能扩展。
