import json
import anthropic
import os
from dotenv import load_dotenv

load_dotenv(os.path.expanduser('~/.anthropic_env'))
client = anthropic.Anthropic()
messages = [{'role': 'user', 'content': 'Please run the compile_docx tool immediately.'}]
tools = [{
    'name': 'compile_docx',
    'description': 'Wraps lib/build.js...',
    'input_schema': {
        'type': 'object',
        'properties': {
            'content_json_path': {'type': 'string'},
            'figures_json_path': {'type': 'string'}
        },
        'required': ['content_json_path', 'figures_json_path']
    }
}]
print('Turn 1...')
res1 = client.messages.create(
    model='claude-haiku-4-5',
    max_tokens=200,
    messages=messages,
    tools=tools,
    tool_choice={'type': 'tool', 'name': 'compile_docx'}
)
print('res1:', res1.stop_reason)
messages.append(res1.model_dump(include={'role': True, 'content': True}))
if res1.stop_reason == 'tool_use':
    tool_results = []
    for block in res1.content:
        if block.type == 'tool_use':
            tool_results.append({
                'type': 'tool_result',
                'tool_use_id': block.id,
                'content': 'Error: Missing argument',
                'is_error': True
            })
    messages.append({'role': 'user', 'content': tool_results})
    print('Turn 2...')
    try:
        res2 = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=200,
            messages=messages,
            tools=tools
        )
        print('SUCCESS')
    except Exception as e:
        print('ERROR:', str(e))
