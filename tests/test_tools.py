import os
import json
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()


client = OpenAI(
    api_key=os.environ["BERGET_API_KEY"],
    base_url="https://api.berget.ai/v1",   # confirm this in Berget's docs
)

tools = [{
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "Returns the current time in ISO 8601 format.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}]

response = client.chat.completions.create(
    model="meta-llama/Llama-3.3-70B-Instruct",   # confirm exact name in Berget's docs
    messages=[{"role": "user", "content": "What time is it right now?"}],
    tools=tools,
)

print(json.dumps(response.model_dump(), indent=2, default=str))