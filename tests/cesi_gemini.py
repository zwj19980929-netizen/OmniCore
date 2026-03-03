import requests
import os
import json


def chat_with_gemini(prompt: str) -> dict:
    # 强烈建议通过环境变量读取 API Key，避免硬编码在代码中泄露
    api_key = "sk-o1em6l8gh71FBjfX2K6cqXZZMlz4NlGElxDFYkISP3D0IE7S"
    if not api_key:
        raise ValueError("未找到 API Key，请先设置环境变量 LLMXAPI_API_KEY")

    # 注意：这里使用的是你文档中提供的第三方中转地址
    url = f"https://llmxapi.com/v1beta/models/[稳定1]gemini-3-pro-preview:generateContent?key={api_key}"

    headers = {
        "Content-Type": "application/json"
    }

    # 按照文档构建 payload
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}]
        }]
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()  # 检查 HTTP 错误

        # 返回解析后的 JSON 响应
        return response.json()

    except requests.exceptions.RequestException as e:
        print(f"请求发生错误: {e}")
        # 尝试打印详细的错误信息（如果服务器有返回的话）
        if hasattr(e, 'response') and e.response is not None:
            print(f"服务器响应: {e.response.text}")
        return None


# --- 测试运行 ---
if __name__ == "__main__":
    # 为了测试，请确保你在终端执行了： export LLMXAPI_API_KEY="你的真实密钥"
    user_input = "Write a story about a magic backpack."
    print("正在发送请求...")

    result = chat_with_gemini(user_input)

    if result:
        print("\n--- 完整 JSON 响应 ---")
        print(json.dumps(result, indent=2, ensure_ascii=False))

        # 尝试提取具体的文本回复内容 (如果响应结构符合常规设定)
        try:
            reply_text = result["candidates"][0]["content"]["parts"][0]["text"]
            print("\n--- 提取的回复文本 ---")
            print(reply_text)
        except (KeyError, IndexError):
            print("\n无法从响应中提取文本，可能是响应格式与预期不符。")