# 云端自动运行设置

这个项目可以用 GitHub Actions 云端自动运行。设置完成后，你的电脑关机也不影响每天生成预测和结算。

## 自动任务

- 北京时间 11:30：抓取竞彩网在售比赛，生成预测、投注方案和网站。
- 北京时间 12:00：抓取前一天竞彩网赛果，结算盈亏并刷新网站。

对应文件：

- `.github/workflows/daily-forecast.yml`
- `.github/workflows/noon-settlement.yml`

## 第一次设置

1. 新建一个 GitHub 仓库。
2. 把本文件夹里的所有文件上传到仓库。
3. 打开仓库的 `Settings -> Actions -> General`：
   - 允许 GitHub Actions 运行。
   - `Workflow permissions` 选择 `Read and write permissions`。
4. 打开 `Settings -> Pages`：
   - `Source` 选择 `GitHub Actions`。
5. 到 `Actions` 页面手动运行一次 `Daily Sporttery Forecast`。
6. 运行成功后，GitHub Pages 会给你一个网页地址。

## Google 日历

Google 日历里已经有两个每日提醒：

- 11:30 查看今日方案
- 12:00 查看昨日盈亏

云端运行后，把日历事件说明里的本地网页路径换成 GitHub Pages 地址即可。

## 注意

GitHub Actions 的定时任务按 UTC 写：

- `30 3 * * *` = 北京时间 11:30
- `0 4 * * *` = 北京时间 12:00

竞彩数据来自竞彩网官方接口。若竞彩网接口临时不可访问，当天工作流会失败，可以在 GitHub Actions 页面手动重跑。
