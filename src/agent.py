import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).parent))
import dispatcher

load_dotenv()

client = OpenAI(
    api_key=os.environ["BERGET_API_KEY"],
    base_url="https://api.berget.ai/v1",
)
MODEL = "meta-llama/Llama-3.3-70B-Instruct"
MAX_ITERATIONS = 15

log = logging.getLogger(__name__)


def _load_system_prompt() -> str:
    prompt_path = Path(__file__).parent / "prompts" / "system.txt"
    return prompt_path.read_text(encoding="utf-8")


def _run_turn(messages: list[dict], tools: list[dict]) -> object:
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
        temperature=0.3,
    )
    return response.choices[0].message


def _assistant_message(msg) -> dict:
    d: dict = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return d


def run_conversation() -> None:
    system_prompt = _load_system_prompt()
    tools = dispatcher.all_tool_specs()

    print("Choral Piece Finder  |  type 'quit' to exit\n")

    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit", "q"}:
            break

        messages.append({"role": "user", "content": user_input})

        for iteration in range(MAX_ITERATIONS):
            log.info("[iter %d] calling model", iteration)
            assistant_msg = _run_turn(messages, tools)
            messages.append(_assistant_message(assistant_msg))

            if not assistant_msg.tool_calls:
                print(assistant_msg.content)
                print()
                break

            for tc in assistant_msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError as exc:
                    args = {}
                    log.warning("bad JSON in tool args for %s: %s", tc.function.name, exc)

                log.info("→ %s(%s)", tc.function.name, str(args)[:120])
                result = dispatcher.dispatch(tc.function.name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )
        else:
            print("[Agent reached the iteration limit without a final answer.]")
            print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_conversation()
