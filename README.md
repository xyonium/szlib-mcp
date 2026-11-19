# szlib-mcp

深圳图书馆数字资源的中文文献检索 MCP。通过图书馆读者账号访问**读秀学术搜索**，
把登录与验证码变成一次性网页交给用户完成，服务本身可以在无图形界面的服务器上运行。

## 能力

| 工具 | 作用 |
|---|---|
| `search_literature(query, limit, session)` | 读秀检索，返回类型/标题/作者/出处/年份 |
| `login(session, login_token)` | 发起或完成登录 |
| `login_status(session)` | 查看某会话登录状态 |
| `health()` | 服务状态（不含凭据） |

## 为什么需要浏览器

- 读秀检索是 JS 渲染，纯 HTTP 取不到结果
- 万方用 gRPC-Web + protobuf，逆向不可维护
- 知网检索有滑块验证

因此运行时必须带真实 Firefox，这也决定了它**不能挂在 mcpo 里**（mcpo 镜像无浏览器依赖）。

## 多人隔离

- 一个 Firefox 进程 + 每个 `session` 一个独立 BrowserContext
- 不同用户的 cookie 物理隔离
- 未配置 `SZLIB_USERNAME/PASSWORD` 时，登录凭据通过一次性网页收集，只存在于该 Context 内存中

## 部署

镜像由 GitHub Actions 构建并推到 GHCR，**不需要本地 build**：

```
ghcr.io/<你的repo>:latest
```

见 `.github/workflows/build-docker.yml`。

```bash
cp .env.example .env
# 填 SZLIB_USERNAME / SZLIB_PASSWORD（可选）与 SZLIB_PUBLIC_BASE_URL
docker compose up -d
```

### compose 片段（加进现有 `mcp` 项目，挂在 `/szlib`）

```yaml
  szlib-mcp:
    image: ghcr.io/<你的repo>:latest
    container_name: szlib-mcp
    restart: unless-stopped
    env_file:
      - ./szlib-mcp.env
    environment:
      # 带路径前缀，验证链接才会生成 /szlib/verify/...
      - SZLIB_PUBLIC_BASE_URL=https://mcp.example.com/szlib
      - SZLIB_VERIFY_PORT=8765
    volumes:
      - szlib-data:/data
    networks:
      - reverse-proxy          # mcpo 也在这个网络上，paper-search 可直接访问
    labels:
      - "traefik.docker.network=reverse-proxy"
      - "traefik.enable=true"
      - "traefik.http.routers.mcp-szlib.entrypoints=websecure"
      - "traefik.http.routers.mcp-szlib.rule=Host(`mcp.example.com`) && PathPrefix(`/szlib`)"
      - "traefik.http.routers.mcp-szlib.middlewares=szlib-strip"
      - "traefik.http.routers.mcp-szlib.service=szlib-svc"
      - "traefik.http.routers.mcp-szlib.tls.certresolver=myresolver"
      - "traefik.http.routers.mcp-szlib.tls.domains[0].main=example.com"
      - "traefik.http.middlewares.szlib-strip.stripprefix.prefixes=/szlib"
      - "traefik.http.services.szlib-svc.loadbalancer.server.port=8765"

volumes:
  szlib-data:
```

命名与同项目里的 `mcp-context7` / `context7-svc` / `context7-strip` 保持一致。

> **为什么不放进 `mcp-servers` 那个 pm2 + mcp-proxy 里**：那个容器是 `node:22-slim`，
> 没有 Firefox 需要的系统库（`libnss3`/`libatk` 等）。szlib 必须自带浏览器运行时。

## 端点

| 路径 | 用途 |
|---|---|
| `/mcp` | MCP streamable-http（OWUI 直连） |
| `/verify/<token>` | 用户浏览器完成登录/验证码 |
| `/api/search` | 给 paper-search 等上层工具用的 REST 接口 |
| `/api/status` | 会话数与环境状态 |
| `/healthz` | 健康检查 |

> MCP 端点会校验 `Host` 头（防 DNS 重绑定）。默认放行 `public_base_url` 的主机、
> `szlib-mcp`、`localhost`；换域名或加反代层时用 `SZLIB_ALLOWED_HOSTS` 追加。

`/api/search` 返回 `202 {"status":"AWAITING_LOGIN","url":...}` 时，把 URL 给用户登录后，
带 `login_token` 再调一次即可。详见 [`docs/paper-search-integration.md`](docs/paper-search-integration.md)。

## 接入 Open WebUI

OWUI 支持直接连接 streamable-http MCP：

```
URL: http://szlib-mcp:8765/mcp
或  https://mcp.example.com/szlib/mcp
```

如果不单独给 szlib 建工具，也可以**只把它当作 paper-search 的一个源**——
那样用户只看到一个检索工具，中文文献自动并进去。

## 登录流程

```
1. agent 调 search_literature(...)
2. 未登录 → 返回
     {status:"AWAITING_LOGIN", url:"https://mcp.example.com/verify/xxx"}
3. 用户在浏览器打开该链接，填读者证号/密码/验证码
4. agent 带 login_token 调 login(...)
5. 之后 search_literature 正常返回题录
```

验证链接一次有效，默认 5 分钟过期。

## 已知边界

- **只做发现层**：读秀题录足够写综述，但不给全文
- **全文仍需人**：读秀文献传递要字符验证码，知网检索要滑块，都不做自动绕过
- **知网/万方未接入**：知网滑块需人工；万方列表渲染不稳定

## 开发

```bash
export PYTHONPATH=src
python -m szlib_mcp.cli check          # 会话状态
python -m szlib_mcp.cli login          # 用 .env 凭据登录
python -m szlib_mcp.server             # 启动 MCP + 验证页
```
