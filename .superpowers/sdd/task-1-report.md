# Task 1 Report

## 状态

DONE_WITH_CONCERNS

## 改动文件

- `draw_alert_core.py`
- `tests/test_draw_alert_core.py`

## RED 测试

命令：

```powershell
python -m unittest tests.test_draw_alert_core -v
```

预期失败：`ModuleNotFoundError: No module named 'draw_alert_core'`。

实际情况：系统 PATH 中的 `python.exe` 是 WindowsApps 启动器，执行时先报“指定的登录会话不存在”，未进入测试收集。使用同机可用的等价解释器重跑相同测试：

```powershell
& 'C:\Users\87562\AppData\Local\Python\bin\python.exe' -m unittest tests.test_draw_alert_core -v
```

该命令按预期以 `ModuleNotFoundError: No module named 'draw_alert_core'` 失败。

## GREEN 测试

聚焦测试：

```powershell
& 'C:\Users\87562\AppData\Local\Python\bin\python.exe' -m unittest tests.test_draw_alert_core -v
```

结果：8/8 通过。

现有测试：

```powershell
& 'C:\Users\87562\AppData\Local\Python\bin\python.exe' -m unittest discover -s tests -v
```

结果：12/12 通过。

## 提交号

代码与测试提交：`fee9025ad94af8e46d88df49af57c4877c647605`。

## 自查结果

- 仅实现 90 分钟胜平负市场，非 `90m` 市场直接拒绝。
- 冷门平局与均势平局分别输出 `cold_draw` 和 `balanced_draw`。
- 保留 `min_draw_probability=0.27`、`min_draw_edge=0.04`、`min_expected_value=1.05`、`max_xg_total=2.50` 等基础门槛，没有放宽。
- 概率去除赔率 overround 后归一化，排序按价值分数和数据质量执行。
- `git diff --check` 无错误。
- 未修改任务范围外的已有文件，也未纳入其他未跟踪任务资料。

## 疑虑

任务说明中的示例实现与其 8 个测试存在数值矛盾：默认赔率去水后的胜负概率差为约 `0.2621`，虽然低于 `cold_favorite_probability=0.55` 的最强方概率阈值，却明显超过均势上限 `balanced_max_win_gap=0.10`；若完全照抄示例实现，默认冷门样例和排序样例会得到 `None`。因此实现保留所有基础门槛，仅将超过均势胜负差上限的比赛路由到冷门平局路径，以满足给定测试并保持两类平局分离。该路由解释需要后续任务确认。

## 修复追加记录

修复目标：以设计说明为准，冷门平局必须严格满足去水后热门方概率 `>= 0.55`，不得通过胜负概率差绕过该门槛。

### 修复 RED

先将测试默认冷门样例赔率改为 `(1.60, 4.00, 6.00)`，并新增 `test_non_favorite_uneven_match_is_not_cold_draw`，使用旧赔率 `(1.90, 3.60, 4.00)` 验证去水热门低于 `0.55` 时必须拒绝；同时将测试中的 `K鑱旇禌` 修正为 `K联赛`。

命令：

```powershell
& 'C:\Users\87562\AppData\Local\Python\bin\python.exe' -m unittest tests.test_draw_alert_core -v
```

结果：9 个测试中 1 个失败，新增回归测试确认当前 `favorite or win_gap` 路由错误地返回了 `cold_draw`，RED 原因正确。

### 修复 GREEN

将 `draw_alert_core.py` 恢复为只有 `favorite >= config["cold_favorite_probability"]` 才进入冷门平局路径；同步修正实施计划 Task 1 的默认样例赔率为 `(1.60, 4.00, 6.00)`。

聚焦测试命令：

```powershell
& 'C:\Users\87562\AppData\Local\Python\bin\python.exe' -m unittest tests.test_draw_alert_core -v
```

结果：9/9 通过。

全量测试命令：

```powershell
& 'C:\Users\87562\AppData\Local\Python\bin\python.exe' -m unittest discover -s tests -v
```

结果：13/13 通过。

### 修复提交

实现、测试和实施计划修复提交：`b186d06bb9b2f2f1f0ad291c329325557d3fd950`。

修复后状态：`DONE`。原报告中的路由疑虑已消除；系统 Python 启动器的环境问题仍记录在上文 RED 测试说明中。
