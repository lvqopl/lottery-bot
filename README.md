# Telegram Giveaway Bot

一个基于 Telegram Bot API 的抽奖机器人，已拆分为多文件结构。

## 结构

- `main.py` 启动入口
- `giveaway_bot/utils.py` 通用工具、时长解析、签名链接
- `giveaway_bot/models.py` 数据模型
- `giveaway_bot/storage.py` SQLite 持久化
- `giveaway_bot/telegram_api.py` Telegram API 封装
- `giveaway_bot/bot.py` 抽奖业务逻辑

## 功能

- `/new_giveaway` 直接创建抽奖，支持一条命令携带参数
- 群内 `@bot` 自然语言创建抽奖，管理员在群里直接描述即可
- 群里点击按钮后跳转到私聊参与，不在群里记录参与
- 已参与人数实时显示并回写公告
- 到期自动开奖、指定人数触发自动开奖、提前开奖、取消、导出参与名单
- 指定用户权重、邀请好友增加权重
- 可选创建领奖话题，向中奖者发送领奖话题邀请链接
- 领奖话题到期自动删除
- SQLite 持久化与操作日志

## 自然语言创建

管理员可以直接在群里 `@bot` 发送一段自然语言来创建抽奖，不需要严格顺序，也不需要先私聊。机器人会从消息里提取这些字段：

- `标题/主题/名称/活动`：抽奖标题，默认 `群内抽奖`
- `奖品`：奖品内容，默认 `奖品1`
- `中奖人数/抽奖人数/人数`：中奖人数，默认按奖品条目数量推断
- `开始时间`：开始时间，默认立即开始
- `结束时间/截止时间`：结束时间
- `时长/持续/多久`：如果没有写结束时间，可以用时长表达，例如 `8h`、`1d2h30m`
- `发布到/公布到/发到`：结果发布的群组或频道，默认当前群
- `关注/加入/需关注`：参与前需要检查是否已加入的群组或频道
- `领奖群聊/领奖群组/领奖群` 和 `领奖话题/话题`：用于开奖后创建领奖话题
- `邀请` 和 `权重`：用于邀请门槛、邀请加权和指定用户权重
- `达到 X 人开奖/满 X 人开奖`：达到指定参与人数后自动开奖

示例：

```text
@bot 新年抽奖，奖品 AirPods，抽 3 人，8 小时后结束，发布到 @mygroup，需关注 @mychannel，@123456 权重 100，满 100 人开奖，领奖群聊 @claimgroup，领奖话题 领奖话题
```

## 发布抽奖

管理员也可以使用命令创建抽奖。推荐在私聊里执行 `/new_giveaway`，并把参数一次写全。群内按钮只负责跳转和参与，不会替代发布流程。

```text
/new_giveaway -title "新年抽奖" -prize "AirPods" -num 3 -condition "关注频道并参与" -start now -t 8h -methods button,keyword,channel,invite -keyword "抽奖" -check_channel @mychannel -invite_need 2 -invite_bonus 1 -weight "123456:100,234567:50" -publish @mygroup -draw_n 100 -claim_group @claimgroup -claim_topic "领奖话题" -claim_hours 72
```

常用参数说明：

- `-title`：抽奖标题。
- `-prize`：奖品。
- `-num`：中奖人数。
- `-start now`：立即开始，或填写具体时间。
- `-t 8h`：从开始时间起 8 小时后结束，也可以写 `1d2h30m`。
- `-check_channel @xxx`：要求参与者先关注指定群组或频道。
- `-invite_need 2`：要求先成功邀请 2 人。
- `-invite_bonus 1`：每成功邀请 1 人，增加 1 点权重。
- `-weight "123:100"`：指定某些 Telegram 用户 ID 的权重。
- `-publish @group`：开奖结果发布到哪个群组或频道。
- `-draw_n 100`：参与人数达到 100 后自动开奖。
- `-claim_group @group` 和 `-claim_topic "领奖话题"`：开奖后可自动创建领奖话题。

## 本地运行

1. 保留仓库里的 `.python-embed` 目录。
2. 设置 `BOT_TOKEN`。
3. 如需代理，设置 `HTTPS_PROXY` 或 `HTTP_PROXY`。
4. 运行：

```powershell
.\.python-embed\python.exe main.py
```

## 说明

- 参与都在私聊中完成，群里的按钮只负责跳转。
- 如果不填写 `-t` 或 `-end`，抽奖可以是不限时长；也可以通过 `-draw_n` 达到人数后自动开奖。
- 若启用领奖话题，机器人需要在领奖群中具备创建/删除论坛话题和创建邀请链接的权限。
