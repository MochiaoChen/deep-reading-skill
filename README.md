# 强力研读 · deep-reading

一个 Claude Code / Claude Desktop **Skill**：把一本非虚构书「长」进你的大脑里，产出一份**日后可以取代原书**的笔记。

方法出自万维钢《万万没想到·用强力研读书》，建立在《如何阅读一本书》之上。

---

## 为什么

关于非虚构书有三个残酷的事实：

1. 大多数人不读这种书。
2. 读了的人大多没读完 —— Kindle 划线数据显示，《时间简史》平均读到 6.6%，《思考，快与慢》6.8%，《二十一世纪资本论》2.4%。
3. 读完了的人大多没读懂 —— 《卧底经济学》通篇讲「稀缺」，而豆瓣热门书评前四篇没有一篇提到这两个字；它们记住的是「咖啡」，那是全书前四页的事。

用那种读法，再读十五本经济学的书也学不会用经济学家的眼光看世界，只能收获一堆饭桌上的段子。

**读书的目的是获得见识以及学习高水平的思维方法。** 这个技能就是为此设计的。

---

## 它做什么

强制每份笔记满足四个要素，缺一不可：

| | 要素 | 谁来做 |
|---|---|---|
| ① | **逻辑脉络** —— 用自己的话写出每章到底想说什么，跳过所有举例的故事 | Claude |
| ② | **亮点** —— 把拍案叫绝的东西连细节一起带走，且**分布不均匀** | Claude |
| ③ | **心得** —— 自己的看法、质疑、灵感 | **你，不能外包** |
| ④ | **联系** —— 这个问题别的书怎么说？谁的证据更强？ | Claude 检索 + 你判断 |

许多笔记只有摘要概括。能做到第一点就已经算优秀笔记了。四点全做到的凤毛麟角 —— 而回报是巨大的。

### 一条铁律

**③心得必须来自你本人，Claude 不代写。**

摘要可以外包，内力不能。Claude 在这一栏只提问、不填答案；你答完它再誊写进笔记。这是这个技能和「让 AI 给我总结一本书」的根本区别。

---

## 安装

**方式一：直接放进 skills 目录（推荐，最稳）**

```bash
git clone https://github.com/MochiaoChen/deep-reading-skill.git
mkdir -p ~/.claude/skills
cp -r deep-reading-skill/skills/deep-reading ~/.claude/skills/
```

只想给某个项目用，就复制到该项目的 `.claude/skills/` 下。

**方式二：作为 plugin 安装**

```
/plugin marketplace add MochiaoChen/deep-reading-skill
/plugin install deep-reading
```

装好后新开一个会话即可。

---

## 怎么用

直接说人话就行，技能会自动触发：

```
我在读《卧底经济学》，陪我强力研读一遍
```
```
这本 EPUB 帮我拆成章，我们一章一章过
```
```
这是我读《思考，快与慢》的笔记，帮我看看行不行
```
```
把我 Apple Books 里的划线整理一下
```
```
帮我看看笔记库里哪些书是孤岛
```

### 两种模式

- **陪读（默认）** —— 你读，Claude 当第二遍的笔杆子和提问者。**产出的是内力。**
- **代读** —— 你把 PDF 丢给它，它产出脉络、亮点、联系，把心得留成问题清单给你。**产出的是地图，不是内力。**

技能会在开工前明确告诉你走的是哪种，不会默默降级。

---

## 附带的三个脚本

三个脚本**只依赖 Python 3 标准库**，不用 pip install 任何东西。

### 1. 划线导入 —— Apple Books / Kindle

```bash
python3 scripts/extract_highlights.py --list              # 哪些书有划线
python3 scripts/extract_highlights.py --source apple      # Apple Books（默认）
python3 scripts/extract_highlights.py --source apple --style 黄
python3 scripts/extract_highlights.py "My Clippings.txt"  # Kindle
```

**Apple Books** 直接读本机标注库，不需要你导出任何东西 —— 书名、作者、划线原文、
附注、**高亮颜色**、时间都能带出来。用颜色编码的人（黄=事实、蓝=存疑）可以 `--style` 分色取。

**Kindle** 认 `My Clippings.txt`，中英文版都行，按书分组并自动去重（同一段反复划线只留最完整的一条）。

> 读不到 Apple Books 数据库最常见的原因是终端没有「完全磁盘访问权限」，脚本会直接把系统设置路径告诉你。

### 2. 正文提取 —— PDF / EPUB

```bash
python3 scripts/book_to_text.py 书.epub --toc              # 先看章节结构和字数
python3 scripts/book_to_text.py 书.pdf --split ./chapters  # 一章一个文件
```

**EPUB** 纯标准库解析，从 `nav.xhtml` / `toc.ncx` 取真实章节名。

**PDF** 优先用系统里已有的 `pdftotext` / `mutool` / `pypdf` / `pdfminer` / `PyMuPDF`；
一个都没装时退回**内置的纯标准库提取器**（zlib + ToUnicode CMap），能处理大多数含嵌入文本的
PDF，包括中文 CID 字体。

遇到字体缺 ToUnicode 表或扫描件时，它**报错而不是吐乱码** —— 并告诉你三条出路
（装 poppler / 装 pypdf / 直接把 PDF 交给 Claude 的 Read 工具按页读）。

默认按章切分，因为「读一章，记一章」既是方法要求，也是上下文预算的要求。

### 3. 笔记库织网

```bash
python3 scripts/link_notes.py ~/notes/reading --report      # 孤岛 / 待写 / 枢纽笔记
python3 scripts/link_notes.py ~/notes/reading --index       # 生成 INDEX.md
python3 scripts/link_notes.py ~/notes/reading --backlinks   # 写入反向链接
```

`--report` 里有一节叫「**该写文章了**」：当同一个议题在三篇以上的笔记里出现不同结论时它会提醒你 —— 到了那个层次，你已经和作者完全平等，甚至可以俯视他们、评判他们的高下。

---

## 目录

```
skills/deep-reading/
├── SKILL.md                        # 操作手册：铁律、两种模式、四条工作流
├── references/
│   ├── method.md                   # 心法：为什么慢、为什么两遍、为什么笔记是核心
│   ├── note-rubric.md              # 四要素 0–3 分评分表 + 正反例
│   └── triage.md                   # 这本书配不配读两遍
├── assets/
│   ├── note-template.md            # 笔记模板
│   └── library-index-template.md   # 笔记库索引模板
└── scripts/
    ├── extract_highlights.py       # Apple Books / Kindle 划线导入
    ├── book_to_text.py             # EPUB / PDF → 分章文本（零依赖）
    └── link_notes.py               # [[链接]] 网络
```

---

## 几个会被它拦下来的做法

- **思维导图做笔记** —— 有实验：三组人读同一篇文章，写文章组 > 多读几遍组 > 画概念图组。画概念图**还不如单纯多读几遍**。眼过千遍不如手过一遍。
- **每章字数差不多的工整笔记** —— 好的读书笔记是不均匀分布的。均匀是在交作业。
- **划段落首句当重点** —— 那是小学生找中心句。真正的读法是「一惊一乍」的。
- **追求读得快** —— 有必要快速读完的书根本不配我们读。娱乐小说和新闻越快越好，学习区里的好书要慢慢读。
- **读三遍以上** —— 思想类书籍两遍正好，而且读完一遍马上读第二遍。

---

## 出处与许可

方法出自万维钢《万万没想到——用理工科思维理解世界》「用强力研读书」一章。本仓库是对该方法的**工程化实现**，不包含原书文本。

代码与文档以 MIT 许可发布。
