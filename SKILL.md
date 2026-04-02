---
name: insdl
description: "Instagram 下载（/p/ /reel/ /tv/）：将图片/视频保存到本地 `/sdcard/Pictures/Instagram/{shortcode}/`。默认走 Python 脚本，支持 Cookie 登录态、Chromium 本地登录态解密、错误分类与一次重试。用于用户发 Instagram 链接并要求本地保存。"
---

# Instagram 本地下载（insdl）

## 用法

```bash
python ~/project/insdl/scripts/ig.py --url "<instagram_url>"
```

认证检查：

```bash
python ~/project/insdl/scripts/ig.py --auth-check
```

默认保存目录：

- `/sdcard/Pictures/Instagram/{shortcode}/`

成功输出：

- `SAVED_DIR|<path>`
- `COUNT|<n>`

## 登录态来源（按优先级）

1. 环境变量：`INSDL_COOKIE` / `INSDL_COOKIE_STRING`
2. `~/project/insdl/.env`
3. `~/project/insdl/scripts/cookie.txt`
4. 本机 Chromium Cookie DB + Safe Storage 直接解密

脚本会输出：

- `AUTH|env`
- `AUTH|dotenv:...`
- `AUTH|file:...`
- `AUTH|browser:<browser>[<profile>]`
- `AUTH|none`

## Chromium 本地登录态解密

脚本直接读取本机 Chromium 系浏览器的 Cookie DB，并结合 Safe Storage 密钥解密 Instagram Cookie。支持：

- Chrome
- Chromium
- Edge
- Brave
- Arc（环境支持时）

可选环境变量：

```bash
INSDL_BROWSER=chromium
INSDL_CHROME_PROFILE=Default
```

说明：

- 只提取并发送 `*.instagram.com` 的 Cookie
- 只输出 Cookie 来源，不回显 Cookie 内容
- 若浏览器里只有匿名 Cookie、没有 `sessionid`，脚本会判定为不可用登录态
- 不再依赖 `browser-cookie3`

## 当前优化点

- Cookie 仅对白名单域名 `*.instagram.com` 发送
- 认证链路支持“手工 Cookie + 本机 Chromium 直接解密”
- 错误分类：`AUTH_REQUIRED` / `RATE_LIMITED` / `PRIVATE_OR_UNAVAILABLE` / `NETWORK`
- 重试策略：认证/私密快速失败；限流/网络最多重试 1 次
- 下载原子写入：`.part` 写完后再重命名
- 去重优先按媒体 id（无 id 回退 URL）
- 新增 `--auth-check` 便于先确认登录态是否可用

## 备注

- 支持公开内容：`/p/`、`/reel/`、`/tv/`
- 若提示 `/sdcard` 无权限，先执行：

  ```bash
  termux-setup-storage
  ```
- 若自动提取失败：优先确认浏览器已登录 `instagram.com`，并且当前浏览器属于 Chromium 系（Chrome / Chromium / Edge / Brave / Arc）
