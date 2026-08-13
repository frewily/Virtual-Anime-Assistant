# 云端 Python 包镜像设计

## 背景

Alibaba Cloud Linux 服务器在构建 VAA 后端镜像时，直连官方 PyPI 下载依赖极慢，导致首次部署长时间无法完成。Docker 基础镜像已通过备用镜像源解决，剩余瓶颈位于 Dockerfile 的 `pip install` 步骤。

## 方案

Dockerfile 增加可选构建参数 `PIP_INDEX_URL`。参数为空时不改变 `pip` 默认行为；云端 Compose 从 `.env` 读取该参数，并在生产模板中使用阿里云 PyPI 镜像。

该参数只参与镜像构建，不进入 VAA 容器运行环境，也不属于秘密。现有本地构建和 CI 未设置参数时继续使用官方 PyPI。

## 验证

- 契约测试确认 Dockerfile 支持可选 `PIP_INDEX_URL`，Compose 正确传递参数，生产模板给出阿里云镜像地址。
- 云端契约测试与 Compose 配置检查通过。
- 服务器重新构建后，`live` 与 `ready` 健康检查通过。
