# ChatGPT GIF Bridge

通过 ChatGPT Plus 网页生成 GIF 的本地服务。首次登录打开浏览器窗口，保存登录状态后，任务通过无头 Chrome 自动提交、等待生成并下载文件。

只提供这一种方式，不使用 Platform API key，也不提供手动上传模式。Plus 订阅并不保证账号已开放 GIF；无头浏览器不会解锁账号权限。

## 启动

需要 Python 3.10+ 和 Google Chrome。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bridge.py
```

1. 打开 [本地管理页](http://127.0.0.1:18763)，输入 `.state/api-token` 中的访问密钥。
2. 点击“登录 ChatGPT”，在打开的窗口中登录支持 GIF 的 Plus 账号。
3. 回到管理页点击“已完成登录”。服务关闭登录窗口，使用相同登录状态启动无头浏览器。
4. 输入提示词，点击“生成”。完成后可预览和下载 GIF。

浏览器配置和登录凭据保存在 `.state/browser`，请勿分享或提交该目录。登录窗口显示在运行服务的电脑上；首次登录需要桌面环境。登录过期后需重新登录。

## 接口

除静态管理页外，接口均需要 `Authorization: Bearer <本地访问密钥>`。

| 请求 | 用途 |
| --- | --- |
| `GET /health` | 服务状态 |
| `GET /session` | 浏览器登录状态 |
| `POST /session/login` | 打开登录窗口 |
| `POST /session/finish` | 保存登录并切回无头浏览器 |
| `POST /v1/gif/jobs` | 提交任务，JSON：`{"prompt":"纸船在水面轻轻摇晃，循环 GIF"}` |
| `GET /v1/gif/jobs` | 任务列表 |
| `GET /v1/gif/jobs/<id>` | 单个任务状态 |
| `GET /v1/gif/jobs/<id>/result` | 下载 GIF |
| `DELETE /v1/gif/jobs/<id>` | 删除已完成或失败的记录 |

任务依次执行，最多接受 4 个待完成任务，保留 8 条记录。任务和结果保存在内存中，重启后清空。服务仅监听本机。

## 验证范围

服务要求网页生成原生动画，不合成静态帧。下载结果必须是可解码的多帧 GIF，原始文件不会重新编码。但仅凭文件格式无法证明上游使用了哪种生成方式。

自动化测试使用模拟 ChatGPT 页面，覆盖登录状态切换、无头提交、下载、原始字节保留、静态图片拒绝和超时失败。**模拟测试通过不代表真实 Plus 账号已经成功生成 GIF。** 实际可用性取决于账号权限、网络和 ChatGPT 网页结构，需要登录后实测。

```sh
.venv/bin/python -m unittest -v
```
