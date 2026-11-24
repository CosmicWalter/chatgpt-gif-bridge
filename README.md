# ChatGPT GIF Bridge

A loopback HTTP service for animated GIF jobs. Use a ChatGPT web handoff with a manual download, or generate automatically with an OpenAI Platform API key. Both paths return the original GIF after verifying that it contains multiple decodable frames.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bridge.py
```

Open [the local dashboard](http://127.0.0.1:18763) and paste the access token from `.state/api-token`. The dashboard provides a Chinese-language interface to create jobs, copy prompts, open ChatGPT, upload a downloaded animation, and preview or download completed GIFs. The official API option becomes available when a Platform key is configured on the server. The local token is held in the browser tab's session storage and cleared on disconnect.

The ChatGPT web workflow requires manual submission and download in your browser. Browser automation can use a visible browser or a headless browser, but neither grants additional model permissions. This project currently provides manual web handoff and the official API backend.

Set `OPENAI_API_KEY` in the service environment to enable the `api` backend. The service uses the official Images API with GPT Image 2.6 Flare. API usage is billed to the key's API project.

```sh
bridge_token="$(cat .state/api-token)"
base=http://127.0.0.1:18763
curl -fsS "$base/v1/gif/jobs" \
  -H "Authorization: Bearer $bridge_token" \
  -H 'Content-Type: application/json' \
  -d '{"backend":"web","prompt":"Make a downloadable looping GIF of a paper boat bobbing."}'
```

Copy the response's `id`. A web job exposes its prompt and a ChatGPT URL at the status endpoint. Open that URL, submit the prompt, save the GIF in the browser, and upload the downloaded file to complete the job. Web GIF availability depends on the account.

```sh
job_id='<job id>'
curl -fsS "$base/v1/gif/jobs/$job_id" \
  -H "Authorization: Bearer $bridge_token"
curl -fsS -X PUT "$base/v1/gif/jobs/$job_id/result" \
  -H "Authorization: Bearer $bridge_token" \
  -H 'Content-Type: image/gif' --data-binary @downloaded.gif
```

For automatic generation, submit with `"backend":"api"` and poll the status endpoint until the job reaches `completed` or `failed`. Both modes expose the result at the same URL.

```sh
curl -fsS "$base/v1/gif/jobs/$job_id/result" \
  -H "Authorization: Bearer $bridge_token" -o animation.gif
```

Use `GET /v1/gif/jobs` to list jobs and `DELETE /v1/gif/jobs/<id>` to remove an awaiting, completed, or failed job. The service requires the local token, accepts up to four outstanding jobs, and retains up to eight jobs in memory, evicting the oldest completed or failed job first. Restarting clears jobs and results. The local bearer token is stored in the ignored `.state/` directory.

Run `.venv/bin/python -m unittest -v` to exercise HTTP upload and download and mock the upstream Images API.

[ChatGPT web GIF help](https://help.openai.com/en/articles/11084440-images-in-chatgpt#create-and-edit-a-gif) · [Images API GIF guide](https://developers.openai.com/api/docs/guides/image-generation#generate-animated-gif-output)
