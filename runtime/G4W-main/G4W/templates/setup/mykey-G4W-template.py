# G4W for GA: DeepSeek Pro + Flash template.
# The initializer replaces both empty apikey values safely.

native_oai_config_main = {
    'name': 'deepseek-v4-pro',
    'apikey': '',
    'apibase': 'https://api.deepseek.com',
    'model': 'deepseek-v4-pro',
    'api_mode': 'chat_completions',
    'reasoning_effort': 'xhigh',
    'stream': True,
}

native_oai_config_lite = {
    'name': 'deepseek-v4-flash',
    'apikey': '',
    'apibase': 'https://api.deepseek.com',
    'model': 'deepseek-v4-flash',
    'api_mode': 'chat_completions',
    'reasoning_effort': 'xhigh',
    'stream': True,
}
