# igneous_refresh

传入基础 Cookie，传出带新鲜 `igneous` 的 Cookie。JSON 进，JSON 出。

## 原理

exhentai.org 会给「带着有效基础 Cookie（`ipb_member_id` + `ipb_pass_hash`）、但**没有** igneous」的请求签发一个新的 igneous（响应头 `Set-Cookie: igneous=...`，有效期 30 天），前提是请求来自站点认可的网络/IP。本服务做的就是这一次请求：剥掉调用方可能带来的旧 igneous，只带基础 Cookie 访问首页，把上游签发的 Cookie 合并后返回。出口网络合不合格由 `REFRESH_PROXY` 指向的代理决定。

## 运行

```bash
docker build -t igneous_refresh .

docker run -d --name igneous_refresh \
  -p 8080:8080 \
  -e REFRESH_PROXY='http://user:pass@proxy-host:3128' \
  -e REFRESH_PATH='/refresh' \
  igneous_refresh
```

或用 compose：把 `REFRESH_PROXY` 写进 `.env` 后 `docker compose up -d --build`。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REFRESH_PROXY` | 空（直连） | 刷新请求走的代理。支持 `http`、`https`、`socks4`、`socks4a`、`socks5`、`socks5h`，可带鉴权，如 `socks5://user:pass@host:1080` |
| `REFRESH_PATH` | `/refresh` | 只有向这个 path 发 POST 才能触发刷新，其余路径一律 404 |
| `PORT` | `8080` | 监听端口 |
| `REFRESH_TIMEOUT` | `30` | 上游请求超时（秒） |
| `TARGET_URL` | `https://exhentai.org/` | 上游地址，仅供测试，生产不要改 |

## 接口

请求（以下三种写法等价）：

```bash
curl -X POST http://127.0.0.1:8080/refresh \
  -H 'Content-Type: application/json' \
  -d '{"cookie": "ipb_member_id=1234567; ipb_pass_hash=0123456789abcdef"}'
```

```json
{"cookies": {"ipb_member_id": "1234567", "ipb_pass_hash": "0123456789abcdef"}}
```

```json
{"ipb_member_id": "1234567", "ipb_pass_hash": "0123456789abcdef"}
```

成功（200）：

```json
{
  "ok": true,
  "igneous": "xxxxxxxxxx",
  "cookies": {
    "ipb_member_id": "1234567",
    "ipb_pass_hash": "0123456789abcdef",
    "igneous": "xxxxxxxxxx"
  },
  "cookie": "ipb_member_id=1234567; ipb_pass_hash=0123456789abcdef; igneous=xxxxxxxxxx"
}
```

失败：

- `400 {"ok": false, "error": "..."}` — 请求体不是 JSON，或缺 `ipb_member_id` / `ipb_pass_hash`
- `502 {"ok": false, "error": "..."}` — 上游没签发 igneous（基础 Cookie 无效、出口 IP 不被站点接受、代理不通等）
- `404` — 路径不是 `REFRESH_PATH`

健康检查：`GET /healthz` → `{"ok": true}`，不会触发刷新。

## 注意

- 输入里就算带了旧的 `igneous` 也会被剥掉——上游只给不带 igneous 的请求签发新的。
- 本服务自身没有鉴权，`REFRESH_PATH` 只是路径门控，别把它直接暴露在公网上；放内网或自有网关后面用。
- Cookie 值不会写进服务日志。
