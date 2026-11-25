# ChatGPT GIF Bridge

本地原生 GIF API。使用自己的 ChatGPT Plus 登录，登录、验证和生成均通过同一个普通可见 Chrome 会话完成，不需要浏览器扩展。网页是否开放 GIF 取决于账号权限。

## 安装与登录

需要 Python 3.10+、Google Chrome 和桌面环境（用于首次登录及真人验证）。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bridge.py
```

打开 [管理页](http://127.0.0.1:18763)，用 `.state/api-token` 中的密钥连接。点击“打开登录 / 验证窗口”，登录 ChatGPT 或完成真人验证，然后点击“已完成登录 / 验证”。服务保存专用浏览器配置，保留同一窗口和会话继续生成，不关闭重开或切换无头。窗口可以最小化，使用期间请保持浏览器运行。日常 Chrome 的登录不会自动共享。

不需要安装扩展。浏览器配置、任务、结果和密钥均保存在本机 `.state/` 中，请勿分享该目录。退出并重启服务会保留登录状态及任务。每个人各自部署、登录自己的账号，不需要公网服务。

## API 配置

- Base URL：`http://127.0.0.1:18763/v1`
- API key：管理页复制的访问密钥
- 模型：`chatgpt-native-gif`
- 接口：`POST /v1/images/generations`

标准 OpenAI Python SDK 可直接调用：

```python
import base64
from pathlib import Path
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:18763/v1",
    api_key="<bridge API key>",
    timeout=400,
    max_retries=0,
)
result = client.images.generate(
    model="chatgpt-native-gif",
    prompt="小纸船在水面轻轻摇晃，原生循环 GIF",
    n=1,
    response_format="b64_json",
)
Path("animation.gif").write_bytes(base64.b64decode(result.data[0].b64_json))
```

将 `response_format` 改为 `url`，则从 `result.data[0].url` 取得有效期一小时的本机下载链接。链接可直接下载，不需要再传 API key；该链接只授权访问一个结果。过期文件自动清理，链接在服务重启后仍可用，前提是 key、状态目录和端口没有改变。服务仅监听本机，链接不适用于其他电脑。

这是 OpenAI Images 生成接口的有限兼容实现，不是完整 CPA。支持 `prompt`、`model`、`n=1`、`response_format=b64_json|url`，默认 Base64。`size=auto|WIDTHxHEIGHT` 和 `quality=auto|low|medium|high|standard|hd` 作为要求传入网页，实际尺寸和质量由网页决定。只支持原生 GIF，`output_format` 只能省略或为 `gif`。不支持的模型或参数会明确报错，不提供静态生图、图片编辑或对话接口。

返回下载文件的原始字节，不重新编码、缩放或拼帧。服务会校验文件是可解码的多帧 GIF。

## 验证与任务恢复

CF 验证可能在相同配置及稳定网络下复用一段时间，但无法保证有效时长；更换网络或站点重新评估会话后也可能重新要求验证。遇到验证会暂停任务与接单，管理页显示“需要真人验证”。服务不会自动解验证码，不使用无头模式。

已经发送的任务记录原聊天地址，恢复时检查原聊天，不重复发送。若发送是否成功无法确认，任务报错并要求检查原聊天。退出时未完成的任务会在重启后恢复；结果不明时不会自动重复提交。

生成接口最多等待 360 秒，建议客户端关闭自动重试，避免重复制图。503 表示浏览器未就绪、需要登录或验证；429 表示队列满；502 表示生成失败；504 表示等待超时。生成失败、等待超时或任务暂停后的响应包含 `error.job_id`，可通过原任务接口检查。

| 接口 | 用途 |
| --- | --- |
| `GET /v1/models` | 模型列表 |
| `GET /session` | 登录、验证和浏览器状态 |
| `POST /session/login` | 打开登录 / 验证窗口 |
| `POST /session/finish` | 检查当前窗口的登录状态，保留同一会话 |
| `POST /v1/gif/jobs` | 异步提交 `{"prompt":"..."}` |
| `GET /v1/gif/jobs` | 最近任务 |
| `GET /v1/gif/jobs/<id>` | 原任务状态 |
| `GET /v1/gif/jobs/<id>/result` | 下载结果 |
| `DELETE /v1/gif/jobs/<id>` | 删除已结束任务 |

除管理页静态资源和签名下载链接外，接口均需 `Authorization: Bearer <API key>`。最多接受四个未完成任务，依次执行，保留最近八条任务。

## 检查

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest -v
```

自动测试使用模拟网页和标准 OpenAI SDK。真实网页验收要求：连续生成两次并下载成功，重启后再生成一次；模拟测试通过不能替代该验收。当前自动生成流程的实际可用性仍需在目标电脑完成登录 / 验证后确认。
